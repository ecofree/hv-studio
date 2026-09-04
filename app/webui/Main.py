"""
Streamlit 控制台（M0 骨架）。

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
st.caption("M0 骨架 · subrouter.ai gpt-image-2 接入")

s = get_settings()
r = get_relay()

# ---- 渠道健康 ----
st.subheader("subrouter 渠道池")
snap = r.snapshot()
cols = st.columns(len(snap))
for col, (name, h) in zip(cols, snap.items()):
    with col:
        st.metric(name, f"{h['score']:.0f}", delta=f"err_rate {1 - h['n_history']/max(1,h['n_history'])}")
        st.caption(f"history: {h['n_history']}  quota: {h['last_quota_usd'] or '—'}")

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

# ---- 占位 ----
st.subheader("M0 状态")
st.info(
    "骨架已就位。image + polyphone 实装可用，其余 9 个 service 是接口 stub，"
    "M1 阶段填实。可在 /v1/ 测试 API，或用 `python -m app.cli image` 跑真生图。"
)
