# hv — 历史解说竖屏短视频流水线

把"解说脚本"转成 9:16 竖屏成片的 Python 流水线（KPL/HiThink 生图通道、CosyVoice 配音、
Ken Burns 运镜、实时字幕、NVENC 合成）。仓库已 git 基线（分支 main），本地生产用。

## 已交付成片

| 产物 | 位置 | 规格 |
|---|---|---|
| **《汉武雄风》15min 全片** | `episodes/hanwu_15min/hanwu_15min.mp4` | 690MB / 885.18s / 22129 帧 / h264 High·level41 / 1080×1920 |
| 分P（一·龙城飞将 / 二·凿空西域 / 三·天马与帝国） | `episodes/hanwu_15min/hanwu_ep{1,2,3}_*.mp4` | 与全片同 profile |

## 架构

```
episodes/{id}.json        ← 单一可信源（Episode/Frames/Shots Pydantic 模型）
  ├─ episodes/{id}/script.md   解说脚本
  ├─ episodes/{id}/audio/      逐帧 TTS wav
  ├─ episodes/{id}/shots/      场景图 + @2x（Ken Burns 余量）
  └─ episodes/{id}/out/{id}_m1.mp4   成片
app/
  config/config.py        ROOT/配置（单一真源；engine/编码/CORS 均在此收敛）
  models/                 Episode/Frame/Shot/MotionSpec schema
  managers/episode.py     load/save/状态机/schema 校验/路径可移植归一
  managers/pipeline.py    按镜头重建编排
  orchestrator.py         ★ 统一真实编排器（CLI 与 API 共用）
  services/
    voice.py              TTS（CosyVoice v3.5 主 / edge 备；文本指纹幂等）
    upscale.py            超分（pil_lanczos；源图变更失效检测；engine 读 config）
    motion.py             Ken Burns 运镜（NVENC High/4.1 单一真源）
    compose.py            合成（.tmp 原子改名 / 超时 / 时序 gate 后置 / 磁盘时长校验）
    script.py             frame 切分
    timing_assert.py      §5.4 时序断言
  controllers/v1/         FastAPI 路由（episode/image/video）
  cli.py                  CLI 入口
```

## 环境

```bash
# 建/复用虚拟环境（当前实测：C:\Users\coumoo\.workbuddy\binaries\python\envs\hv）
python -m venv <hv-venv>
<hv-venv>/pip install -r requirements.txt     # 最小可装依赖
# 复现本机锁定版本：pip install -r requirements.lock.txt
```

- 需 ffmpeg（NVENC `h264_nvenc`；无 NVENC 自动降级 libx264，见 config `compose.fallbacks`）
- `.env`：AI 密钥（见 `.env.example` 占位）；真实 `.env` 已入 `.gitignore`
- 低 pagefile 沙箱跑 Python 前设：`OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1`

## 用法

```bash
# 统一真实编排（脚本→配音→合成→产物校验；状态由产物真值导出）
<hv-venv>/python app/cli.py episode produce <id> [--stage script|tts|compose|verify ...]

# 剧本→分镜（新片：只需 script.md + scene_plan.json，不必手写 shots 脚本）
<hv-venv>/python app/cli.py episode shots <id> [--plan path] [--dry-run]
#   scene_plan.json 见 episodes/hanwu-feature/scene_plan.json 样例（B 项管线）

# API（需 fastapi/uvicorn）
<hv-venv>/python app/cli.py api                    # 默认 127.0.0.1
# POST /v1/episode/{id}/produce?stage=verify       真实编排；默认 /run 只读 verify
# GET  /v1/episode/{id}

# 验收测试（最低线上线）
<hv-venv>/python -m pytest tests/ -v
```

## 自动化 / 状态可信度

- **统一编排器** `app/orchestrator.py`：CLI 与 API 共用同一条真实生产链。
- **状态只能由产物推进**：`run_stage` 无真实执行函数即拒绝；`produce` 依产物真值导出状态。
- **schema 硬阻断**：生产前 `validate_schema` 不合格即抛错，绝不到付费生图。
- **路径可移植**：episode 数据里的 `D:/hv` 绝对路径在 `load()` 边界按当前 `ROOT` 重映射，
  仓库搬移/换盘符后无需改数据即可继续消费。
- **配置=运行事实**：upscale 实际 `pil_lanczos`（config 声明一致）；I2V/RealESRGAN 未实现
  已标注为 disabled / 占位，不虚报已启用。

## 剧本→分镜（scene_plan 规格）

新片流程：先写 `script.md` + 跑 script 切分出 frames（含 TTS），再写一份 `scene_plan.json`，
用 `episode shots <id>` 一次性装配 shots。格式：

```jsonc
{
  "episode": "my-ep",
  "images": {
    "S01": {"path": "episodes/my-ep/shots/S01.png"},   // 本集生成的新底图
    "SC24": {"reuse_from": "hw-002"}                   // 跨集复用（零新增成本）
  },
  "scenes": [
    {"frames": [0, 2],   "image": "S01", "grades": ["frost", "dawn", "dusk"]},
    {"frames": [3, 6],   "image": "SC24", "grades": ["dawn", "noon"]}
    // frames 为闭区间（ep.frames 下标），须恰好覆盖全部帧、不重叠、无空洞
    // grades 只取 dawn/noon/dusk/night/fire/frost，按镜循环
  ]
}
```

引擎自动派生：同底图首镜 zoom=1.0、后续走 config zoom 阶梯；运镜 push/drift/pull/pan
轮换（禁连续同型）；grade 按 scene.grades 循环；呼吸段（head 100ms + tail 150ms）预扣。
**护栏**：空洞帧 / 重复覆盖 / 越界 / 缺图 / 非法 grade 一律拒绝，绝不产出错位 shots。
样例见 `episodes/hanwu-feature/scene_plan.json`（与手写 ch1 逐帧一致，0 mismatch）。

## 验收基线（tests/）

覆盖 2026-09-04 审计的最低验收线：
- `test_tts_fingerprint`：TTS 幂等由旁白文本指纹驱动（文本变→重合成）
- `test_upscale_invalidation`：upscale 复用检测源图变更
- `test_compose_first_production`：**无旧成片的首产 compose 成功且无 .tmp 残留**
- `test_shot_builder`：剧本→分镜合法 plan 装配 + 非法 plan 护栏拒绝
- `test_state_truthfulness`：run_stage 需真实执行、schema 检 dangling、produce verify 导出真状态

## 已知边界

- 低 pagefile 机器合成超大集（3344×1882）可能 OOM——单帧驻留 + gc 已缓解，非代码缺陷。
- RealESRGAN / I2V 为 M2 占位（basicsr 编译 / relay 生产链路未落地）。
- 本仓已是 git 仓库（分支 main，含代码 + 交付集状态历史）。`.gitignore` 忽略音频/build/
  out/shots/大 mp4 等生成产物，但保留 `episodes/*.json` 模型入库。新集首次提交时需显式
  `git add episodes/{新集}.json`（其产物目录因体积/可再生成不入库）。
