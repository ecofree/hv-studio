"""
最终合成服务（M2 实装：单遍编码成片）。

把 ep.shots 按时间顺序出帧（运镜 + 烧字幕）+ 拼旁白 → 一个 mp4。
由 bin/m1_compose.py 演化而来，逻辑一致（proven），仅抽成可复用函数，
供 pipeline 编排器 / CLI 统一调用。

设计要点（继承自 M1，勿改回）：
  1) 呼吸间隙 250ms（100 head + 150 tail）只在「源句子」切换处插入。
  2) 不用 libass/subtitles 滤镜烧字幕（本机 commit 内存只剩 1~2GB，
     libass 渲染 1080×1920 稳定报 Cannot allocate memory）。
     改为 PIL 逐帧直接画字幕（stroke 描边 + 阴影），走 rawvideo 管道。
  3) 直接从超分 @2x 原图出帧，不经中间 mp4：少一次解码，画质最好。
     单镜预览 mp4（如要）由 motion.render() 生成，主合成路径不依赖它——
     编码参数两处已统一走 config.compose（见 motion._compose_vcodec_args）。

输出：episodes/{id}/out/{id}_m1.mp4
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from app.managers import save
from app.services.motion import FPS, H, W, apply_grade
from app.config.config import ROOT
from app.services.upscale import upscale

# BASE 单一真源：从 config.ROOT 推导（= 仓库根 / episodes），勿硬编码盘符
# （2026-09-04 收敛：换盘符/换机失效）。兼容外部传入 base_dir 场景。
BASE = ROOT / "episodes"
HEAD_MS = 100
TAIL_MS = 150

# 字幕排版（对齐 config.yaml subtitle 段，Task #28 修复）
#  - font: config 声明「思源宋体」→ 本机即 Noto Serif SC（同字形，Adobe/Google 联合设计的开源思源宋体，
#    NotoSerifSC-VF.ttf 即 Source Han Serif SC 的 Noto 名）。已确认存在，避免 ImageFont.truetype 抛异常。
FONT_PATH = "C:/Windows/Fonts/NotoSerifSC-VF.ttf"   # 思源宋体(Noto Serif SC)，替代原硬编码 msyhbd 黑体粗
FONT_SIZE = 72                                    # 对齐 config subtitle.font_size
TEXT_W = W - 150          # 字幕可用宽度（每行留左右各 ~75px）
# 字幕块底边距 = config subtitle.margin_bottom(320)，即字幕底部在 y=H-320 处，
# 由字幕块向上生长（bottom-anchor），永远不越过底栏安全区。
SUBTITLE_BOTTOM = H - 320
STROKE = 5
LINE_GAP = 16

# 中文避头尾 / 折行标点法则
#  禁行首：不可作「行首」的收尾/终句标点 —— 折行时并入上一行，杜绝孤标点（"…的人，"整行同句）。
_NO_LINE_START = set("，。、；：？！）」』》〉·…—")
#  禁行尾：不可作「行尾」的开括号/引号 —— 断点若落其后不合法。
_NO_LINE_END = set("（「『《〈【“‘")
#  标点分界：偏好在此类标点之后断行（让上一行以语义标点收尾，更符合阅读习惯）。
_BREAK_AFTER = set("，。、；：？！…—")
_SENT_END_PUNCT = set("。！？；：…—")   # 句末标点：大字渲染前 rstrip 掉（防孤行/视觉占位）


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """加载思源宋体并固定到 SemiBold(600) 字重。

    NotoSerifSC-VF.ttf 是可变字体，ImageFont.truetype 默认落在最细的
    ExtraLight(200) —— 竖屏大字字幕太细、识别度差。这里显式设到
    SemiBold：600 字重 + 现有 STROKE 描边，清晰又不发糊。
    """
    f = ImageFont.truetype(FONT_PATH, size)
    try:
        f.set_variation_by_name("SemiBold")
    except Exception:
        try:
            # 退路：按 Weight 轴坐标设置（SemiBold≈600）
            f.set_variation_by_axes([600])
        except Exception:
            pass  # 非可变字体 / 无该轴 → 用默认
    return f


_FONT_CACHE: dict[int, ImageFont.FreeTypeFont] = {}


def _get_font(size: int) -> ImageFont.FreeTypeFont:
    """模块级缓存字体，避免 7,475 帧每帧 ImageFont.truetype 重载（Task #28 P2）。"""
    f = _FONT_CACHE.get(size)
    if f is None:
        f = _load_font(size)
        if len(_FONT_CACHE) > 16:
            _FONT_CACHE.clear()
        _FONT_CACHE[size] = f
    return f


