# 历史解说短视频生产流水线 — 技术设计文档

> 项目代号 `hv`（history-video） · 文档版本 v1.0 · 2026-09-02
> 对标：《原点档案》系列历史人物解说短视频
> 本文覆盖：脚本 → 配音 → 画面 → 运镜 → 字幕 → 合成的本地可落地方案
> 本文不覆盖：平台分发、数据复盘（阶段二）

---

## 0. 决策基线

| 决策项 | 结论 | 依据 |
|---|---|---|
| 画面路线 | **A：全 AI 静态图 + Ken Burns 运镜** | 0 边际成本，不依赖 GPU 状态，可当日落地 |
| TTS | **双轨：Kokoro-82M 打底 + Qwen3-TTS 出片** | Kokoro 纯 CPU 210x 实时用于快速迭代；Qwen3 中文韵律最优用于成片 |
| 算力 | ~~修复 GPU~~ → **GPU 实际可用，无需修复**（见 §1.2） | 12GB 显存，CUDA 正常，仅 NVSMI 管理工具缺失 |
| 首步交付 | 技术设计文档 → 30 秒垂直切片 | 先验证质感，再谈批量 |
| 画幅 | 1080×1920（9:16 竖屏） | 抖音主战场 |
| 帧率 | 25 fps | 抖音推荐区间 25–30，25 在 CPU 合成下更快 |

### 0.1 关键判断：GPU 完全可用，之前的诊断结论作废

2026-09-02 二次验证（`torch 2.14.0+cu126`）实测结果：

```
cuda avail : True
device name: NVIDIA GeForce RTX 2060
VRAM       : 12.00 GB   （可用 10.95 GB）
SM count   : 34
matmul ok  : True
```

**结论修正（重要）**：

1. **GPU 计算能力完全正常，不需要修驱动。** `nvidia-smi` 报 NVML 失败只是**管理工具**（NVSMI / nvml.dll）缺失或版本不匹配，**与 CUDA 计算通路无关**。NVML 走管理接口，CUDA 走驱动 API，两者独立。此前"驱动未安装"的推断是错的——能跑通 CUDA matmul 就证明内核驱动栈是完整的。
2. **显存是 12GB 而非 6GB。** RTX 2060 存在 6GB / 12GB 两个版本，本机为 12GB 版。这大幅放宽了模型选型。
3. **M0 里程碑已经完成**，环境侧没有阻塞项。

**12GB 显存带来的能力变化**（相对文档初稿的 6GB 假设）：

| 能力 | 6GB 假设 | **12GB 实测** |
|---|---|---|
| Qwen3-TTS 0.6B / 1.7B | 勉强 | 轻松，可开 fp16 全量 |
| CosyVoice 3 | 紧张 | 轻松 |
| SDXL 生图 | 需 slicing，并发限 2 | 无需 slicing，并发 4–6 |
| **Wan2.2 图生视频（I2V）** | **判定不可行** | **1.3B 版可行，5B 量化版或许可行** |

最后一行是关键：**路线 B（部分镜头走 I2V）在本地变得可行**，边际成本从 API 计费降为接近 0 电费。虽然当前决策是路线 A，但 A 与 B 之间新增了一个低成本的过渡选项，建议 M1 后视质感决定是否升级（详见 §14 附注）。

---

## 1. 运行环境基线

### 1.1 实测硬件（2026-09-02 本机勘测）

| 项 | 实测值 | 备注 |
|---|---|---|
| CPU | Intel i7-12700 | 12 核 20 线程，CPU 推理主力 |
| 内存 | 32 GB | 无瓶颈 |
| GPU | **NVIDIA GeForce RTX 2060 · 12GB 版**（PCI\VEN_10DE&DEV_1F03） | CUDA 可用；**实测单进程显存上限 6.00 GB**，非报告值 10.95 GB（见 §1.3） |
| 磁盘 | C 盘可用 52 GB / D 盘可用 **809 GB** | 项目与素材库全部落 D 盘 |
| FFmpeg | **8.1.1 full_build (gyan.dev)** | 已编译 `libwhisper` `libass` `NVENC` `CUDA` `Vulkan` |
| Python 3.13.14（managed 3.13.12） | **已实测可用，torch 2.14 提供 cp313 wheel** |
| Node | 22.22.2 | 仅用于可选的前端素材管理 |

> FFmpeg 编译带 `libwhisper` 是本机的隐藏资产——绝大多数流水线做字幕靠"按字数均分时间轴"，永远和语音错半拍。本地强制对齐是免费就能拉开差距的地方。

### 1.2 GPU 诊断：NVSMI 失效 ≠ 驱动损坏（结论已修正）

#### 一轮诊断（文件系统级，结论为**误判**）

