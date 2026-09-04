"""
音色塑形 A/B 试听：同一句旁白，不同调音方案，一次产出多个候选。

背景：用户反馈"声音不够浑厚，有点薄"。
可调杠杆（CosyVoice v3.5 原生支持）：
  1. pitch_rate  音调倍率，<1 压低 → 更沉更浑厚（改善"薄"最直接）
  2. instruction 自然语言风格引导，如"低沉浑厚、胸腔共鸣"
  3. model       flash(便宜) / plus(音质更好，价约 2 倍)
  4. voice_prompt 声音设计描述，决定基础音色（换它等于换一个人）

用法：
  python bin/probe_voice_tone.py            # 生成全部候选
  python bin/probe_voice_tone.py --text "自定义测试句"
候选输出到 episodes/_voicetone/{name}.mp3
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path("D:/hv")
sys.path.insert(0, str(ROOT))

TEST_TEXT = "然而张骞始终手持旌节，不肯屈服。"

OUT_DIR = ROOT / "episodes" / "_voicetone"

# 更强调"浑厚/低频/胸腔共鸣"的声音设计描述（用于候选 D：换个更厚的基础音色）
DEEP_VOICE_PROMPT = (
    "一位成熟的男中音播音员，嗓音低沉宽厚、胸腔共鸣饱满，低频扎实有力，"
    "音色温暖而厚重，不带鼻音，不尖锐，语速沉稳舒缓，吐字清晰有力，"
    "适合大型历史纪录片旁白。"
)


def gen(name: str, text: str, **kw) -> Path | None:
    """生成一个候选。kw 透传给 synthesize_dashscope_whole。"""
    from app.services.cosyvoice import synthesize_dashscope_whole
    out = OUT_DIR / f"{name}.mp3"
    try:
        synthesize_dashscope_whole(text, out, response_format="mp3",
                                   overwrite=True, **kw)
        print(f"  OK  {name:14s} {out.stat().st_size/1024:6.0f}KB")
        return out
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL {name:14s} {type(e).__name__}: {str(e)[:120]}")
        return None


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    text = TEST_TEXT
    if "--text" in sys.argv:
        text = sys.argv[sys.argv.index("--text") + 1]
    print(f"测试文本: {text}")
    print("生成候选：")

    results = {}

    # A 基准：当前线上设置（pitch 1.0，无 instruction）
    results["A_基准_pitch1.0"] = gen("A_基准_pitch1.0", text,
                                     pitch_rate=1.0, instruction=None)

    # B 轻压音调
    results["B_浑厚_pitch0.9"] = gen("B_浑厚_pitch0.9", text,
                                     pitch_rate=0.9,
                                     instruction="用低沉、浑厚、胸腔共鸣的男中音朗读，声音厚实稳重")

    # C 重压音调 + 略放慢
    results["C_更厚_pitch0.85"] = gen("C_更厚_pitch0.85", text,
                                      pitch_rate=0.85, speech_rate=0.95,
                                      instruction="用低沉、浑厚、胸腔共鸣的男中音朗读，声音厚实稳重")

    # D plus 模型（音质更好）+ 浑厚调音
    results["D_plus_pitch0.9"] = gen(
        "D_plus_pitch0.9", text, model="cosyvoice-v3.5-plus",
        voice_id=_plus_voice_id(), pitch_rate=0.9,
        instruction="用低沉、浑厚、胸腔共鸣的男中音朗读，声音厚实稳重")

    ok = [n for n, p in results.items() if p]
    print(f"\n完成 {len(ok)}/{len(results)} 个候选 → {OUT_DIR}")
    for n in ok:
        print(f"  {n}")


def _plus_voice_id() -> str | None:
    """取 plus 模型专属 voice_id（voice_id 与 target_model 绑定，不可跨模型复用）。"""
    try:
        from app.services.cosyvoice import design_voice
        vd = design_voice(voice_prompt=DEEP_VOICE_PROMPT,
                          prefix="hvnplus",
                          target_model="cosyvoice-v3.5-plus")
        return vd["voice_id"]
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] plus voice_id 获取失败: {str(e)[:120]}")
        return None


if __name__ == "__main__":
    main()
