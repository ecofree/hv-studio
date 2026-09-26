# hv — 历史解说竖屏短视频流水线

2026-09-12：`episode produce` 和 API `/v1/episode/{id}/produce` 默认使用音频优先的 v2 链路：script → images → tts → delivery → quality → alignment → timeline → compose → verify。旧片预览重建必须显式设置 `--pipeline legacy-preview`。`CANDIDATE` 表示可供审核的技术候选，不代表发布批准。完整操作与恢复说明见 [生产运行手册](docs/production_runbook.md)。

Windows 启动器默认使用隔离的 `.venv-core`。先用 `scripts/bootstrap-core.ps1` 和 `requirements.core.lock.txt` 建立核心环境；本地 ASR 使用独立环境。所有生成额度默认 0。`--max-requests`、`--max-characters`、`--max-images` 限制实际调用与字符，失败、超时及重试均计入，整批共享一个持久账本，续跑不重置。新一轮明确授权使用新的 `--budget-id`（批量使用新的 batch-id）。默认入口会自动构造字幕及镜头时间轴；已有素材先按请求与散列复用。缺项返回 BLOCKED 和非零退出码。

试听、画面审核和发布继续使用 `python -m app.cli narration review-audio / review-video / publish`；审批绑定确切候选及输入散列。历史改造和试听记录见 [生产改造记录](docs/production_upgrade_20260910.md)，该记录描述的旧入口状态以本文最新说明为准。下文旧版按帧流程的操作示例需选择 `legacy-preview`。

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
  orchestrator.py         ★ 唯一编排器（CLI 与 API 共用）
                            2026-09-05 起双轨编排器 managers/pipeline.py 已删，
                            单镜重建走 `episode reshot --key`
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

# 单镜重建：删旧图 → 重生成 → 重装配 → 重合成（想让某镜重画就用这个）
<hv-venv>/python app/cli.py episode reshot <id> --key LGT01 [--key LGT02] [--no-verify]

# 孤儿 wav 清理（默认 dry-run 只列出，--apply 才真删）
<hv-venv>/python app/cli.py episode audit-wav <id> [--apply]
<hv-venv>/python app/cli.py episode audit-wav --all --apply    # 扫全部集

# 其他子命令：init / show / checkpoint --label X
#   episode run 已废弃（仅打印指路提示后 exit 1，不执行任何逻辑）

# build/ 中间产物清理（默认 dry-run 只列清单与体积，--apply 才真删）
<hv-venv>/python app/cli.py clean --all                        # 只看能清出多少
<hv-venv>/python app/cli.py clean --all --apply                # 清中间产物（~120MB）
<hv-venv>/python app/cli.py clean --all --segments --apply     # 连段一起清（~900MB）
<hv-venv>/python app/cli.py clean --episode lsm-15min --apply  # 只清某一集
#   ⚠️ 图片资产（底图/调试帧 .png）永不删除 —— 白名单之外的东西一律不动，
#      被硬闸拦下的图片会在输出里 [skip] 记账，不会静默忽略。
#   ⚠️ 段（build/segments/seg-*.mp4）是断点续跑缓存，删了下次 compose 全量重编，
#      所以默认不清段，必须显式 --segments。

# 花销对账（金额需自建 state/cost_prices.json，口径=成功落盘张数下界）
<hv-venv>/python app/cli.py cost [--since 2026-09-04] [--raw]

# 回归基线：打印所有 episode/scene_plan 的内容指纹 + notes 长度
<hv-venv>/python bin/fp_episodes.py

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
  已标注为 disabled / 占位，不虚报已启用。2026-09-05 复查删掉了三处零消费者的死配置段
  （`aligner` / 顶层 `motion` / `profile.rhythm`），规格常量收口在 `app/services/spec.py`。
- **时序以磁盘 wav 为唯一真源**（2026-09-26）：镜头长度按磁盘 wav 的真实采样数算，
  json 的 `frame.audio_duration_ms` 降级为「是否过时」的交叉校验（超
  `_WAV_TOLERANCE_MS=120ms` 即报错）。此前按 json 值算镜头长度、却把真实 wav 摆到
  音频轨，两者不同步时误差逐镜累积成音画整体漂移（上界 23.6s），而 verify 只数总帧数、
  根本看不出来。现在两边算的是同一个 wav，漂移结构上不可能发生；拼完旁白另有
  「实际采样数 == 时间轴承诺采样数」硬断言。
- **换图黑场过渡**（`compose.image_transition_frames`，默认 6 帧 ≈ 0.24s）：只在
  **底图真的变了**的镜头首尾生效，在**既有帧内调亮度** —— 不加帧、不动音频，所以
  总帧数与音画对位一字不变，段划分/断点续跑/verify 全不受影响。同一底图的相邻镜头
  **不会**加黑场（那是连续运镜，插黑等于人为制造剪辑）。设 0 即回到纯硬切。
- **段参数指纹**含 vcodec、**ffmpeg 版本**、过渡帧数、字幕样式、grade 表与每镜完整
  motion spec：任一改动都会作废旧段强迫重编。ffmpeg 版本曾在 legacy 路径缺席 ——
  升级 ffmpeg 后重跑会复用旧版本编的段，成片混着两种编码器输出而 verify 察觉不到。

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

