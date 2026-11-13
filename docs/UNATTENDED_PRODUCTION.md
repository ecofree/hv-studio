# 无人生产操作边界

当前无人模式使用持久队列。队列按集串行生产，单集内部不同图片供应商仍并行；同一供应商跨模型、跨进程共用配置核定的并发槽位（默认一个）。旧的同步 `batch` 命令保留给有人看守的阶段操作。

## 入队与恢复

先在控制台核查当前原稿、史实、分镜和图片提示词，再提交输入审核。导演要求可明确选择“先按现有控制生成候选，成片后逐项审核”；选择绑定当前源稿、分镜和声音资产，不代表表演已经实现，也不批准成片。未选择执行方案且尚无当前导演处置时，仍阻断。当前音频的已知待办仍须真实处置。输入不合格不会消费预算，也不挡住其他合格剧集。

```powershell
& .venv-core/Scripts/python.exe -m app.cli batch --enqueue --file episodes-to-produce.txt --batch-id my-stable-batch --max-images 0 --max-requests 0 --max-characters 0
& .venv-core/Scripts/python.exe bin/production_worker.py --once
```

默认额度为零。付费前由操作者明确填写整批上限；重试和不确定结果都计入次数。相同批次不能改额度或换清单；重启不会得到新预算。预算用尽后停止，不能用自动新建批次的方式绕过限额。

`--job-timeout` 为每集总时限，默认 3600 秒、最长 14400 秒；`--job-retries` 为任务重试上限，默认 2、最多 3。重试只对超时、连接故障等暂时问题开放。永久输入错误等待输入改变；达到尝试上限或预算耗尽后保留失败状态，需要操作者处理。

工作进程持久保存每集状态、所属进程、结果和事件。进程死亡后接管旧任务，核对进程创建时间再停止残留子进程。供应商返回的图片响应先保存，再下载、解码和安装；下载最多尝试 3 次，后续恢复仍使用原响应，不重新生图。无法使用的响应保留待处置。

候选片完成后进入“等待审核”。只有当前哈希绑定的配音、导演、画面与史实核查满足原有发布要求，才显示“审核通过”。输入审核不能替代成片审核。

## 常驻与停止

损坏的批次、逐集状态和子进程结果会写入 `state/production-queue/errors`，原始记录保留；其余合格任务继续。控制台显示未解决的异常。扫描待审任务期间也会维持心跳。

已停止任务可以明确授权有限的额外执行机会，原累计尝试、预算消耗和历史保留：

```powershell
& .venv-core/Scripts/python.exe -m app.cli queue-retry --batch-id my-stable-batch --episode my-episode --operator operator-name --reason "故障已修复，沿用原预算恢复" --extra-attempts 1
```

续跑仍经过输入与预算检查，不提高付费额度。活跃进程或结构损坏的记录不能用此命令强行重置。

控制台的“队列暂停与恢复”在当前剧集完成后暂停取新任务，工作进程保持在线；恢复无需重新启动监管进程。暂停状态写入 `state/production-queue/PAUSE`，操作者与原因保存在 `controls`。这与下述 STOP 的立即中断行为不同。“修复后受控续跑”表单沿用同一预算，额外运行尝试数不增加付费额度。

```powershell
& scripts/install-production-worker.ps1 -StartNow
```

安装位置是当前 Windows 用户的启动文件夹。该用户登录后启动后台监管进程；这不等于开机后无需登录的系统服务。监管进程检查心跳，最多重启 5 次；持续故障会停止并留下状态，避免无限重启。

立即启动通过 Windows 进程代理完成，避免关闭终端或自动化会话时连带终止生产进程。队列默认每 30 秒检查一次待审核状态，避免频繁重读成片和素材；正在执行的任务仍每半秒更新心跳。

在 `state/production-queue/STOP` 创建一个空文件会暂停队列并终止当前所属生产进程。恢复时明确移除 STOP，再启动监管进程。`state/worker-service/stop` 用于停止监管进程。原始媒体、候选片和已消耗预算保留。

控制台显示工作进程心跳、批次、逐集状态及待处理原因。详细事件、有限重试记录和子进程日志位于 `state/production-queue`；监管状态位于 `state/worker-service`。此实现提供本地控制台提示，不向外发送消息。

