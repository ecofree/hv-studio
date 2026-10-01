"""
字幕字面完整性门禁（P0-1，2026-09-29 caomin-01 / lishimin-04 实测事故）

事故
----
`split_script` 的 docstring 写明了两条约定：

    frame.text           = 原文片段      → 字幕显示
    frame.narration_text = polyphone 替换后 → TTS（多音字读音修正）

    "切分必须在【原文】上做，polyphone 替换只作用于 narration_text。
     否则「匈奴单于」会被替换成「匈奴缠于」并直接烧进字幕。"

制作 caomin-01 / lishimin-04 时这两句都读了，但脚本取了
`f.narration_text` 并同时写进 `text` 与 `narration_text`。两集都用**外部
音频、根本没有 TTS**，于是 polyphone 修正纯属多余，却直接烧进了字幕：

    caomin-01   天可汗  → 天克寒   (2 处)
    lishimin-04 高句丽  → 高勾离   (1 处)，颉利可汗 同理

为什么没有任何门禁拦住：镜长预检、时序断言 #1–#5、`stage_verify` 全部只
校验时长与帧数，**没有一项读字幕字面**。错字一路带着 verify ok 通过，交付
后靠人工抽帧才发现。

本模块提供缺失的那一环
----------------------
1. `assert_subtitle_fidelity(ep)`：字幕文本必须与剧本原文逐字一致，且不得
   含有 polyphone 规则的「替换后」形态。两个方向都查：
     - 正向：帧文本 == strip_script_markdown 后的剧本正文
     - 反向：帧文本中不得出现任何 polyphone 规则的 dst（当 src 也在文中时）
2. `polyphone_leaks(text)`：返回被 polyphone 污染的位置，供诊断使用。

约定
----
- 纯函数、无 I/O 副作用，可直接单测。
- 不改 polyphone.yaml 本身：这些替换对 **TTS 是对的**（可汗 hàn→hán 是
  为了让引擎读准），错的只是它们不该出现在字幕上。
"""
from __future__ import annotations

import re

from app.models import Episode

# 剧本正文清洗：与 script.split_script 内部同一条路径，保证同口径。
def _narration_source(ep: Episode) -> str:
    from app.orchestrator import _load_script_md
    from app.services.script import strip_script_markdown
    return strip_script_markdown(_load_script_md(ep))


def _visible(text: str) -> str:
    """字幕实际显示的字符：去空白。用于逐字比较时忽略排版差异。"""
    return "".join(ch for ch in (text or "") if not ch.isspace())


def polyphone_leaks(text: str) -> list[dict]:
    """列出 `text` 中命中 polyphone 规则的「替换后」形态。

    返回 [{src, dst, at}]，按出现位置排序。仅报告 src != dst 的规则
    （同形规则无需提示）。
    """
    from app.services import polyphone

    # rules() 返回副本，不触发任何命中计数。**不要在这里调 preprocess()**：
    # 它会计入 _HITS_COUNTER 并写缓冲日志，把「校验」变成「产生副作用」，
    # 一次门禁检查就能把 state/polyphone_hits.log 写脏，并让
    # test_hits_are_buffered_until_flush 这类计数测试随机变红
    # （2026-09-29 实测：全套 shared 跑下来该用例偶发失败）。
    out: list[dict] = []
    for src, dst in polyphone.rules().items():
        if not src or src == dst:
            continue
        for m in re.finditer(re.escape(dst), text or ""):
            # 原文本身就写着 dst（作者本意）不算污染：只有 src 不在文中才是。
            if src not in (text or ""):
                out.append(dict(src=src, dst=dst, at=m.start()))
    out.sort(key=lambda r: r["at"])
    return out


