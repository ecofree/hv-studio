# 生产运行与恢复

适用版本：2026-09-12 的 v2 修复版本。目标是自动生成有完整证据的本地待审候选；技术检查、内容批准、视频平台上传是不同状态。

## 安装与验收

核心环境使用 Python 3.13，完整依赖锁为 `requirements.core.lock.txt`；其中包括执行验收所需的 pytest。本地 ASR 使用 `.venv-asr` 和 `requirements.asr.lock.txt`，模型与环境预检通过后才进入依赖语音检查的生产阶段。

```powershell
powershell -File scripts/bootstrap-core.ps1 -Python <Python3.13路径>
# 已保存离线包时可使用 -Wheelhouse <wheelhouse目录>，会先校验散列
powershell -File bin/setup_local_asr.ps1 -Python <Python3.13路径>
powershell -File scripts/ai-project-check.ps1
```

ffmpeg、ffprobe 和配置指定的中文字体须可用。密钥留在本机 `.env`，不会放进源码或重建包。`HV_PYTHON` 可以显式覆盖启动器的 Python；默认使用本项目 `.venv-core`。旧的含 GPU/人脸扩展的混合环境保留，核心启动器已与其隔离。

## 单集与整批生产

先通过已有初始化入口建立剧集，把原稿放到 `episodes/<id>/script.md`。已有 `scene_plan.json` 和 `image_prompts.json` 优先复用，两者须共同存在且分镜引用与当前原文吻合；缺省时按完整旁白单元生成来源明确的配图草案。草案和既有图片均需最终语义审核。

```powershell
.\cli.bat episode produce <id> --stage script
# 默认 0 额度适用于已具备全部缓存的续跑
.\cli.bat episode produce <id>
# 只有获准实际生成时才填写明确预算；下列 N/C/I 均须替换为授权上限
.\cli.bat episode produce <id> --max-requests N --max-characters C --max-images I --budget-id <本次授权标识>
.\cli.bat batch --file <剧集列表> --batch-id <本批标识> --max-requests N --max-characters C --max-images I
.\cli.bat batch --file <同一列表> --batch-id <同一标识> --resume --max-requests N --max-characters C --max-images I
```

额度是实际调用硬上限，不是美元报价。TTS 请求数、字符数与图片调用分别累计。失败、超时、进程死亡后的不明结果都保守占用一次额度；不自动退款。图片重试也消耗额度。整批共享账本，后续剧集不能重新获得一整份预算。缓存命中不占额度。余额不足在可预见的依赖检查阶段先阻断，实际调用前再原子检查。

TTS 只使用登记的 CosyVoice 3.5 音色，不自动切回 Qwen 或更换人声。导演指令只有在音色能力验证为 supported 时进入请求；未支持的语气/画面要求列入明确处置队列。数字停顿通过声学词边界插入精确 PCM 静音，原始 take 保留。新的主轨记录每次插入的位置和样本数。

## 合成、配乐与人物参考

默认时间轴来自原文范围和声学边界，字幕覆盖完整原文，图片使用缓慢推拉；显式分镜 motion 优先。配置的 NVENC 会先实际试编码，不可用或运行失败时回退软件编码并写入记录。长镜头也被切成不超过 45 秒的段，跨段保持同一运镜轨迹。

分段恢复核对原收据中的输入身份、帧范围、文件散列和帧数。同帧数的内容替换不能复用。所有渲染入口共用目标锁；锁由操作系统持有，进程死亡后自动释放。保留锁文件本身，不通过删除它“解锁”。恢复时重跑相同入口即可。

背景音乐放在 `assets/bgm/<profile_name>.mp3|wav|m4a|flac`，生成时间轴时自动绑定路径与散列，混音保留旁白电平并限峰；没有音乐素材时不凭空生成。`image_prompts.json` 的图片条目可指定 `reference_path`（相对于该集目录）或只含一张图的 `reference_paths`。参考图内容进入请求身份；当前图生图通道只支持一张明确参考，不能静默丢弃更多参考。参考注入不等于人物一致性已经通过视觉审核。

## 监控、逐项核查与批准

```powershell
.\webui.bat
.\api.bat
.\cli.bat episode produce <id> --stage verify --timeout 600
```

网页显示同一 `production/v2/run.json` 的当前状态、候选、语音疑点、导演提示和已用额度。旧 episode 状态保留为 `legacy_status`，不再冒充当前完成状态。界面逐项试听和填写审核人/发现后，才会保存结论。语音真有口误时登记否决，后续缓存、合成和发布均拒绝该音频；只在另行明确预算下重做对应单元。

命令行也可保存逐项结论：

```powershell
.\cli.bat narration review-quality <quality报告> <unit_id> --reviewer <审核人> --findings <试听依据> --decision asr_false_positive
# 真口误使用 --decision spoken_error
.\cli.bat narration review-direction <manifest> <cue_id> --reviewer <审核人> --findings <实际效果与处置依据> --decision accepted_existing
# 仍需调整使用 --decision needs_revision
.\cli.bat narration review-audio <manifest> --reviewer <审核人> --findings <全片听审依据> --decision approved
.\cli.bat narration review-video <candidate收据> --reviewer <审核人> --findings <画面字幕史实审核依据> --decision approved
.\cli.bat narration publish <candidate收据> <画面审核记录> <听审记录> episodes/<id>/production/v2/published.json
```

发布还会检查当前语音疑点和导演提示的处置。该 publish 只切换本地发布指针，不上传视频平台。不得为了消除阻断由程序伪造审核人或批准。源文、音频、字幕、图像或相关实现变化会使旧证据失效。

## 服务恢复与故障验证

`serve.bat` 在本机回环地址启动 API，并对异常退出做有上限的重启。默认最多 5 次，退避上限 30 秒，写入 `state/service/service.json`。创建 `state/service/stop` 会正常停止；重启前由操作者明确移除该停止标记。服务启动只使用本地状态，不因供应商配额查询阻塞。不会因为启动服务而自动生成媒体。

```powershell
.\serve.bat
.venv-core\Scripts\python.exe bin/probe_render_recovery.py
.venv-core\Scripts\python.exe bin/probe_service_recovery.py
```

探针只创建新的隔离目录，使用合成测试输入。它们验证进程退出、锁释放、缓存与真实编码/解码、API 恢复，不能充当真实内容批准。生产成片仍需单独保留当前完整验证和听看证据。

## 源码和素材备份

源码版本包含所有 v2 模块、启动器、锁文件及验收入口。生成媒体、运行账本、音色资产、ASR 模型、第三方字体和密钥各自备份，不能只备份 Git 后就声称能够还原业务成片。离线核心 wheelhouse 的来源是已安装环境的依赖闭包快照，并附 SHA-256 清单；这不冒充从 PyPI 重新下载的来源认证。
