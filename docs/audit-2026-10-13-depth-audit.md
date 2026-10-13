# 深度审计报告

---

* 审计日期：2026-10-13
* 基线：HEAD = `dc1ab07`，分支 `main`
* 未提交改动：仅 `episodes/caomin-02.json`（**stat 缓存残留，非真实内容改动**），另有 6 个此前未跟踪的 `bin/*.py` 与 1 份 `resource/` 剧本
* 环境：Python 3.13.14（`.venv-core` / `.venv-asr` 各自），ffmpeg 仅存在于 `D:\hv\tools\ffmpeg\bin`（不在 PATH）
* 测试基线：`-m production` 188 passed；`-m shared` 6 failed；`-m legacy` 5 failed

---
## 1. 当前项目能做什么、不能做什么

### 能做

- `v2` 的 **script → images → tts → delivery → quality → alignment → timeline → compose → verify** 九阶段在代码层面全部实现并可执行，`production` 测试组 188 条全绿。外部母带接入（`dc1ab07`）之后，**新集可以不再依赖 CosyVoice 付费配音**，这是刚打通的关键路径。
- v2 的证据模型本身是扎实的：candidate 的 228–307 项输入全部逐 sha256 绑定，`artifacts_current()` 一票否决；两份人工审听/审看各自绑自己的 receipt 哈希；`published.json` 指针绑定 candidate 字节。这套设计没有为了"跑得通"而放松。
- `delivery.apply` 对外部母带已正确跳过，并留下明确记录而非静默。
- 影像资产质量是真的：**没有一张底图是字节级或感知级重复**（256 位 pHash，Hamming ≤ 16 判近重，全量 687 张全部唯一）。

### 不能做（**这是本次审计的核心结论**）

**一集都没有可供投产的成片。** 具体地说：

- **v2 侧 0 集交付**。3 集（laogongtuan、lishimin-01、lishimin-01b）持有 `approved=True` 的 `published.json`，但其中 **2 集的 candidate 已被代码漂移判 STALE，第 3 集（lishimin-01b）的 candidate.mp4 根本不在磁盘上** —— 审批是真的审了，但被审批的那版视频已经不存在了。
- **legacy 侧只剩 2 集可用**（laogongtuan、lishimin-01，`content_approved=True`）。这两集恰好就是 v2 里已 STALE 的两集，所以是全仓**唯一**的两份可信交付。
- **caomin-02 的成片字节已经变了**：审批时 `20420f907e…`，磁盘上现在是 `9190173647…`，26229 帧、1049.232s 完全一致，文件大小 +3.6MB。mtime 2026-10-12 21:14，审批时间 2026-10-08 18:59 —— 审批后 4 天成片被静默重编码，而全盘搜索**找不到任何一份匹配审批哈希的备份**。`output_binding.verify()` 准确报了 `ok=False`，但 `production_admission.check()` 因此判 caomin-02 不合格，且这个失败**没有任何自动化提醒**。
- **生产 readiness 为 False**，两项 blocker：worker 心跳已停 14.4 小时；`production-validation.json` 的 `source_fingerprint` 是 `c575192b…`，当前实现指纹是 `ee9bf537…`，**不匹配**。

### 一句话状态

**能稳定复现已验证的影像，不能产出任何一集可发布的成片。**

---
## 2. 实际工作链路与权威数据源

```
【作者输入】（唯一真源）
  episodes/{id}.json            帧结构（唯一权威，见 §3-①）
  episodes/{id}/script.md        解说脚本原文
  episodes/{id}/scene_plan.json 人工分镜 → 引用的底图
  episodes/{id}/image_prompts.json  每张底图的 en 描述（机器拼风格，见 §3-⑥）
        │
        ├─► v2：script → images → tts(外部母带/CosyVoice) → delivery → quality
        │        → alignment → timeline → compose → verify → published.json
        │        权威：run.json + receipt(228–307 项输入 sha256) + published.json
        │
        └─► legacy：script → assets → shots → compose → verify
                 权威：input-approval.json(输入指纹) + output-binding.json(输出字节)
                       + external-audio-binding.json(音频-帧绑定)

【最终决策权】
  帧结构裁决   → episodes/{id}.json（绝不能由 split_script 重算）
  音频-帧绑定  → external-audio-binding.json（音轨字节级）
  v2 成片裁决 → published.json + review + listening_review（三份哈希互锁）
  legacy 成片 → input-approval.json + output-binding.json
  生产资格    → state/acceptance/production-validation.json + assess()
```

