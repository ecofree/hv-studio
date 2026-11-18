# HV Studio

本项目是独立的视频工作台。启动 `studio.bat`；活动作品和状态位于 `workspace/episodes`、`workspace/state`。原项目 `D:/hv` 不改动，根目录 `episodes/` 是历史素材库。

## 在新 Windows 电脑安装

1. 克隆本仓库，安装 Windows 版 Python 3.13 和 FFmpeg（`ffmpeg`、`ffprobe` 均需在 PATH；也可自行放在 `tools/ffmpeg/bin`）。
2. 在项目目录运行 `powershell -File scripts/bootstrap-core.ps1 -Python py`。如果 `py` 默认不是 Python 3.13，改为该版本的 `python.exe` 完整路径。安装完成后运行 `studio.bat`。
3. 需要对现有旁白生成字幕时，另外运行 `powershell -File bin/setup_local_asr.ps1 -Python py`，安装独立 ASR 环境并下载本地识别模型；该步骤不配音、不渲染，但需要网络和磁盘空间。需要图片或新配音时，再自行配置对应服务与明确预算，默认预算为零。

Git 仓库提供代码、规则和已跟踪的创作源文件；`workspace/`、音频、图片、视频、模型及 FFmpeg 不在仓库中。全新克隆可新建作品并使用仓库内源稿，但不会自动带回本机的剧集、母带、素材、预算台账或审核记录。如需恢复旧作品，须从可信备份单独迁移并核对路径和素材，不要把旧审核当作新版本批准。本项目内的原稿与规则是唯一创作来源，导入不再比较或读取原项目。

`.studio-project.json` 的 `legacy_path_roots` 仅用于把历史地址转换到本项目相同相对位置，不会查找或读取旧目录。本地缺少文件时如实报缺失，不回退到旧项目。历史回执与原稿不批量重写，旧地址只作溯源；本项目仍需自己安装运行环境、FFmpeg、字体及所选本地模型，配置自己的服务凭据。

环境检查（不配音、不渲染）：`powershell -File scripts/ai-project-check.ps1 -ProjectRoot (Get-Location).Path -Quick`。检查包括 FFmpeg 与字体；通过仅表示本机核心环境可用。

制作页提供局部录音定位、看片跳转、累计耗时和空间管理。自动定位失败时先试听前后文并保存语音范围，再继续本地旁白检查；不会把手工范围当成听审通过。当前正文的关键事件确认同时适用于工作台和批量入口，分镜变化只重新做对应技术检查，不再另填批量输入审核。

空间管理默认只列清单：`cli.bat clean --episode <id> --v2`。加 `--segments` 包含已完成版本的分段缓存；只有加 `--apply` 才实际删除。执行会锁定本集、检查清单变化并核对保留成片的哈希，保留原始图片、录音、字幕、回执和候选成片。分段缓存清理后，后续修订可能需要重新编码；不完整的渲染留作恢复。

备份使用本地历史地址映射；缺失或不可读依赖在 `bin/production_backup.py create --dry-run` 中列出，不再静默遗漏。外部依赖仍须另外保存，恢复后不自动启动旧任务。累计制作耗时从启用新统计后的真实任务开始，失败和重做同样计入，不补造过去耗时、人工用时或费用。

现行流程：正文与关键事件确认 → 复用或制作旁白 → 配图与字幕 → 完整听看 → 确认本地成片。样片可选。具体口径见 [工作台规范](docs/studio-workflow-policy.md)。

## 当前入口

- `studio.bat`：工作台，提交的长任务在后台运行，保留日志并支持原任务续做。
- `cli.bat status <id>`：查看本集实际进度和下一步。
- `cli.bat episode produce <id>`：v2 生产。未提供额度时只复用已有素材。
- `cli.bat episode produce <id> --stage tts --external-master "音频绝对路径"`：导入已完成的旁白，不重新配音。
- `cli.bat episode produce <id> --stage images --image-key <KEY> --max-images <总尝试次数> --budget-id <本轮标识>`：仅补指定底图。继续同一轮使用相同标识。
- `cli.bat episode produce <id> --pipeline legacy-preview --stage compose --stage verify`：用现有历史素材重建预览。该路径不配音、不生图。
- `cli.bat image test --help`：带持久额度的供应商诊断，和正式作品分开保存。