| 检查项 | 结果 |
|---|---|
| `DriverStore\FileRepository\nv_dispi.inf_amd64_*` | 不存在 |
| `System32\drivers\nvlddmkm.sys` | 不存在 |
| `C:\Program Files\NVIDIA Corporation\NVSMI\` | 不存在 |
| `System32\nvidia-smi.exe` / `nvml.dll` | 存在（2026-07-23，第三方孤儿文件） |
| PnP 状态 | OK，驱动版本 32.0.16.1088 |

据此一度推断"显示驱动未安装"。**该推断被二轮实测推翻。**

#### 二轮诊断（运行时级，以此为准）

```bash
python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
# True  NVIDIA GeForce RTX 2060
# VRAM 12.00 GB，可用 10.95 GB，SM 34，matmul 通过
```

**真实结论**：显示驱动与 CUDA 通路**均工作正常**。
只是 **NVSMI 管理工具链缺失/版本不匹配**（System32 下的 `nvidia-smi.exe` + `nvml.dll` 是第三方软件放入的孤儿文件，与运行中的驱动版本对不上，导致 NVML 初始化失败）。

至于 `nv_dispi.inf` 与 `nvlddmkm.sys` 不在常规路径下——现代 NVIDIA 驱动（DCH / NVIDIA App 体系）已不将这两个文件部署在 System32\drivers 与标准 DriverStore 命名下，因此"文件不存在"不构成驱动缺失的证据。**文件系统探测不是可靠的驱动状态判据，运行时探测才是。**

#### 处置（可选，非必需）

NVSMI 缺失只影响**显存监控与温度观察**，不影响任何计算任务。若要恢复监控能力：

1. 从 NVIDIA 官网下载完整驱动包，选择"自定义 → 清洁安装"，NVSMI 会被重新部署到
   `C:\Program Files\NVIDIA Corporation\NVSMI\`
2. 或直接单独提取 NVSMI 组件，不必动驱动

**优先级：低。** 建议在 M1 垂直切片跑通之后再处理，或干脆不处理——流水线内部用 `torch.cuda.mem_get_info()` 自行汇报显存即可，不依赖 nvidia-smi。

> 教训记录：本轮走了弯路。判断 GPU 可用性应**优先跑 `torch.cuda.is_available()`**，而不是先验文件系统再下结论。文件缺失可能只是部署方式变了。

### 1.3 显存真相：报告值虚高，实测上限 6.00 GB

`torch.cuda.mem_get_info()` 在本机报告 **12.00 GB 总显存 / 10.95 GB 空闲**，但这是**误导性数据**。

用 0.25 GB 小块逐步累积实测（排除碎片因素）：

```
实际可用上限  :  6.00 GB
分配后报告空闲:  4.95 GB
合计         : 10.95 GB   ← 与报告总量吻合
不可分配部分  :  4.95 GB   ← 真实存在但 CUDA 拿不到
```

小块（0.25 GB）与中块（1.0 GB）测试结果一致，均为 6.00 GB，且启用 `expandable_segments:True` 后无变化——**这是硬上限，不是碎片问题**。成因是 Windows WDDM 驱动模型下报告值包含不可锁定的共享显存部分。

**各模型可行性判定（上限 6.00 GB）**：

| 模型 | 权重 | 激活 | 合计 | 判定 |
|---|---|---|---|---|
| SDXL fp16 全量 | 6.95 | 2.5 | **9.45** | ❌ 不可行 |
| SDXL fp8 / GGUF Q5 | 4.20 | 2.5 | **6.70** | ❌ 不可行（超 0.70） |
| SDXL + CPU offload | 6.95 | 0.5 | **7.45** | ❌ 不可行 |
| SD 1.5 + 古风 LoRA | 2.10 | 1.5 | 3.60 | ✅ 可运行（画质较差） |
| **RealESRGAN x2（tile 512）** | 0.07 | 1.2 | 1.27 | ✅ 可运行 |
| **Qwen3-TTS 0.6B fp16** | 1.20 | 1.0 | 2.20 | ✅ 可运行 |
| **Qwen3-TTS 1.7B fp16** | 3.40 | 1.5 | 4.90 | ✅ 可运行 |
| Kokoro-82M | 0.08 | — | 0.08 | ✅ CPU 运行，不占显存 |
| Wan2.2 I2V 1.3B | 2.60 | 3.0 | 5.60 | ⚠️ 临界，需实测 |

**结论**：

1. **本地 SDXL 确认不可行**——fp16 权重 6.95 GB 就已超过 6.00 GB 上限，连加载都做不到，与激活优化无关。这印证了生图走 gpt-image-2 的决策是**正确的**，不是妥协。
2. **超分、TTS 完全不受影响**——RealESRGAN（tile 模式）与 Qwen3-TTS 都在预算内，本地运行安全。
3. **Wan2.2 I2V 1.3B 处于临界区**（5.60 / 6.00 GB）——路线 A+ 仍有希望，但需实测确认。
4. **判定规则**：任何新模型上线前，先跑 `bin/probe_sdxl_vram.py`，以实测上限为准，**不得采信 `mem_get_info()` 报告值**。

### 1.3 依赖清单与版本锁

Python 版本：**已实测 Python 3.13 可用**，无需退回 3.11。
`torch 2.14.0+cu126` 与 `torchaudio 2.11.0+cu126` 均提供 cp313 wheel，安装与 CUDA 调用全部正常。环境就绪，此前的版本顾虑解除。

已就绪环境（venv: `C:\Users\coumoo\.workbuddy\binaries\python\envs\hv`）：

```
Python 3.13.14
torch      2.14.0+cu126      CUDA available: True
torchaudio 2.11.0+cu126      MMS_FA 强制对齐可用
numpy      (已装)
FFmpeg     8.1.1             libwhisper / libass / NVENC 已编译
```

```
# 运行时
Python 3.11.x  (venv: D:\hv\.venv)
FFmpeg 8.1.1   (已装，路径需加入 PATH)

# TTS
kokoro>=0.9        + misaki[zh]     # Kokoro-82M，CPU
onnxruntime-gpu    (GPU 修复后) / onnxruntime (CPU 降级)
qwen-tts / CosyVoice 依赖          # Qwen3-TTS，GPU

# 强制对齐（字幕）
torchaudio>=2.6                     # forced_align API（主选）
# 或 whisper.cpp / FFmpeg libwhisper # fallback

# 生图（GPU 修复后本地跑）
diffusers + transformers + accelerate
# CPU / 降级期：云端 API client

# 一致性抽检
insightface (可选，用于角色人脸相似度闸门)

# 工程
pydantic>=2    # episode.json schema 校验
typer          # CLI
orjson         # 快速序列化
```

---

## 2. 目录结构

```
D:\hv\
├── bin\                        # 可执行入口
├── config\
│   ├── style.yaml              # 全局风格规范（画风/音色/节奏/字幕样式）
│   ├── models.yaml             # 模型路径与降级链定义
│   └── voices.yaml             # 音色注册与参数
├── assets\                     # 跨集复用资产库（核心成本控制手段）
│   ├── characters\             # 角色定妆照与锚定参考
│   │   └── liuche\anchor.png
│   ├── scenes\                 # 通用场景：宫殿/草原/战场/朝堂/集市
│   ├── bgm\                    # 古风 BGM，按情绪分目录
│   ├── sfx\                    # 马蹄/篝火/刀剑/风声
│   └── fonts\                  # 思源宋体等
├── episodes\
│   └── hw-002\                 # 汉武大帝第 2 集
│       ├── episode.json        # ★ 唯一真相源
│       ├── build\              # 中间产物（可清空重建）
│       │   ├── audio\          # 整段 + 按句切片
│       │   ├── frames\         # 生图输出
│       │   ├── motion\         # 运镜后的视频段
│       │   ├── subs\           # ASS 字幕
│       │   └── concat.txt
│       └── out\hw-002.mp4
├── cache\                      # 内容寻址缓存（跨集复用）
│   ├── tts\<hash>.wav
│   ├── image\<hash>.png
│   └── motion\<hash>.mp4
├── logs\
└── state\                      # 断点续跑状态
```

**设计原则**：`episodes/` 下的一切都可以从 `episode.json` + `assets/` + `cache/` 完整重建。`build/` 是纯中间态，可随时 `rm -rf` 重跑。

---

## 3. `episode.json` — 唯一真相源

### 3.1 设计原则

1. **单一真相源**：分镜、提示词、音频、画面、字幕全部由它派生，任何环节不得持有独立状态
2. **幂等**：同一份 `episode.json` 跑 N 次，结果完全一致（seed 固定 + 缓存命中）
3. **可断点续跑**：每个 shot 独立记录 stage 状态，中断后从断点继续
4. **可单镜头重生成**：改一个镜头的提示词，只重建这一个镜头，不重跑全片
5. **人工可读写**：脚本阶段由 LLM 生成草稿，人工直接在 JSON 上改文案

### 3.2 Schema

```jsonc
{
  "schema_version": "1.0",
  "meta": {
    "id": "hw-002",
    "series": "汉武大帝：刘彻简史",
    "episode": 2,
    "title": "张骞出使西域与马邑之谋",
    "platform": "douyin",
    "aspect_ratio": "9:16",
    "width": 1080, "height": 1920,
    "fps": 25,
    "target_duration_sec": 240
  },

  // 继承全局风格，可覆盖
  "style": {
    "ref": "config/style.yaml#ancient-realistic",
    "overrides": {}
  },

  // 配音配置
  "voice": {
    "model": "qwen3-tts",           // 主选
    "fallback": "kokoro-82m",       // 降级
    "speaker": "zm_yunxi",          // 沉稳男声
    "speed": 1.0,
    "emotion": "steady",
    "ref_audio": null               // 零样本克隆参考（可选）
  },

  // 角色表：一致性锚定的基础
  "cast": [
    {
      "id": "liuche",
      "name": "刘彻",
      "anchor_ref": "assets/characters/liuche/anchor.png",
      "trigger": "liuche man, han dynasty emperor, black crown, dark red robe",
      "seed_pool": [1001, 1002, 1003],
      "ip_adapter_weight": 0.7
    }
  ],

  // 场景资产引用（跨集复用）
  "scenes": [
    { "id": "palace",   "ref": "assets/scenes/palace_hall.png",  "reusable": true },
    { "id": "grassland","ref": "assets/scenes/grassland.png",    "reusable": true }
  ],

  // ★ 分镜序列 —— 流水线的驱动核心
  "shots": [
    {
      "shot_id": "S001",
      "narration": "公元前138年，汉武帝刘彻招募使者，出使西域寻找大月氏。",
      "visual_prompt": "liuche man sitting on throne in grand han palace hall, "
                       "ancient chinese architecture, cinematic lighting, warm tone, "
                       "ancient realistic painting, no modern elements",
      "negative_prompt": "modern, text, watermark, lowres, extra limbs",
      "shot_type": "wide",          // wide / medium / close_up / insert
      "motion": {
        "type": "push_in",          // push_in / pull_out / pan_l / pan_r / tilt_u / tilt_d / drift / static
        "intensity": 0.12,          // 缩放或位移幅度
        "easing": "ease_in_out"
      },
      "cast_refs": ["liuche"],
      "scene_ref": "palace",
      "keywords": ["大月氏", "西域"],   // 需要在画面上强化显示的历史名词
      "sfx": null,
      "bgm_intensity": 0.4,

      // ↓ 以下字段由流水线回填，人工不写
      "state": {
        "tts": "pending", "image": "pending",
        "motion": "pending", "review": "pending"
      },
      "derived": {
        "audio_start_ms": null,     // 由强制对齐回填
        "audio_end_ms": null,
        "duration_ms": null,
        "frames": null,
        "image_path": null,
        "motion_path": null,
        "face_sim": null            // 一致性抽检分数
      }
    }
  ]
}
```

### 3.3 状态机

每个 shot 独立推进，任一 shot 失败不影响其他 shot：

```
pending → tts_done → image_done → motion_done → review → composed
                                                    ↓
                                                rejected（人工打回，仅重跑该 shot）
