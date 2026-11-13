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

from app.services.provenance import file_sha256, atomic_json
import hashlib
import json
import os
import re
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
from app.paths import episode_dir
from app.services.upscale import upscale
# 输出规格常量（FPS/W/H/HEAD_MS/TAIL_MS/NARRATION_SR）统一来自 spec（整改 C1）。
# 由 motion 转手的 FPS/W/H 保留（motion re-export，值同源）；间隔与采样率直接从
# spec 取，不再在本模块另写一份。
from app.services.spec import HEAD_MS, NARRATION_SR, TAIL_MS  # noqa: F401

# P1-6（2026-09-05）：原 `BASE = ROOT / "episodes"` 已删。它是 import 期常量，
# 测试无法重定向 → 测试只能真写 D:/hv/episodes/，再靠「会话末按前缀通配删」
# 兜底（进程被杀就残留；前缀与交付集相撞就是误删）。现统一走
# app.paths.episode_dir()（调用期求值），本模块不再自己拼 episodes 根。


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

# ---------------------------------------------------------------------------
# 字幕排版（整改 C2：改读 config profile.subtitle，不再硬编码）
# ---------------------------------------------------------------------------
# 此前这里是 FONT_PATH / FONT_SIZE / SUBTITLE_BOTTOM / STROKE 四个硬编码值，
# 而 config.yaml 的 subtitle 段与 ProfileSubtitle 模型一直存在却**无人读取**——
# 注释还写着「对齐 config subtitle.font_size」，纯属自我安慰：没有任何机制保证
# 对齐，改 config 完全不生效。现在由 config 驱动，下面这些是 config 缺失/读取
# 失败时的兜底（= 2026-09-05 之前的实际渲染值，保证行为不跳变）。
DEFAULT_FONT_PATH = "C:/Windows/Fonts/NotoSerifSC-VF.ttf"   # 思源宋体(Noto Serif SC)
DEFAULT_FONT_SIZE = 72
DEFAULT_MARGIN_BOTTOM = 320
DEFAULT_STROKE = 5
# 无 config 对应项的两个值（排版细节，暂不进 config，避免为凑对称造字段）
TEXT_W = W - 150          # 字幕可用宽度（每行留左右各 ~75px）
LINE_GAP = 16

# config 里 font 是「字体名」不是路径 → 名字到本机路径的映射。
# 思源宋体（Adobe/Google 联合设计的开源 Source Han Serif）在 Noto 名下叫
# Noto Serif SC，可变字体文件即 NotoSerifSC-VF.ttf。
_FONT_NAME_TO_PATH = {
    "思源宋体": DEFAULT_FONT_PATH,
    "source han serif": DEFAULT_FONT_PATH,
    "noto serif sc": DEFAULT_FONT_PATH,
    "notoserifsc": DEFAULT_FONT_PATH,
}

_SUB_CFG = None      # 惰性缓存：get_settings() 会读 .env + yaml，不宜在 import 期跑


def _subtitle_cfg():
    """取 profile.subtitle（惰性 + 缓存）。config 不可用 → 返回 None，走兜底。"""
    global _SUB_CFG
    if _SUB_CFG is None:
        try:
            from app.config.config import get_settings
            _SUB_CFG = get_settings().get_profile().subtitle
        except Exception:  # noqa: BLE001 —— 配置坏了不该让合成直接崩
            _SUB_CFG = False      # False 而非 None：区分「没取过」与「取不到」
    return _SUB_CFG or None


def reset_subtitle_cfg() -> None:
    """清缓存（测试/热更新用）。改了 config.yaml 后调它立即生效。"""
    global _SUB_CFG
    _SUB_CFG = None


# ---------------------------------------------------------------------------
# 时间轴叠加层（标题卡 / 幕卡）—— 与字幕**分层**，见 app/services/overlay.py
# ---------------------------------------------------------------------------
# 为什么不能复用字幕那条路：subtitle_fidelity 要求每条 cue 的文本逐字等于剧本
# 原文，而标题不是剧本文字，放进 cue 表会被门禁正确地打回。字幕还要满足
# 0.8~4s / 两行 / 字数上限（要在旁白速度下读完），标题要的恰好相反：大、短、居中。
#
# 缺席即无操作：没有 episodes/{id}/overlays.json 时计划为空，渲染分支不进循环，
# 段指纹也不加 overlays 键 —— 产物与本文件改动前字节等价，旧段不会重渲。
_OVERLAY_PLAN = None


def overlay_plan(ep_id: str | None = None):
    """载入并缓存当前集的叠加层计划（惰性，与 _SUB_CFG 同风格）。

    ``ep_id`` 省略时只取当前计划。加载失败**抛错而非静默跳过** —— 写坏的
    overlays.json 若被吞掉，观众看到的是「我明明配了标题卡怎么没有」。
    """
    global _OVERLAY_PLAN
    from app.services import overlay as _ov
    if ep_id is not None:
        try:
            plan = _ov.load(ep_id)
        except (ValueError, OSError) as exc:
            raise ValueError(f"{ep_id} 的 overlays.json 无效：{exc}") from exc
        _OVERLAY_PLAN = plan
        if plan.specs:
            print(f"  [overlay] 载入 {len(plan.specs)} 条叠加层"
                  f"（指纹 {plan.fingerprint()}）")
    return _OVERLAY_PLAN if _OVERLAY_PLAN is not None else _ov.EMPTY


def reset_overlay_plan() -> None:
    """清缓存（测试/热更新用）。"""
    global _OVERLAY_PLAN
    if _OVERLAY_PLAN is not None:
        _OVERLAY_PLAN.close()
    _OVERLAY_PLAN = None


# ---------------------------------------------------------------------------
# 逐词高亮（卡拉OK 字幕）—— 同样可选、同样缺席即无操作
# ---------------------------------------------------------------------------
_WORD_PLAN = None


def word_plan(ep_id: str | None = None):
    from app.services import word_timing as _wt
    global _WORD_PLAN
    if ep_id is not None:
        try:
            plan = _wt.load(ep_id)
        except (ValueError, OSError) as exc:
            raise ValueError(f"{ep_id} 的 word_timing.json 无效：{exc}") from exc
        _WORD_PLAN = plan
        if plan.words:
            print(f"  [karaoke] 载入 {len(plan.words)} 帧字级时间戳，逐词高亮开启")
    return _WORD_PLAN if _WORD_PLAN is not None else _wt.EMPTY