def _wrap(draw, text: str, font, max_w: int) -> list[str]:
    """中文字幕折行：避头尾 + 语义断点优先 + 最小两字单元。

    对剩余 seg 找最优断点 cut（断在 seg[:cut] / seg[cut:] 之间）：
      - 覆盖：seg[:cut] 宽度 <= max_w；
      - 合法：seg[cut] 不得是禁行首收尾标点；seg[cut-1] 不得是禁行尾开括号；
      - 优选：取「最晚合法语义标点（，。、；：？！…—）之后」断行，让上一行以
        标点收尾（避免生拆"招募"等词）；无标点则取最晚合法像素断点；
      - 最小单元：断后余段若不足 2 字且非全文结尾，回吐使不产生孤字开新行。
    返回折行后的行列表。
    """
    lines: list[str] = []
    seg = text
    while seg:
        if draw.textbbox((0, 0), seg, font=font)[2] <= max_w:
            lines.append(seg)
            break
        best_legal, best_sem = 0, -1
        w = 0
        for cut in range(len(seg)):
            w += draw.textbbox((0, 0), seg[cut], font=font)[2]
            if w > max_w:
                break
            # cut 是合法断点？（断在 seg[cut-1] | seg[cut] 之间，即 seg[:cut] / seg[cut:]）
            if seg[cut] in _NO_LINE_START:
                continue                    # 断后首字是收尾标点 → 不合法
            if seg[cut - 1] in _NO_LINE_END:
                continue                    # 断前末字是开括号 → 不合法
            best_legal = cut                # 越晚越好（保持最长行）
            if seg[cut - 1] in _BREAK_AFTER:
                best_sem = cut              # 上一个字符是语义标点 → 断点更优
        cut = best_sem if best_sem > 0 else best_legal
        # 最小单元：避免「孤字开新行」（断后余段仅 1 字且后面还有内容）
        while len(seg) - cut < 2 and cut < len(seg) - 1:
            cut += 1
        lines.append(seg[:cut])
        seg = seg[cut:]
    return lines


def _draw_subtitle(img: Image.Image, text: str) -> None:
    d = ImageDraw.Draw(img)
    font = _get_font(FONT_SIZE)
    # 句末标点 rstrip：大字解说不渲染句尾「。！？」（对抖音竖屏是行业惯例，避免孤标点/视觉占位）
    t = text.rstrip("".join(_SENT_END_PUNCT))
    if not t:
        t = text
    lines = _wrap(d, t, font, TEXT_W)
    heights = [d.textbbox((0, 0), ln, font=font)[3] for ln in lines]
    total_h = sum(heights) + LINE_GAP * (len(lines) - 1)
    # 字幕块底边对齐 config margin_bottom(320)，多行字幕从底边向上生长，
    # 永远不越过底栏 320px 安全区。
    y = SUBTITLE_BOTTOM - total_h
    for i, ln in enumerate(lines):
        w = d.textbbox((0, 0), ln, font=font)[2]
        x = (W - w) // 2
        d.text((x + 3, y + 4), ln, font=font, fill=(0, 0, 0),
               stroke_width=STROKE, stroke_fill=(0, 0, 0))
        d.text((x, y), ln, font=font, fill=(255, 255, 255),
               stroke_width=STROKE, stroke_fill=(16, 16, 16))
        y += heights[i] + LINE_GAP


def _group_of(frame_id: str) -> str:
    parts = frame_id.split("-")
    return parts[1] if len(parts) > 1 else frame_id


def _read_wav(p: Path):
    with wave.open(str(p), "rb") as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        return data, w.getframerate()


def _disk_wav_duration_ms(p: Path) -> int:
    """读磁盘 wav 的真实时长(ms)。用于校验 frame.audio_duration_ms 是否过时。"""
    with wave.open(str(p), "rb") as w:
        return int(w.getnframes() / float(w.getframerate() or 1) * 1000)


_WAV_TOLERANCE_MS = 120  # 磁盘 wav 时长 vs json 记录的可接受偏差（采样取整抖动）