```

状态写入 `state/` 目录下的 `hw-002.state.json`，CLI 启动时读取并跳过已完成节点。

---

## 4. 流水线环节设计

### 4.0 总览

```
episode.json
  ├─[1] script   LLM 生成分镜草稿 + 人工事实校验    → episode.json
  ├─[2] tts      整段旁白 → full.wav（韵律连贯）    → build/audio/full.wav
  ├─[3] align    full.wav + 文本 → 句级/字级时间戳   → build/audio/segments.json
  ├─[4] image    每 shot 的 visual_prompt → 图       → build/frames/S001.png
  ├─[5] motion   图 + motion 参数 + 帧数 → 视频段    → build/motion/S001.mp4
  ├─[6] subtitle 时间戳 → ASS 卡拉 OK 字幕          → build/subs/main.ass
  └─[7] compose  视频段 + full.wav + ASS + BGM      → out/hw-002.mp4
```

### 4.1 stage `script`

**输入**：选题（如"张骞出使西域"）+ `config/style.yaml`
**输出**：`episode.json` 草稿

要点：
- LLM 输出**必须**通过 pydantic schema 校验后才落盘，杜绝字段缺失
- 分镜粒度：**一句解说 = 一个镜头**，句长控制在 8–25 字（对应 2–4 秒）
- 每个 shot 必须同时产出 `narration`（听觉）和 `visual_prompt`（视觉），且二者语义对齐
- **人工闸门 G1**（见 §11）：历史事实校验

### 4.2 stage `tts` —— 整段生成，不做逐句拼接

**关键设计**：绝不能逐句 TTS 再拼接。逐句生成时每句都是独立韵律起点，拼接后听感是"机器人念清单"。

正确做法：**整段旁白一次性合成**，得到韵律连贯的 `full.wav`。句边界的切分交给下一个 stage 用强制对齐解决。

```bash
# Kokoro（CPU 打底，210x 实时，240 秒音频约 1.5 秒生成）
python -m hv.tts --engine kokoro --speaker zm_yunxi --speed 1.0 \
  --text episode_narration.txt --out build/audio/full.wav

# Qwen3-TTS（GPU 出片，中文韵律最优）
python -m hv.tts --engine qwen3 --speaker zm_yunxi --out build/audio/full.wav
```

降级链：`qwen3(GPU)` → `qwen3(fp16 低显存)` → `kokoro(CPU)` → 报错并保留占位静音

### 4.3 stage `align` —— 整段音频的句边界定位

这一 stage 同时服务两个目的：**切镜头** 和 **出字幕**。一次计算，两处复用。

**主选方案：torchaudio forced_align（字符级精度）**

torchaudio 提供基于 CTC 的强制对齐 API，配合 `torchaudio.pipelines.MMS_FA` 模型（支持中文），输出**字符级**时间戳，精度远超 whisper 的句级输出，且 CPU 可跑、速度快。

```python
import torch, torchaudio
from torchaudio.functional import forced_align
from torchaudio.pipelines import MMS_FA as bundle

model, tokenizer, aligner = bundle.get_model(), bundle.get_tokenizer(), bundle.get_aligner()
emission, _ = model(waveform)          # 声学特征
tokens = tokenizer(text)               # 文本转 token
# token_spans: 每个 token 的 [start_frame, end_frame, score]
token_spans = aligner(emission[0], tokenizer.tokens)
```

**Fallback：FFmpeg libwhisper（本机已编译）**

```bash
ffmpeg -i build/audio/full.wav \
  -vf "whisper=model=ggml-base.bin:language=zh:queue=3:destination=build/subs/raw.srt:format=srt" \
  -f null -
```

精度是句级而非字级，但零部署成本，作为降级路径足够。

**输出** `build/audio/segments.json`：

```jsonc
[
  { "shot_id": "S001", "start_ms": 0,     "end_ms": 3820, "confidence": 0.94 },
  { "shot_id": "S002", "start_ms": 3820,  "end_ms": 7140, "confidence": 0.91 }
]
```

同时回填每个 shot 的 `derived.audio_start_ms` / `audio_end_ms` / `duration_ms` / `frames`。

### 4.4 stage `image`

**输入**：shot 的 `visual_prompt` + `cast_refs`（角色参考包）+ `scene_ref`
**输出**：`build/frames/S001.png` → 超分后 `build/frames/S001_2x.png`

**流程**：gpt-image-2 生成 → RealESRGAN 2× 超分 → 入库

```
参考包 image[] ─┐
                ├─ gpt-image-2 /v1/images/edits ──→ 1024×1792
scene_prompt ───┘                                        │
                                                         ↓
                                        RealESRGAN 2× (本地 CUDA)
                                                         ↓
                                              2048×3584（运镜裁切源）