### 关键事实：`split_script` 不是恢复路径

AGENTS.md 反复强调这一点，本次独立验证确认。`episodes/{id}.json` 的 `frames[]` 是**手工编辑过**的产物（caomin-01: 4 帧删除、2 对合并、2 个 bed 标记），没有任何东西记录这些编辑。任何工具如果基于 script.md 重算帧列表，产出的就是**另一部片子**。这是全仓最高优先级的一条架构约束。

---
## 3. 问题清单（按严重程度）

### P0-1 · caomin-02 成片在审批后被静默替换，且原件已不可恢复

| 项 | 内容 |
|---|---|
| 类型 | 证据缺失 + 恢复风险 |
| 证据 | `output-binding.json` 记 `sha256=20420f907e…`、`bytes=498581896`；磁盘 `episodes/caomin-02/out/caomin-02_m1.mp4` 为 `9190173647…`、`502190148`，mtime 2026-10-12 21:14:41；审批 `reviewed_at=2026-10-08 18:59:11`。ffprobe 两侧 `nb_frames=26229`、`duration=1049.232125` 完全一致 |
| 触发路径 | legend-legacy `compose` 重跑或任何重新导出；帧数守恒所以**没有任何帧数断言会响** |
| 复现状态 | **已复现**：file_sha256 实测不等；全盘 `rglob('*.mp4')` 无一份匹配 `20420f907e…` |
| 影响 | caomin-02 已审批成片不可恢复；`production_admission.check('caomin-02')` 判 ineligible，但**无任何告警机制**。当前 caomin-02 磁盘上的版本是未审批字节 |
| 根因 | 输出绑定是**事后校验**而非**写入保护**：它只回答"现在和审批时是否一致"，从不阻止替换，也没有人消费这个 `ok=False` |
| 最小修复 | ①把 `out/*.mp4` 纳入包装性保护（compose 时若与绑定不符，要求显式 `--force` 并写事件记录）；②`check()` 的 ineligible 结果进入 `readiness.assess()` 的 blocker，而非只躺在 episode 状态里 |
| 反例 / 验收 | 验收：改动 `out/caomin-02_m1.mp4` 一个字节后重跑 `check()`，必须报 ineligible 且 `assess()` 出现新 blocker；恢复后幂等 |

### P0-2 · lishimin-01b 的已审批 candidate.mp4 已从磁盘消失，且 22 张底图缺失使重建不可能

| 项 | 内容 |
|---|---|
| 类型 | 证据缺失 + 数据丢失 |
| 证据 | `published.json` 的 `receipt` 指向 `renders/cf571dcd…/candidate.json`（存在，307 项输入），其 `candidate_path` 指向同目录 `candidate.mp4` —— **`exists()==False`**；`review=approved`、`listening_review=approved`。同时 `scene_plan.json` 57 个 scene 中 **22 个引用的底图不可解析**（`LA01/LA02/LA03/LA05/LA06/LA07/LA10/LB07/LB08/LB09/LB17/LC01/LC03/LC04/LC06/LC07/LC10/LC11/LD01/LD04/LD10/LD11`，指向 `episodes/lishimin-01b/shots/*.png`，文件不存在）。`can_assemble_shots('lishimin-01b')==False` 是唯一正确报警的 gate |
| 复现状态 | **已复现**（静态确认 + 路径存在性） |
| 影响 | lishimin-01b **永远无法重新出片**：审批记录指向不存在的字节，重建所需的底图也已丢失。这是一次不可逆交付丢失 |
| 根因 | 审批与"资产仍在"之间没有绑定；`published.json` 的 approved 指针从不校验 candidate 文件存在 + 底图可解析 |
| 最小修复 | ①`published()` 与 `delivered_approval()` 立即增加 `candidate_path.is_file()` 短路（`delivered_approval` 已有此检查，但 `published.json` 自身的 approved 标志没有）；②把 22 张底图缺失作为 readiness blocker；③评估是否从 lishimin-03 的 100 个可解析 scene 中回收（41 张来自 reuse） |
| 反例 / 验收 | 验收：`published.json` approved 但 candidate 不存在时，`get_status()` 必须 status=STALE **且** 给出"审批成片已丢失"专属 blocker（现在只给 "candidate inputs changed"） |