def _verify_audio_vs_disk(shots, frames_by_id) -> None:
    """§6#5 立规矩：出片前校验「磁盘 wav 真实时长」与「json 记录的 audio_duration_ms」
    一致，杜绝 json 里时长过时/被篡改、门禁却放行的情况（审计 P0-3 的盲区）。
    任一偏差超允差 → 抛 RuntimeError 指明 frame 与 wav 路径，不静默出片。"""
    for sh in shots:
        f = frames_by_id.get(sh.frame_id)
        if not f or not f.audio_path:
            raise RuntimeError(
                f"shot {sh.id} 的 frame {sh.frame_id} 无 audio_path，无法校验音频")
        p = Path(f.audio_path)
        if not p.exists():
            raise RuntimeError(
                f"frame {f.id} 音频缺失: {f.audio_path}（需先跑 TTS）")
        disk = _disk_wav_duration_ms(p)
        rec = f.audio_duration_ms or 0
        if abs(disk - rec) > _WAV_TOLERANCE_MS:
            raise RuntimeError(
                f"frame {f.id} 音频时长不一致：磁盘 wav {disk}ms vs json 记录 "
                f"{rec}ms（偏差 {abs(disk-rec)}ms > {_WAV_TOLERANCE_MS}ms）。"
                f"wav 可能被覆盖/损坏或 json 过时。请重跑 TTS 或核对。\n  {p}")


def _kill_proc(proc) -> None:
    """尝试 kill 子进程，异常静默（清理用，不掩盖主错误）。"""
    try:
        proc.kill()
    except Exception:
        pass


def _read_stderr(proc) -> str:
    """读取并收起 ffmpeg stderr，避免管道满导致 ffmpeg 卡死。"""
    try:
        if proc.stderr:
            return proc.stderr.read().decode("utf-8", "ignore")
    except Exception:
        pass
    return ""


def _wait_proc(proc, timeout_sec: float, what: str):
    """带超时的 proc.wait：超时则 kill 并抛错，防 ffmpeg 卡死导致 compose 永久挂起
    （审计 P1 2.1 的 Popen 死锁模式）。timeout_sec<=0 视为不设超时（仅收口 wait）。"""
    if timeout_sec <= 0:
        return proc.wait()
    try:
        return proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        raise RuntimeError(
            f"编码 {what} 超时({timeout_sec:.0f}s 未收口)，已 kill ffmpeg。"
            f"请检查 GPU/编码器是否卡住，清理 build/ 后重试")


_NVENC_CACHE: dict[str, bool] = {}


def _probe_nvenc(encoder: str = "h264_nvenc") -> bool:
    """探测当前环境 ffmpeg 是否可用指定硬件编码器（缓存按 encoder 名）。

    探测用与真实合成一致的 profile/level/preset/cq，避免「探测能用、真跑
    却因 profile 不被 NVENC 支持而挂」的假阳性。失败/不可用返回 False。
    """
    if encoder in _NVENC_CACHE:
        return _NVENC_CACHE[encoder]
    if not encoder.startswith("h264_nvenc") and not encoder.startswith("hevc_nvenc"):
        _NVENC_CACHE[encoder] = False
        return False
    _codec = encoder
    _extra = ["-profile:v", "high", "-level", "4.1"] if encoder == "h264_nvenc" else []
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-f", "lavfi", "-i", "color=c=red:s=320x568:d=0.05",
             "-c:v", _codec, *_extra, "-preset", "p4", "-cq", "20",
             "-f", "null", "-"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
        _NVENC_CACHE[encoder] = (r.returncode == 0)
    except Exception:
        _NVENC_CACHE[encoder] = False
    return _NVENC_CACHE[encoder]