```

**尺寸约束（硬性）**：

`1024×1792` 小于目标输出 `1080×1920`（宽少 56px、高少 128px），
所以 **RealESRGAN 超分是必选项而非可选优化**——不做超分，成片达不到 1080p。

超分后 `2048×3584` 裁 `1080×1920`，余量 **水平 484px / 垂直 832px**，
配合保守运镜模式（最多用掉 60% 平移余量）完全够用，且生图成本是最低档。

**缓存键**（无 seed，与本地方案不同）：

```
sha256(prompt + size + quality + model_id + ref_pack_hash + style_version)
```

> 因为 gpt-image-2 不暴露 seed，缓存只能命中"完全相同的调用"。
> 这意味着**改一个字就得重新付费生成**，无法像本地 SDXL 那样靠固定种子微调。
> 实践建议：M1 迭代阶段用 `quality=low`（约 $0.006/张）放开试错，
> 提示词定稿后再用 `high` 重制入选镜头。

**降级链**：见 §10，主路径 gpt-image-2，本地 SDXL 作为兜底。

### 4.5 stage `motion` —— 混合运镜

**不再是纯 Ken Burns，改为按镜头类型分流**：

```
shot.hero == true  → I2V API（真动态，图生视频）
shot.hero == false → 本地 Ken Burns（FFmpeg，零成本）
```

分流依据是 `episode.json` 中每个 shot 的 `hero` 标记，由脚本阶段人工或规则判定。

**为什么不全上 I2V** —— 两个理由：

1. **成本**：全镜头 I2V 月成本 3900–8100 元，英雄镜头策略压到 2500–3800 元
2. **一致性风险（更关键）**：I2V 过程中**人脸与手部是最容易崩的部位**。一个 gpt-image-2 精心锁定的角色，过一遍 I2V 可能又变了。局部使用反而比全量使用更安全。

**I2V 后的校验**：生成结果与源图做人脸相似度比对，低于 0.60 自动回退 Ken Burns（见 `models.yaml` 的 `on_face_drift`）。

详见 §6.7。

### 4.6 stage `subtitle`

由 `align` 阶段得到的字符级时间戳生成 ASS 字幕，支持逐字高亮：

```ass
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Outline, Shadow, Alignment, MarginV, Encoding
Style: Main,思源宋体,72,&H00FFFFFF,&H00000000,&H80000000,0,4,1,2,320,1
Style: KW,思源宋体,80,&H0000D7FF,&H00000000,&H80000000,0,5,1,2,320,1
```

- 主字幕：白字黑边，底部安全区上方 320px
- `keywords` 中的历史名词（如"大月氏""匈奴"）用 `KW` 样式——金色放大，配合旁白做视觉锤
- 卡拉 OK 逐字高亮：`{\k<duration>}` 标签，duration 单位为厘秒

### 4.7 stage `compose`

```bash
# 1. 视频段拼接（demuxer 方式，无重编码，秒级完成）
ffmpeg -f concat -safe 0 -i build/concat.txt -c copy build/video_raw.mp4

# 2. 合成音画 + 字幕 + BGM
ffmpeg -i build/video_raw.mp4 -i build/audio/full.wav -i assets/bgm/epic_01.mp3 \
  -filter_complex "\
    [1:a]volume=1.0[a1]; \
    [2:a]volume=0.18,afade=t=out:st=235:d=5[a2]; \
    [a1][a2]amix=inputs=2:duration=first:dropout_transition=0[aout]; \
    [0:v]subtitles=build/subs/main.ass:fontsdir=assets/fonts[vout]" \
  -map "[vout]" -map "[aout]" \
  -c:v h264_nvenc -preset p4 -rc vbr -cq 20 \
  -c:a aac -b:a 192k -pix_fmt yuv420p -r 25 -movflags +faststart \
  out/hw-002.mp4
```

- 编码器降级：`h264_nvenc` → `libx264 -preset medium -crf 20`
- BGM 必须 `afade` 淡出，否则尾部硬切非常廉价
- `-movflags +faststart` 必须加，影响首帧加载

---

## 5. 时序契约（核心）

### 5.1 帧数换算

```
frames = round(duration_ms / 1000 × fps) + pad_frames
pad_frames = round(pad_ms / 1000 × fps)
```

**呼吸间隙（pad）设计**：镜头长度 = 音频时长 + 250ms。音频从镜头内 100ms 处开始，尾部留 150ms。

```
|<---------- 镜头 S001 (frames) ---------->|
[100ms 静默][===== 音频 3.82s =====][150ms 静默]
```

不做这个 padding 的话，镜头之间是硬切硬接，运镜动作每次都被"斩断"，观感非常生硬。有了 250ms 喘息，运镜有起止，切换才顺。

### 5.2 边界规则

- 镜头边界 = `align` 得到的句边界，**不接受任何手工设定的固定秒数**
- 每个镜头的音频切片从 `full.wav` 中按 `[start_ms - 100, end_ms + 150]` 裁切
- 相邻镜头之间**不做交叉溶解**（xfade 会破坏音画同步），改用音频侧 30ms 微交叉淡化

### 5.3 长句处理

单句音频 > 6 秒 → 强制在最近的逗号/顿号词边界处拆成两个镜头。画面静止超过 6 秒，完播率会断崖下跌。

### 5.4 校验断言

合成前必须全部通过，任一失败即中止：

| 断言 | 容差 |
|---|---|
| `Σ(镜头时长) ≈ full.wav 总时长` | ± 100 ms |
| 每个镜头帧数 ≥ 40（1.6s） | 下限保护 |
| 每个镜头帧数 ≤ 200（8s） | 上限保护 |
| 字幕首帧时间戳 ≥ 0 | 硬约束 |
| 视频流总帧数 = 最后一镜头结束帧 | 严格相等 |

---

## 6. Ken Burns 运镜设计（路线 A 的质量命门）

选了路线 A，成品好不好看**几乎全部压在这里**。静态图 + 匀速缩放 = 廉价 PPT；静态图 + 有呼吸感的运镜 + 叠加层 = 能起量的视频。

### 6.1 廉价感的三个来源

1. **匀速缩放** — 人眼对匀速运动最敏感，会立刻识别出"这是机器做的"。必须缓入缓出。
2. **所有镜头同方向** — 连续 5 个推镜会让观众眩晕并产生催眠感。
3. **无叠加层** — 纯图片推拉缺少景深、粒子、光影，画面"发死"。

### 6.2 运镜类型库

| type | 实现 | 适用 |
|---|---|---|
| `push_in` | 缩放 1.00 → 1.12 | 强调、情绪推进 |
| `pull_out` | 缩放 1.12 → 1.00 | 场景介绍、收尾 |
| `pan_l` / `pan_r` | 水平位移 | 长卷、全景、行军 |
| `tilt_u` / `tilt_d` | 垂直位移 | 建筑、人物全身 |
| `drift` | 极小幅位移 + 极小缩放（1.00→1.03） | 对话、静态叙事 |
| `static` | 无运动，仅粒子层 | 关键词卡片、地图 |

### 6.3 FFmpeg 实现：分两类，不要一刀切

**平移/摇镜（pan/tilt）→ 用 `crop`，零抖动**

`crop` 的 x/y 支持 `t` 时间表达式，输出尺寸固定，无重采样抖动：

```bash
D=3.82
ffmpeg -loop 1 -framerate 25 -t $D -i build/frames/S001.png \
  -vf "scale=1620:2880:flags=lanczos,
       crop=w=1080:h=1920:x='(iw-ow)*0.5+120*(t/$D)':y='(ih-oh)*0.5',
       scale=1080:1920:flags=lanczos,setsar=1" \
  -c:v libx264 -crf 18 -pix_fmt yuv420p build/motion/S001.mp4