### P0-3 · caomin-01 / lishimin-04 的外部音频绑定自相矛盾：磁盘 wav 与绑定记录逐帧不符

| 项 | 内容 |
|---|---|
| 类型 | 代码缺陷（数据完整性） |
| 证据 | 两集 binding 的 sha256 **全部匹配**（238/238、195/195，0 mismatches），但 `_wav_frames()` 与记录的 `samples` **全部不符**（238/238、195/195）。长度比直方图：caomin-01 `{2.0: 122, 0.0: 115}`，lishimin-04 `{2.0: 102, 0.0: 92}`。即 **115/238 与 92/195 个 wav 是 44 字节的空文件（0 采样）**，其余正好是记录值的 2 倍。episode `audio_duration_ms` 与 binding 一致（80/80）而非与磁盘（1/80）。交付 mp4 帧数 20934/18638，而当前帧计划只推出 20655/18478 帧 → **成片比帧计划多 279/157 帧**，说明成片早于 2026-10-01 的帧修复 |
| 复现状态 | **已复现**（逐文件 wave 解析） |
| 影响 | 这两集的 legacy 交付**不可重新装配也不可验证**：`verify_binding` 报 FAIL，`production_admission` 判 ineligible。且 `verify_binding` 在第一个失败处 `break`，把"238 帧全错"显示成"frame-000 一个错" |
| 根因 | 2026-10-01 的 caomin-01 事故与后续修复只恢复了**帧元数据与绑定哈希**，没有恢复（或从未正确写入）`audio/*.wav` 实体；`_wav_frames` 校验虽然写对了注释（"sha256 只能证明文件没被换过，不能证明切对了"）却在 break 处掩盖了规模 |
| 最小修复 | ①移除 `verify_binding` 的 `break`，汇总全部失败；②二选一：a) 从母带用 binding 的 `start_sample/samples` 重新切分（binding 自洽，此路可验证）；b) 明确把这两集标记为"不可重建的遗留交付"并冻结 |
| 反例 / 验收 | 验收：修完 `caomin-01` 的 `verify_binding` 必须 `ok=True` 且返回 238 个 `fails=[]`；若选 b)，`get_status` 必须有专属 blocker 而非含糊的"binding invalid" |

### P0-4 · 三集的已审批 v2 candidate 全部因代码漂移失效 —— 包括"只改了源码"这一类

| 项 | 内容 |
|---|---|
| 类型 | 设计不合理 |
| 证据 | `laogongtuan`（228 项输入漂移 21）、`lishimin-01`（262 项漂移 21）、`lishimin-01b`（307 项漂移 19）。拆分解耦后：**内容输入漂移 = 0**（图片、音频、timeline、manifest 全部未变），漂移集中在 20 个 `app/**/*.py` + `app/config/config.yaml`。`review` 与 `listening_review` 两份都是 `approved`，originals 均完好（`candidate_bytes_match=True`，lishimin-01b 除外） |
| 关键澄清 | `config.yaml` 的漂移**不是误报**：`5eb2b51`（2026-09-27）新增 `compose.image_transition_frames: 6`，发生在这些 candidate 渲染之后。所以旧 candidate 确实不代表当前渲染行为。**代码漂移是真实语义，不是指纹过敏** |
| 影响 | 全仓唯一两份可信交付（laogongtuan、lishimin-01，无论在 v2 还是 legacy）都处于 STALE；新代码跑出来的片子和审批时看的不再是同一套行为，但**没有任何机制区分"行为等价的重构"与"改变像素的重构"** |
| 根因 | candidate 的输入绑定把**渲染器源码本身**当作输入。这在字节语义上是对的，但在"审批有效期"语义上是错的：一次无害的 import 重排就让两集退化为不可交付 |
| 最小修复 | 把 candidate 绑定拆成两层：`content_inputs`（media/plan/timeline，变了必须重建）与 `renderer_inputs`（源码哈希，变化时标记 `renderer_changed` 但**不**撤销审批；只在 `renderer` 变更影响了像素语义时要求重审）。像素语义影响由"渲染器是否读取了会改变输出的配置/逻辑"判定，最小可执行做法是把 `implementation_identity` 细化为 `frame-affecting` 与 `administrative` 两个指纹 |
| 反例 / 验收 | 验收：①纯注释/重排改动 → `approved` 保持，status 不得变为 STALE；②改动 `image_transition_frames` 或运镜公式 → 必须要求重审。反例测试：对 caomin-02 做一次"只加空行"的提交，确认审批不被撤销 |

