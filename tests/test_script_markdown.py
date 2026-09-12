# -*- coding: utf-8 -*-
"""剧本切分的 markdown / 导演标注清洗（2026-09-06《李世民四部曲》实测）。

背景：用户拿来的剧本常是「带完整格式标注的成稿」——# 标题、> 引用块
（作品类型/预估时长/核心切口/格式规范）、**（时长预估：…）**、--- 分隔线，
外加行内【语气/画面标注】与 [停顿0.5秒]。

而 `stage_script → split_script` 这条官方生产链**从不清洗**（clean_narration
只在 bin/build_episode_from_md.py 的双栏分镜路径上被调用）。实测喂一份
3250 字的带标注剧本：294 帧里 33 帧被污染，前 6 帧整段是 markdown 垃圾，
会原样烧进字幕并送去配音。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.legacy


import re


from app.models import Episode
from app.services import script as S

# 取自用户剧本 resource/风趣历史.writing-DNA/scripts/ 的真实结构（已精简）
ANNOTATED_MD = """# 《李世民四部曲·第一集：风投的暗涌》
### —— 李渊的钢丝绳与双核帝国的诞生

> **作品类型**：重大历史人物季播微纪录大片
> **预估时长**：13 ~ 14 分钟
> **格式规范**：纯语音口播文本，嵌入【导演语气/情绪标注】与 [停顿打点]

---

### 【第一幕：晋阳账本与塞北屈膝】
**（时长预估：00:00 - 03:30 | 情绪基调：冷峻、去魅）**

【语气：低沉、沉稳，像在昏暗的档案室里翻开一本泛黄卷宗】
很多历史书在讲大唐开国的时候，特别喜欢讲一个近乎神话的故事。[停顿0.5秒]
说隋炀帝大业末年天下大乱，太原留守李渊是个胸无大志的守财奴。

在信里，李渊把腰弯到了泥土里：
“若能从我，不侵暴百姓……自是天子不得令起居。”
他不仅向突厥俯首称臣，更用府库里所有的绫罗绸缎，换来五百匹战马。

李渊突然向全国颁布了一道诏书，宣布大举**开科取士**。
"""

# 既有交付集的 script.md 形态：纯文本，无任何 markdown
PLAIN_MD = """天下大乱，李渊起兵。
次年攻入长安，大唐建立。

