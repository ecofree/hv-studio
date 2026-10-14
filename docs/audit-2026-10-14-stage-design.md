# 分阶段设计审计报告（v2 九阶段）

- 日期：2026-10-14
- 基线：`53b0d94`，分支 `main`
- 方法：三组并行只读审计（script+images / tts+quality+alignment / timeline+compose+verify），关键指控已由审计员亲自抽验代码确认
- 边界：未改生产代码/配置/产物，未提交；未调用付费接口；新增材料仅审计临时目录

---

## 1. 项目状态：能做什么、不能做什么

**链路是通的，门禁是密的，但三处"灯绿了、事没保住"。**

- 能：script→verify 九阶段全部可执行；xlc-01 已走通全链（430 字幕、159/159 词级时间、32800 帧成片）；laogongtuan/lishimin-01 在 Q3 拆分后回到 APPROVED。
- 不能：
  - 改一行绘图代码（overlay/karaoke/word_timing），已缓存的 v2 段**不会失效**，且 verify 只数帧数——修了等于没修，还没人知道。
  - 只看 `approved==True` 的调用方，可能放行一张**底图已丢失、永远无法重建**的集（approved 与缺图 blocker 可并存）。
  - 新集首跑 images 仍走无 scene 的 fallback 分支：**无 prescreen、无参数校验、无去重**，占位符 prompt 可直接烧配额。

## 2. 实际流程图与权威来源

```
script.md(真源, fingerprint)
  → document.json(解析基线) + plan.json(可演进指针, plan_id)
  → images: scene_plan.json + image_prompts.json → build_image_jobs → generate_many
      → plates（幂等键: request_fingerprint + receipt + output_sha256）
  → tts: takes/<unit>-<uuid>/{compiled_request,effective_request,raw.wav,audio.wav,quality.json}
      → master(拼接/规范化) + manifest.json（plan_sha/master_sha/takes sha/alignment display_text）
  → delivery: pause 插入精确 PCM（direction 只记台账，不执行）
  → quality: quality.json + dispositions/<fp>.json → pending() 活账
  → alignment: alignment_evidence(alignment/) ← forced aligner 逐 take 重算词时
  → timeline: shots[] + subtitles[]（scene 边界必须落词界，否则抛错）
  → compose: segments → concat → mux → candidate.mp4 + receipt.json（455 项输入 sha）
  → verify: verify_media（帧数/时长/全量解码）→ CANDIDATE
  → publish: visual + listening 双评审 + hash 互锁 → published.json → APPROVED
```

权威来源：帧结构=`episodes/<id>.json`；口播=`script_narration.md`；unit 文本三字段以 `document.json` 为准（`display_text` 除外——见 P1-3）；成片=`candidate.mp4`+receipt；发布=`published.json`+双评审。

## 3. 问题清单（按严重程度）

### P0

**P0-1 v2 段缓存实现 hash 漏掉 overlay/karaoke/word_timing，修了白修还看不见**
- 类型：代码缺陷。`app/services/video_segment_cache.py:12-17` DEPENDENCIES 仅 8 文件，无三者；legacy `compose.py:1010-1011 layer_src` 反而含了，不对称已抽验确认。
- 触发：改三者任一实现 → 跨目录 `restore`（`production_v2.py:411-418`）照旧复用旧段 → verify 只数帧数（`production_v2.py:434-436`）→ 绿灯。
- 影响：绘图层 bug 修复永远到不了复用段；或反向——以为增量失效正常。
- 最小修复：在 DEPENDENCIES 加三模块（一行）。验收反例：改 `karaoke.py` 空行，前后段指纹必须不同。

**P0-2 `approved=True` 可与“底图丢失”并存，只看灯即放行无法重建的集**
- 类型：设计不合理。`production_status.py:99-113`：approved 在 99 行算完，缺图 blocker 在其后追加。已抽验确认。
- 最小修复：`approved &= not scene_images_missing`（一行），或文档强制调用方同时看 blockers。验收：01b 缺图状态下 `approved` 必须为 False。

**P0-3 无 scene 新集的 images fallback 无 prescreen、无参数校验、无去重**
- 类型：设计不合理。`production_inputs.py:28-45` 自建 prompt，不调 `screen_prompt`，不校验参数；`build_image_jobs` 不报 dup（`screen_image_prompts:261-264` 的逻辑 build 路径没有）。
- 影响：下个新集首跑即可能白烧配额，且 `>50KB 即幂等跳过` 会把错图永久化（orchestrator 注释事故）。
- 最小修复：fallback 复用同一 `screen_prompt` + `strict_param_validation` + 按 `request_fingerprint` 去重（~30 行）。

