# HV Studio

本项目是独立的视频工作台。启动 `studio.bat`；活动作品和状态位于 `workspace/episodes`、`workspace/state`。原项目 `D:/hv` 不改动，根目录 `episodes/` 是历史素材库。

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

正式配音使用已绑定的声音方案或外部母带。本地 Qwen 服务是外部依赖，不由测试启动。语音识别使用本地 Whisper，不回退到旧云端识别。机器识别告警保留到听审处置，不等于实际口误，也不自动触发重配音。

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
