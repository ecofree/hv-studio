"""
Ken Burns 运镜服务（M1 实装：PIL 逐帧生成 + rawvideo 管道）。

═══════════════════════════════════════════════════════════════
为什么是 PIL 逐帧 + 管道，而不是 ffmpeg 滤镜链（重要，别改回去）
───────────────────────────────────────────────────────────────
踩过的两个坑，都实测过：

1) zoompan 会先把输入缩放到输出尺寸 s 再裁剪，等于一次有损下采样。
   实测（1882×3344 的 1px 棋盘格 → 1080×1920）：
       zoompan=z=1.0（原生输入）          高频能量 0.4   ← 细节全糊
       zoompan=z=1.0（先 scale=4320:-1）  高频能量 0.3   ← 预放大毫无意义
       crop=1080:1920                     高频能量 255.0 ← 1:1 完美
   结论：预放大到 8000px 那套"经典配方"在 zoompan 下是无效的。

2) `-loop 1 -i 大图.png` + scale(eval=frame) 在 2160×3840 输入下
   频繁报 `get_buffer() failed / Cannot allocate memory`，
   且同一条命令时成时败（内存临界）。滤镜图越大越不稳。

最终方案：Python 里算好每帧的裁切窗，PIL crop+resize，
通过 rawvideo 管道喂给 ffmpeg 只做编码。
   - 内存恒定（一次只有一帧在内存）
   - z=1 时裁切窗恰好 1080×1920，跳过 resize，真 1:1 像素映射
   - 运镜曲线是纯函数，可单测，不依赖 ffmpeg 表达式求值时机
═══════════════════════════════════════════════════════════════

坐标约定：
  z    相对缩放；z=1 时裁切窗 = 1080×1920 原生像素（最锐，无重采样）
       z>1 表示推近（裁切窗变小，再放大到 1080×1920）
  px,py 裁切窗左上角在「可移动范围」内的归一化位置 [0,1]；0.5 = 居中

运动类型（motion.type）：
  push   z: 1.0 → 1+intensity          推近
  pull   z: 1+intensity → 1.0          拉远
  pan    z 固定 1+intensity*0.5，px/py 从 start_xy 移到 end_xy
  drift  极轻微推近 + 轻微位移（默认）
  static z=1 完全静止

为避免连续两镜头同向运动，调用方应交替 motion.type。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image

from app.models import Shot

# 输出规格
FPS = 25
W = 1080
H = 1920
CRF = 18
PRESET = "medium"


def _compose_vcodec_args() -> list[str]:
    """编码单一真源（P1 修复）：motion.render() 预览编码与 compose 主合成走同一
    套 config.compose 参数（NVENC High/4.1 优先，libx264 High/4.1 兜底），
    避免此前硬编码 `libx264 -crf 18` 产出 Main/非 4.1 预览、与成片码流不一致。"""
    try:
        from app.config.config import get_settings as _gs
        c = _gs().compose
    except Exception:
        # config 不可用时回退 libx264 High/4.1（仍是可兼容码流，不静默错参数）
        return ["-c:v", "libx264", "-profile:v", "high", "-level", "4.1",
                "-preset", PRESET, "-crf", str(CRF)]
    # 探测 NVENC（与 compose._probe_nvenc 同逻辑，本地做一次轻量探测）
    enc = getattr(c, "video_encoder", "h264_nvenc")
    try:
        from app.services.compose import _probe_nvenc
        nv_ok = _probe_nvenc(enc)
    except Exception:
        nv_ok = False
    if enc == "h264_nvenc" and nv_ok:
        return ["-c:v", "h264_nvenc", "-profile:v", "high", "-level", "4.1",
                "-preset", getattr(c, "preset", "p4"), "-cq", str(getattr(c, "cq", 20))]
    _crf = 20
    if getattr(c, "fallbacks", None):
        _crf = c.fallbacks[0].get("crf", 20)
    return ["-c:v", "libx264", "-profile:v", "high", "-level", "4.1",
            "-preset", getattr(c, "preset", "medium"), "-crf", str(_crf)]


# ---------------------------------------------------------------------------
# 缓动 & 运动解析（纯函数）
# ---------------------------------------------------------------------------
def ease(easing: str, u: float) -> float:
    """u ∈ [0,1] → 缓动后的进度 p ∈ [0,1]。"""
    u = min(1.0, max(0.0, u))
    if easing == "linear":
        return u
    if easing == "ease_in":
        return u * u
    if easing == "ease_out":
        return 1 - (1 - u) * (1 - u)
    return u * u * (3 - 2 * u)  # ease_in_out: smoothstep


def resolve_motion(spec) -> tuple[float, float, tuple[float, float], tuple[float, float]]:
    """
    MotionSpec → (z0, z1, (px0,py0), (px1,py1))。

    start_zoom 作为「机位基准」乘进各运动曲线：
      start_zoom=1.0（默认）时与历史行为逐位一致，hw-001 基线不受影响。
    """
    t = (spec.type if spec else "drift") or "drift"
    k = spec.intensity if spec else 0.08

    z_base = (spec.start_zoom if (spec and spec.start_zoom) else 1.0) or 1.0

    if t == "push":
        return z_base, z_base * (1.0 + k), (0.5, 0.5), (0.5, 0.5)
    if t == "pull":
        return z_base * (1.0 + k), z_base, (0.5, 0.5), (0.5, 0.5)
    if t == "pan":
        z = z_base * (1.0 + k * 0.5)
        s_xy = spec.start_xy if spec and spec.start_xy else (0.0, 0.5)
        e_xy = spec.end_xy if spec and spec.end_xy else (1.0, 0.5)
        return z, z, tuple(s_xy), tuple(e_xy)
    if t == "static":
        return z_base, z_base, (0.5, 0.5), (0.5, 0.5)
    # drift
    s_xy = spec.start_xy if spec and spec.start_xy else (0.5, 0.46)
    e_xy = spec.end_xy if spec and spec.end_xy else (0.5, 0.54)
    return z_base, z_base * (1.0 + k * 0.6), tuple(s_xy), tuple(e_xy)


def frame_box(iw: int, ih: int, z: float, px: float, py: float) -> tuple[int, int, int, int]:
    """
    单帧裁切窗 (x0, y0, x1, y1)。z 为相对缩放，px/py 归一化位置。
    结果会被夹在图像范围内。
    """
    cw = W / max(0.01, z)
    ch = H / max(0.01, z)
    x = (iw - cw) * min(1.0, max(0.0, px))
    y = (ih - ch) * min(1.0, max(0.0, py))
    x0 = int(round(min(max(0.0, x), max(0.0, iw - cw))))
    y0 = int(round(min(max(0.0, y), max(0.0, ih - ch))))
    x1 = int(round(min(iw, x0 + cw)))
    y1 = int(round(min(ih, y0 + ch)))
    return x0, y0, x1, y1


def frames_for_duration_ms(duration_ms: float, fps: int = FPS) -> int:
    """单镜帧数——全项目唯一真值口径，勿在别处另写一份。

    ``plan_frames`` / ``compose.expected_frames`` / ``timing_assert`` 三处共用此式，
    确保「渲染写出帧数」「段完成判据」「时序断言 #5」永不口径分裂。

    历史坑：曾出现 ``round(Σ时长/1000*fps)``（一次舍入）与
    ``Σ round(单镜/1000*fps)``（逐镜舍入）两种算法并存的状况，逐镜舍入会
    累积差额——实测 laogongtuan 差 26 帧（31461 vs 31487），按 90s 切 15 段
    时 11 段不等，导致分段渲染的「已完成段跳过」永远判不中。
    """
    return max(1, int(round((duration_ms or 0) / 1000.0 * fps)))


def plan_frames(iw: int, ih: int, dur_s: float, spec, fps: int = FPS):
    """生成每一帧的裁切窗列表。纯函数，便于单测与预览。"""
    n_frames = frames_for_duration_ms(dur_s * 1000.0, fps)
    z0, z1, (px0, py0), (px1, py1) = resolve_motion(spec)
    easing = spec.easing if spec else "ease_in_out"
    boxes = []
    for n in range(n_frames):
        p = ease(easing, n / max(1, n_frames - 1))
        boxes.append(frame_box(iw, ih, z0 + (z1 - z0) * p,
                               px0 + (px1 - px0) * p, py0 + (py1 - py0) * p))
    return boxes


# ---------------------------------------------------------------------------
# 光线调色（场景复用第二轴：同一张底图卖出晨 / 午 / 昏 / 夜四种光）
#
# 为什么是 PIL point() 而不是 ffmpeg 滤镜：
#   compose 走的是 rawvideo 管道，逐帧已经在 Python 里；在 Python 侧调色
#   不额外增加编解码，且每个镜头可以独立取色，不依赖 ffmpeg 表达式。
#
# 每个 grade = (r_gain, g_gain, b_gain, r_bias, g_bias, b_bias,
#               brightness, contrast, saturation, vignette)
# gain/bias 走 256 项 LUT，一次 point() 完成，比逐像素 Python 循环快两个数量级。
# ---------------------------------------------------------------------------
GRADE_MAP: dict[str, tuple] = {
    # 晨光：偏冷蓝的阴影 + 暖调高光，低反差，空气感
    "dawn":  (1.05, 1.01, 0.95,   4,  2, -2,   1.03, 0.95, 1.05, 0.10),
    # 正午：中性、高反差、提亮，最"纪实"
    "noon":  (1.00, 1.00, 1.00,   0,  0,  0,   1.06, 1.10, 0.98, 0.00),
    # 黄昏：琥珀金，暖高光压蓝，剪影感
    "dusk":  (1.14, 0.99, 0.80,   6,  0, -8,   0.98, 1.06, 1.15, 0.22),
    # 夜色：压暗 + 冷蓝，暗角重，配火把/星月
    "night": (0.86, 0.90, 1.08,  -6, -4, 10,   0.74, 1.04, 0.92, 0.40),
    # 火光：暗底 + 橙红跳光，帐篷/烛火内景
    "fire":  (1.16, 0.94, 0.76,  10, -2, -12,  0.86, 1.08, 1.20, 0.34),
    # 霜雪：去饱和的青灰，葱岭/雪山
    "frost": (0.96, 0.99, 1.06,  -2,  1,  8,   1.08, 0.92, 0.80, 0.12),
}


def _channel_lut(gain: float, bias: float) -> list[int]:
    return [max(0, min(255, int(round(i * gain + bias)))) for i in range(256)]


def apply_grade(img, grade: str | None):
    """对 RGB PIL 图像施加光线调色。grade 为 None/未知时原样返回。"""
    if not grade:
        return img
    g = GRADE_MAP.get(grade)
    if not g:
        return img

    r_gain, g_gain, b_gain, r_bias, g_bias, b_bias, bright, contrast, sat, vig = g

    from PIL import ImageEnhance

    img = img.point(_channel_lut(r_gain, r_bias)
                    + _channel_lut(g_gain, g_bias)
                    + _channel_lut(b_gain, b_bias))
    img = ImageEnhance.Brightness(img).enhance(bright)
    img = ImageEnhance.Contrast(img).enhance(contrast)
    img = ImageEnhance.Color(img).enhance(sat)

    if vig > 0:
        img = _vignette(img, vig)
    return img


# 暗角遮罩按 (w,h,strength) 缓存，同尺寸多镜只算一次，并规避 int64 mgrid 的大块瞬时分配
# （3344×1882 超分图上 int64 mgrid 需 ~100MB 瞬时峰值，在低 pagefile 机器上会 OOM）
_VIGNETTE_CACHE: dict[tuple[int, int, float], "np.ndarray"] = {}


# 暗角径向梯度（L 蒙版）——在低分辨率下用 numpy 算一次正确几何后缓存，
# 应用时由 PIL 缩放到帧尺寸并 composite。全程无「全帧尺寸」numpy 数组，
# 规避大图在低内存机器上的 _ArrayMemoryError（此前 int64/float32 全帧分配实测触发）。
_VIGNETTE_MASK_CACHE: dict[tuple[int, int, float], "PIL.Image.Image"] = {}


def _vignette_mask(w: int, h: int, strength: float):
    """返回 0(中心)…strength*255(角落) 的径向 L 蒙版，按尺寸/强度缓存。"""
    import numpy as np
    from PIL import Image

    key = (w, h, strength)
    m = _VIGNETTE_MASK_CACHE.get(key)
    if m is not None:
        return m
    # 低分辨率算几何（1/6 边长），几百 KB 级 numpy，远低于全帧数组
    mw, mh = max(8, w // 6), max(8, h // 6)
    yy, xx = np.mgrid[0:mh, 0:mw].astype(np.float32)
    cx, cy = (mw - 1) / 2.0, (mh - 1) / 2.0
    # 到角落的距离（归一化：中心 0 → 角落 1）
    corner_d = float(np.hypot(mw / 2.0, mh / 2.0))
    d = np.hypot(xx - cx, yy - cy) / corner_d
    # 强度线性映射到蒙版亮度 0(中心) → strength*255(角落)
    lum = np.clip(strength * 255.0 * (d ** 1.6), 0, 255).astype(np.uint8)
    m = Image.fromarray(lum, "L")
    if len(_VIGNETTE_MASK_CACHE) > 16:
        _VIGNETTE_MASK_CACHE.clear()
    _VIGNETTE_MASK_CACHE[key] = m
    return m


def _vignette(img, strength: float):
    """径向暗角 —— 蒙版低分辨率算好缓存，PIL 缩放后 composite，无全帧 numpy 大分配。"""
    from PIL import Image

    w, h = img.size
    mask = _vignette_mask(w, h, strength).resize((w, h), Image.LANCZOS)
    black = Image.new("RGB", (w, h), (0, 0, 0))
    # composite: 蒙版亮(角落)取 black → 四角压暗；蒙版暗(中心)取原图 → 中心保持
    return Image.composite(black, img, mask)


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def render(shot: Shot, fps: int = FPS) -> str:
    """
    M1 主入口。返回生成的 mp4 路径。
    输入为 shot.image_path（未超分原图）；内部先 upscale 2x 取得运镜余量。
    """
    from app.services.upscale import upscale

    if not shot.image_path:
        raise ValueError(f"shot {shot.id} 没有 image_path")

    src_p = upscale(shot.image_path)
    shot.upscaled_path = str(src_p).replace("\\", "/")

    img = Image.open(src_p).convert("RGB")
    grade = getattr(shot.motion, "grade", None) if shot.motion else None
    if grade:
        img = apply_grade(img, grade)
    iw, ih = img.size
    dur_s = (shot.duration_ms or 3000) / 1000.0

    out = Path(src_p).parent / f"{shot.id}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    shot.motion_path = str(out).replace("\\", "/")

    boxes = plan_frames(iw, ih, dur_s, shot.motion, fps)

    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{W}x{H}", "-r", str(fps), "-i", "-",
        *_compose_vcodec_args(),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(out),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for (x0, y0, x1, y1) in boxes:
            cw, ch = x1 - x0, y1 - y0
            if cw == W and ch == H:
                frame = img.crop((x0, y0, x1, y1))          # 1:1，不重采样
            else:
                frame = img.crop((x0, y0, x1, y1)).resize((W, H), Image.LANCZOS)
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
    except BrokenPipeError:
        pass
    err = proc.stderr.read().decode("utf-8", "ignore") if proc.stderr else ""
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"ffmpeg 编码失败: {err[-1200:]}")

    if not out.exists() or out.stat().st_size < 1024:
        raise RuntimeError(f"输出异常: {out} ({out.stat().st_size if out.exists() else 0}B)\n{err[-600:]}")
    return str(out)
