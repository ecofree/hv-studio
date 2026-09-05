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
from app.services.motion import (
    FPS, H, W, apply_grade, frames_for_duration_ms, plan_frames as _plan_frames,
)
from app.config.config import ROOT
from app.services.upscale import upscale

# BASE 单一真源：从 config.ROOT 推导（= 仓库根 / episodes），勿硬编码盘符
# （2026-09-04 收敛：换盘符/换机失效）。兼容外部传入 base_dir 场景。
BASE = ROOT / "episodes"
HEAD_MS = 100
TAIL_MS = 150


def expected_frames(timeline_slice) -> int:
    """段/片预期帧数——全项目唯一真值口径，勿在别处另写一份。

    口径：``Σ 逐镜 round(duration_ms/1000*FPS)``，与 ``motion.plan_frames``
    逐镜同式（二者都委托 ``motion.frames_for_duration_ms``）。

    为什么不能用 ``round(Σ时长/1000*FPS)``：
        一次舍入 vs 逐镜舍入会累积差额。实测 laogongtuan：
        ``round(1258444ms/1000*25)=31461``，而逐镜累加 = ``31487``（差 26 帧）。
        按 90s 切 15 段时，**11 段两者不等**（差 1~5 帧）——分段渲染若用
        一次舍入做「段已完成则跳过」的等值判据，这 11 段永远判不完，
        断点续跑会静默退化成每次全量重编（分段唯一的核心收益归零）。

    该差额此前未暴露，是因为旧代码里 expected 只用于
    ``n_total < expected`` 的断管检测（``>=`` 即放行），改成分段的等值
    判据后才成为硬伤。故此处定死为逐镜求和，供：
      - 分段渲染的「段已完成」判据
      - 断管检测
      - ``timing_assert`` #5（视频总帧数 = Σ镜头帧）
    三处共用，杜绝口径分裂。

    参数 timeline_slice：``[(shot, s0, s1), ...]`` 或 ``[shot, ...]`` 均可。
    """
    total = 0
    for item in timeline_slice:
        sh = item[0] if isinstance(item, (tuple, list)) else item
        total += frames_for_duration_ms(sh.duration_ms or 0, FPS)
    return total

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

# 运行期降级闸门：硬件编码器（NVENC 等）在真实大分辨率编码时资源失败过一次后
# 置 True，后续所有段直接走软件编码，不再重复撞同一个 OOM。
#
# 为什么需要运行期降级，光靠 _probe_nvenc 不够：
#   探测只用 320x568 的 0.05s 素材，NVENC 会话/显存占用极小，很容易通过；
#   真实编码是 1080x1920 逐帧喂，显存与 NVENC surface 占用量级完全不同。
#   2026-09-05 实测：探测通过、真跑崩 `CreateInputBuffer failed: out of memory`，
#   而此处原本直接 raise → 整片合成失败（测试里 4 个 compose 用例全红）。
_HWENC_DISABLED = False

# 硬件编码器「资源类」失败特征（区别于参数错误：参数错降级也没用）
_HWENC_FAIL_SIG = (
    "createinputbuffer failed",
    "openencodesessionex failed",
    "out of memory",
    "cannot allocate memory",
    "no capable devices found",
    "device does not support",
    "driver does not support",
    "generic error in an external library",
)


def _is_hw_vcodec(vcodec: list[str]) -> bool:
    """vcodec 参数是否是硬件编码器（nvenc/qsv/amf/vaapi）。"""
    try:
        codec = vcodec[vcodec.index("-c:v") + 1]
    except (ValueError, IndexError):
        return False
    return any(k in codec for k in ("nvenc", "qsv", "amf", "vaapi", "videotoolbox"))


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


def _build_vcodec(use_nvenc: bool) -> list[str]:
    """构造视频编码参数（NVENC High/4.1 或 libx264 退路）。config 驱动。

    运行期若已因硬件编码器资源失败降级过（_HWENC_DISABLED），无论入参如何
    都走软件编码——避免后续每段都重试一遍 NVENC 再失败（2× 耗时）。
    """
    from app.config.config import get_settings as _gs
    cfg = _gs().compose
    if _HWENC_DISABLED:
        use_nvenc = False
    if use_nvenc and cfg.video_encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-profile:v", "high", "-level", "4.1",
                "-preset", cfg.preset, "-cq", str(cfg.cq)]
    # 退路 preset/crf 必须读 config.fallbacks[0] 自己声明的值，绝不能用顶层
    # cfg.preset（那是 h264_nvenc 的 p4，libx264 无此 preset → 无 NVENC 机器
    # 走退路时 ffmpeg 直接报错失败）。config 已配 medium/veryfast，此处曾漏取。
    _fb = cfg.fallbacks[0] if cfg.fallbacks else {}
    _x264_preset = _fb.get("preset") or "medium"
    _crf = _fb.get("crf", 20)
    return ["-c:v", "libx264", "-profile:v", "high", "-level", "4.1",
            "-preset", _x264_preset, "-crf", str(_crf)]