def reset_word_plan() -> None:
    global _WORD_PLAN
    if _WORD_PLAN is not None:
        _WORD_PLAN.close()
    _WORD_PLAN = None


def _highlight_span(wp, frame_id, source, ms):
    """(start, end) in display coords for the word spoken at ``ms``, else None."""
    if not wp or not wp.words:
        return None
    ws = wp.words.get(frame_id)
    if not ws:
        return None
    from app.services import word_timing as _wt
    display = subtitle_display_text(source)
    spans = _wt.map_words(display, source, ws)
    if spans is None:
        return None
    for w, sp in zip(ws, spans):
        if w.ms_start <= ms < w.ms_end:
            return sp
    return None


def _sub_font_path() -> str:
    """config.subtitle.font → 本机字体路径。"""
    cfg = _subtitle_cfg()
    name = (getattr(cfg, "font", "") or "").strip() if cfg else ""
    if not name:
        return DEFAULT_FONT_PATH
    if name.lower() in _FONT_NAME_TO_PATH:
        return _FONT_NAME_TO_PATH[name.lower()]
    # 看起来就是个路径（含分隔符或字体扩展名）→ 直接用，方便换机时写绝对路径
    if any(sep in name for sep in ("/", "\\")) or \
            name.lower().endswith((".ttf", ".ttc", ".otf")):
        return name
    return DEFAULT_FONT_PATH


def _sub_font_size() -> int:
    cfg = _subtitle_cfg()
    v = getattr(cfg, "font_size", None) if cfg else None
    return int(v) if v else DEFAULT_FONT_SIZE


def _sub_bottom() -> int:
    """字幕块底边 y 坐标（bottom-anchor，由下向上生长，永不越过底栏安全区）。"""
    cfg = _subtitle_cfg()
    v = getattr(cfg, "margin_bottom", None) if cfg else None
    return H - (int(v) if v else DEFAULT_MARGIN_BOTTOM)


def _sub_stroke() -> int:
    cfg = _subtitle_cfg()
    v = getattr(cfg, "outline", None) if cfg else None
    return int(v) if v else DEFAULT_STROKE


# 兼容既有 `from app.services.compose import FONT_PATH` 的写法。
# 注意：这些是**兜底值**；真实生效值走上面的 _sub_*()，因为 config 可能不同。
FONT_PATH = DEFAULT_FONT_PATH
FONT_SIZE = DEFAULT_FONT_SIZE
SUBTITLE_BOTTOM = H - DEFAULT_MARGIN_BOTTOM
STROKE = DEFAULT_STROKE

# 中文避头尾 / 折行标点法则
#  禁行首：不可作「行首」的收尾/终句标点 —— 折行时并入上一行，杜绝孤标点（"…的人，"整行同句）。
_NO_LINE_START = set("，。、；：？！）」』》〉·…—")
#  禁行尾：不可作「行尾」的开括号/引号 —— 断点若落其后不合法。
_NO_LINE_END = set("（「『《〈【“‘")
#  标点分界：偏好在此类标点之后断行（让上一行以语义标点收尾，更符合阅读习惯）。
_BREAK_AFTER = set("，。、；：？！…—")
# 上屏前剥掉的尾部标点（防孤行/视觉占位）。
#   2026-09-06 补 `，` `、`：原来只剥句末，切出来的碎片「幸好，」「陇西平原，」
#   仍顶着逗号上屏 —— 竖屏大字每行都是独立碎片，句读标点在行尾毫无意义。
# ⚠️ 不收 `.`：会吃掉「3.5」「0.5秒」这类小数点（实测剧本里有）。
# ⚠️ 本集合进了段参数指纹（见 _seg_params_fingerprint），改它旧段会自动作废。
_SENT_END_PUNCT = frozenset("。！？；：…—，、")

# 上屏标点 → 空格（2026-09-25 用户规则：「视频里的标点符号都换成空格」）。
# 与 _SENT_END_PUNCT 的分工：
#   _SENT_END_PUNCT  只作用于**尾部**（rstrip 直接剥掉）；
#   本集合作用于**整句**（translate 成空格，保留标点造成的视觉间隔）。
# 例：「浅水原迫降薛仁杲、柏壁逐宋金刚、」→「浅水原迫降薛仁杲 柏壁逐宋金刚」
# ⚠️ 同样不收 `.` `,`：小数/千分位（「3.5」「2,000」）会被吃掉（沿用 line 179 的既有约束）。
# ⚠️ 本集合进段参数指纹，改它旧段会自动作废重渲（渲染结果确实变了）。
_SUB_PUNCT_TO_SPACE = frozenset(
    "。！？；：…—，、·"          # 全角句末/停顿
    "“”‘’「」『』"              # 全角引号
    "（）〔〕【】《》〈〉"          # 全角括号 / 书名号
    "!?;:~"                     # 半角（安全：不用于小数点）
)


def _sub_strip_punct() -> str:
    """上屏前剥掉的尾部标点，**排序后**返回（进指纹，顺序必须稳定）。"""
    return "".join(sorted(_SENT_END_PUNCT))


def _sub_space_punct() -> str:
    """上屏前换成空格的标点，**排序后**返回（进指纹，顺序必须稳定）。"""
    return "".join(sorted(_SUB_PUNCT_TO_SPACE))


_SPACE_RUN = re.compile(r"\s+")
_PUNCT_TO_SPACE_TABLE = {ord(c): " " for c in _SUB_PUNCT_TO_SPACE}


def strip_sub_tail(text: str) -> str:
    """上屏文案：剥掉尾部句读/句末标点；剥光了退回原文（别出空字幕）。"""
    t = text.rstrip(_sub_strip_punct())
    return t or text