**P0-4 `spoken_error` 重合成依赖所有入口经门禁，外部直调 `render` 无门**
- 类型：证据缺失。`require_usable` 只在 `current_manifest/current_candidate/_render/_publish` 生效；`hv narration render` 直调可绕过（subagent 静态确认）。
- 最小修复：在 `production_v2.render` 入口加 `require_usable(validate_assembly(...))`（两行），与 `_render` 内已有检查同源。

### P1

**P1-1 `screen_prompt` 无风格词表，AGENTS.md 禁令零代码守护**（217 处违规的根因延续）
- `orchestrator.py:212-237` 只查空/占位/过短；风格词与“图中文字”要求直接进 `en` 与 `negative_clause` 打架。最小修复：与 AGENTS.md 同源的黑词表，strict 拒、默认 warn；build 与 fallback 共用。

**P1-2 重复生图请求无去重**：`generate_many` 前按 `request_fingerprint()` 合并同 fp job，一存多用；`build_image_jobs` 输出 dup 报告。注：xlc-01 的 159→45 已是人工去重，机器层仍无此能力。

**P1-3 `display_text` 不在 plan 防篡改校验内**：`narration_pipeline.py:142,171` 只比三字段，`display_text` 直入 manifest（`:286`）即字幕门禁。karaoke 索引用的还是 `source_text`（`karaoke.py:76`），与门禁源头不一致。最小修复：校验加 `display_text`（或断言三文本一致）+ karaoke 改取 `display_text`。

**P1-4 三遍 ASR 全串行**：合成时 check → `quality.build` → `alignment.build`，`production_quality.py:12-29` 与 `forced_alignment.py:133-145` 逐 take 串行。首刀：有界并发 W=4（与 `config.yaml:28` threads 对齐），local_asr 缓存保证幂等；次刀：`quality.build` 对 `passed` take 复用 transcript 不重识别。

**P1-5 overlay 改一处全片重渲**：overlay sidecar 进 `inputs` 但无段窗口过滤（抄 `compose.py:1026-1032` 做法，把 sidecar 内容按段窗口进段指纹）。与 P0-1 同文件区，可一次改完。

**P1-6 NVENC 降级混合编码固化**：`production_v2.py:426-433` 只记 `encoder_fallback` 不告警，软硬混段 `concat -c copy` 成片。最小修复：学 legacy 打印警告 + `get_status` 暴露 `encoder_mixed` 维度（`reusable_records` 失效是重手，先只做可见性）。

**P1-7 local_asr 缓存 key 含 threads**：调参即全 bust（`local_asr.py:53`）；`CHUNK_SAMPLES_16K` 一改分段缓存全 miss。分离“音频/模型”与“性能参数”键，或改参时告警。

**P1-8 direction 三口径术语统一**：`effective_unit` 窄 / `dispositions` 中 / `admission.inputs` 宽，同一 cue 三处状态名不同。附映射表注释，不改行为。

### P2

- P2-1 `MIN_UNIT_COVERAGE` 在 `external_narration.py:58,69` 重复定义，删第一处。
- P2-2 `technical_quality`（快照）与 `pending()`（活账）双轨易误读，状态页加一处文案。
- P2-3 `review_required`（`production_timeline.py:71`）零消费者：要么进门禁，要么改名 `human_review_pending` 并注明仅声明。
- P2-4 输入先批、成片后绑的空窗：`output_binding` skipped-ok + admission 仅有 `output_sha256` 时复核，应在 status 显式标 `unbound` 而非 `ok`。
- P2-5 v2 `script` stage 实为空转（`episode_production.py:141-142 pass`），与 legacy `stage_script` 同名不同义，改名或 result 标注 noop。
- P2-6 `_probe_nvenc()` 无参调用与 `compose._probe_nvenc(encoder)` 签名错位，显式传参。
- P2-7 `check_capacity` 预检信息带上 retries 余量说明（`provider_budget.py:119-124` vs `:96-97` 已分离但提示没跟上）。
- P2-8 `generate_many` 把 done job 也入队（`image.py:775-777`）；缓存键用相对路径以便跨目录复用。收益小，排最后。

## 4. 多轮修复仍反复的共同根因（本次新增第五条）