引擎自动派生（2026-09-26 起，见 `docs/镜头衔接诊断_2026-09-26.md`）：
**同一 scene 内的镜头串成一条连续轨迹** —— 后一镜的 `start_zoom`/`start_xy` 直接接
前一镜的**渲染落点**（落点必须问 `motion.resolve_motion` 要：legacy 口径下 spec 里写的
`end_zoom` 并不等于渲染落点，`resolve_motion` 会用 `type + start_zoom` 乘自己的公式重算）。
横向往复、zoom 在 `[1.0, scene 上限]` 内往复；**只有换图（真实剪辑）才归位**。
运镜类型 push/drift/pull/pan 轮换（禁连续同型），且 `push` 不越 scene 上限、`pull`
不在 `z==1.0` 处选（否则整镜静止）；grade 按 scene.grades 循环；呼吸段
（head 100ms + tail 150ms）预扣。
轨迹用新的几何版本 `explicit_traj_v1` 显式写出：裁切几何仍与 legacy 逐位相同
（`scale=1.0`），只有「轨迹由公式决定」改为「由 spec 决定」，所以构图不变。
**护栏**：空洞帧 / 重复覆盖 / 越界 / 缺图 / 非法 grade 一律拒绝；装配完再硬校验
`_continuity_breaks` —— 同底图相邻镜头的渲染落点不相接即 `SystemExit` 拒绝落盘。
样例见 `episodes/hanwu-feature/scene_plan.json`（与手写 ch1 逐帧一致，0 mismatch）。

已交付集的历史运镜口径**冻结**在 `motion.legacy_motion`，`bin/hanwu_ch1_shots.py` 与
`bin/hanwu_ch3_shots.py` 原先各存一份逐字节相同的副本，已收编为那唯一一份（行为逐位
不变）。`bin/m2_shots.py` 有自己的额外逻辑（方向交替 + 高倍镜位 pan 降级 drift），
不在这套口径内。**新片一律用 `episode shots`**，不要新增 shots 脚本。

## 验收基线（tests/）

覆盖 2026-09-04 审计的最低验收线：
- `test_tts_fingerprint`：TTS 幂等由旁白文本指纹驱动（文本变→重合成）
- `test_upscale_invalidation`：upscale 复用检测源图变更
- `test_compose_first_production`：**无旧成片的首产 compose 成功且无 .tmp 残留**
- `test_shot_builder`：剧本→分镜合法 plan 装配 + 非法 plan 护栏拒绝
- `test_state_truthfulness`：run_stage 需真实执行、schema 检 dangling、produce verify 导出真状态

## 本地 ASR 与字幕听审

当前 `ancient-realistic` 配置使用 `local-whisper`，TTS 保持 CosyVoice 3.5。
首次在新机器执行 `powershell -File bin/setup_local_asr.ps1 -Python <Python路径>`；
模型下载较慢时可显式增加 `-Mirror`，下载内容仍核对原发布版本的 SHA256。
ASR 使用独立 `.venv-asr` 和 `requirements.asr.lock.txt`，识别期间不联网，
缺模型、环境版本不符或超时不会回退到云端。正式 TTS 入口先检查本地 ASR 是否可用。

`python -m app.cli narration align-draft <manifest.json> <输出目录>`
生成字幕草稿、SRT 和 `review.html`。使用
`python bin/serve_alignment_review.py <草稿所在目录> --port 8767`
后打开 `http://127.0.0.1:8767/review.html`，支持点击字幕定位试听。
该服务只监听本机，并仅提供听审页面、音频和 SRT 文件。

口播文本对破折号和省略号做有版本、有位置映射的标点转换；原稿和字幕不变。
被人工否决的音频按原始与标准化音频 SHA256 登记到 `state/audio_rejections`，
缓存、已完成的提供商响应、候选渲染和发布都会检查。重新生成仍需要明确预算，
不会因为音频被否决而自动调用提供商。缓存原音的本地复核生成新 QA 记录，保留旧证据。

默认 alignment 阶段将独立听写和原文约束的声学对齐分开保存：前者检查读音疑点，后者提供完整原文的词边界。超长单元只在实际识别到的匹配词边界切窗，不按字数推算词时间。字幕按声学边界分组，要求原文不丢不重、0.8–4 秒、最多两行且实测描边/阴影在安全区内。机器验证只允许生成待审候选；对齐不能证明音频读对，更不能替代听审、史实和画面审核。旧 align-draft 路径仍保留人工对齐审核要求。

## 已知边界

- 低 pagefile 机器合成超大集（3344×1882）可能 OOM——单帧驻留 + gc 已缓解，非代码缺陷。
- RealESRGAN / I2V 为 M2 占位（basicsr 编译 / relay 生产链路未落地）。
- 本仓已是 git 仓库（分支 main，含代码 + 交付集状态历史）。`.gitignore` 忽略音频/build/
  out/shots/大 mp4 等生成产物，但保留 `episodes/*.json` 模型入库。新集首次提交时需显式
  `git add episodes/{新集}.json`（其产物目录因体积/可再生成不入库）。