def subtitle_display_text(text: str) -> str:
    """最终上屏文案：标点符号 → 空格（2026-09-25 用户规则）。

    尾部仍整体剥掉（保持 strip_sub_tail 语义，避免孤标点占位）；
    句中标点换成空格，保留停顿造成的视觉间隔；连续空白压成一个，
    剥光了退回原文（别出空字幕）。
    """
    base = strip_sub_tail(text)
    t = _SPACE_RUN.sub(" ", base.translate(_PUNCT_TO_SPACE_TABLE)).strip()
    return t or base


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """加载思源宋体并固定到 SemiBold(600) 字重。

    NotoSerifSC-VF.ttf 是可变字体，ImageFont.truetype 默认落在最细的
    ExtraLight(200) —— 竖屏大字字幕太细、识别度差。这里显式设到
    SemiBold：600 字重 + 描边，清晰又不发糊。
    """
    f = ImageFont.truetype(_sub_font_path(), size)
    try:
        f.set_variation_by_name("SemiBold")
    except Exception:
        try:
            # 退路：按 Weight 轴坐标设置（SemiBold≈600）
            f.set_variation_by_axes([600])
        except Exception:
            pass  # 非可变字体 / 无该轴 → 用默认
    return f


def check_font_available(size: int | None = None) -> None:
    """字幕字体可用性自检：字体缺失/路径失效 → 立即抛错，不等到烧字幕时。

    返回 None 表示可用。抛出的 RuntimeError 带排查指引（字体名 + 当前路径 +
    怎么改），因为换台机器就撞上、且报错点在几十分钟渲染之后，极难定位。
    """
    path = _sub_font_path()
    try:
        _load_font(size or _sub_font_size())
    except Exception as e:  # noqa: BLE001 —— OSError/IOError 都指向「字体不可用」
        raise RuntimeError(
            f"字幕字体不可用：{path}\n"
            f"  原因：{type(e).__name__}: {e}\n"
            f"  处理：在本机安装思源宋体（Noto Serif SC），或把 compose.FONT_PATH "
            f"改为本机存在的中文字体（如 C:/Windows/Fonts/msyh.ttc）。\n"
            f"  提示：换机/换盘后 C:/Windows/Fonts 下的字体不一定齐，"
            f"此自检就是为这个场景加的。") from e


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


SUB_FILL = (255, 255, 255)
HILITE = (255, 214, 102)      # 与标题卡的第 N 集同色，一套视觉语言


def _draw_subtitle(img: Image.Image, text: str, hl: tuple[int, int] | None = None) -> None:
    """Burn one cue into ``img``.

    ``hl`` is an optional ``(start, end)`` range in **display** coordinates (see
    ``word_timing.cover``). The word inside it is drawn in the accent colour;
    everything else stays white. Without it this is byte-for-byte the previous
    behaviour, which is why existing segments keep their fingerprints.
    """
    d = ImageDraw.Draw(img)
    font = _get_font(_sub_font_size())
    # 标点 → 空格（2026-09-25 用户规则）；尾部仍整体剥掉，避免孤标点占位
    t = subtitle_display_text(text)
    if not t:
        t = text
    lines = _wrap(d, t, font, TEXT_W)
    heights = [d.textbbox((0, 0), ln, font=font)[3] for ln in lines]
    total_h = sum(heights) + LINE_GAP * (len(heights) - 1)
    # 字幕块底边对齐 config subtitle.margin_bottom，多行字幕从底边向上生长，
    # 永远不越过底栏安全区。
    stroke = _sub_stroke()
    # Include outline and the four-pixel shadow in the bottom safe margin.
    y = _sub_bottom() - total_h - stroke - 4
    # 折行后每行在整句显示文本里的起止偏移，高亮区间才能落到正确的行上。
    # _wrap 把原文切成 seg[:cut]/seg[cut:]，各行字符数之和恰好等于原串长度
    # ——折行处**不占字符**，所以这里只累加 len(ln)。
    starts, acc = [], 0
    for ln in lines:
        starts.append((acc, acc + len(ln)))
        acc += len(ln)
    for i, ln in enumerate(lines):
        w = d.textbbox((0, 0), ln, font=font)[2]
        x = (W - w) // 2
        base, end = starts[i]
        segs = [(ln, SUB_FILL)]
        if hl and hl[0] < end and hl[1] > base:
            a, b = max(hl[0], base) - base, min(hl[1], end) - base
            segs = ([(ln[:a], SUB_FILL)] if a else []) + \
                   [(ln[a:b], HILITE)] + \
                   ([(ln[b:], SUB_FILL)] if b < len(ln) else [])
        cx = x
        for seg, fill in segs:
            if not seg:
                continue
            sw = d.textbbox((0, 0), seg, font=font)[2]
            d.text((cx + 3, y + 4), seg, font=font, fill=(0, 0, 0),
                   stroke_width=stroke, stroke_fill=(0, 0, 0))
            d.text((cx, y), seg, font=font, fill=fill,
                   stroke_width=stroke, stroke_fill=(16, 16, 16))
            cx += sw
        y += heights[i] + LINE_GAP


def _group_of(frame_id: str) -> str:
    """帧所属「源句子组」。单一真源在 spec.group_of（见该函数说明）。"""
    from app.services.spec import group_of
    return group_of(frame_id)


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


def _start_stderr_reader(proc):
    import threading
    from collections import deque
    proc._hv_errors=deque(maxlen=80)
    def drain():
        try:
            for line in iter(proc.stderr.readline,b""):
                proc._hv_errors.append(line.decode("utf-8","replace")[:1024])
        except (OSError,ValueError): pass
    proc._hv_reader=threading.Thread(target=drain,daemon=True)
    proc._hv_reader.start()