def _encode_video_span_once(
    timeline_slice,
    upscaled: dict[str, Path],
    out_mp4: str | Path,
    vcodec: list[str],
    timeout_sec: float = 0,
    plan_frames=None,
) -> int:
    """把一段 timeline 逐镜出帧喂 ffmpeg，编码成 **无音轨** 的视频 mp4。

    只出视频、不混音。分段合成的核心单元：
      - 每段独立编码参数（vcodec 由 _build_vcodec 全片一致传入，保证可 concat）
      - 段编码【不带 +faststart】（它在段末做 moov 前移重写，会抬内存——
        正是低 pagefile 机器上首次 compose 崩 WinError 1455 的同类风险。
        只有最终整片 mux 才需要 +faststart 便于流式播放）
      - 幂等判定由调用方按返回帧数 nb_frames 决定是否跳过

    返回实际写出帧数。失败/断管/未收口一律 raise（不留半截段）。
    """
    # 显式 -f mp4：输出名若非 .mp4 结尾（如 .tmp），ffmpeg 无法从扩展名推断
    # muxer，必须用 -f mp4 指明。
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
        "-i", "-",
        *vcodec, "-pix_fmt", "yuv420p",
        "-an",                      # 只出视频，音轨由最终 mux 一次加
        "-f", "mp4", str(out_mp4),
    ]
    pf = plan_frames or _plan_frames
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    expected = expected_frames(timeline_slice)
    n_total = 0
    encode_ok = False
    _deadline = timeout_sec or 0
    try:
        try:
            import gc as _gc
            for sh, s0, s1 in timeline_slice:
                grade = getattr(sh.motion, "grade", None) if sh.motion else None
                img = Image.open(upscaled[sh.id]).convert("RGB")
                boxes = pf(*img.size, sh.duration_ms / 1000.0, sh.motion, FPS)
                for n, box in enumerate(boxes):
                    x0, y0, x1, y1 = box
                    cw, ch = x1 - x0, y1 - y0
                    if cw == W and ch == H:
                        frame = img.crop(box)
                    else:
                        frame = img.crop(box).resize((W, H), Image.LANCZOS)
                    # 光线调色在已裁帧上做（峰值内存 ~1/3，低 pagefile 避免与编码器争）
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
            if n_total < expected:
                _kill_proc(proc)
                derr = _read_stderr(proc)
                raise RuntimeError(
                    f"段编码中途断管：已写 {n_total}/{expected} 帧。"
                    f"ffmpeg stderr:\n{derr[-1200:]}")
        else:
            rc = _wait_proc(proc, _deadline, "段编码")
            derr = _read_stderr(proc)
            if rc != 0 or n_total < expected:
                _kill_proc(proc)
                raise RuntimeError(
                    f"段编码未完整收口：rc={rc} 已写 {n_total}/{expected} 帧。"
                    f"ffmpeg stderr:\n{derr[-1200:]}")
            encode_ok = True
    finally:
        if proc.poll() is None:
            _kill_proc(proc)
        if not encode_ok:
            try:
                Path(out_mp4).unlink(missing_ok=True)
            except Exception:
                pass
    return n_total