### P1-5 · 217 处生图 prompt 违反 AGENTS.md 明令禁止的风格词规则，且预检完全放过

| 项 | 内容 |
|---|---|
| 类型 | 代码缺陷（门禁缺失）+ 设计不合理 |
| 证据 | `image_prompts.json` 中 `en` 字段含禁用风格词共 217 处：`photorealistic` 78+57+64=199（lishimin-01/lishimin-01b/lishimin-02 全集）、`chiaroscuro` 2（laogongtuan、lsm-15min）、`flat` 12、`comic` 1，另有 4 处要求画面渲染文字。运行时拼接后的 prompt 形如 `…, photorealistic, cinematic.` **紧接** negative_clause `…no cartoon, no anime, no 3d render…` —— 要求与禁止**正面冲突**。`screen_prompt()`（orchestrator.py:206-238）只查空/占位符/过短/超长，**没有任何风格词检查**；`tests/` 全库 0 条断言 style words 缺失 |
| 关联 | 这正是 AGENTS.md 记录的 caomin-01 事故（风格词覆盖 house look，36 张全部重做）。规则写在文档里，**没有一行代码或测试守护它** |
| 影响 | 已交付集的视觉一致性不可证明：prompt 同时要求 photorealistic 与禁止 realistic，模型必然择一，实际风格不可预期。caomin-02/03/04 干净，lishimin 系列全军覆没 |
| 根因 | AGENTS.md 把一条**会被模型解析冲突的生产规则**寄托于人工记忆，未将其转化为 `screen_prompt` 的硬断言 |
| 最小修复 | 在 `screen_prompt()` 增加禁用词表检查（词表直接来自 `_meta` 的自述规则），命中即 `ok=False` 并给出键名；同时新增一条 production 测试，对全部 episodes 断言 `en` 不含禁用词 |
| 反例 / 验收 | 反例：把 `photorealistic` 写进任一 `en`，`build_image_jobs` 必须抛错。注意：**修 prompt 需要重新生成图片，这是付费决策，不在本轮范围内** |

### P1-6 · production-validation 证据与当前实现不符；worker 已死 14.4 小时

| 项 | 内容 |
|---|---|
| 类型 | 证据缺失 |
| 证据 | `state/acceptance/production-validation.json` 的 `source_fingerprint=c575192b…` vs `implementation_identity()=ee9bf537…`；evidence 的 5 项验证（multi_episode_current_rebuild / kill_resume / timeout_recovery / real_provider_models / unattended_worker_full_batch）全部为 `true`，但都基于 2026-09-22 的 `batch-launch-20260922` 快照。worker.json `pid=43152`，`heartbeat_age=14.4h`，`alive=False`。**14 个 episode 残留 `.produce.lock`**，5 个 queue lock |
| 影响 | `assess()` 正确地报 `ready=False`，但生产资格实际上**只由一份 21 天前的快照背书**；任何"是否可投产"的判断目前没有当前证据 |
| 最小修复 | 无人值守批次必须重跑以刷新指纹；在此之前 readiness 应明确显示"证据过期"而非仅"worker 不在线" |

### P2-7 · 6.54 GiB 冗余渲染产物；14 个陈旧锁；7 个 smoke 测试残留

| 项 | 内容 |
|---|---|
| 类型 | 设计不合理（资源） |
| 证据 | `laogongtuan` 3 个 render 目录中 2 个冗余（1.18 GiB），`lishimin-01` 11 个中 9 个冗余（5.36 GiB），合计 6.54 GiB 重复段字节。14 个 `.produce.lock` 与 5 个 queue lock 全为死进程残留。7 个 `smoke-1788923*` episode 在册。`episodes/` 共 53.31 GiB |
| 影响 | 磁盘与认知负荷；`--image-key` 续跑会因目录堆积而更难判断哪个 candidate 是当前产物 |
| 最小修复 | 增加 `render` 目录的保留策略（保留 published 指向的 + 最近一个）；锁改为带 owner heartbeat 强制解锁；smoke 集移出生产可见面 |

