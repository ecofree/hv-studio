# 四批整改：实现边界与实际验收

基于 main `8430d4700cb47f29eab62e9d2f4225bdea3541f1`。本分支供独立测试，**不自动合并 main**。
原稿、旧录音、已批准成片和旧预算不迁移、不覆盖。没有新增付费授权。

## 四批的代码范围

| 批次 | 实现 | 仍须实际验收 |
| --- | --- | --- |
| 一：正确性 | 活动 workspace 备份及停止恢复；统一图片总尝试上限；识别缓存/QA 判定版本分离；验收顺序、任务轮次、进程与预算因果绑定 | 在独立目录做真实备份恢复，核对外部素材和绝对路径 |
| 二：入口和恢复 | `recording_input.episode_source` 统一完整/编号/选用/已绑定录音解析；显式 WAITING_RESOURCE；管理 Token 轮换不改变媒体任务身份；只读统一运行视图 | Studio、CLI、Batch 使用同一组有效素材分别执行，确认无误触 TTS |
| 三：验收基线 | 冻结 plan、明确授权、共用台账、拥有进程的 kill/timeout/resume、真实队列、TTS+图片回执采集、不可冒充真实验收的 offline 模式 | 用户授权后实际供应商、多集完整生产和人工听看；本轮不执行 |
| 四：质量与效率 | 连续同图/字幕密度/试听定位/参考图对照、局部重做建议、预算与阶段指标、默认串行且可显式启用的资源预约 | 固定内容 A/B 观感、人物/服饰/叙事一致性、真实硬件吞吐 |

没有第二套任务数据库、调度器或审批状态。既有 Studio/Batch/V2 记录仍为权威。
模型判断/技术指标不自动批准内容。没有加入未经验证的 CosyVoice 结果查询 API。

## 安装与离线测试

使用仓库 `.venv-core/Scripts/python.exe` 与 `requirements.core.lock.txt`；FFmpeg/ffprobe 在 PATH，
中文字体保持原配置。Windows CI 会在临时机器安装固定来源及校验值的媒体依赖。

```powershell
python -m pytest -q tests/test_four_batch_correctness.py tests/test_four_batch_recovery.py tests/test_acceptance_runner.py tests/test_resource_diagnostics.py tests/test_production_readiness.py
python -m pytest -q -m production
python -m pytest -q tests/test_hv_ops.py tests/test_production_hardening.py tests/test_production_regressions.py tests/test_output_binding_and_legacy_status.py tests/test_checkpoint_rotation.py
```

缺少依赖/字体的本地基线比较不等于完整测试通过。用当前提交的 CI JUnit 核对。

## 第三批：先运行零费用监督器冒烟

从当前仓库运行，输出选一个全新的目录。以下**不会生成真实视频或调用模型**。

```powershell
python bin/accept_production.py plan --offline --episodes acceptance-offline-one acceptance-offline-two --timeout 60 --output D:/hv-tests/offline-01/plan.json
python bin/accept_production.py authorize --plan D:/hv-tests/offline-01/plan.json --operator tester --reason "仅离线进程演练" --allow-faults
python bin/accept_production.py run --plan D:/hv-tests/offline-01/plan.json --scenarios multi_episode_current_rebuild kill_resume timeout_recovery
```

证据的 `evidence_mode=offline` 不能被生产就绪验证器接受。offline 不证明无人队列或真实供应商。
这些子进程是真实启动/被终止/重新执行，但产物是明确标注的测试 JSON，不是成片。

## 第三批：用户授权后的真实代表性基线

建议在 Studio 新建三集**专用** `acceptance-*` 测试作品，真实原始资料仍保留：

1. 一份完整外部母带，已确认当前正文；不设置 TTS 预算。
2. 一组编号 WAV，无缺号/重号，显式选择编号交付。
3. 已登记 VoiceAsset 的 TTS 剧集，明确文字、声音和图片需求。

三集使用当前真实图片渠道；既有当前可复用素材优先复用，不为了证据强制重复付费。
需要新供应商调用才能证明真实调用路径；如果全部命中缓存，`real_provider_models` 会保持阻塞，
不能写一个 `is_mock=false` 就放行。真实模式只允许独立 workspace 内未发布的 `acceptance-*` 剧集。
关键事件确认、图片输入和声音来源必须先满足现有准入，工具不会替你审核史实。