def _read_stderr(proc) -> str:
    """读取并收起 ffmpeg stderr，避免管道满导致 ffmpeg 卡死。

    读完必须 close：stderr=PIPE 的读端是个 BufferedReader，不关就一直占着
    一个 fd（C5 删掉全局 filterwarnings 后暴露：一个成片有多少段就漏多少个）。
    已关闭时再调会 return ""，不会掩盖后面的错误信息。
    """
    if hasattr(proc,"_hv_reader"):
        proc._hv_reader.join(timeout=5)
        return "".join(proc._hv_errors)
    try:
            if proc.stderr:
                try:
                    return proc.stderr.read().decode("utf-8", "ignore")
                finally:
                    _safe_close(proc.stderr)
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
    transitions: dict[str, tuple[int, int]] | None = None,
    overlays=None,
    karaoke=None,
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
    ov = overlays if overlays is not None else overlay_plan()
    kk = karaoke if karaoke is not None else word_plan()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    _start_stderr_reader(proc)
    import threading
    watchdog = threading.Timer(timeout_sec, lambda: _kill_proc(proc)) if timeout_sec > 0 else None
    if watchdog:
        watchdog.daemon = True
        watchdog.start()
    expected = expected_frames(timeline_slice)
    n_total = 0
    encode_ok = False
    _deadline = timeout_sec or 0
    try:
        try:
            import gc as _gc
            for sh, s0, s1 in timeline_slice:
                grade = getattr(sh.motion, "grade", None) if sh.motion else None
                # 用 with 包住：Image.open 返回的是懒加载文件对象，.convert()
                # 只是强制读像素、不会关闭底层 fd。此前一个成片循环里会累积
                # 上百个未关闭句柄（C5 删掉全局 filterwarnings 后才暴露）。
                # convert() 返回的是独立新图，close 后继续使用安全。
                with Image.open(upscaled[sh.id]) as _im:
                    img = _im.convert("RGB")
                boxes = pf(*img.size, sh.duration_ms / 1000.0, sh.motion, FPS)
                # 换图处的黑场过渡：只在**既有帧内**调亮度，不加帧、不动音频。
                # fade_in = 本镜开头几帧从黑里淡入（前一镜换了底图时）
                # fade_out = 本镜结尾几帧淡入黑（下一镜换了底图时）
                # 见 compose_episode 的 _image_transitions()。
                fin, fout = (transitions or {}).get(sh.id, (0, 0))
                n_frames = len(boxes)
                for n, box in enumerate(boxes):
                    x0, y0, x1, y1 = box
                    from app.services.motion import camera_crop
                    frame = camera_crop(img,box)
                    # 光线调色在已裁帧上做（峰值内存 ~1/3，低 pagefile 避免与编码器争）
                    if grade:
                        frame = apply_grade(frame, grade)
                    if fin and n < fin:
                        frame = _fade_to_black(frame, 1.0 - n / max(1, fin))
                    elif fout and n >= n_frames - fout:
                        frame = _fade_to_black(frame, (n - (n_frames - fout) + 1) / max(1, fout))
                    g_ms = s0 - sh.audio_start_ms + n * 1000.0 / FPS
                    # 叠加层在换图黑场**之后**合成：片头黑场淡入时标题保持全亮，
                    # 而不是跟着底图一起变暗。dim 压暗的是**底图**，字幕最后画、
                    # 始终在最上层保持满对比度。
                    if ov:
                        _d = ov.dim_at(g_ms)
                        if _d > 0.0:
                            frame = _fade_to_black(frame, _d)
                        for _tile, _box in ov.layers_at(g_ms):
                            frame.paste(_tile, _box, _tile)
                    if s0 <= g_ms <= s1:
                        # 字级高亮用**帧内相对毫秒**（= n/FPS），而不是 g_ms：
                        # word_timing 存的就是相对本帧音频的偏移。
                        _hl = (_highlight_span(kk, sh.frame_id, sh.subtitle_text, n * 1000.0 / FPS)
                               if kk else None)
                        _draw_subtitle(frame, sh.subtitle_text, _hl)
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
        if watchdog: watchdog.cancel()
        if proc.poll() is None:
            _kill_proc(proc)
        # 兜底：异常早退时 _read_stderr 可能没跑到，这里保证 stderr 读端收口
        _safe_close(proc.stderr)
        if not encode_ok:
            _safe_unlink(Path(out_mp4))
    return n_total


def _fade_to_black(frame, amount: float):
    """把一帧按 amount 压暗到黑，返回**新**图（amount=0 时原样返回入参）。

    ⚠️ PIL 的 ``Image.point(lut)`` 返回**新图**，不修改原图（本机 PIL 12.3 实测：
    ``f.point([0]*768)`` 之后 f 仍为原值）。所以必须用返回值重新绑定 ——
    写成 `frame.point(lut)` 而不接返回值就是一个静默空操作，黑场过渡会
    「配了但什么都没发生」。

    用 point() 的 LUT 一次性完成（与 apply_grade 同思路），逐像素 Python 循环
    在 1080x1920 上会慢两个数量级。amount<0 或 >1 被夹住，调用方不必自己保证。
    """
    a = 0.0 if amount < 0 else (1.0 if amount > 1 else amount)
    if a <= 0.0:
        return frame
    if a >= 1.0:
        return frame.point([0] * 768)      # 全黑：全零 LUT
    lut = [max(0, min(255, int(round(i * (1.0 - a))))) for i in range(256)]
    return frame.point(lut * 3)