### P2-8 · 5 条 legacy 测试失败、6 条 shared 失败均为存量，其中 3 条与 P0-3 同根

`test_external_audio_binding.py` 3 条与 `test_output_binding_and_legacy_status.py` 3 条，实测根因就是 **P0-3 的绑定矛盾**（已 stash 验证：与我的改动无关，改动前同样红）。`test_state_truthfulness.py` 3 条与 `test_tts_fingerprint.py` 2 条涉及 FileNotFound，属同一批遗留。这说明**测试组很早就红了，但默认 `-m production` 门禁看不见它们**。

---
## 4. 多轮修复后问题仍反复出现的共同根因

### 根因一：门禁写在注释里，不在代码里

这是最致命的一条。P0-3 的注释**已经完整正确地描述了失效模式**（"sha256 只能证明文件没被换过，不能证明切对了"），但它决定的行为是"报一个错然后 break"。P1-5 的规则在 AGENTS.md 里写得非常详细——**而后只有 0 行代码、0 条测试守护它**。caomin-01 已经因为违反这条规则烧掉 36 张图，规则仍然没有变成断言。

**规律：每条"踩过的坑"都以散文形式记录在人读的文档里，而不是以失败测试的形式记录在机器读的仓库里。** `-m production` 默认门禁只跑 188 条，shared/legacy 的 11 条红色被系统性屏蔽，于是"官方 PASS"与"实际有 11 条红"可以长期共存。

### 根因二：哈希绑定的是"字节"，审批需要的是"语义"

P0-4 与 P0-1 是同一个病的两面。`implementation_identity` 的意图是"实现变了，证据可能失效"，但它无法区分无害重构与改像素的重构，于是**一次无害改动就能撤回一次人工审批**。而 P0-1 反过来：`output_binding` 绑定了字节，却**没有任何机制阻止字节被替换**——它只在校验时报告，校验结果又只被 `production_admission` 消费成一个布尔，无人告警。

**规律：把"是否该信任这份产物"简化成了"指纹是否匹配"，而匹配与不匹配都不能推出审批应该有效或失效。** 正确的答案需要区分"帧内容输入"和"渲染行为输入"、需要"写入时保护+替换时留痕"，而不是更严格的哈希。

### 根因三：恢复路径按"元数据可重建"设计，资产本身不被保护

lishimin-01b 的审批记录、receipt、307 项输入哈希全都完好，唯独 `candidate.mp4` 不在；caomin-01/lishimin-04 的帧元数据与绑定哈希全都自洽，唯独 115/92 个 wav 是 44 字节空文件。两次都是**索引完好、实体丢失**。

**规律：证据链围绕"可以重新算出"的元数据构建，而把不可再生的字节（渲染结果、切割音频）当作可随时重派的缓存。** 一旦唯一的可再生前提（帧结构、底图）也失效，整集就永久归零。

### 根因四：报告真相与阻止错误耦合在同一函数里

`verify_binding` 把"报告全部问题"和"快速失败"混在一起，`break` 让 238 个错显示成 1 个。`screen_prompt` 把"体检空 prompt"和"提示词合规"混在一起，于是后者从未实现。`output_binding.verify` 同时承担"复核"和"唯一防线"两个角色，因此它从来只是复核。

---
## 5. 三份清单

### 必须修改（阻碍投产，不修则产出不可信）

1. **P0-1** `out/*.mp4` 写入保护 + `check()` 不合格时进入 readiness blocker
2. **P0-2** `published.json` approved 的 candidate 存在性前置校验 + lishimin-01b 22 张底图的处置决策（回收 or 冻结）
3. **P0-3** caomin-01 / lishimin-04 音频绑定：重切 or 冻结（**需要你拍板**）；无论哪条，先移除 `verify_binding` 的 `break`
4. **P0-4** candidate 绑定拆 `content_inputs` / `renderer_inputs`，让无害重构不撤回审批
5. **P1-5** `screen_prompt()` 风格词硬断言 + 对应测试（**修数据需重新生图，属付费决策**）
6. **P1-6** 刷新 production-validation 证据或明确标注过期

### 必须清理（不阻碍投产但持续造成误判）

