"""
Streamlit 轻量控制台（monitor/utility）。

⚠️ 定位：**只读监控 + 小工具**，不是出片驱动。真实生产链走
`python app/cli.py episode produce <ep>`（orchestrator 全链）或 API /produce。
本页展示渠道健康 / 图像参数 / 多音字预检，无状态修改。

启动：python -m app.cli webui
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st

from app.config.config import get_settings
from app.managers import get_relay
from app.services.polyphone import preprocess

st.set_page_config(page_title="hv 控制台", page_icon="🎬", layout="wide")

st.title("hv — 历史解说短视频流水线")
st.caption("监控台 · 出片走 CLI produce · 生图通道 subrouter.ai gpt-image-2")

s = get_settings()
r = get_relay()

# ---- 渠道健康（ok_rate 真实成功率；err_rate = 1 - ok_rate） ----
st.subheader("subrouter 渠道池")
snap = r.snapshot()
if not snap:
    st.warning("无可生图渠道（.env 未配或未同步）")
else:
    cols = st.columns(len(snap))
    for col, (name, h) in zip(cols, snap.items()):
        ok_rate = h.get("ok_rate", 1.0)
        err_rate = 1 - ok_rate
        with col:
            st.metric(
                name,
                f"{h['score']:.0f}",
                delta=f"err {err_rate*100:.0f}%" if h.get("n_history", 0) else "无历史",
                delta_color="inverse",  # err 越低越好
            )
            st.caption(f"ok {ok_rate*100:.0f}% · {h['n_history']} 次 · "
                       f"quota ${h['last_quota_usd'] or '—'}")

# ---- 关键参数 ----
st.subheader("图像配置")
c1, c2, c3 = st.columns(3)
c1.metric("请求 size", s.image_size)
c2.metric("预测实际", f"{s.predict_actual_image_dims()[0]}×{s.predict_actual_image_dims()[1]}")
c3.metric("超分后", f"{s.predict_upscaled_dims()[0]}×{s.predict_upscaled_dims()[1]}")

# ---- 多音字测试 ----
st.subheader("多音字预处理")
text = st.text_area("原文", value="张骞被匈奴扣押十年，单于冒顿派阏氏看守")
if text:
    out = preprocess(text)
    st.code(out, language="text")

# ---- 使用指引 ----
st.subheader("生产入口")
st.info(
    "真实出片不在此页。用：\n"
    "- `python app.cli.py episode produce <ep>`：一键全链（assets→shots→compose→verify），"
    "长片中断后重跑即断点续跑；\n"
    "- `... episode produce <ep> --stage verify`：只读校验产物真值；\n"
    "- `... episode --all audit-wav --apply`：清孤儿音频；\n"
    "- API：`POST /v1/episode/{id}/produce`。"
)