def _encode_video_span(
    timeline_slice,
    upscaled: dict[str, Path],
    out_mp4: str | Path,
    vcodec: list[str],
    timeout_sec: float = 0,
    plan_frames=None,
) -> int:
    """_encode_video_span_once + 硬件编码器失败自动降级。

    硬件编码器（NVENC）在真实大分辨率编码时可能因显存/NVENC 会话耗尽而崩，
    而 _probe_nvenc 的小素材探测通不过不了这个场景。这里捕获「资源类」失败
    并降级到 libx264 重试一次，避免整片合成失败。

    只降级资源类失败；参数类错误（如非法 preset）降级也必然失败，直接抛。
    """
    global _HWENC_DISABLED
    # 之前已降级过，但调用方手里还是旧的硬件 vcodec（它在循环外只算了一次）
    if _HWENC_DISABLED and _is_hw_vcodec(vcodec):
        vcodec = _build_vcodec(False)
    try:
        return _encode_video_span_once(timeline_slice, upscaled, out_mp4,
                                       vcodec, timeout_sec, plan_frames)
    except RuntimeError as e:
        if not _is_hw_vcodec(vcodec) or _HWENC_DISABLED:
            raise
        msg = str(e)
        if not any(sig in msg.lower() for sig in _HWENC_FAIL_SIG):
            raise
        _HWENC_DISABLED = True
        sw = _build_vcodec(False)
        # 已写好的段不会重编：本片会是「前段硬件 + 后段软件」混合。两者同为
        # H.264 High/4.1，concat -c copy 可接受；相比整片失败这是明确更优解。
        print(f"  [compose] 硬件编码器资源失败，本片后续段降级 {sw[1]}："
              f"{msg.splitlines()[0][:110]}\n"
              f"            注意：此前已写好的段保留硬件编码（不重编），成片为混合编码，"
              f"需重跑以获得统一编码器时请先清理 build/segments/。")
        Path(out_mp4).unlink(missing_ok=True)
        return _encode_video_span_once(timeline_slice, upscaled, out_mp4,
                                       sw, timeout_sec, plan_frames)


def _probe_segment_frames(p: Path) -> int:
    """读段视频实际帧数（不整片解码，用容器元数据；缺失/损坏返回 -1）。"""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-count_packets", "-show_entries", "stream=nb_read_packets",
             "-of", "default=noprint_wrappers=1:nokey=1", str(p)],
            capture_output=True, text=True, timeout=60)
        if r.returncode != 0 or not r.stdout.strip():
            return -1
        return int(r.stdout.strip())
    except Exception:
        return -1