def _image_transitions(timeline, frames_per_shot: dict[str, int], fade: int) -> dict:
    """算出每个镜头在换图处要淡入/淡出几帧。

    只在**底图真的变了**的边界做过渡。同一张底图的相邻镜头之间**不能**加黑场 ——
    那是连续运镜（见 shot_builder 的 _chained_motion），中间插一段黑等于人为
    制造一次「剪辑」，把刚修好的连续性又破坏掉。

    关键设计：**不加帧、不动音频**。原本要给每个换图点插入 K 帧黑场 + K 帧静音，
    那会同时改动 frames_for_duration_ms、expected_frames、分段边界和音频时钟
    （历史上正是这些口径分裂导致过「11 段永远判不完」）。改为在既有帧内调亮度：
    总帧数与总采样数一字不变，音画对位天然保持，段划分/断点续跑/verify 全不受
    影响。代价只是切点那一帧不到全黑，视觉上仍读作一次有意的暗场切换。
    """
    if fade <= 0:
        return {}
    out: dict[str, tuple[int, int]] = {}
    shots = [sh for sh, _, _ in timeline]
    for i, sh in enumerate(shots):
        prev = shots[i - 1] if i else None
        nxt = shots[i + 1] if i + 1 < len(shots) else None
        n = frames_per_shot.get(sh.id, 0)
        if n <= 0:
            continue
        fin = min(fade, n // 2) if (prev is not None and prev.image_path != sh.image_path) else 0
        fout = min(fade, n // 2) if (nxt is not None and nxt.image_path != sh.image_path) else 0
        if fin or fout:
            out[sh.id] = (fin, fout)
    return out


def _encode_video_span(
    timeline_slice,
    upscaled: dict[str, Path],
    out_mp4: str | Path,
    vcodec: list[str],
    timeout_sec: float = 0,
    plan_frames=None,
    transitions: dict[str, tuple[int, int]] | None = None,
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
                                       vcodec, timeout_sec, plan_frames,
                                       transitions)
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
                                       sw, timeout_sec, plan_frames, transitions)


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


def _safe_unlink(p: Path) -> None:
    """删临时文件——失败绝不能让渲染失败。

    2026-09-06 成片回归实测：`_concat_segments` 收尾的 `list_file.unlink()`
    抛了一个**非 Exception 的 BaseException**（沙箱删除闸走 SystemExit 类），
    `except Exception` 抓不住，于是 16 段渲染 1 小时的成果被打断在
    「已 concat、还没 mux 音轨」的半成品状态——成片 816MB、无音轨。

    清理是纯副作用，它失败没有任何理由让主流程失败，所以这里连 SystemExit
    一起吞。**只用于「删临时文件」这一个动作**，不要拿它包业务逻辑。
    """
    try:
        p.unlink(missing_ok=True)
    except (Exception, SystemExit):  # noqa: B014  SystemExit 也吞，见上
        pass


def _safe_close(f) -> None:
    """同上：关 stderr/管道这类清理动作，失败不得冒泡。"""
    try:
        if f is not None and not getattr(f, "closed", False):
            f.close()
    except (Exception, SystemExit):  # noqa: B014
        pass


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
        # 清理失败不得打断主流程（见 _safe_unlink 的实测事故）
        _safe_unlink(list_file)
        _safe_unlink(tmp)


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


def _ffmpeg_version() -> str:
    """ffmpeg 版本串，进段指纹。

    为什么必须进指纹：编解码器行为随 ffmpeg 版本变（默认封装、色彩空间转换、
    码率控制的实现差异）。2026-09-26 本机 ffmpeg 从 8.x 换到 9.0.2，若指纹不含
    版本，升级后重跑会**复用旧版本编的段** —— 同一成片里混着两种编码器输出，
    而 verify 只数帧数，看不出来。v2 链路早就有这个字段（production_v2._render
    的 runtime['ffmpeg']），legacy 这条路是漏的。
    """
    try:
        out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True,
                             timeout=15)
        return (out.stdout or out.stderr or "").splitlines()[0].strip()
    except Exception:
        return "unavailable"


def _layer_sha(name: str) -> str | None:
    """Source hash of an overlay-layer module, or None if it is absent.

    A new layer that does not exist yet must not change the fingerprint -- that
    would invalidate every segment for a file nobody added.
    """
    import importlib.util
    spec = importlib.util.find_spec(f'app.services.{name}')
    if spec is None or not spec.origin:
        return None
    p = Path(spec.origin)
    return file_sha256(p) if p.is_file() else None


def _seg_params_fingerprint(timeline, vcodec, mode: str, segs,
                            transition_frames: int = 0) -> str:
    """成片内容指纹（G 批）：一切「改了就该重编」的参数。

    断点续跑的跳过判据此前只有「段文件存在 + 帧数 == 预期」——只要改的东西
    不改变帧数（字幕样式、grade 调色、cq/preset、编码器、ffmpeg 版本），所有段
    就全部命中跳过，成片纹丝不动。最阴的一种：NVENC 中途降级 libx264 之后重跑，
    前 N 段仍命中跳过 → 硬件段 + 软件段混合编码被永久固化。
    """
    shots = []
    for sh, _s0, _s1 in timeline:
        m = getattr(sh, "motion", None)
        shots.append({
            "id": getattr(sh, "id", ""),
            "dur": getattr(sh, "duration_ms", 0),
            "img": file_sha256(sh.image_path) if getattr(sh,"image_path",None) and Path(sh.image_path).is_file() else None,
            "up": file_sha256(sh.upscaled_path) if getattr(sh,"upscaled_path",None) and Path(sh.upscaled_path).is_file() else None,
            "sub": getattr(sh, "subtitle_text", "") or "",
            "grade": getattr(m, "grade", None) if m else None,
            "motion": str(m) if m else "",   # pan/zoom/ease 等整份运镜 spec
        })
    payload = {
        "renderer_version": "legacy-render-v4",
        # 叠加层的**源码哈希**必须进指纹。此前只有硬编码常量 renderer_version,
        # 改 overlay.py 的绘制逻辑不会改变指纹：加了 overlay.json 会重编一次（但
        # 那一版有 bug 什么都没画），之后无论怎么修代码，段都被判为"像素未变"直接
        # 复用 —— 修复永远到不了成片。transition_frames / 标点集合进指纹是同一个道理：
        # 凡是改像素的东西都要能被察觉。沿用 forced_alignment 的 adapter_sha256 做法。
        "layer_src": {name: _layer_sha(name) for name in
                      ('overlay', 'word_timing')},
        "ffmpeg": _ffmpeg_version(),
        "transition_frames": int(transition_frames or 0),
        "grade_values": __import__("app.services.motion",fromlist=["GRADE_MAP"]).GRADE_MAP,
        "mode": mode,                          # single / segmented
        "layout": [len(sl) for sl in segs],    # 短片与长片都写 seg-000，必须区分
        "vcodec": list(vcodec),
        "spec": [W, H, FPS, HEAD_MS, TAIL_MS],
        "subtitle": [file_sha256(_sub_font_path()) if Path(_sub_font_path()).is_file() else None, _sub_font_size(), _sub_bottom(),
                     _sub_stroke(), _sub_strip_punct(), _sub_space_punct()],
        "shots": shots,
    }
    # 叠加层按**时间窗**过滤后入指纹：片头加一张标题卡只会作废真正包含它的那一段，
    # 而不是全部 11 段。无叠加层时整键缺席 —— 这样「本次改动前的成片」指纹不变，
    # 已验收四集的旧段不会被无谓重渲。
    _ov = overlay_plan()
    if _ov and timeline:
        _spans = [(s0, s1) for _sh, s0, s1 in timeline]
        _win = (min(a for a, _b in _spans), max(b for _a, b in _spans))
        _fp = _ov.fingerprint(_win)
        if any(s.start_ms < _win[1] and s.end_ms > _win[0] for s in _ov.specs):
            payload["overlays"] = _fp
    # 逐词高亮同样按窗口过滤：highlight 只在 cue 自己的跨度内改变像素。
    _wt = word_plan()
    if _wt and timeline:
        _win = (min(a for _sh, a, _b in timeline), max(b for _sh, _a, b in timeline))
        _hit = [k for k, ws in _wt.words.items() if ws and ws[-1].ms_end]
        if _hit:
            payload["karaoke"] = _wt.fingerprint(_win)
    return hashlib.md5(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        .encode("utf-8")).hexdigest()[:16]


