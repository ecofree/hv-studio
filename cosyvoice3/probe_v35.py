# -*- coding: utf-8 -*-
"""
CosyVoice v3.5 (DashScope 云 API) 男声档位对比验证脚本。

用途：
  1. 用「语音设计」创建沉稳纪录片男声 voice_id（第一次会扣少量费，成功后缓存复用）。
  2. flash 与 plus 各合成同一段历史旁白文案，落盘试听。
  3. 输出时长，供你试听后定档（flash 够好就用 flash，否则 plus）。

运行（务必用 hv venv，dashscope 装在那）：
  C:\\Users\\coumoo\\.workbuddy\\binaries\\python\\envs\\hv\\Scripts\\python.exe probe_v35.py

先决条件：
  D:\\hv\\.env 里已填 DASHSCOPE_API_KEY=sk-...  (去掉待填写注释)
"""
from __future__ import annotations

import sys
from pathlib import Path

# 让脚本能 import 到 hv 的 app 包
_HV_ROOT = Path(__file__).resolve().parent.parent  # D:\hv
if str(_HV_ROOT) not in sys.path:
    sys.path.insert(0, str(_HV_ROOT))

from app.services.cosyvoice import (  # noqa: E402
    DashScopeCosyVoiceSynthesizer,
    design_voice,
)

# 验证用历史旁白文案（沉稳男声纪录片语气）
TEST_TEXT = (
    "公元前一百四十年，一个十六岁的少年登上皇位，"
    "没有人预料到，他将用五十四年的时间，"
    "把一个王朝推向前所未有的巅峰。"
)

OUT_DIR = Path(__file__).parent / "probe_out"
VOICE_PROMPT = (
    "一位沉稳的中年男声播音员，声音低沉浑厚、富有磁性，语速平稳，吐字清晰，"
    "适合历史纪录片旁白或新闻播报。"
)


def _model_tag(m: str) -> str:
    """从模型名提取档位标签：flash / plus。"""
    if "flash" in m:
        return "flash"
    if "plus" in m:
        return "plus"
    return m.split(".")[-1].split("-")[0]


def main() -> int:
    # 0) 先校验 key 与 workspace
    from app.services.cosyvoice import (
        _dashscope_key, _resolve_voice_design_endpoint, _env_get,
    )
    _dashscope_key()
    ws = _env_get("DASHSCOPE_WORKSPACE")
    print("[ok] DASHSCOPE_API_KEY 已配置")
    print(f"    workspace   = {ws or '(未配，用通用端点)'}")
    print(f"    design 端点 = {_resolve_voice_design_endpoint()}")

    # 每个模型需各自建专属 voice_id（音色绑定 target_model，不能跨模型复用）
    models = ("cosyvoice-v3.5-flash", "cosyvoice-v3.5-plus")
    voices: dict[str, str] = {}
    for model in models:
        tag = _model_tag(model)
        print(f"[1] {model} 声音设计沉稳男声 voice_id ...")
        try:
            vd = design_voice(
                voice_prompt=VOICE_PROMPT,
                preview_text="汉武帝刘彻，十六岁登基，五十四年励精图治，开创了中国历史上第一个盛世。",
                prefix=f"hvn{tag}",  # 每模型独立 prefix，避免撞缓存
                target_model=model,
            )
            voices[model] = vd["voice_id"]
            print(f"    voice_id = {vd['voice_id']}  (cached={vd.get('cached')})")
            if vd.get("preview_path"):
                print(f"    preview  = {vd['preview_path']}")
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            print(f"    FAIL voice design({model}): {msg}")
            if "403" in msg or "AccessDenied" in msg:
                print("\n  ▲ CosyVoice 未授权：百炼控制台(华北2北京)开通 CosyVoice v3.5 后重跑。")
            return 1

    # 2) 各模型用各自的 voice_id 合成
    for model in models:
        if model not in voices:
            continue
        tag = _model_tag(model)
        out = OUT_DIR / f"narrator_{tag}.mp3"
        print(f"[2] 合成 {model} (voice={voices[model][:24]}...) ...")
        try:
            svc = DashScopeCosyVoiceSynthesizer(voice=voices[model], model=model,
                                                response_format="mp3")
            path, dur_ms = svc.synthesize(TEST_TEXT, out, overwrite=True)
            print(f"    OK: {path}  {dur_ms/1000:.2f}s")
        except Exception as e:  # noqa: BLE001
            print(f"    FAIL {model}: {e}")

    print(f"\n试听文件目录：{OUT_DIR}")
    print("（可对比 flash 与 plus 音质，定档后把 .env 的 COSYVOICE_V35_MODEL 改到位）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