```powershell
python bin/accept_production.py plan --episodes acceptance-full acceptance-parts acceptance-tts --max-requests 6 --max-characters 3000 --max-images 9 --timeout 7200 --output D:/hv-tests/real-01/plan.json
python bin/accept_production.py authorize --plan D:/hv-tests/real-01/plan.json --operator actual-reviewer --reason "确认本次总预算及专用进程故障演练" --allow-paid --allow-faults
python bin/accept_production.py run --plan D:/hv-tests/real-01/plan.json
```

上面的额度**只是填写示例，不是推荐花费或自动授权**，必须按你的剧本改成实际同意的上限。
所有场景共用一份持久账本，累计额度不因场景/失败/恢复重置；预算不足会阻塞。
故障只作用于监督器创建、身份匹配的子进程，不终止原来运行的其他作品或模型服务。
默认故障点是 compose 阶段入口的受控停顿：证明前序产物和预算可恢复，不宣称是所有编码器/
供应商网络中断点的覆盖。超时演练是监督器期限，不伪装成供应商服务端已完成后超时。

发生问题先核对 `runs/summary.json` 和各 trial 的日志、事件、产物/预算哈希。
不确定付费结果仍需原恢复工具或人工裁决，禁止删账本“重新跑”。
已完成场景保持原证据；未完成场景只有显式 `run --resume` 才再尝试，每场景最多三次，仍用原总预算。
输入、代码、模型参数或路径变化后，不能修改原 plan 继续冒用；另做新计划并保留旧证据与消费。

完成后用现有 `bin/record_production_validation.py` 收集 `runs/` 内五类真实证据，
source-fingerprint/git-revision 使用本次冻结计划值。缺失任何必需场景会拒绝生成。
该收集器不是人工听看或当前 worker 在线证明；完整成片仍需实际听看并使用既有审核操作。

## 备份与恢复

使用 `bin/production_backup.py create` / `verify` / `restore` 的现有参数（以 `--help` 为准）。
源目录和目标必须分开。恢复保留布局、原预算、PAUSE、拒绝记录和任务资料，同时添加 STOP。
恢复文件中的绝对路径/进程身份不自动改写或自动续跑；逐项核对、显式重绑定，确认后才解除 STOP。
外部依赖只登记内容哈希，必须另外保存实际外部文件。凭据不包含在快照中。

## 质量复核和 A/B

```powershell
python bin/production_diagnostics.py --episode acceptance-full --output D:/hv-tests/report-before
# 只做明确需要的修订后，另存新的报告目录
python bin/production_diagnostics.py --episode acceptance-full --output D:/hv-tests/report-after --baseline D:/hv-tests/report-before/diagnostics.json
```

也可在 Studio 制作页展开“只读生产与质量诊断”。页面不改变任务、媒体或审批。
报告聚合连续使用同一像素的镜头、旁白位置、字幕速度、原参考图以及真实阶段计时。
长镜头和高阅读密度只是提醒，不强制切图、不改文案、不自动购图。
人物身份、时代服饰、动作自然度、整集叙事仍需实际观看；没有声称已建立自动艺术质量判定器。
局部重做是依赖建议，执行仍走现有阶段验证；不能用建议跳过字幕/时钟/媒体检查。
未记录的人工时间、货币成本保持 `null`，不编造“每分钟成本”。已有共享账本与当前任务消费分别显示。

## 显式资源重叠（默认关闭）

不改配置时最多一个完整 V2 任务。活动 `paths.state_dir()/resource-policy.json` 可设：

```json
{"max_active_episodes": 2, "min_available_ram_mb": 4096}
```

上限只能 1..4，最低保留 RAM 不得小于 2048 MB。应先退出正在运行的任务再改策略。
这允许**不同入口/进程的不同剧集**在资源许可时重叠，不是把现有串行 Batch 改成并行调度器。
每集仍独占自己的写锁；同卷工作盘+临时盘需求相加并扣除已有预约；内存不足进入等待；
本地重模型和编码器各保留独占资源通道；进程死亡后靠内核锁回收预约，不扫描删除历史素材。
不能同时运行旧版本进程和新资源协议；暂停全部旧工作器、更新后再启动。
GPU 的型号/显存未自动识别；编码器保持一条通道，不能据此宣称任意 GPU 多路安全。

## 完成定义

代码、离线子进程演练、完整回归和 Git 推送分别记录。用户实际供应商/硬件/听看结果独立记录。
四批工程入口齐备并不等于已经证明连续无人值守或专业成片质量；不通过自动放宽阈值、加额度、
复制批准、删失败记录或把离线模式改成 real 来凑验收。