def compose_episode(ep_id: str, plan_frames=None, render=None,
                    run_timing_gate: bool = True) -> Path:
    """
    合成整集成片。返回最终 mp4 路径。
    plan_frames / render 允许注入（默认从 motion 导入），便于测试。

    run_timing_gate=True 时，合成前（时间轴重算之后、编码之前）跑 §5.4 时序断言
    （短镜 / 超长镜 / 音频对齐统一前置拦截），任一项失败即 raise TimingError，不出片。
    这是对『pipeline 路径』与『compose 直通入口』的统一门禁——
    修复 2026-09-03 审计 #A：此前直通 compose 从不调断言，短镜能悄悄出片。
    """
    from app.managers import load
    from app.services.timing_assert import assert_timing_or_raise
    from app.services.motion import plan_frames as _pf, render as _render
    plan_frames = plan_frames or _pf
    render = render or _render

    ep = load(ep_id)
    base = BASE / ep_id
    build = base / "build"
    out_dir = base / "out"
    build.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    shots = sorted(ep.shots.values(), key=lambda s: s.index)
    frames = {f.id: f for f in ep.frames}

    # ---------- 1) 呼吸间隙 + 时间轴 ----------
    prev_g, head_of = None, {}
    for sh in shots:
        g = _group_of(sh.frame_id)
        head_of[sh.id] = HEAD_MS if (prev_g is not None and g != prev_g) else 0
        prev_g = g
    tail_of = {}
    for i, sh in enumerate(shots):
        nxt = shots[i + 1] if i + 1 < len(shots) else None
        tail_of[sh.id] = TAIL_MS if (nxt is None or _group_of(nxt.frame_id) != _group_of(sh.frame_id)) else 0

    timeline, t = [], 0
    for sh in shots:
        f = frames[sh.frame_id]
        a_dur = f.audio_duration_ms or 0
        sh.audio_start_ms = head_of[sh.id]
        sh.duration_ms = sh.audio_start_ms + a_dur + tail_of[sh.id]
        sh.audio_end_ms = sh.audio_start_ms + a_dur
        s0 = t + sh.audio_start_ms
        timeline.append((sh, s0, s0 + a_dur))
        t += sh.duration_ms
    total_ms = t

    # ---------- 1.5) 合成前护栏（P0-3 修复：在【重算之后】跑门禁，而非用持久化值） ----------
    # 此前 gate 在重算前用 episode.json 里存的 shot 时长断言（自洽但可能过时），
    # 而合成用 frame.audio_duration_ms 重算 —— 若某 frame 漏合成(audio=0)，旧 gate 通过、
    # 重算却出 ~250ms 闪镜静默绕门。现改为：先校验磁盘 wav 真实时长与 json 一致，
    # 再对【重算后的内存 shot 值】跑 §5.4 门禁（此时 sh.* 已被本函数按真实音频回填，
    # gate 看到的就是合成将用的口径）。任一失败 raise，绝不静默出片。
    if ep.shots:
        _verify_audio_vs_disk(shots, frames)
        if run_timing_gate:
            assert_timing_or_raise(ep)   # 重算后：失败即抛 TimingError，不出半成品

    # ---------- 2) 拼旁白 ----------
    sr, chunks = 16000, []
    for sh, s0, s1 in timeline:
        f = frames[sh.frame_id]
        data, sr = _read_wav(Path(f.audio_path))
        chunks.append(np.zeros(int(sr * sh.audio_start_ms / 1000), dtype=np.int16))
        chunks.append(data)
        chunks.append(np.zeros(int(sr * tail_of[sh.id] / 1000), dtype=np.int16))
    nwav = build / "narration.wav"
    with wave.open(str(nwav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(np.concatenate(chunks).tobytes())

    # ---------- 2.5) 混入 BGM（Task #29；assets/bgm/{profile}.mp3 存在才生效） ----------
    # 约定路径，无文件则优雅跳过（保持纯旁白成片）。bgm.volume / fade_out_sec 来自 config。
    audio_input = nwav  # 默认走纯旁白
    try:
        from app.services.bgm import pick_bgm, mix_voice_with_bgm
        from app.config.config import get_settings as _gs
        _bgm_cfg = _gs().get_profile().bgm
        _bgm_path = pick_bgm(_gs().profile_name, base_dir=ROOT)
        if _bgm_path is not None:
            _mixed = mix_voice_with_bgm(
                voice_wav=nwav, bgm_path=_bgm_path, total_ms=total_ms,
                volume=_bgm_cfg.volume, fade_out_sec=_bgm_cfg.fade_out_sec,
                bgm_fade_in_sec=2,
                out_wav=build / "voice_bgm_mix.wav",
            )
            audio_input = _mixed
            print(f"[bgm] 接入：{_bgm_path.name}  vol={_bgm_cfg.volume}  "
                  f"fade_out={_bgm_cfg.fade_out_sec}s")
        else:
            print(f"[bgm] assets/bgm/{_gs().profile_name}.mp3 未找到，跳过 BGM")
    except Exception as e:
        # BGM 任何环节失败都回退纯旁白，不阻塞出片
        print(f"[bgm] 接入失败，回退纯旁白：{e}")

    # ---------- 3) 出帧 + 编码 ----------
    # 场景复用下多个 shot 指向同一张底图，超分只需做一次（否则 93 镜重复放大 93 次）
    _up_cache: dict[str, Path] = {}
    upscaled = {}
    for sh, _, _ in timeline:
        if sh.image_path not in _up_cache:
            _up_cache[sh.image_path] = upscale(sh.image_path)
        upscaled[sh.id] = _up_cache[sh.image_path]
    final = out_dir / f"{ep_id}_m1.mp4"
    # 收口原子化（P1 2.1 修复）：ffmpeg 先写 .tmp，全部成功后 os.replace 成真名。
    # 中途中断/失败只留 .tmp，绝不覆盖已存在的好片（半成品以真名留下的历史坑）。
    final_tmp = out_dir / f"{ep_id}_m1.mp4.tmp"
    # --- 编码参数（Task #31 配置对齐：从 config.compose 读取） ---
    # config 声明的首选编码器是 h264_nvenc（GPU，几乎不占系统 RAM），
    # 退路是 libx264。此前 libx264 软编在低 pagefile 机器上峰值 ~1.8GB，
    # 与 python 出帧争抢提交额导致间歇 OOM。--threads 去掉（nvenc 走 GPU）。
    from app.config.config import get_settings as _get_settings
    _compose_cfg = _get_settings().compose
    venc = _probe_nvenc(_compose_cfg.video_encoder)
    if venc and _compose_cfg.video_encoder == "h264_nvenc":
        # ★ 必须显式 High/4.1：NVENC 默认 Main/level4.0，超 1080×1920 上限，
        #   会被 Windows Media Player / Edge / QuickTime 拒播（0xC00D36C4）。
        vcodec = ["-c:v", "h264_nvenc", "-profile:v", "high", "-level", "4.1",
                  "-preset", _compose_cfg.preset, "-cq", str(_compose_cfg.cq)]
    else:
        # libx264 退路：High profile 天然全兼容，crf 取自 config fallback 首项
        _crf = _compose_cfg.fallbacks[0].get("crf", 20) if _compose_cfg.fallbacks else 20
        vcodec = ["-c:v", "libx264", "-profile:v", "high", "-level", "4.1",
                  "-preset", _compose_cfg.preset, "-crf", str(_crf)]
    _ab = _compose_cfg.audio_bitrate  # 例如 "192k"
    _ac = _compose_cfg.audio_codec     # 例如 "aac"
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
        "-i", str(audio_input),
        *vcodec, "-pix_fmt", "yuv420p",
        "-c:a", _ac, "-b:a", _ab, "-ar", "44100",
        # 不用 -shortest：旁白与视频帧数均由 timeline 精确对齐（297.9s → 7448 帧），
        # -shortest 会在低内存下 ffmpeg 缓冲波动时把成片提前截断（实测 2124 帧/85s）。
        "-movflags", "+faststart",
        # 显式 -f mp4：输出是 .tmp 名（非 .mp4 结尾），ffmpeg 无法从扩展名推断
        # muxer，必须用 -f mp4 指明（否则报 Unable to choose an output format）。
        "-f", "mp4", str(final_tmp),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    expected = int(round(total_ms / 1000.0 * FPS))
    n_total = 0
    _deadline = getattr(_compose_cfg, "timeout_sec", 0) or 0  # >0 则编码收口超时上限
    encode_ok = False   # 显式成功标志：仅编码完整收口(rc==0 且帧数达标)才置 True
    try:
        try:
            # 逐镜解码+调色后立刻出帧：单次只驻留当前镜头的一张已调色底图，
            # 出完即释放，低内存机器上避免同时驻留多张大图导致 OOM。
            import gc as _gc
            for sh, s0, s1 in timeline:
                grade = getattr(sh.motion, "grade", None) if sh.motion else None
                img = Image.open(upscaled[sh.id]).convert("RGB")
                boxes = plan_frames(*img.size, sh.duration_ms / 1000.0, sh.motion, FPS)
                for n, box in enumerate(boxes):
                    x0, y0, x1, y1 = box
                    cw, ch = x1 - x0, y1 - y0
                    if cw == W and ch == H:
                        frame = img.crop(box)
                    else:
                        frame = img.crop(box).resize((W, H), Image.LANCZOS)
                    # 光线调色在「已裁到成片尺寸」的帧上做，而非全尺寸超分源图：
                    # 1080×1920 帧的 grade 峰值内存只有 3344×1882 源图的 ~1/3，
                    # 在低 pagefile 机器上避免与 ffmpeg 编码器争抢提交额。
                    if grade:
                        frame = apply_grade(frame, grade)
                    g_ms = s0 - sh.audio_start_ms + n * 1000.0 / FPS
                    if s0 <= g_ms <= s1:
                        _draw_subtitle(frame, sh.subtitle_text)
                    proc.stdin.write(frame.tobytes())
                    n_total += 1
                del img, frame
                _gc.collect()
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            # Windows 下 ffmpeg 提前退出时 stdin.write 可能抛 OSError(Errno 22)
            # 而非 BrokenPipeError。两者都表示 ffmpeg 已收口，必须报错而非静默出短片。
            if n_total < expected:
                _kill_proc(proc)
                # 读取 ffmpeg stderr，露出真实退出原因（编码错误 / OOM / 输入拒绝）
                derr = _read_stderr(proc)
                raise RuntimeError(
                    f"编码中途断管：已写 {n_total}/{expected} 帧 "
                    f"(成片被 ffmpeg 提前截断)。ffmpeg stderr:\n{derr[-1200:]}")
        else:
            # 全部帧已写完并已发 stdin EOF。此后必须【等待 ffmpeg 真正收口】，
            # 否则 compose 会提前返回，而 ffmpeg 仍在后台编码缓冲帧 + 用
            # +faststart 回写 moov——此时读取/改名成片会拿到损坏的 NAL 流
            # 或缺失 moov（历史多次「OK 但 mp4 损坏」的根因）。
            # 带超时（P1 2.1 修复）：ffmpeg 出错 stderr 填满管道缓冲导致写阻塞时，
            # wait 永不返回、主进程卡死。超时则 kill 并抛错。
            rc = _wait_proc(proc, _deadline, f"{ep_id} 编码")
            # 读取并收起 ffmpeg stderr，避免管道满导致 ffmpeg 卡死。
            derr = _read_stderr(proc)
            if rc != 0 or n_total < expected:
                _kill_proc(proc)
                raise RuntimeError(
                    f"编码未完整收口：rc={rc} 已写 {n_total}/{expected} 帧。"
                    f"ffmpeg stderr:\n{derr[-1200:]}")
            encode_ok = True   # 编码完整收口，.tmp 为可用的成片，交给原子改名
    finally:
        # 孤儿 ffmpeg 清理（P1 2.2 修复）：任何异常/中断（含 KeyboardInterrupt）都
        # 确保子进程被 kill；Windows 强杀主 python 不连带杀 ffmpeg 的历史坑。
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        # P0-2 修复：只删【未成功】的 .tmp。
        # 旧写法 `if not (final_tmp.exists() and final.exists())` 在【新 episode 首产】
        # 且编码成功时（旧成片 final 不存在 → 条件为 True）会误删成功产出的 .tmp，
        # 随后原子改名处报 "未生成 .tmp 成片" —— 首次生产永远失败。改用显式标志：
        # 仅 encode_ok=False（失败/中断）才清理 .tmp；成功则保留交给下方 os.replace。
        if not encode_ok:
            try:
                final_tmp.unlink(missing_ok=True)
            except Exception:
                pass

    # 原子收口：编码成功（final_tmp 已生成）→ 改名成真名，覆盖旧片
    if not final_tmp.exists():
        raise RuntimeError(f"编码后未生成 .tmp 成片：{final_tmp}（疑似失败被清理）")
    try:
        os.replace(final_tmp, final)
    except OSError as e:
        # 极端：真名被占用（如外部播放器正读旧片）→ 保留 .tmp，明确报错
        raise RuntimeError(f"原子改名失败 {final_tmp} → {final}: {e}。"
                           f"请关闭正在读取旧成片的程序后重试。") from e
    save(ep)
    print(f"成片: {final}  {final.stat().st_size/1024/1024:.1f}MB  "
          f"{total_ms/1000:.2f}s  {n_total}帧")
    return final


if __name__ == "__main__":
    ep_id = sys.argv[1] if len(sys.argv) > 1 else "hw-001"
    compose_episode(ep_id)
