# 2026-09-10 生产改造记录

本轮已落地音频优先的 v2 路径和一批原链路可靠性修复。**项目尚未完成生产验收**：用户已反馈新音频整体明显改善，但第一段语速偏快；两段 ASR 因额度限制未完成，逐词对齐、真实视觉样片和整片内容批准仍待完成。

## 当前证据

- 原审计与本地 HEAD 相同：`9877098a7d66090a0b9ed1ade4d3e8d32ea17c7e`。本轮修改仍在工作区，未提交、未发布。
- 基线测试：267 passed / 1 failed。原 smoke 失败源于可用的临时绝对路径被加载器改写；现已修复。
- 最后全量回归：**285 passed，119.37 秒**。真实 FFmpeg 回归包含 v2 的 4 秒、6.04 秒合成；300 镜累计舍入反例通过。这些是构造测试，不代表长篇真实成片验收。
- 安装的 dashscope 是 **1.27.3**。通过其真实 `call()` 和模拟 WebSocket transport 验证：SDK 会自动开启 `enable_ssml`；rate 从 1.0 改到 1.2 确实改变外发参数。不能由此声称特定设计音色支持所有 SSML 标签。
- 已记录的 291 个历史 episode 输入、音频和音色缓存文件散列均未变化；tracked episode JSON 无 diff。该核对范围不等于全部历史媒体。
- 用户授权的小样：**3 次 CosyVoice 3.5 flash TTS、3 次 paraformer ASR，无自动重试**。271 字，24 kHz mono PCM，主轨 59.99 秒。第一段 ASR passed；后两段返回 `AllocationQuota.FreeTierOnly`，状态为 unavailable。
- 首次原音收到服务端流式 WAV 长度占位头。没有重新合成；在确认 SDK 请求成功后生成仅修正文件头的派生 WAV，验证 PCM 样本逐字节不变，保留服务端原件。
- 用户反馈："前20秒语速过快，后面30多秒语速正常，整体比以前好很多"。这属于节奏及整体听感反馈，未扩写为全部内容/发音批准。
- 已生成仅第一段 `atempo=0.92` 的本地节奏预览，62.363 秒，后两段 32.42 秒的 PCM 样本不变，新增接口调用为 0。另已准备第一段原生 TTS rate=0.92 的新计划，尚未执行。
- 当前实际配置已经是 `engine=cosyvoice`、`cosyvoice_model=cosyvoice-v3.5-flash`，本轮小样没有使用 Qwen。

## 已实现的路径

`script.md → document/plan → explicit VoiceAsset → compiled/effective request → immutable takes → continuous master → sample timeline → candidate → exact-hash reviews → published pointer`

入口仍是 `python -m app.cli`，新增 `narration` 子命令。已有 `episode produce` 是 v1 兼容预览路径，其结果明确返回 `production_ready=false`，不能用其 COMPOSED 状态代替 v2 生产批准。旧集不会因打开、检查或导入自动重切、重配音。

| 模块 | 本轮行为 | 验收边界 |
|---|---|---|
| `asr_check.py` | 唯一临时 WAV；finally 清理；passed/failed/unavailable/disabled；音频/正文散列；数字、单位、遗漏/插入、指定实体独立问题 | PER 是辅助指标；不等于自然度或事实检查 |
| `script_document.py` | 保存原稿、正文、引文、标题、元数据、导演提示、来源范围；纯稿/双栏共用；保留小数、问叹号、英文空格和停顿数值 | 未执行自动历史事实核查；未知提示保留并待审 |
| `narration_pipeline.py` | 完整段落单元；原文禁止静默改写；完整请求指纹；按实际缺失请求做预算预检；原音不可变；样本级连续主轨 | 未验证的发音/指令能力不能直接启用；没有按字数伪造词时间戳 |
| `cosyvoice.py` | 记录 SDK 实际序列化消息、模型、voice_id、版本、参数、request_id、耗时和结果；保留服务端字节 | 旧音色缓存没有 prompt 来源时，不自动把新 prompt 绑定到旧音色；使用显式导入资产 |
| `production_v2.py` | 镜头和字幕独立引用主轨；整数累计样本边界；fit-cover 几何；真实音视频检查；候选与人工审批分别绑定散列；一次原子发布指针 | 整句边界来自真实音频单元；句内字幕需要有署名的人工对齐，自动强制对齐尚未接入 |
| 旧链路 | 移除组末强制慢读；统一回检；保留候选；禁止假恢复；修复坏图缓存、同路径换图/字体依赖、局部分段失效、无声中间片覆盖最终文件问题 | v1 的镜头/逐帧音频时间模型仍保留供兼容；正式生产使用 v2 |
| 编排/运维 | batch 检查失败返回；stop-on-error 先落盘；resume 绑定阶段/输入；reshot 产出独立候选，保留原图和手调镜头；内核文件锁及线程互斥；编码 stderr 消费和 watchdog | 旧 API 所有旁路资产写入口尚未全面收敛；跨完整流水线统一 deadline 尚未完成 |

## 审计发现对应状态

`fixed` 表示所述实现有回归证据；`mitigated` 表示范围有限或仍需人工；`unverified` 不算通过。