命令行的生成、渲染、验收长任务同样需要后台启动并重定向日志。不要在会超时终止子进程的工具调用内等待。

## 画面输入与复用

人工分镜使用本集 `scene_plan.json` 和 `image_prompts.json`，两份应一同提供。按旁白单元安排画面时，正式输入是本集 `production/v2/source_image_briefs.json`：

```json
{
  "source_sha256": "当前 plan.json 中的 source_sha256",
  "images": {
    "unit-000": {"en": "Soldiers regroup beside a camp at dawn."}
  }
}
```

`en` 只描述人物、动作、环境与道具；画风由统一配置补齐。`source_sha256` 是源文本指纹，不是直接计算 Markdown 文件字节哈希。生成目录 `storyboards/` 是输出，修改其中的副本不会改下一次请求。

新付费画面必须有独立描述。旧的旁白提示词仅用于定位和恢复已有缓存。请求变化后，同名旧图不能冒充新图；旧文件保留，待新素材齐全后再构建时间轴。

图片是底图，不要求原生 1080×1920。读取实际像素后，等比放大并裁切到视频尺寸。有效的 1024×1536 等图片可复用，不因尺寸不同再次生成。

## 额度与恢复

图片、配音的实际请求在发送前写入持久账本；失败、超时、结果不确定均占用尝试次数。`--max-image-retries` 是旧参数，仅增加同一个总尝试上限，并非独立重试池。成功落盘统计不是供应商账单。

有已保存响应时先恢复。内容拒绝或结果不确定会停止相同请求；另开预算不能解除停止记录。新画面描述必须有实际内容变化，不能靠更换标识重复提交。

正式配音使用已绑定的声音方案或外部母带。本地 Qwen 统一由本项目的 `.venv-qwen3-tts` 和 `bin/qwen_worker.py` 执行，不调用共享 WebUI/HTTP 服务或旧项目环境。模型权重可以继续位于独立的 `D:/models/qwen3-tts` 数据目录。语音识别使用本地 Whisper，不回退到旧云端识别。机器识别告警保留到听审处置，不等于实际口误，也不自动触发重配音。

Qwen 的制作页、`bin/studio_narrate.py`、`start_qwen3_tts.bat` 与旧配音脚本都进入同一条剧集配音流程。运行 `.venv-core/Scripts/python.exe bin/studio_narrate.py <剧集ID> --plan` 只检查已选声音、分段种子与参数，不启动模型；实际配音继续使用原有的明确预算。没有剧集参数的启动器只显示帮助。

新配音保留已确认说话人、模型、采样参数与倍速，只移除已识别的旧试听段落指令，并在每段生成前设置稳定种子。旧声音合同、旧音轨与旧回执保留。声学一致性报告自动定位音高、共鸣特征和音量的异常位置，制作页可直接试听；不新增审批或自动重配。详情见 [Qwen 本地配音说明](docs/qwen-local-voice.md)。

## 开发与验证

使用 `.venv-core/Scripts/python.exe`；ASR 使用 `.venv-asr`。Windows 启动器和验收脚本会加入项目的 FFmpeg 路径。

默认离线验收：

```powershell
powershell -File scripts/ai-project-check.ps1 -ProjectRoot D:/hv-studio -Suite production
```

组件变化按需运行 `shared` 或 `legacy`；跨模块清理使用 `all`。测试默认隔离作品与状态目录，禁止修改历史成片、真实母带或审批记录，也不允许真实供应商调用。

测试通过只说明已执行的离线检查通过，不等于画面、听感、史实或发布审核完成。候选成片与正式确认保持区分。

## 历史代码

旧的逐帧配音入口、云端 Qwen/V-API 合成、自动音色创建、旧 `reshot`/`run` 命令及付费 legacy 分支已经移除。历史预览、字幕校验、手改帧结构、现有媒体、已用音色及原始失败证据保留。当前清理记录见 [全仓复盘](docs/repository-cleanup-20261105.md)。