1. **门禁写在注释/文档里，不在代码里**：风格词（AGENTS.md 有、代码无）、`review_required`（有写无读）、曾经的 `requires_upscale`（已删）。每条踩过的坑都以散文记录，无对应失败测试。
2. **哈希绑定字节，审批要语义**：receipt 绑 renderer 源码（Q3 已拆）；approved 与缺图 blocker 可并存（P0-2 本轮新例）。
3. **恢复按元数据设计，资产不被保护**：见 10-13 审计，不重复。
4. **报告与阻止耦合**：见 10-13 审计，不重复。
5. **粒度错配（本轮新根因）**：指纹太粗（全 receipt/全段）或太细（单文件）与变化粒度对不上——overlay 内容改→全段失效（浪费），overlay 代码改→零失效（漏网），renderer 改→审批撤销（过度）。修法统一：**让指纹粒度与变化的影响域同级**（P0-1、P1-5、Q3 都是同一病的三张脸，不要拆成三个活干，尽量一次改完指纹策略）。

## 5. 三份清单

**必须修改**：P0-1、P0-2、P0-3、P0-4、P1-1、P1-4（并发部分）、P1-6（可见性部分）。
**必须清理**：P1-8（注释）、P2-1、P2-3、P2-5、P2-6；`bin/` 下 5 个未跟踪脚本（align/build_overlays/draw_sandtable/install_plate/make_broll_plate）入版或删除，不应悬空。
**暂时保留**：双轨并行；v2 无多音字替换/无镜长上限；`require_usable` 双审；`spoken_text` 标点纯变换；逐段编码+断点续跑；`external_narration` 诚实门禁；`spoken_error`不清零语义（测试已钉）；`delivery.apply` 对外部跳过；`check_capacity` 不含 retries 的预检语义（已是记录行为）。

## 6. 整改顺序与验收（按依赖排，不新增 stage/controller）

1. **P0-1 + P1-5（一次改完，都是段指纹策略）**：DEPENDENCIES 加三模块；overlay sidecar 按段窗口进指纹。验收：改 karaoke 空行→段指纹变；改一处标题→仅当段重编，其余复用。
2. **P0-2**：`approved &= not scene_images_missing`。验收：01b 缺图态 approved 必 False；补齐后恢复（现有测试 `test_every_status_shape…` 仍绿）。
3. **P0-3 + P1-1 + P1-2（一处：fallback/build 共用同条 image_jobs 管线）**：prescreen + 参数校验 + fp 去重 + dup 报告。验收：占位符 en 在花钱前被拦；构造重复 prompt 只付一次钱。
4. **P0-4**：`render` 入口加 `require_usable`。验收：被拒 take 的旧 manifest 直调 render 必抛。
5. **P1-4**：quality/alignment per-take 有界并发 + passed 短路。验收：47 unit 集 wall time 下降（先测基线再并行，注意 1455/pagefile 前科，小步放量）。
6. **P1-6**：NVENC 降级警告 + `encoder_mixed` 维度。验收：混合成片在 status 可见。
7. **P1-3 + P1-8**：display_text 纳入校验 + karaoke 同源 + 术语表注释。验收：手改 display 后 tts 必抛；karaoke 高亮抽帧与字幕一致。
8. **P1-7 + P2 系**：缓存键分离、注释/改名类清理，打包一次做完。验收：全组测试绿。

## 7. 审计覆盖表

| 区域 | 程度 | 说明 |
|---|---|---|
| script（plan/document/切分/四字段/cues） | 已检查 | 读源码 + 13 集 prompts 计数 |
| images（prepare 双分支/幂等/并发/重试/计费/scoped/prescreen） | 已检查 | 读源码 + 并发数/阈值实测注释核对 |
| tts（synthesize 缓存/计费/canonical/双路门禁） | 已检查 | 读源码；未重跑合成 |
| quality（build/pending/disposition/spoken 语义） | 已检查 | 读源码 + tmp 拷贝实测循环 |
| alignment（windows/atoms/pack/evidence/长 unit 风险） | 已检查 | 读源码；40s+ 风险为静态推断 |
| timeline（边界/motion 默认/字幕 bounds/review 标记） | 已检查 | 读源码 + grep 无消费者 |
| compose（分段/缓存键/双层缺席/NVENC/音频钟/upscale） | 已检查 | 读源码；效率数字为静态估算 |
| verify（七层门/CANDIDATE-APPROVED 分野/publish 绑定/缺口） | 已检查 | 读源码 |
| BGM/音效轨道 | 未检查 | 本轮范围外 |
| 实际重跑验证（如 47 unit 并发收益） | 未验证 | 需跑生产链，已列验收前置 |
| 付费 API 行为 | 未验证 | 约束内不调用 |
| Vision 视觉质量 | 未验证 | 约束内不调用 |

审计止于此。未改任何生产代码，未提交。等你确认整改范围。