```

**推拉（push/pull）→ 用 `zoompan`，但必须超采样压制抖动**

`zoompan` 的抖动是出了名的（整数像素采样导致）。解法是**先在 2× 分辨率上做缩放，再 lanczos 降采样**：

```bash
D=3.82; FRAMES=96
ffmpeg -loop 1 -framerate 25 -t $D -i build/frames/S001.png \
  -vf "scale=3240:5760:flags=lanczos,
       zoompan=z='1.0+0.12*(1-cos(3.1415926*on/$FRAMES))/2'
              :d=$FRAMES:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'
              :s=3240x5760:fps=25,
       scale=1080:1920:flags=lanczos,setsar=1" \
  -c:v libx264 -crf 18 -pix_fmt yuv420p build/motion/S001.mp4
```

注意 `z` 表达式用的是 `(1-cos(πx))/2` —— **这就是缓入缓出**，取代常见的线性 `min(zoom+0.0015,1.5)`。这一行是整个运镜质感的差别所在。

**组合运镜**（推 + 移）：`zoompan` 输出大尺寸 → `crop` 做位移 → `scale` 降采样。

### 6.4 抗抖动要点

| 措施 | 说明 |
|---|---|
| 输入图放大到 2× | 1620×2880 → 3240×5760，缩放采样密度翻倍 |
| 降采样用 `lanczos` | 不要用默认 bilinear，细节损失明显 |
| 帧率与 `-t` 严格一致 | 抖动常源于 `d` 参数与实际帧数不匹配 |
| 输出 `setsar=1` | 防止 SAR 不一致导致的拉伸 |

### 6.5 相邻镜头编排规则（自动化）

生成 `concat.txt` 前跑一遍编排器，违反规则自动调整：

1. **禁止连续两个同 type 的 `push_in`**
2. **推拉交替**：push_in 之后优先 pull_out 或 pan
3. **幅度递减**：高潮段落（关键词出现时）用大幅 push_in，叙事段用 drift
4. **首尾约束**：第 1 镜头用 `pull_out`（建立场景），最后 1 镜头用 `pull_out`（收束）

### 6.6 叠加增强层（低成本高回报）

```bash
# 尘埃/光尘粒子（古风场景通用）
-vf "...,noise=alls=6:allf=t+u,format=yuv420p"

# 光影闪烁（篝火、烛光场景）
-vf "...,eq=brightness='0.02*sin(2*PI*t*1.5)'"

# 边缘暗角（提升电影感）
-vf "...,vignette=PI/5"
```

**关键词卡片**：`keywords` 字段中的历史名词，在对应镜头内做独立一层——不放在主字幕里，而是在画面上方 1/3 处以 `KW` 样式弹出，带 0.15s 缩放弹入。这是原视频"用醒目艺术字强化历史名词"的做法，对记忆点贡献很大。

### 6.7 英雄镜头策略（I2V 分流规则）

**判定标准**：一个镜头是否需要 I2V，看画面里有没有**实体运动**。

| 适合 I2V（hero: true） | 适合 Ken Burns（hero: false） |
|---|---|
| 骑兵冲锋、战场厮杀 | 朝堂对话、君臣奏对 |
| 张骞在草原跋涉 | 地图示意、疆域变迁 |
| 霍去病挥剑、旌旗招展 | 人物特写、静态肖像 |
| 军队行军、旌旗猎猎 | 宫殿空镜、器物特写 |
| 篝火、烟尘、飘雪 | 文字卡片、时间轴 |

**数量控制**：每集 8–15 个。超过 15 个成本显著上升，少于 8 个则"动画感"不足。

**为什么静态镜头用 Ken Burns 反而更好**：这些镜头本就是静态构图，加缓推缓拉更有电影感；用 I2V 反而会引入无意义的面部抖动和肢体畸变。**I2V 不是万能增强，它是有副作用的。**

**成本对齐**：I2V 生成时长需匹配音频时长，避免生成 5 秒却只裁 2 秒的浪费。
`max_waste_ratio: 0.35` —— 浪费超过 35% 则改用更短的生成档位。

**降级**：I2V 失败或人脸漂移 → 自动回退 Ken Burns，绝不阻塞流水线。

---

## 7. 角色一致性方案（已按 gpt-image-2 重写）

生图改走 gpt-image-2 后，原基于 SDXL + IP-Adapter 的方案**整章作废**。两者机制根本不同：

| 能力 | SDXL + IP-Adapter | gpt-image-2 |
|---|---|---|
| seed 固定复现 | ✅ 有 | ❌ **无 seed** |
| 持久 character id | 可用 LoRA 训练 | ❌ 无 |
| 一致性手段 | IP-Adapter 权重 + denoise | **Character Lock + 最多 16 张参考图** |
| 参数敏感度 | weight / denoise 需调 | 参考图数量与质量 |

因此一致性从"参数调优"变成了**"反复喂同一组已批准的像素"**。

### 7.1 角色参考包（Reference Pack）

**第一步：生成定妆照矩阵（每角色 12 张，一次性）**

维度必须拉开。实证结论是 3 张可用、14 张明显更好，取 12 张作为性价比折中：

- **角度**：正面 / 三分之三侧 / 全侧（正面帧的身份信号最干净，侧脸过多反而干扰）
- **景别**：特写 / 半身 / 全身
- **光线**：正面光 / 侧光 / 逆光
- **表情**：**统一中性**——这是关键，见下方"表情污染"说明
- **尺寸**：参考图长边缩到约 1024px 再上传，更快更省，对身份识别无可见影响

**第二步：人工挑选并锁定 master**

从 12 张中挑 1 张作为 `master.png`，其余作为参考包成员。
**不要选最有戏剧性的那张，要选最容易稳定复现的那张。**

**第三步：每镜头调用时喂入参考包**

走 `/v1/images/edits`，`image[]` 传入参考包，prompt 按六段结构拼装：

```
[identity_anchor]   见 config/style.yaml 模板
[scene_prompt]      场景、动作、光线、机位
[expression_spec]   必须显式写
[pose_spec]         必须显式写
[style_suffix]
[negative_clause]
```

**⚠️ 最大的坑：表情污染**

参考图会**同时传递表情和姿态**，不只是身份。若定妆照都是微笑，生成的每个镜头都会微笑——包括该悲壮的场景，观感极其诡异。

必须在 prompt 中显式覆盖，每个镜头都要写：

```
expression: grim and resolute, jaw set, brow furrowed
pose: standing three-quarter angle, hand on sword hilt
```

同时不要过度指定强度（"serious, intense, lips closed" 会读起来像在生气），追求自然：
"a calm composed expression, soft brow, not stern, not posed"。

**第四步：自动抽检闸门（G2）**

用 InsightFace 提取人脸 embedding，与 master 计算余弦相似度：

| 相似度 | 处置 |
|---|---|
| ≥ 0.65 | 通过 |
| 0.50 – 0.65 | 告警，记录日志，进入人工抽检队列 |
| < 0.50 | **判定变脸，自动重生成**（最多 3 次，每次微调 prompt 措辞） |

> 注意：因为没有 seed，"重生成"无法靠换种子保证不同，
> 只能靠微调 prompt 措辞或调整参考图子集。这是无 seed 的固有代价。

这道闸门把"一致性"从玄学变成可量化的 CI 检查项。

### 7.2 场景资产复用

宫殿、草原、战场、朝堂、集市是跨集通用的。`assets/scenes/` 下的图直接作为参考图之一传入 `image[]`，**不重新生成**，只改提示词换光线与时间。这是成本控制的第二个杠杆（第一个是缓存）。

---

## 8. 字幕系统

见 §4.3（对齐）与 §4.6（渲染）。补三个实践细节：

**分行规则**：单行不超过 15 个汉字，超出按标点断行。竖屏 1080 宽下，72px 字号一行约 15 字，再多就要缩字号。

**停留时间**：单条字幕最短 0.8s，最长 4s。太短看不清，太长会盖住两句话导致视觉滞留。

**安全区**：底部留 320px（避开抖音的点赞/评论/分享栏），顶部留 200px（避开账号信息与关注按钮）。这个数值不能拍脑袋，抖音的 UI 遮挡区是实测约 280–350px。

---

## 9. 缓存、幂等与断点续跑

### 9.1 缓存键设计

| stage | 键内容 |
|---|---|
| `tts` | `sha256(text + engine + speaker + speed + model_version)` |
| `align` | `sha256(audio_sha + text + aligner_version)` |
| `image` | `sha256(prompt + negative + seed + model_id + lora_hash + anchor_sha)` |
| `motion` | `sha256(image_sha + motion_type + intensity + easing + frames + fps)` |
| `subtitle` | `sha256(segments_sha + style_sha)` |

缓存命中直接软链接到 `build/`，不重复计算。**改一句文案只重建那一个镜头**，这是批量生产能跑起来的前提。

### 9.2 断点续跑

`state/hw-002.state.json` 记录每个 shot 的 stage 完成度。CLI 启动扫描状态，跳过已完成节点：

```bash
# ⚠️ 以下 `hv run` 命令已废弃（2026-09-05 双轨编排器 managers/pipeline.py 删除）
python -m hv run --episode hw-002              # 从断点继续
python -m hv run --episode hw-002 --force tts  # 强制重跑指定 stage
python -m hv run --episode hw-002 --only S007  # 只重建第 7 个镜头