| 审计发现 | 状态 | 说明 |
|---|---|---|
| F01 图片请求指纹 | mitigated | 坏图不能当缓存；旧生图请求的完整内容身份尚未全面迁移 |
| F02 同路径换图/字体缓存 | fixed | 内容 SHA 参与依赖；按段失效测试通过 |
| F03/F04 裁切、终点和缩放 | fixed for v2 | fit_cover_v2 使用完整 cover 比例与显式端点；旧坐标语义保留 |
| F05/F06 静态提示词语义、角色参考实参 | unverified | 本轮未生成新图，未宣称 provider 参考图能力已验证 |
| F07 单句/单镜绑定 | mitigated | v2 主轨、镜头、字幕已解耦；创作分镜仍需给出和审阅 |
| F08 累计音画漂移 | fixed for v2 | 样本累计边界和真实媒体时长核对；v1 模型不冒充新契约 |
| F09 TTS 指纹和旧音频补签名 | fixed for v2 | 完整有效规格；旧音频不自动补成当前请求；单段修改只重生该段 |
| F10 能量切片冒充强对齐 | mitigated | v2 不切主轨、不用比例伪造词边界；句内自动对齐仍待接入 |
| F11 表演/回滚 | mitigated | 中性基线、逐段 rate、不可变 takes；局部发音与表演需进一步实测 |
| F12 reshot 删除旧素材/手调参数 | fixed | 独立候选 episode；失败保留原 revision |
| F13 发布事务 | fixed for v2 | 技术/听审/视觉审核绑定精确候选，再原子更新指针 |
| F14 verify 无音轨假成功 | fixed | 必需音视频流、尺寸、帧数、解码、音轨时长独立检查 |
| F15/F16 批处理和 resume | fixed | 失败不能进入 done；先保存失败；阶段/输入改变不跳过 |
| F17 并发锁 | mitigated | 官方 produce/reshot、v2 合成/发布已加锁；旧 API 旁路仍待收敛 |
| F18 软件编码 preset | fixed | 不把 NVENC p4 传给 libx264 |
| F19 参数白名单重复 | unverified | 未在本轮变更 |
| F20 超时边界 | mitigated | 视频写入阶段加 watchdog；全流程 deadline 仍未统一 |
| F21 成本台账 | mitigated | v2 每次真实 TTS 请求独立记录，不按最后文件数计请求；旧聚合 JSON 台账仍待替换 |
| F22 测试与真实效果混淆 | mitigated | 285 测试和用户实听反馈分开记录；未把小样通过扩展为整片生产通过 |

## 使用与迁移

使用本机环境：`C:\Users\coumoo\.workbuddy\binaries\python\envs\hv\Scripts\python.exe`。不需要改全局 Python、驱动或 `.env`。

```powershell
# 完整子命令说明
python -m app.cli narration --help

# 导入来源，生成不可变计划；只选小样时明确限制单元数
python -m app.cli narration prepare episodes/lishimin-01/script.md state/my-samples --max-units 3

# 导入已存在的音色身份，不创建新音色；导入后仍是 unverified
python -m app.cli narration import-voice .cache/voice_design_hvndrama_cosyvoice-v3.5-flash.json state/my-voice.json

# 必须明确实际请求预算；force 是新候选，不删除历史文件
python -m app.cli narration synthesize <plan.json> state/my-voice.json --max-requests 3 --max-characters 271

# 只修改一个单元的真实合成语速，生成新计划并引用其他旧音频
python -m app.cli narration revise-delivery <plan.json> narr-4aff5b4871df47bc --rate 0.92

python -m app.cli narration render <manifest.json> <timeline.json> state/my-candidates
python -m app.cli narration review-audio <manifest.json> --reviewer <姓名> --findings <实际听审记录> --decision approved
python -m app.cli narration review-video <candidate.json> --reviewer <姓名> --findings <实际观看记录> --decision approved
python -m app.cli narration publish <candidate.json> <video-review.json> <audio-review.json> state/published.json
```

`timeline.json` 的 `shots` 必须连续覆盖主轨样本：`id / sample_start / sample_end / image_path / motion`；`subtitles` 必须覆盖原文：`unit_id / sample_start / sample_end / text_start / text_end / text`。句内对齐额外要求 `alignment_source=human_reviewed` 和 `reviewer`；不得拿自动估点填成人工记录。

回滚：旧音视频和计划不删除。v2 发布只切换 JSON 指针，重新指向已有且复核通过的旧候选即可；不能重新请求一条声音冒充回滚。代码回退应仅撤销本轮相关文件 diff，保留用户原先的 resource 文档变更；不要使用全仓 reset/clean。

## 产物位置与测试副作用

- `state/production-20260910/listening/neutral_master.wav`：59.99 秒原生中性主轨。
- `state/production-20260910/listening/first_unit_slower_preview.wav`：只降低第一段速度的本地试听版。
- 同目录 `unit-01..03`：服务端 raw、规范 WAV、compiled/effective requests、质量结果。
- `state/production-20260910/capability_matrix.json`：当前特定音色能力边界。
- `state/production-20260910/baseline_preservation.json`：历史文件核对范围。
- `.tmp/production-20260910`：基线清单、所有测试日志、SDK transport 探针、计划和原始 assembly。

原仓库 Qwen 测试使用 `app/services/_tmp_qwen_test.mp3` 与 `_tmp_qwen_test2.mp3` 固定路径，基线运行覆盖并删除了原有的这两个未跟踪调试文件；未保存其原始字节，不能声称已经恢复。已改成 pytest 临时路径。原基线测试和早期针对性回归没有完整外网隔离，不能由本地记录证明其真实接口调用次数为零；后续测试已同时封锁 socket 和提供商凭据。明确授权的 271 字小样有独立请求记录，3 次 TTS、3 次 ASR 可追溯；结算金额以提供商账单为准。

下一步应先听第一段节奏预览，确定该段目标语速；随后做该段一次原生 CosyVoice 3.5 重生，复用后两段。再解决剩余文字检查/人工复核，选择第二段真实正文验证泛化，最后进行真实画面、字幕对齐和整片验收。当前不得恢复整季自动批量生成或标记 production_ready。