def frame_text_report(ep: Episode) -> dict:
    """逐帧报告字幕文本相对剧本原文的偏差（不抛异常）。"""
    # 只取剧本正文，**不再调 split_script**：
    #   1. 判定只需「帧文本 vs 原文」，切分结果不参与（帧数本来就不要求相等）；
    #   2. split_script 内部对每帧调 preprocess(log_hits=True)，会给 polyphone
    #      的全局命中计数器加噪。全套测试里它跑在 test_polyphone 之前，导致
    #      test_hits_are_buffered_until_flush 的计数断言偶发失败——一个**只读
    #      校验**函数绝不该有副作用（2026-09-29 实测）。
    src = _visible(_narration_source(ep))
    if not src:
        # 没有可比对的正文（合成/smoke episode 只有 frames 没有 script.md）。
        # 与 assert_subtitle_fidelity 同一个判据：缺少基线不是字面缺陷。
        return dict(ok=True, applicable=False, reason='无剧本正文可比对',
                    frames=len(ep.frames), source_chars=0, fails=[],
                    warns=[], leaks=[], drift=[])
    actual = [f.text or "" for f in ep.frames]

    # 帧数不做硬性要求：短帧合并、空帧剔除、幕间空镜拆帧都会合法地改变帧数
    # （caomin-01 242→238、lishimin-04 196→195 都是这么来的）。真正的不变
    # 量是「帧文本拼接 == 剧本正文」，与怎么切无关。
    fails: list[str] = []
    leaks: list[dict] = []
    for i, f in enumerate(ep.frames):
        text = f.text or ""
        for hit in polyphone_leaks(text):
            leaks.append(dict(frame=f.id, **hit))
    if leaks:
        seen = set()
        for hit in leaks:
            key = (hit["src"], hit["dst"])
            if key in seen:
                continue
            seen.add(key)
            frames_with = [h["frame"] for h in leaks if (h["src"], h["dst"]) == key]
            fails.append(
                f"{hit['src']} 被写成 {hit['dst']}（polyphone 替换后形态）于 "
                f"{len(frames_with)} 帧，例：{frames_with[0]}"
                "；字幕须用 frame.text（原文），polyphone 修正只送 TTS")

    # 全文逐字比对：把实际帧文本拼起来与剧本正文比，容忍切分边界差异。
    # 用相似度分档：低于 DRIFT_WARN_RATIO 一律失败（真事故：漏字/错字）；
    # 高于它只记为 warn（可解释的历史改动：调序、删无旁白帧）。否则 lsm-15min
    # 这类长期噪声会把门禁变成只会误报的红灯。
    actual_all = "".join(_visible(t) for t in actual)
    drift: list[dict] = []
    warns: list[str] = []
    if actual_all != src:
        import difflib
        sm = difflib.SequenceMatcher(None, src, actual_all, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag != "equal":
                drift.append(dict(tag=tag, source=src[i1:i2][:40],
                                  frames=actual_all[j1:j2][:40]))
        head = (f"帧文本拼接与剧本正文不一致（相似度 {sm.ratio()*100:.2f}%，"
                f"{len(drift)} 处）")
        for d in drift[:5]:
            head += f"\n      · {d['tag']}: 剧本「{d['source']}」 vs 帧「{d['frames']}」"
        (fails if sm.ratio() < DRIFT_WARN_RATIO else warns).append(head)
    return dict(ok=not fails, fails=fails, warns=warns, leaks=leaks, drift=drift,
                frames=len(ep.frames), source_chars=len(src))


# 逐字比对下「差异属正常」的上限。低于它一律失败；高于它只作提示，因为
# 历史集里存在**可解释**的正文改动：
#   lsm-15min  97.86% —— 14 处段落顺序被调整过（同一批句子挪了位置），
#                       字数几乎不变，属重排而非漏字。
#   lishimin-duori 98.63% —— 4 帧无旁白被剔除（用户确认删除），
#                       但另有 7 处 polyphone 泄漏，那部分由 leaks 独立拦截。
# 门槛定在这里而不是 100%，是为了让门禁拦「错字/漏字」这类真事故，
# 而不被「人工调过顺序」的历史集长期噪声淹没。
DRIFT_WARN_RATIO = 0.98


def assert_subtitle_fidelity(ep: Episode) -> dict:
    """字幕字面门禁。失败即 raise —— 错字绝不能带着 verify ok 交付。

    放在花钱前预检的同一位置（stage_images 之前）与 verify 之后各跑一次：
    前者防止错字集继续花钱，后者防止成片带错字交付。

    没有可比对的剧本正文时**直接放行**（报告 not_applicable）。合成测试用的
    合成 episode 往往只有 frames 没有 script.md，那不是字面缺陷；把「缺少
    基线」当成缺陷会让 verify 在这些集上一律失败（2026-09-29 实测）。
    """
    from app.services.timing_assert import TimingError

    # 没有可比对的剧本正文时**直接放行**（报告 not_applicable）。合成测试用的
    # episode 往往只有 frames 没有 script.md（conftest.build_episode_dict 就不
    # 写 script.md），那不是字面缺陷；把「缺少基线」当缺陷会让 verify 在这些
    # 集上一律失败。校验 _load_script_md 而不是 _narration_source：后者是
    # strip_script_markdown 的输出，剥离后可能变成空串，两者的「有基线」判据
    # 必须一致（2026-09-29 实测：两者对空剧本都返回假，但顺序不同易误判）。
    from app.orchestrator import _load_script_md
    if not ep.frames or not _load_script_md(ep).strip():
        return dict(ok=True, applicable=False, reason='无剧本正文可比对',
                    frames=len(ep.frames), fails=[], leaks=[], drift=[],
                    warns=[])

    report = frame_text_report(ep)
    if report.get("applicable") is False:
        return report
    if not report["ok"]:
        raise TimingError(
            "字幕字面校验失败（字幕与剧本原文不一致）:\n  - "
            + "\n  - ".join(report["fails"])
            + "\n  处置：frame.text 必须是剧本【原文】，只能进字幕；"
              "polyphone 修正只作用于 frame.narration_text（送 TTS）。"
              "外部音频集没有 TTS，两个字段应保持同一份原文。"
              "改法见 app/services/script.py:split_script 的 docstring。")
    report["applicable"] = True
    return report