1. **P2-7** 6.54 GiB 冗余 render、14 个死锁、7 个 smoke 集
2. **P2-8** 11 条存量红测试：把 shared/legacy 纳入默认门禁，或显式记录其不跑的原因
3. `episodes/caomin-02.json` 的 stat 缓存假修改（`git update-index --refresh` 可消）
4. 6 个未跟踪的 `bin/*.py` 与 1 份未跟踪 `resource/` 剧本：入版或删除，不应长期悬空

### 暂时保留（有正当理由，不因本次审计而否定）

1. **v2 与 legacy 双轨**：v2 是目前唯一能花钱的路，legacy 承担全部已交付集，`produce()` 默认 `v2` 且 legacy 拒绝付费阶段 —— 分工清晰，正确
2. **v2 无多音字替换、无镜长上限**：AGENTS.md 已论证是有意设计分歧（加 `MAX_FRAMES` 会作废三集时间线）
3. **v2 的 `require_usable` / 双审机制**：每组镜头人工审查，审批绑定 receipt 哈希，语义严谨
4. **`spoken_text.compile_text` 的标点纯变换 + 硬断言**：这是 v2 消除多音字污染的手段，比在 legacy 加门禁更彻底
5. **`production_v2` 的逐段编码 + 断点续跑 + 采集守恒断言**：26229 帧守恒已验证
6. **`external_narration`（`dc1ab07`，本轮新增）**：诚实性门禁设计正确，forced_alignment 47/47 通过即证明切片正确；20/47 transcript QA pending 是既有 ASR 能力限制，非缺陷

---
## 6. 整改顺序与验收标准

依赖关系：**P0-3 的"是否冻结"与 P0-4 的拆分方案需要你先拍板**，其余可并行。

### 第 0 步（阻塞其余全部）· 决策项

- **Q1** caomin-01 / lishimin-04 是重切音频重建，还是标记为不可重建的遗留交付冻结？
- **Q2** lishimin-01b 的 22 张底图是否从 lishimin-03 等 100 个可解析 scene 回收，还是接受该集永久丢失审批？
- **Q3** candidate 绑定拆分是否接受"源码变化标记但不撤销审批"？

### 第 1 步 · 止血（无决策依赖）

| 项 | 动作 | 验收反例 |
|---|---|---|
| P0-2a | `published.py` 增加 candidate 存在性短路 | 删掉 lishimin-01b candidate.mp4 后 `get_status` 必须出现"审批成片已丢失"，而非"inputs changed" |
| P0-1a | `output_binding.verify` 结果接入 `assess()` | 篡改 caomin-02 mp4 一字节 → readiness 出现新 blocker |
| P1-6a | `assess()` 在指纹不符时报"证据过期 N 天" | 用旧 evidence → blocker 文案含天数 |

### 第 2 步 · 依赖 Q1/Q2 的历史集处置

| 项 | 动作 | 验收反例 |
|---|---|---|
| P0-3a | 移除 `verify_binding` 的 `break`，返回全部失败 | caomin-01 必须报 238 个 fails 而非 1 个 |
| P0-3b | 按 Q1 执行 | 选重建：修完 `verify_binding.ok=True`；选冻结：`get_status` 有专属 blocker |
| P0-2b | 按 Q2 执行 | 选回收：`can_assemble_shots('lishimin-01b')==True`；选放弃：readiness blocker 明确列出 22 键 |

### 第 3 步 · 依赖 Q3 的审批语义修正

| 项 | 动作 | 验收反例 |
|---|---|---|
| P0-4a | 拆 `content_inputs` / `renderer_inputs` | 纯注释提交不得撤销审批 |
| P0-4b | 区分帧影响型与行政型源码变更 | 改 `image_transition_frames` 必须触发重审；改 import 顺序不得触发 |

### 第 4 步 · 质量门禁补课

| 项 | 动作 | 验收反例 |
|---|---|---|
| P1-5a | `screen_prompt()` 风格词断言 | 任一 `en` 含 `photorealistic` → 抛错 |
| P1-5b | production 测试：全 episodes 断言风格词 | 注入违规 prompt → 测试红 |
| P2-8a | shared/legacy 入默认门禁或显式豁免 | 默认 `pytest` 必须显式报告这 11 条的状态，不能静默 deselect |

### 第 5 步 · 证据刷新与清理