def _concat_segments(seg_paths: list[Path], out_mp4: Path) -> None:
    """ffmpeg concat demuxer -c copy 拼接无音轨段视频。要求各段编码参数一致。"""
    list_file = out_mp4.with_suffix(".concat.txt")
    list_file.write_text(
        "".join(f"file '{p.as_posix()}'\n" for p in seg_paths),
        encoding="utf-8")
    # concat 输出同样先写 .tmp 再原子改名，中途失败不留半截
    tmp = out_mp4.with_suffix(".mp4.concat_tmp")
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
             "-i", str(list_file), "-c", "copy", "-f", "mp4", str(tmp)],
            capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(
                f"concat 拼接失败: {r.stderr[-800:]}")
        os.replace(tmp, out_mp4)
    finally:
        try:
            list_file.unlink(missing_ok=True)
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _segments_of(timeline, max_sec: float) -> list:
    """按镜头边界把 timeline 切成 ≤ max_sec 的段。段长仅由镜头累积时长决定，
    绝不切开单个镜头（否则呼吸 pad / 字幕窗口会错位）。"""
    if max_sec <= 0:
        return [timeline]
    segs, cur, acc = [], [], 0.0
    for row in timeline:
        dur = row[0].duration_ms / 1000.0
        if cur and acc + dur > max_sec:
            segs.append(cur)
            cur, acc = [], 0.0
        cur.append(row)
        acc += dur
    if cur:
        segs.append(cur)
    return segs


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

    # ---------- 1.4) 图可达性预检（防跨集删图 → 渲染到那一镜才炸） ----------
    # laogongtuan/hanwu-* 存在跨集绑图（hw-002 底图被 5 集引用）。删/挪一张，
    # 若到渲染才 Image.open 才发现，白跑前面的段。合成前统一探一次，早失败。
    _imgs = {sh.image_path for sh, _, _ in timeline if sh.image_path}
    _gone = [p for p in _imgs if not Path(p).exists()]
    if _gone:
        raise RuntimeError(
            f"episode {ep_id} 有 {len(_gone)} 张底图缺失(可能被删/跨集挪移)，"
            f"合成前拦截: {sorted(_gone)[:5]}…")

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
    # 场景复用下多个 shot 指向同一张底图，超分只需做一次（否则 93 镜重复放大 93 次）。
    # ★ 超分必须在切段之前全片一次性构建——若退化成段内构建，14 段 × 60 张会重复放大。
    _up_cache: dict[str, Path] = {}
    upscaled = {}
    for sh, _, _ in timeline:
        if sh.image_path not in _up_cache:
            _up_cache[sh.image_path] = upscale(sh.image_path)
        upscaled[sh.id] = _up_cache[sh.image_path]
    final = out_dir / f"{ep_id}_m1.mp4"

    # --- 编码参数（config.compose 驱动） ---
    from app.config.config import get_settings as _get_settings
    _compose_cfg = _get_settings().compose
    venc = _probe_nvenc(_compose_cfg.video_encoder)
    vcodec = _build_vcodec(venc)
    _ab = _compose_cfg.audio_bitrate
    _ac = _compose_cfg.audio_codec
    _timeout = getattr(_compose_cfg, "timeout_sec", 0) or 0

    # --- 4) 编码：短片单段 / 长片分段 + concat + 一次混音 ---
    # 预期帧数统一用逐镜求和口径（见 expected_frames）。它同时是
    #   - 分段「段已完成则跳过」的等值判据（口径分裂会导致 11/15 段永远判不完）
    #   - 断管检测阈值
    #   - 是否走分段的门槛（< segment_min_frames → 旧单段路径，保短片行为不变）
    n_total_all = expected_frames(timeline)

    def _mux_full_audio(video_src: Path, out_path: Path) -> None:
        """把无音轨视频 + 全片音轨一次 mux 成最终 mp4（+faststart 在此加）。

        音频必须按 config.compose 编码（aac/192k），绝不能 `-c copy` 直拷 wav：
        pcm_s16le 装进 mp4 后大量播放器无声（2026-09-05 lsm-15min 实测：
        码流有 pcm 音轨、波形正常，但播放端静音）。视频流仍 copy（零重编）。
        """
        tmp = out_path.with_suffix(".tmp")
        mux = [
            "ffmpeg", "-y", "-v", "error",
            "-i", str(video_src), "-i", str(audio_input),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy",
            "-c:a", _ac, "-b:a", _ab,
            # 不用 -shortest：旁白与视频帧数由 timeline 精确对齐（见历史注释），
            # -shortest 会在低内存下缓冲波动时提前截断成片。
            "-movflags", "+faststart",
            "-f", "mp4", str(tmp),
        ]
        r = subprocess.run(mux, capture_output=True, text=True, timeout=1200)
        if r.returncode != 0:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            raise RuntimeError(f"音画 mux 失败: {r.stderr[-800:]}")
        os.replace(tmp, out_path)

    if n_total_all < _compose_cfg.segment_min_frames:
        # 短片：单段直出（纯视频无音轨），再统一 mux 音轨。
        # 保持历史可读性且不回归：产物等价（video+h264 + audio+aac +faststart）。
        seg_dir = build / "segments"
        seg_dir.mkdir(parents=True, exist_ok=True)
        vid = seg_dir / "seg-000.mp4"
        if not (vid.exists() and _probe_segment_frames(vid) == n_total_all):
            _encode_video_span(timeline, upscaled, vid, vcodec, _timeout,
                               plan_frames=plan_frames)
        _mux_full_audio(vid, final)
        n_actual = n_total_all
    else:
        # 长片：按镜头边界切段，逐段编码（可断点续跑），concat copy + 一次混音
        seg_dir = build / "segments"
        seg_dir.mkdir(parents=True, exist_ok=True)
        segs = _segments_of(timeline, _compose_cfg.segment_max_sec)
        seg_paths: list[Path] = []
        rendered_any = False
        for i, sl in enumerate(segs):
            exp = expected_frames(sl)
            p = seg_dir / f"seg-{i:03d}.mp4"
            # 断点续跑：段已存在且帧数 = 预期 → 跳过（用逐镜求和口径，11/15 段能真命中）
            skip = False
            if p.exists():
                nf = _probe_segment_frames(p)
                if nf == exp:
                    skip = True
                else:
                    print(f"  [compose] 段 seg-{i:03d} 帧数 {nf}≠{exp}，重编")
                    try:
                        p.unlink(missing_ok=True)
                    except Exception:
                        pass
            if not skip:
                print(f"  [compose] 渲染段 {i+1}/{len(segs)} ({len(sl)} 镜, {exp} 帧)")
                _encode_video_span(sl, upscaled, p, vcodec, _timeout,
                                   plan_frames=plan_frames)
                rendered_any = True
            seg_paths.append(p)
        # concat 无音轨视频
        _concat_segments(seg_paths, final)
        # 一次 mux 全片音轨（BGM 已合进 narration，此步只加音轨，不按段 fade）
        _mux_full_audio(final, final)
        n_actual = n_total_all

    save(ep)
    print(f"成片: {final}  {final.stat().st_size/1024/1024:.1f}MB  "
          f"{total_ms/1000:.2f}s  {n_actual}帧")
    return final


if __name__ == "__main__":
    ep_id = sys.argv[1] if len(sys.argv) > 1 else "hw-001"
    compose_episode(ep_id)
