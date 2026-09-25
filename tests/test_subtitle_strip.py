# -*- coding: utf-8 -*-
"""字幕上屏文案：尾部句读/句末标点必须剥掉（2026-09-06）。

竖屏大字的每一行都是切分器切出来的**碎片**，不是完整句子。碎片行尾挂个
「，」「。」既占视觉位置，又让观众以为这句完了。

⚠️ 剥的必须是**渲染层**的显示文案（strip_sub_tail），不是 frame.text：
  · frame.text 还是 TTS 的输入 —— 去掉句号 TTS 就不知道该停顿；
  · frame.text 还是帧指纹/场景装配的依据 —— 改它等于把已交付集的配音全作废。

剥标点集合 `_SENT_END_PUNCT` 已进段参数指纹，改集合会自动作废旧段
（否则改了上屏样式，断点续跑却命中旧段 → 成片纹丝不动）。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.legacy


from app.services import compose as C


@pytest.mark.parametrize("raw,shown", [
    ("幸好，", "幸好"),                                   # 句读逗号（本次补的）
    ("陇西平原，", "陇西平原"),                            # 句读逗号
    ("他们是不会跟你讲程序正义的。", "他们是不会跟你讲程序正义的"),  # 句号
    ("黄河以东，刘武周的骑兵直插晋阳老家；", "黄河以东，刘武周的骑兵直插晋阳老家"),
    ("浅水原之战，大军断粮三天，", "浅水原之战，大军断粮三天"),
    ("看，这叫“关陇军政风投股份有限公司”。",
     "看，这叫“关陇军政风投股份有限公司”"),                # 句号在闭引号外
])
def test_trailing_punct_is_stripped(raw, shown):
    assert C.strip_sub_tail(raw) == shown


@pytest.mark.parametrize("raw", [
    "3.5秒",            # 小数点不能被当句号吃掉
    "、“国中之国”",      # 行内的句读标点不动
    "他说：“不”",
    "公元618年",
])
def test_inner_punct_is_kept(raw):
    assert C.strip_sub_tail(raw) == raw


def test_all_punct_text_falls_back_to_original():
    """整帧只剩标点时不能剥成空字幕。"""
    for raw in ("。", "，", "…", "。！"):
        assert C.strip_sub_tail(raw) == raw


def test_comma_and_fullwidth_comma_are_in_strip_set():
    assert "，" in C._SENT_END_PUNCT
    assert "、" in C._SENT_END_PUNCT
    assert "。" in C._SENT_END_PUNCT
    # 数字小数点绝不能进集合
    assert "." not in C._SENT_END_PUNCT


def test_strip_punct_is_part_of_segment_fingerprint():
    """剥标点集合必须进指纹：否则改了上屏样式，旧段命中跳过 → 成片不变。"""
    tl = [(type("S", (), {"id": "s", "duration_ms": 1000, "image_path": "",
                          "upscaled_path": "", "subtitle_text": "幸好，",
                          "motion": None})(), 0, 1000)]
    fp1 = C._seg_params_fingerprint(tl, ("h264_nvenc",), "segmented", [tl])
    orig = C._SENT_END_PUNCT
    try:
        C._SENT_END_PUNCT = frozenset("。！？；：…—")   # 去掉逗号
        fp2 = C._seg_params_fingerprint(tl, ("h264_nvenc",), "segmented", [tl])
    finally:
        C._SENT_END_PUNCT = orig
    assert fp1 != fp2, "剥标点集合变了指纹却没变 → 旧段不会被作废"


# ---------------------------------------------------------------------------
# 标点 → 空格（2026-09-25 用户规则：「视频里的标点符号都换成空格」）
# ---------------------------------------------------------------------------
def test_punctuation_becomes_space_not_deleted():
    """句中标点必须换成**空格**（保留停顿的视觉间隔），不是直接删掉。"""
    from app.services.compose import subtitle_display_text
    got = subtitle_display_text("浅水原迫降薛仁杲、柏壁逐宋金刚、")
    assert got == "浅水原迫降薛仁杲 柏壁逐宋金刚", got
    assert "  " not in got, "连续标点应压成一个空格"


def test_quotes_and_title_marks_also_become_space():
    """引号 / 书名号同属标点，一并换空格。"""
    from app.services.compose import subtitle_display_text
    assert subtitle_display_text("奉上一顶“天可汗”的至高桂冠。") == "奉上一顶 天可汗 的至高桂冠"
    assert subtitle_display_text("《唐会要》白纸黑字") == "唐会要 白纸黑字"


def test_trailing_punctuation_still_stripped():
    """尾部语义不变：句末标点仍整体剥掉（避免孤标点占位）。"""
    from app.services.compose import subtitle_display_text
    assert subtitle_display_text("甲。") == "甲"
    assert subtitle_display_text("真的吗？！") == "真的吗"


def test_decimal_point_is_never_eaten():
    """⚠️ 沿用既有约束：`.` `,` 不入集合 —— 小数/千分位不能被吃掉。"""
    from app.services.compose import subtitle_display_text, _SUB_PUNCT_TO_SPACE
    assert "." not in _SUB_PUNCT_TO_SPACE and "," not in _SUB_PUNCT_TO_SPACE
    assert "3.5" in subtitle_display_text("耗时3.5秒，很快。")
    assert "2,000" in subtitle_display_text("共2,000人。")
    assert subtitle_display_text("耗时3.5秒，很快。") == "耗时3.5秒 很快"


def test_all_punctuation_falls_back_to_source():
    """整句都是标点时退回原文，不出空字幕。"""
    from app.services.compose import subtitle_display_text
    assert subtitle_display_text("……") == "……"


def test_space_punct_set_is_in_segment_fingerprint():
    """新集合必须进段参数指纹，否则改了上屏样式旧段不会作废 → 成片纹丝不动。"""
    import inspect
    from app.services import compose as C
    src = inspect.getsource(C._seg_params_fingerprint)
    assert "_sub_space_punct()" in src