# 现行命令
python -m app.cli episode produce hw-002             # 全链重跑（可 --stage tts 等）
python -m app.cli episode produce hw-002 --stage tts # 只重跑 TTS
python -m app.cli episode reshot hw-002 --key S007   # 单镜重建（删图→重生图→重合成）
```

---

## 10. 降级链

| 环节 | 主选 | 降级 1 | 降级 2 | 兜底 |
|---|---|---|---|---|
| TTS | Qwen3-TTS (GPU) | Qwen3 fp16 低显存 | Kokoro-82M (CPU) | 静音占位 + 告警 |
| 对齐 | torchaudio MMS_FA | FFmpeg libwhisper | 按字数线性估算 | 无字幕 |
| 生图 | SDXL (GPU) | 云端 API | 场景库最相似图 | 纯色卡 + 关键词 |
| 运镜 | crop / zoompan 2×超采样 | zoompan 1× | 静态图定格 | 静态图 |
| 合成 | h264_nvenc | libx264 medium | libx264 veryfast | — |

**降级必须记录日志并醒目标注**，不能静默降级——静默降级会导致成片质量突然变差而无人察觉。

---

## 11. 人工闸门

自动化能省掉 90% 的工时，但这两道闸门**不能省**：

### G1：历史事实校验（脚本阶段，before TTS）

- 校验内容：年代、人名、地名、事件因果、官职称谓
- 历史号翻车一次就是账号级事故，这是唯一值得花时间的环节
- 建议做法：LLM 生成时同时输出**引用来源标注**（如"《史记·大宛列传》"），人工按标注抽查
- 单集预算：5–10 分钟

### G2：视觉抽检（合成前，before compose）

- 逐镜头快速过一遍图，重点看：变脸、肢体畸形、现代元素穿帮（电线杆、水泥路、简体字）
- 60 个镜头约 3 分钟
- 自动化的部分：人脸相似度（§7.1 第三步）、现代元素可用 CLIP 零样本分类初筛

---

## 12. 产能测算与并行策略

### 12.1 单集耗时拆解（4 分钟 / 约 60 镜头）

| 环节 | 主路径（gpt-image-2 API + 本地超分） | 兜底（本地 SDXL） |
|---|---|---|
| 脚本生成 + G1 人工校验 | 10 min | 10 min |
| TTS（240s 音频） | 10 s（Qwen3 fp16） | 2 s（Kokoro） |
| 强制对齐 | 30 s | 40 s |
| 生图 60 张 | 3–5 min（API，并发 6） | 4 min（12GB，并发 4–6） |
| **超分 60 张** | **3 min**（RealESRGAN，本地 CUDA） | 不需要 |
| 运镜渲染 60 段 | 2 min | 2 min |
| 合成 | 1 min（h264_nvenc） | 1 min |
| G2 视觉抽检 | 3 min | 3 min |
| **合计** | **约 22 min** | 约 20 min |

时间上两条路线接近，差别在**钱**：

| 方案 | 单集生图成本 | 备注 |
|---|---|---|
| gpt-image-2 low（迭代） | 约 ¥2.6 | 60 张 × $0.006，可放开试错 |
| gpt-image-2 high（成片） | 约 ¥60–90 | 60 张，随实际尺寸与质量档浮动 |
| **low 迭代 + high 成片** | **约 ¥70–95** | 迭代 3 轮低成本，仅入选镜头用 high 重制 |
| 本地 SDXL（兜底） | 约 ¥0 | 仅电费，但需先下载约 7GB 模型 |

### 12.1.1 加入 I2V 后的完整成本（英雄镜头策略）

| 项目 | 单集 | 月成本（日更 30 集） |
|---|---|---|
| 生图（low 迭代 + high 成片，60 张） | ¥70–95 | ¥2,100–2,850 |
| I2V（12 个英雄镜头 × ¥1–3） | ¥12–36 | ¥360–1,080 |
| 超分 / Ken Burns / 合成（本地） | ≈¥0 | ≈¥1（电费） |
| **合计** | **¥82–131** | **¥2,460–3,930** |

对照全镜头 I2V 方案（60 段 × ¥1–3 = ¥60–180/集），月成本会到 **¥3,900–8,100**。
英雄镜头策略节省 60–80%，且降低了 I2V 崩脸风险。

**成本控制的四个杠杆**：
1. 英雄镜头策略（§6.7）—— 视频成本降 60–80%
2. 场景资产跨集复用（§7.2）
3. 内容寻址缓存（§9.1）
4. low 档迭代筛选后再 high 重制

### 12.2 真实上限

- 计算侧可压到 25 min/集，理论日产 15+ 集
- **但 G1 事实校验（10 min）+ G2 视觉抽检（3 min）是硬瓶颈**，且不可并行化给机器
- 单人实际日产上限：**6–8 集/天**
- 若要突破，只能靠建立"事实库"（已校验的历史知识点沉淀复用）来压缩 G1

### 12.3 并行策略

- **集间并行**：多集同时跑，生图与合成阶段错峰
- **集内并行**：生图 60 张可 4–6 路并发（GPU 显存 6GB 需限制并发数，API 路则看配额）
- 运镜渲染是 CPU 密集，可用进程池打满 20 线程

---

## 13. 已知风险与坑

| 风险 | 影响 | 对策 |
|---|---|---|
| `zoompan` 抖动 | 画面廉价 | 2× 超采样 + lanczos 降采样（§6.4） |
| 逐句 TTS 拼接导致韵律断裂 | 听感机械 | 整段生成 + 强制对齐切片（§4.2） |
| 中文 TTS 多音字错误（如"月氏"读 zhī 非 shì） | 硬伤，评论区立刻被指出 | 建立**专有名词读音替换表**，TTS 前做文本预处理 |
| **显存报告值虚高** | 按 12GB 选型必然 OOM | **实测单进程上限仅 6.00 GB**，报告空闲 10.95 GB 中有 4.95 GB 实际不可分配（Windows WDDM 驱动模型所致）。**所有模型选型必须以 `bin/probe_sdxl_vram.py` 实测值为准，不得采信 `mem_get_info()` 报告值** |
| **本地 SDXL 跑不动** | 降级路径失效 | fp16 全量需 9.45 GB > 6.00 GB 上限，**确认不可行**。降级链已改为：fp8 量化版（约 5.7 GB，临界需实测）→ SD 1.5 + 古风 LoRA（3.6 GB，安全但画质差） |
| **中转不支持 edits 端点** | **角色一致性方案失效** | 上线前必须跑 `bin/probe_image_api.py`（P2/P5 项）。若不支持则降级为纯提示词描述 + 人工全检 |
| **gpt-image-2 无 seed** | 改一个字就要重新付费 | 迭代阶段强制用 `quality=low`（$0.006/张），提示词定稿后再 high 重制 |
| **表情污染** | 悲壮场景里的角色在微笑 | 定妆照统一中性表情；每个镜头 prompt 必须显式写 `expression_spec` + `pose_spec`（§7.1） |
| **超分环节被跳过** | 成片达不到 1080p | `requires_upscale: true` 硬标记；合成前断言帧尺寸 ≥ 1080×1920 |
| 转场/运镜依赖超分质量 | 放大伪影 | RealESRGAN 对古风绘画表现良好，但需在 M1 实测确认；必要时换 4x 模型再降采样 |
| 抖音 UI 遮挡字幕 | 观感受损 | 底部留 320px 安全区（§8） |
| 素材版权（BGM/字体） | 下架风险 | BGM 用无版权古风曲库或自生成；字体用思源宋体（SIL OFL） |
| `full.wav` 与镜头总时长漂移 | 音画不同步 | §5.4 断言校验，超 100ms 即中止 |
| 历史事实错误 | 账号事故 | G1 闸门不可省（§11） |

> **多音字表是历史类账号的隐形门槛**。"月氏(zhī)"、"冒顿(mò dú)"、"阏氏(yān zhī)"、"龟兹(qiū cí)" 这些读错一次，评论区就会盖楼。这张表需要持续积累，是长期资产。

---

## 14. 里程碑

| 里程碑 | 交付物 | 验收标准 |
|---|---|---|
| **M0** ✅ **已完成** | venv + 依赖 + GPU 验证 | `torch.cuda.is_available() == True`；显存 12GB；torch 2.14.0+cu126 / torchaudio 2.11.0+cu126 就绪 |
| **M1** | **30 秒垂直切片** | "张骞被匈奴扣押"片段完整跑通 7 个 stage，产出可播放 MP4，**人工判断质感** |
| **M2** | `episode.json` schema 固化 + CLI | ~~`hv run --episode X --only S007` 可单镜头重建~~ → 该命令已废弃，现为 `hv episode reshot X --key S007` |
| **M3** | 缓存 + 幂等 + 断点续跑 | 改一句文案，只重建对应镜头 |
| **M4** | 单集完整产出（汉武大帝第 2 集） | 4 分钟成片，通过 G1/G2 |
| **M5** | 批量 10 集 + 资产库复用 | 第 3–10 集复用宫殿/草原场景与角色定妆照，单集成本下降 40% |

**M1 是生死线**。质感不过关，后面全部推倒重来。所以 M1 优先于一切工程化工作。

---

## 附录 A：待确认事项（已更新，仅剩 3 项）

~~1. Python 版本~~ — **已解决**：3.13 实测可用，torch 2.14 提供 cp313 wheel，无需退回 3.11。
~~2. 生图走本地还是 API~~ — **已解决**：本地 12GB 显存足矣，API 降级为备用路径。

| # | 待确认 | 说明 |
|---|---|---|
| 1 | **BGM 版权来源** | 需确定无版权古风曲库渠道，或用自生成方案 |
| 2 | **首集选题** | 建议用"汉武大帝第 2 集"作为垂直切片对象（有原片可对拍） |
| 3 | **是否升级到路线 A+** | 12GB 显存使本地 I2V（Wan2.2 1.3B）成为可能。是否要在 M1 后，把每集 8–12 个"英雄镜头"改成图生视频？边际成本接近 0，质感提升显著 |

### 附注：路线 A+（建议 M1 后评估）

当前决策是路线 A（全静态图 + Ken Burns）。12GB 显存打开了一个低成本的中间选项：

- 每集挑 **8–12 个英雄镜头**（如"卫青直捣龙城""霍去病河西大穿插"）走 **Wan2.2 1.3B I2V**，生成 2–3 秒动态片段
- 其余 50 个镜头仍用 Ken Burns
- 成本：本地推理，仅电费，约 3–5 min 额外耗时
- 收益：这 8–12 个镜头正是观众截图传播的高光点，也是原视频"动画感"的主要来源

**建议**：M1 垂直切片先严格按路线 A 做。切片出来后对比原视频判断质感差距，再决定是否需要 A+。不要一开始就加复杂度。

---

## 附录 B：M0 阶段采纳的 MPT 工程骨架（2026-09-03）

**背景**：项目处于 M0 阶段（设计/探测），代码量几乎为零。用户决定采纳 `harry0703/MoneyPrinterTurbo`（MPT，十几万星）的工程骨架作为参考，但材料层完全自建（gpt-image-2 + 角色一致性 + Ken Burns + RealESRGAN 取代 stock footage）。

### B.1 采纳 / 不采纳 / 自造

| 类别 | MPT 模式 | 我们怎么处理 |
|---|---|---|
| 采纳 | `app/services/` 单职责分层 | 11 个 service，单文件单职责 |
| 采纳 | `app/controllers/manager/{memory,redis}_manager.py` | 改为 `app/managers/{episode,relay}.py`（SQLite + 内存 health） |
| 采纳 | `config.example.toml` provider 区块 | `app/config/config.yaml`（YAML）+ Pydantic Settings |
| 采纳 | `resource/{fonts,songs,public}/` | 改名 `resource/`，装角色定妆/历史 BGM/字体 |
| 采纳 | `cli.py` + `main.py` + `webui/Main.py` 三入口 | `app/cli.py`（argparse）+ `app/main.py`（FastAPI）+ `app/webui/Main.py`（Streamlit） |
| 采纳 | `models/schema.py` Pydantic | `app/models/{episode,shot,relay}.py` |
| **不采纳** | `services/material.py`（pexels/pixabay 拉视频，98KB） | **不可用**：历史类无 stock footage。改为 `services/image.py`（gpt-image-2 三渠道池） |
| **不采纳** | `services/llm.py`（任意主题脚本生成） | 用法不同：我们的脚本是模板化的汉武大帝系列，LLM 只用于 prompt 润色/校对等局部 |
| **不采纳** | 通用 BGM 库 | 需要朝堂/边塞/战场/祭祀的氛围 BGM，定向采编 |
| ~~自造~~ **已删** | ~~角色一致性（ref pack + face sim）~~ | ~~`services/character_lock.py`~~ 2026-09-05 删除（定妆包能力未落地，全仓零消费者） |
| ~~自造~~ **已删** | ~~torchaudio MMS_FA 字符级强对齐~~ | ~~`services/alignment.py`~~ 2026-09-05 删除（MMS_FA 是英文模型，不能对齐中文；从未被 produce/compose 调用） |
| **自造** | 音频时长反向驱动镜头切割（250ms 呼吸间隙） | `services/script.py` + `managers/episode.py` |
| **自造** | Ken Burns 运镜（crop + zoompan + 2× 超采样） | `services/motion.py` |
| **自造** | RealESRGAN 2× 必跑管线 | `services/upscale.py` |
| **自造** | 多音字预处理（polyphone.yaml） | `services/polyphone.py`（M0 已实装） |
| **自造** | subrouter.ai 三渠道轮询 + 健康度降级 | `managers/relay.py`（M0 已实装） |

**比例**：MPT 骨架 ≈ 55%，自造 ≈ 45%。

### B.2 当前 D:\hv 目录结构（M0 完成态）

```
D:\hv\
├── app/                            ← 核心代码（MPT 风格）
│   ├── __init__.py
│   ├── main.py                     ← FastAPI 入口
│   ├── asgi.py                     ← uvicorn 入口
│   ├── cli.py                      ← argparse CLI（替代 bin/）
│   ├── config/                     ← 配置
│   │   ├── __init__.py
│   │   ├── config.yaml             ← 主配置（合并 style + models）
│   │   ├── config.py               ← Pydantic Settings + 渠道池发现
│   │   ├── polyphone.yaml          ← 多音字表（独立长期资产）
│   │   └── voices.yaml             ← TTS 音色注册表
│   ├── models/                     ← Pydantic schema
│   │   ├── __init__.py
│   │   ├── episode.py              ← Episode/Frame/Subtitle + 状态机
│   │   ├── shot.py                 ← Shot/MotionSpec/CharacterRefPack
│   │   └── relay.py                ← subrouter 响应模型 + 配额
│   ├── managers/                   ← 状态层
│   │   ├── __init__.py
│   │   ├── episode.py              ← episode.json 持久化 + 状态机迁移
│   │   └── relay.py                ← 三渠道池健康度跟踪
│   ├── services/                   ← 11 个单职责服务
│   │   ├── __init__.py
│   │   ├── image.py                ← 【已实装】gpt-image-2 + 渠道池
│   │   ├── polyphone.py            ← 【已实装】多音字预处理
│   │   ├── script.py               ← [STUB] 脚本切分
│   │   ├── voice.py                ← [STUB] TTS
│   │   ├── alignment.py            ← ❌【已删】强制对齐（2026-09-05）
│   │   ├── character_lock.py       ← ❌【已删】角色一致性（2026-09-05）
│   │   ├── motion.py               ← [STUB] Ken Burns
│   │   ├── upscale.py              ← [STUB] RealESRGAN
│   │   ├── subtitle.py             ← ❌【已删】字幕烧录（2026-09-05；烧字幕实装在 compose.py）
│   │   ├── bgm.py                  ← [STUB] BGM
│   │   └── compose.py              ← [STUB] 最终合成
│   ├── controllers/v1/             ← FastAPI 路由
│   │   ├── __init__.py
│   │   ├── episode.py
│   │   ├── image.py
│   │   └── video.py
│   └── webui/
│       ├── __init__.py
│       └── Main.py                 ← Streamlit 控制台
│
├── resource/                       ← (M1 阶段) 角色定妆 / 历史 BGM / 字体
├── episodes/                       ← 任务输入（episode.json）
├── state/                          ← 运行时状态（checkpoint、relay_health、polyphone_hits）
├── cache/                          ← 临时缓存
├── logs/                           ← 探测 / 错误日志
│
├── bin/                            ← 旧探测脚本保留（可重用）
│   ├── probe_image_api.py
│   ├── probe_video_api.py
│   ├── probe_sdxl_vram.py
│   ├── probe_real_call.py
│   └── probe_size_matrix.py
│
├── assets/probe/                   ← 探测产物（5 张原图）
│
├── docs/                           ← (M1) 文档
├── .env                            ← 真实密钥（git ignore）
├── .env.example                    ← 模板
├── requirements.txt                ← M0-essential
├── requirements-m1.txt             ← M1 阶段追加
├── cli.bat / api.bat / webui.bat   ← Windows 启动
├── cli.sh                          ← Linux/macOS 启动
│
└── config/                         ← 【LEGACY】旧 style/models/polyphone/voices
    （M0 完成后可删除，新位置在 app/config/）