def _sync_segment_params(seg_dir: Path, timeline, vcodec, mode: str, segs,
                         transition_frames: int = 0) -> None:
    """参数指纹落盘；与上次不一致就作废所有旧段，强迫重编。

    指纹里必须含 vcodec：内容没变、只有编码器变了（NVENC 降级）时，若前 N 段
    沿用旧段，成片就是硬件+软件混合编码。ffmpeg 版本同理（换版本重跑不得复用
    旧段）。transition_frames 也必须进：它改的是像素，改完还命中旧段就等于没改。
    """
    fp = _seg_params_fingerprint(timeline, vcodec, mode, segs, transition_frames)
    pf = seg_dir / "params.json"
    try: old=json.loads(pf.read_text(encoding="utf-8"))
    except (OSError,ValueError): old={}
    segment_fps={f"seg-{i:03d}.mp4":_seg_params_fingerprint(sl,vcodec,"segment",[sl],transition_frames) for i,sl in enumerate(segs)}
    if old.get("fp")==fp and old.get("segments")==segment_fps: return
    for p in seg_dir.glob("seg-*.mp4"):
        if old.get("mode")!=mode or old.get("segments",{}).get(p.name)!=segment_fps.get(p.name):
            p.unlink(missing_ok=True)
    atomic_json(pf,{"fp":fp,"mode":mode,"vcodec":list(vcodec),"segments":segment_fps})


def _run_audio_mux(video_src: Path, audio_src: Path, out_path: Path,
                   audio_codec: str, audio_bitrate: str,
                   timeout_sec: int = 1200) -> None:
    """一次 mux：视频 copy + 音频按给定编码（module-level 版本，便于测试）。

    与 compose_episode 内的内嵌 _mux_full_audio 行为完全一致。
    视频 `-c:v copy`（零重编），音频按给定编码（默认 aac/192k）——
    **严禁 `-c copy` 直拷 wav**：pcm_s16le 装入 mp4 大量播放器静音
    （2026-09-05 lsm-15min P0-1 实测：码流有 pcm 音轨、波形正常，但播放端静音）。

    用 .tmp 原子收口：失败时 .tmp 不留，成功才 os.replace 成真名。
    """
    tmp = out_path.with_suffix(".tmp")
    mux = [
        "ffmpeg", "-y", "-v", "error",
        "-i", str(video_src), "-i", str(audio_src),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", audio_codec, "-b:a", audio_bitrate,
        # 不用 -shortest：旁白与视频帧数由 timeline 精确对齐（见历史注释），
        # -shortest 会在低内存下缓冲波动时提前截断成片。
        "-movflags", "+faststart",
        "-f", "mp4", str(tmp),
    ]
    # Python 语义：subprocess.run(timeout=0) 立即 TimeoutExpired；按 cfg 注释
    # 「0 = 不设超时」的约定，0 翻译成 None（不设超时），其它值原样传。
    r = subprocess.run(mux, capture_output=True, text=True, timeout=timeout_sec or None)
    if r.returncode != 0:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise RuntimeError(f"音画 mux 失败: {r.stderr[-800:]}")
    os.replace(tmp, out_path)


def build_timeline(shots, frames, disk_ms=None, head_of=None, tail_of=None):
    """Compose the render timeline: ``[(shot, audio_start_ms, audio_end_ms)]`` + total_ms.

    Extracted verbatim out of ``compose_episode`` so tooling that needs absolute
    timeline positions (act-card placement, the opening-audit strip) reads the
    *same* numbers as the renderer instead of re-deriving them. A second
    implementation of this arithmetic is a second thing to keep in sync, and a
    drift between them would silently misplace overlays.

    Mutates ``shot.audio_start_ms`` / ``duration_ms`` / ``audio_end_ms`` — the
    timing gate and the audio mux read those fields back, so this is not a pure
    function by design.

    ``head_of`` / ``tail_of`` / ``disk_ms`` are optional inputs; omitting them
    recomputes them from ``shots``. ``compose_episode`` passes its own so the
    wavs are read once.
    """
    if head_of is None:
        head_of = {}
        prev_g = None
        for sh in shots:
            g = _group_of(sh.frame_id)
            head_of[sh.id] = HEAD_MS if (prev_g is not None and g != prev_g) else 0
            prev_g = g
    if tail_of is None:
        tail_of = {}
        for i, sh in enumerate(shots):
            nxt = shots[i + 1] if i + 1 < len(shots) else None
            tail_of[sh.id] = TAIL_MS if (nxt is None or
                                         _group_of(nxt.frame_id) != _group_of(sh.frame_id)) else 0
    if disk_ms is None:
        disk_ms = {}
        for sh in shots:
            f = frames.get(sh.frame_id)
            if f and f.audio_path and Path(f.audio_path).exists():
                disk_ms[sh.id] = _disk_wav_duration_ms(Path(f.audio_path))

    timeline, t = [], 0
    for sh in shots:
        a_dur = disk_ms.get(sh.id)
        if a_dur is None:                      # 无 shots 的空集路径，保持旧口径
            a_dur = frames[sh.frame_id].audio_duration_ms or 0
        sh.audio_start_ms = head_of[sh.id]
        sh.duration_ms = sh.audio_start_ms + a_dur + tail_of[sh.id]
        sh.audio_end_ms = sh.audio_start_ms + a_dur
        s0 = t + sh.audio_start_ms
        timeline.append((sh, s0, s0 + a_dur))
        t += sh.duration_ms
    return timeline, t


