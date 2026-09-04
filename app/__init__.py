"""hv — A 股数据中枢的史官：抖音历史解说短视频生产流水线。

本项目以 harry0703/MoneyPrinterTurbo 为工程骨架参考，但材料层完全自建
（gpt-image-2 + 角色一致性 + Ken Burns + RealESRGAN 取代 stock footage）。

目录约定：
    app/services/   — 业务逻辑，单职责
    app/controllers — FastAPI 路由（v1/）
    app/managers/   — 状态管理（episode.json 状态机 + relay 配额跟踪）
    app/models/     — Pydantic schema
    app/config/     — 配置（YAML + Pydantic Settings + .env 渠道池发现）
    app/webui/      — Streamlit 控制台（可选）
"""
__version__ = "0.1.0"