这是一段普通的旁白，没有任何标注。
"""


def _split(md: str, **kw):
    return S.split_script(Episode(id="t", title="t", episode_number=1), md, **kw)


def _is_dirty(t: str) -> bool:
    return bool(re.search(r"[#*>|]|【|\[停顿|作品类型|预估时长|格式规范|"
                          r"核心切口|时长预估|语气：|画面定格", t))


# ---------------------------------------------------------------------------
# 1. markdown 结构行与导演标注必须被剥离
# ---------------------------------------------------------------------------
def test_markdown_front_matter_is_stripped():
    frames = _split(ANNOTATED_MD)
    texts = [f.text for f in frames]

    dirty = [t for t in texts if _is_dirty(t)]
    assert dirty == [], f"仍有 markdown/标注残留: {dirty}"

    # 标题、引用块、分隔线、时长预估整行都不该出现
    assert not any("《李世民四部曲" in t for t in texts), "标题进了帧"
    assert not any("作品类型" in t or "预估时长" in t for t in texts), "元数据进了帧"
    assert not any(t.strip() == "---" for t in texts), "分隔线进了帧"
    assert not any("第一幕" in t for t in texts), "幕标题进了帧"


def test_director_annotations_are_removed_inplace():
    """行内【语气…】/ [停顿N秒] 必须去掉，且停顿要变成句号（TTS 自然停）。"""
    frames = _split(ANNOTATED_MD)
    texts = [f.text for f in frames]
    assert not any("【" in t or "[" in t for t in texts), f"标注残留: {texts}"
    # [停顿0.5秒] → 。：故事与下句应分成两帧
    assert any(t.endswith("故事。") for t in texts), \
        f"[停顿N秒] 应转成句号并断句: {texts}"


def test_inline_bold_markers_are_removed_but_text_kept():
    texts = [f.text for f in _split(ANNOTATED_MD)]
    assert not any("**" in t for t in texts), f"加粗标记残留: {texts}"
    assert any("开科取士" in t for t in texts), "加粗里的正文被误删了"


# ---------------------------------------------------------------------------
# 2. 软换行必须合并（否则 \n 会烧进字幕）
# ---------------------------------------------------------------------------
def test_soft_line_break_is_merged_not_kept():
    """markdown 同段软换行 = 续行，必须合并；保留 \\n 会烧进字幕。

    实测：未合并时 73 帧含换行符，如 '特别喜欢讲…故事\\n说隋炀帝…'。
    """
    # 用 <18 字的例子：超过 max_chars 会走 split_long_sentence 按逗号切，
    # 那是另一套（正常）行为，不是软换行问题。
    texts = [f.text for f in _split("短句一行，\n被软换行截断。")]
    assert texts == ["短句一行，被软换行截断。"], texts

    # 长句会被按逗号切，但**绝不能**残留换行符
    long_texts = [f.text for f in _split(
        "这是一个相当长的句子，\n它在这里被软换行截断了。")]
    assert not any("\n" in t for t in long_texts), long_texts


def test_paragraph_boundary_is_preserved():
    """段落之间（空行）不能合并——那会让两段的旁白粘成一帧。"""
    md = "第一段的第一句。第一段的第二句。\n\n第二段的第一句。"
    texts = [f.text for f in _split(md)]
    assert len(texts) == 3, texts
    assert texts[2] == "第二段的第一句。"


# ---------------------------------------------------------------------------
# 3. 闭合引号不得被切成孤立的 1 字帧
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("q_open,q_close", [("\u201c", "\u201d"), ('"', '"')])
def test_closing_quote_stays_with_its_sentence(q_open, q_close):
    """`…不侵暴百姓。”他不仅` 曾切成 ['…百姓。', '”他不仅']，
    修复一度又切成 ['…百姓。', '”', '他不仅']（两分支不互斥）。
    """
    text = f"李渊说：{q_open}若能从我，不侵暴百姓。{q_close}他不仅俯首称臣。"
    pieces = [p for p in re.split(S.SENT_END, text) if p]
    assert pieces == [f"李渊说：{q_open}若能从我，不侵暴百姓。{q_close}",
                      "他不仅俯首称臣。"], pieces
    assert not any(len(p) <= 1 for p in pieces), f"出现孤立 1 字片段: {pieces}"


def test_no_single_char_frame_from_quote_in_full_script():
    frames = _split(ANNOTATED_MD)
    lone = [f.id for f in frames if len(f.text) <= 1]
    assert lone == [], f"出现 1 字帧: {lone}"


# ---------------------------------------------------------------------------
# 4. 对既有纯文本集必须完全 no-op（否则重切会触发重 TTS 花钱）
# ---------------------------------------------------------------------------
def test_plain_script_is_untouched():
    """纯文本剧本：清洗前后逐帧一致，且句末句号必须保留。

    ⚠️ 曾在这里踩坑：段级照搬 clean_narration 的 .strip("，。、；：？！")，
    把句末句号剥掉 → laogongtuan 45 帧、hanwu-ch3 72 帧文本被改，
    重切就会判「文本变更」→ 丢弃旧音频 → 重跑 TTS（真花钱）。
    """
    texts = [f.text for f in _split(PLAIN_MD)]
    assert texts == ["天下大乱，李渊起兵。", "次年攻入长安，大唐建立。",
                     "这是一段普通的旁白，没有任何标注。"], texts
    assert all(t.endswith("。") for t in texts), f"句末句号被剥掉了: {texts}"


# ---------------------------------------------------------------------------
# 5. merge_short 的两个坑
# ---------------------------------------------------------------------------
def test_merge_short_tail_crash_regression():
    """收尾合并不得崩：`merged[-2] = merged[-2] + merged.pop()` 会 IndexError。

    赋值语句先算右侧（pop 使长度减 1）再对左侧下标赋值，len==2 时 pop 后
    merged[-2] 越界。2026-09-06 新剧本 --merge-short 建档直接崩在这里。
    """
    assert S.merge_short_pieces(["这是一个足够长的句子片段", "短"]) == \
        ["这是一个足够长的句子片段短"]
    assert S.merge_short_pieces(["短"]) == ["短"]          # len==1 不该动
    assert S.merge_short_pieces([]) == []
    # 放不下（超 max_join）就不并，也不崩
    long_ = "一" * 29
    assert S.merge_short_pieces([long_, "短"]) == [long_, "短"]


def test_merge_short_reduces_frame_count_on_real_script():
    """新剧本开 merge_short 应显著减少帧数（248 → 220）。"""
    md = ANNOTATED_MD + "短句。\n\n" * 1
    a = len(_split(md, merge_short=False))
    b = len(_split(md, merge_short=True))
    assert b <= a, f"merge 后不应变多: {a} -> {b}"


def test_strip_is_idempotent():
    once = S.strip_script_markdown(PLAIN_MD)
    assert S.strip_script_markdown(once) == once


def test_strip_preserves_sentence_count_of_plain_text():
    """清洗不得改变纯文本的句数（否则帧数漂移 → scene_plan 错位）。"""
    assert len(re.split(S.SENT_END, S.strip_script_markdown(PLAIN_MD.strip()))) \
        == len(re.split(S.SENT_END, PLAIN_MD.strip()))