def compose_episode(ep_id: str, plan_frames=None, render=None,
                    run_timing_gate: bool = True) -> Path:
    """
    合成整集成片。返回最终 mp4 路径。
    plan_frames / render 允许注入（默认从 motion 导入），便于测试。

    run_timing_gate=True 时，合成前（时间轴重算之后、编码之前）跑 §5.4 时序断言
    （短镜 / 超长镜 / 音频对齐统一前置拦截），任一项失败即 raise TimingError，不出片。
    这是对『orchestrator 路径』与『compose 直通入口』的统一门禁——
    修复 2026-09-03 审计 #A：此前直通 compose 从不调断言，短镜能悄悄出片。
    """
    from app.managers import load
    from app.services.timing_assert import assert_timing_or_raise
    from app.services.motion import plan_frames as _pf, render as _render
    plan_frames = plan_frames or _pf
    render = render or _render

    ep = load(ep_id)
    base = episode_dir(ep_id)
    build = base / "build"
    out_dir = base / "out"
    build.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 字幕字体自检（整改 B4）：字体是机器特定的绝对路径，换机/换盘/系统精简版
    # 都可能没有。此前只在「逐帧烧字幕」时才抛 —— 那时可能已经跑了几十分钟的
    # 渲染与编码，失败成本极高。这里在合成入口先试载一次，把晚失败变早失败。
    check_font_available()

    # 叠加层计划在此载入：与字体自检同理，早失败远好于渲了几十分钟才发现
    # overlays.json 写坏。段指纹（下文 _sync_segment_params）与逐帧编码都读这一份。
    overlay_plan(ep_id)
    word_plan(ep_id)

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

    # ---------- 1.05) 镜头时长一律以**磁盘 wav 的真实采样数**为准 ----------
    # 旧实现用 json 的 frame.audio_duration_ms 算镜头长度，却把**真实 wav** 放到
    # 音频轨（见下方 chunks 拼接）。两者一旦不同步（TTS 重跑后 json 过时、
    # 手工换过 wav），每镜的差值会逐镜累积成音画漂移 —— 197 镜各差 20ms 就是
    # 约 4s，而 _WAV_TOLERANCE_MS 允许单镜差到 120ms（累积上界 23.6s），
    # verify 阶段又只数总帧数，压根看不出来。
    #
    # 现在反过来：**磁盘是时序的唯一真源**，json 值降级为「是否过时」的交叉
    # 校验（_verify_audio_vs_disk，超容差即报错）。漂移因此不再是「被容差
    # 限制的风险」，而是结构上不可能发生 —— 两边算的是同一个 wav。
    # 代价是多读一遍 wav 头（197 次 open，纯 IO，毫秒级），换掉一整类静默故障。
    disk_ms: dict[str, int] = {}
    if ep.shots:
        for sh in shots:
            f = frames.get(sh.frame_id)
            if not f or not f.audio_path:
                raise RuntimeError(
                    f"shot {sh.id} 的 frame {sh.frame_id} 无 audio_path，无法校验音频")
            p = Path(f.audio_path)
            if not p.exists():
                raise RuntimeError(f"frame {f.id} 音频缺失: {f.audio_path}（需先跑 TTS）")
            disk_ms[sh.id] = _disk_wav_duration_ms(p)
        _verify_audio_vs_disk(shots, frames)

    timeline, t = build_timeline(shots, frames, disk_ms, head_of, tail_of)
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
    # 重算却出 ~250ms 闪镜静默绕门。现改为：对【重算后的内存 shot 值】跑 §5.4 门禁
    # （此时 sh.* 已被本函数按真实音频回填，gate 看到的就是合成将用的口径）。
    # 任一失败 raise，绝不静默出片。
    # _verify_audio_vs_disk 已上移到 1.05（必须早于时序构造：它校验的正是时序
    # 所依据的那个 wav），此处不再重复调用。
    if ep.shots and run_timing_gate:
        assert_timing_or_raise(ep)   # 重算后：失败即抛 TimingError，不出半成品

    # ---------- 2) 拼旁白 ----------
    # 采样率必须逐帧校验（整改 B3）：此前 `sr` 被循环里最后一次 _read_wav 的返回
    # 值覆盖，且**静音段长度也用这个被覆盖的 sr 去算**。若混入一个不同采样率的
    # wav（手动补录、别的引擎导出），拼接处的静音时长会按错误速率算，音频整体
    # 变速/变调，而代码一行不报——靠「voice 统一转 16k」的约定才一直没出事。
    # 约定要变成断言：出错即停，而不是静默产出一部变了声的片子。
    chunks = []
    promised_samples = 0
    for sh, s0, s1 in timeline:
        f = frames[sh.frame_id]
        data, sr = _read_wav(Path(f.audio_path))
        if sr != NARRATION_SR:
            raise RuntimeError(
                f"{sh.frame_id} 的音频采样率 {sr}Hz ≠ 约定值 {NARRATION_SR}Hz"
                f"（{f.audio_path}）：混采样率拼接会导致成片变速变调。"
                f"请用 voice 的转码口径重新导出该段 wav。")
        head_n = int(NARRATION_SR * sh.audio_start_ms / 1000)
        tail_n = int(NARRATION_SR * tail_of[sh.id] / 1000)
        # 本镜的「整毫秒取整残差」补进尾部静音。
        #
        # _disk_wav_duration_ms 把时长截断成整数毫秒（丢 <1ms = <16 采样），而音频
        # chunk 用的是**精确**采样数。两者不补的话，每镜欠下最多 15 个采样，197 镜
        # 累积约 800 采样（50ms）—— 表现为整片音画缓慢错开，且总量对不上。
        # 补进本镜尾部后，下一镜的音频起点就精确落在视频期望的位置，误差不累积；
        # 镜内残留 <1ms，听感不可闻。
        a_ms = sh.duration_ms - sh.audio_start_ms - tail_of[sh.id]
        residual = len(data) - (a_ms * NARRATION_SR // 1000)
        chunks.append(np.zeros(head_n, dtype=np.int16))
        chunks.append(data)
        chunks.append(np.zeros(tail_n + residual, dtype=np.int16))
        promised_samples += head_n + len(data) + tail_n + residual
    nwav = build / "narration.wav"
    narration = np.concatenate(chunks)
    with wave.open(str(nwav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(NARRATION_SR)
        w.writeframes(narration.tobytes())

    # ---------- 2.1) 音轨对位硬断言 ----------
    # narration 的实际长度必须**精确等于**上面按镜累加出来的承诺值。不等就意味着
    # 「视频按 A 算长度、音频按 B 摆位置」。allowed 是按镜累加的分量算的，所以
    # 这里容差为 0 —— 任何偏差都是真 bug（某镜 wav 被改写、混了不同采样率、
    # 或上面的残差补偿被改坏），而不是取整误差。
    allowed = len(timeline)          # 每镜至多 1 采样的 float→int 抖动
    drift = abs(len(narration) - promised_samples)
    if drift > allowed:
        raise RuntimeError(
            f"旁白实际 {len(narration)} 采样 ≠ 逐镜累加承诺 {promised_samples} 采样"
            f"（差 {drift} > 容差 {allowed}，{len(timeline)} 镜）。"
            f"音画会整体错位，拒绝出片。\n"
            f"  常见原因：某镜 wav 在读取期间被改写、混入了不同采样率的片段，"
            f"或尾部残差补偿被改坏。")
    if drift:
        print(f"  [compose] 音轨对位残差 {drift} 采样（{drift/NARRATION_SR*1000:.1f}ms，"
              f"容差 {allowed}）—— 整毫秒取整所致，不累积")

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
    published = out_dir / f"{ep_id}_m1.mp4"
    import uuid
    final = out_dir / f"{ep_id}.{uuid.uuid4().hex}.candidate.mp4"

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
        实行为抽出的 module-level `_run_audio_mux`——这里是它的薄壳，闭包持有
        audio_input/_ac/_ab/timeout，让 compose_episode 主体不漏参数。
        """
        _run_audio_mux(video_src, audio_input, out_path, _ac, _ab, _timeout)

    # ---------- 段划分 + 参数指纹 ----------
    # 分段方案先定：短片整片一段、长片按镜头边界切。指纹里必须带上分段布局——
    # 短片与长片都写 seg-000.mp4，只按内容 hash 的话，短片切长片会把「整片」
    # 当成「长片的第 0 段」直接跳过（P2-10）。
    if n_total_all < _compose_cfg.segment_min_frames:
        segs, mode = [timeline], "single"
    else:
        segs, mode = _segments_of(timeline, _compose_cfg.segment_max_sec), "segmented"

    # 换图处的黑场过渡。只改亮度、不加帧/不加采样，所以总帧数与音频时钟不变 ——
    # 分段边界、断点续跑、expected_frames、verify 全部不受影响。
    # 必须排在 _sync_segment_params 之前：过渡帧数进了段指纹。
    _fade = int(getattr(_compose_cfg, "image_transition_frames", 0) or 0)
    _trans = _image_transitions(
        timeline, {sh.id: expected_frames([(sh, 0, 0)]) for sh, _, _ in timeline}, _fade)

    seg_dir = build / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    _sync_segment_params(seg_dir, timeline, vcodec, mode, segs, _fade)

    if _trans:
        print(f"  [compose] 换图黑场过渡：{len(_trans)} 镜首尾淡入淡出（各 ≤{_fade} 帧，"
              f"不加帧不动音频）")

    if mode == "single":
        # 短片：单段直出（纯视频无音轨），再统一 mux 音轨。
        # 保持历史可读性且不回归：产物等价（video+h264 + audio+aac +faststart）。
        vid = seg_dir / "seg-000.mp4"
        if not (vid.exists() and _probe_segment_frames(vid) == n_total_all):
                _encode_video_span(timeline, upscaled, vid, vcodec, _timeout,
                                   plan_frames=plan_frames, transitions=_trans)
        _mux_full_audio(vid, final)
    else:
        # 长片：按镜头边界逐段编码（可断点续跑），concat copy + 一次混音
        seg_paths: list[Path] = []
        rendered_any = False
        for i, sl in enumerate(segs):
            exp = expected_frames(sl)
            p = seg_dir / f"seg-{i:03d}.mp4"
            # 断点续跑：段已存在且帧数 = 预期 → 跳过（用逐镜求和口径，11/15 段能真命中）
            # 「参数变了但帧数没变」的作废已在 _sync_segment_params 统一处理。
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
                                   plan_frames=plan_frames, transitions=_trans)
                rendered_any = True
            seg_paths.append(p)
        # concat 无音轨视频
        _concat_segments(seg_paths, build / "concat.video.mp4")
        # Never put a silent intermediate at the published path.
        _mux_full_audio(build / "concat.video.mp4", final)

    # 成片已写出：读**真实**帧数，而不是预期值。
    #   此前 `n_actual = n_total_all` 把「打算编多少帧」当成「实际编了多少帧」
    #   打印出来，排错时会被误导；且 assert_timing_or_raise 合成后不传
    #   video_total_frames，第 5 项断言只在 verify 阶段跑 —— 直通 compose
    #   的出片帧数漂移 0 拦截（F 批：让漂移在合成时就炸，而不是等 verify）。
    n_actual = _probe_segment_frames(final)
    if n_actual <= 0:
        raise RuntimeError(
            f"成片读不出帧数（{final}）——文件可能损坏或未完整写出。"
            f"清理 build/ 与 out/ 后重跑 compose")
    assert_timing_or_raise(ep, video_total_frames=n_actual)

    from app.services.media_integrity import verify_media
    verify_media(final, expected_frames=n_actual)
    save(ep)
    os.replace(final, published)
    final = published
    print(f"成片: {final}  {final.stat().st_size/1024/1024:.1f}MB  "
          f"{total_ms/1000:.2f}s  {n_actual}帧")
    return final


if __name__ == "__main__":
    ep_id = sys.argv[1] if len(sys.argv) > 1 else "hw-001"
    compose_episode(ep_id)