```

### B.3 CLI 子命令

```
python -m app.cli config                              打印当前配置
python -m app.cli relay status                        渠道健康度
python -m app.cli relay sync                          强制同步配额
python -m app.cli image --prompt "..."                真实最小生图
python -m app.cli polyphone --text "单于冒顿"         多音字测试
python -m app.cli episode init ep-001                 初始化 episode
python -m app.cli episode show ep-001                 查看 episode.json
python -m app.cli episode produce ep-001              真实生产链（--stage 可指定子集）
python -m app.cli episode reshot ep-001 --key LGT01   单镜重建
# episode run —— 已废弃（2026-09-05），仅打印指路提示后 exit 1
python -m app.cli api --port 8000                     启动 FastAPI
python -m app.cli webui --port 8501                   启动 Streamlit
```

### B.4 subrouter.ai 接入总结（M0 探测结论）

| 项目 | 结论 |
|---|---|
| 路由 | **key 级**，三 key 独立配额池（aicost/qcyc/cxk） |
| 模型清单 | 三 key 都看到同一份：`[{id: "gpt-image-2", supported_endpoint_types: [..., "image-generation", "image-edit", "openai-video"]}]` |
| 生图端点 | `POST /v1/images/generations` + `POST /v1/images/edits`（多参考图） |
| 视频端点 | `POST /v1/videos`（任务制，task_id）+ `GET /v1/videos/{id}` + `GET /v1/videos/{id}/content` |
| 配额查询 | `GET /v1/dashboard/billing/subscription` |
| **陷阱 1** | 静默忽略非法 `size/quality` 并真去生成（已计费约 $0.012） |
| **陷阱 2** | 错误响应有**两套 schema**（图像 OpenAI 风格 / 视频 `{code,message,data}`） |
| **陷阱 3** | 总像素固定 **1,572,864 = 1024×1536**，请求 `size` 仅控长宽比 |
| 实测耗时 | low 档 ~29-33s/张（1080x1920 → 940x1672） |
| 返回格式 | `b64_json`（含 `revised_prompt` 字段） |

### B.5 预测公式（subrouter 真实像素）

```python
import math
MP_FIXED = 1024 * 1536  # 1,572,864
def predict(w, h):
    ratio = w / h
    h_actual = int(math.sqrt(MP_FIXED / ratio))
    return int(ratio * h_actual), h_actual
# 1080x1920 → (940, 1672) → ×2 RealESRGAN = (1880, 3344)
# 对 1080x1920 输出余量: (800, 1424)
```

### B.6 与本附录相关，但本附录未涵盖的

- **附录 A 的"待确认事项"** 与 M0 实测结论已多处过期：
  - "生图走本地还是 API"：用户拍板**走 API**（gpt-image-2 + subrouter），本地 SDXL 仅作降级项
  - 路线 A+ 评估时机未到，**等 M1 竖切片实际比对**
- **旧 config/ 目录**仍是可工作的副本，新位置 `app/config/` 已是规范。建议 M1 第一周内删除 `config/`（不删也不影响功能）。

---

*附录 B 完。下一步：进 M1 竖切片（30s 张骞被扣 segment，端到端跑通 TTS→alignment→image→upscale→motion→compose）。*