## 空间与清理

付费前检查项目盘和系统临时盘。根据原稿长度估算时长，按每秒约 6 MB 加余量预留，最低 2500 MB；无法查询空间也停止。所有 v2 生产入口共用一把存储锁，避免多集同时按同一份剩余空间启动。

不会自动清理旧配音、78 张历史图片、批准素材或候选片。日志与已结束任务的临时产物由操作者在确认不再用于恢复后按具体目录归档；不运行历史剧集目录通配清理。空间估算是保守预检，其他应用在运行期间占满磁盘仍会导致本次任务失败并保留恢复状态。

## 复用历史图片

```powershell
& .venv-core/Scripts/python.exe -m app.cli reuse-images my-episode
```

将当前显式分镜引用的本集图片、跨集 reuse_from 图片绑定为现有资产，不改动图片，不制造原始生图回执。记录原稿、分镜、提示词和图片哈希；任一绑定改变即要求重新核查。该操作不是画面批准，也不把旧音频冒充当前声音资产生成的配音。

2026-09-22 实测 aicost 的 flare/sunburst 无可用渠道，已从生产支持列表隔离。保留 aicost 原有 gpt-image-2 和 CXK 三个成功小样的模型声明；不得自动恢复失效组合，也不为更换模型重生成已可复用素材。

## 生产备份与隔离恢复

```powershell
& .venv-core/Scripts/python.exe bin/production_backup.py create --episode lishimin-01 --episode laogongtuan --destination D:/hv_backup/production --dry-run
& .venv-core/Scripts/python.exe bin/production_backup.py create --episode lishimin-01 --episode laogongtuan --destination D:/hv_backup/production
& .venv-core/Scripts/python.exe bin/production_backup.py verify --snapshot D:/hv_backup/production/SNAPSHOT_ID
& .venv-core/Scripts/python.exe bin/production_backup.py restore --snapshot D:/hv_backup/production/SNAPSHOT_ID --destination D:/hv_restore/NEW_DIRECTORY
```

快照覆盖选定剧集及其引用的共享图片、原始配音、当前/批准候选、回执、审核、声音资产、队列和预算账本，附带源码与依赖声明。重建用的历史未批准渲染缓存无需全部重复备份。密钥、虚拟环境和外部字体等依赖不打包；外部文件记录哈希。

快照保留 PAUSE/STOP 控制标记及操作记录；隔离恢复不会清除操作者的暂停要求。

复制前持有生产与队列锁；扫描源文件前后验证一致性，异常快照保留 incomplete 标志，不能作为成功备份。服务在备份占用队列锁时保持等待心跳。恢复只允许写入新的隔离目录，不覆盖项目，也不自动运行；路径重新绑定、独立安装相同运行环境及凭据恢复必须在正式切换前完成。文件恢复与预算保留验证不等于整机灾备切换验收。

## 视频片段复用

候选回执继续绑定完整源码、配置和输入。维护代码变动后仍需重新验证并生成当前回执；旧批准不会自动转移。视频片段另按画面依赖建立缓存键：镜头边界、图片内容、运镜、实际字幕、字体、编码参数、渲染实现及运行时。队列、备份和供应商路由等不影响画面的改动，可以在重新验证输入后复用已编码片段。

跨候选复用必须通过文件哈希和实际帧数检查，随后重新合并音轨并校验完整候选。源图、字幕、运镜或渲染实现改变时，相应片段失效。旧版没有视频依赖签名的片段不自动授予新签名，因此升级后的首次构建仍需编码；已有媒体保持原样。

## 验收口径

`scripts/ai-project-check.ps1` 默认运行生产代码测试。`-RequireReady` 检查常驻状态、当前版本的多集实测证据、进程中断恢复、超时恢复、新模型真实调用及成片审核。`-EvidencePath` 必须真实写出证据；未知参数直接报错。代码测试通过不会自动成为无人生产验收通过。

当前版本真实验收记录放在 `state/acceptance/production-validation.json`，必须绑定检查器源码指纹、实际产物哈希和至少两个剧集；禁止用离线假供应商结果填写真实供应商验收。