| 项 | 动作 | 验收反例 |
|---|---|---|
| P1-6b | 重跑无人值守批次刷新 evidence | `source_fingerprint` 与 `implementation_identity()` 一致，worker `alive=True` |
| P2-7a | 清理冗余 render / 死锁 / smoke | 磁盘下降 ≥6.5 GiB；无死锁残留 |

### 第 6 步 · 新集闭环（验证 2a 真的可用）

用 2a 路径完整跑一通新的 caomin 集至 `verify`，产物受第 3 步之后的门禁保护。**这一步是本次整改是否成功的唯一证明。**

---
## 7. 审计覆盖表

| 区域 | 覆盖程度 | 方法 / 说明 |
|---|---|---|
| Git 基线、未提交改动 | **已检查** | `git status` + blob hash 比对（确认 caomin-02.json 为 stat 假象） |
| 有效配置 | **已检查** | `config.yaml` 全量展开 + 3 个 channel + compose 段落；`requirements`/`runtime` 未逐版本核对 |
| 入口 | **已检查** | `cli.py` 全部 action、`orchestrator.produce` 分发、API/worker 入口 |
| v2 九阶段实际路径 | **已检查** | 逐 stage 读 `episode_production.run`，确认 `tts` 双分支、`delivery` 外部跳过、`images` 不预留 TTS 预算 |
| 外部母带接入（本轮新增） | **已检查** | `external_narration.py` 全文 + 47/47 单测 + 端到端 342 字幕、0 乱序、forced_atoms 47/47 通过 |
| 交付状态（24 集） | **已检查** | `get_status` / `_legacy_status` / `output_binding.verify` / `external_audio.verify_binding` / `production_admission.check` 全量跑 |
| v2 已审批 candidate | **已检查** | published.json + receipt 输入漂移拆分为 content/code 两类 |
| caomin-02 成片替换 | **已复现** | sha256 实测 + ffprobe 两侧帧数/时长 + 全盘搜索无备份 |
| lishimin-01b 底图缺失 | **已复现** | `_resolve_image` 逐个解析 57 scene |
| caomin-01/04 音频绑定矛盾 | **已复现** | 逐文件 wave 解析 + 长度比直方图 + sha256 比对 |
| 底图质量（重复/尺寸） | **已检查** | 687 张全量 pHash + sha256；`@2x` 副本已正确排除 |
| 生图 prompt 合规 | **已复现** | 词表扫描 12 集 + 运行时拼接 prompt 抽样 + `screen_prompt` 源码 + 测试库断言扫描 |
| 底图实际被 scene_plan 使用 | **已检查** | 13 集 scene_plan 引用全部可解析（lishimin-01b 除外） |
| frames_fingerprint 一致性 | **未验证** | 尝试调用 `frames_fingerprint` 签名错误，未能比对 stored vs recomputed。**列为待验证** |
| Cost / 预算账本 | **仅抽查** | 读了 `provider_budget` 接口与 `run.json` 的 budget_path，**未核对真实计费流水** |
| 付费 API 行为（生图 / TTS） | **未验证** | 按约束不调用。`strict_param_validation=False` 与 channel 配额的运行时行为**属待验证** |
| Vision 视觉质量 | **未验证** | 按约束不调用。版权、朝代服饰、风格一致性**未经视觉确认** |
| worker / 无人值守真实恢复 | **未验证** | 只验证 heartbeat 已死 14.4h，**未重跑 kill_resume / timeout_recovery** |
| BGM / 音效轨道 | **未检查** | 未进入本次范围 |
| 6 条 shared + 5 条 legacy 红测试根因 | **已复现** | stash 验证与本次改动无关；其中 3+3 条根因即 P0-3 |

---
## 结语

本次审计**未修改任何生产代码、配置、输入或产物，未删除文件，未提交推送**。所有复现材料位于 `C:\Users\coumoo\AppData\Local\Temp\opencode\audit\`，为只读诊断脚本。

项目当前的真实状态是：**影像生成链路已经修好且证据可信，但没有任何一集的"已审批视频"是当前可交付的** —— 一集审批后被静默替换（P0-1）、一集审批的字节已丢失且无法重建（P0-2）、两集的音频绑定与磁盘实体全面矛盾（P0-3）、三集的 v2 审批被源码漂移撤回（P0-4），而唯一两份幸存交付恰好来自这批 STALE 剧集。

请确认整改范围，特别是第 0 步的三个决策项。**在你确认之前，我不会实施任何修复。**
