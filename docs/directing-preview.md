# 原画、原声的导演计划与 xlc-01 样片

2026-10-18：首个实现与 84 秒改善样片已完成，仍是待审看的候选片。

## 本次结果

- 对比页：`state/creative-pilots/xlc-01-20261018/review/comparison.html`。两个视频和 HTML 保持在同一目录，可本地打开；当前预览地址为 `http://127.0.0.1:8875/comparison.html`。
- 原版与改善版均为 84 秒、1080×1920、25 fps、2100 帧，完整解码通过。原版片段由既有成片截取并转码，不能称为原文件的逐字节复制。
- 改善版沿用 5 张原画，编排 16 个镜头与 5 张标题卡。第一帧即时显示“托孤，还是枷锁？”，后续视点围绕病榻、双手、承继者与皇位推进。标题卡标记的是开场内部的叙事段落，不是整集已经完成的分幕。
- 原声前 2,016,000 个 PCM 样本与原母带完全一致；混音不变调、不变速、不改字。重点句局部增益为 0.6–1.5 dB，配以本地生成的轻量氛围声；47.3–55.96 秒撤掉背景音。混音峰值约 -1.41 dBFS，无削波样本。
- 源文本、分集 JSON、画面缓存、原母带、原成片等 17 个保护文件哈希不变；候选片输入哈希无漂移。本次没有调用模型或付费供应商。

## 接入方式

`app/services/directing.py` 编译可选的 `episodes/<id>/directing.json`。现有分集没有这个文件时保持原流程。本次导演计划只保存在样片目录，未写入 xlc-01 的正式分集目录。

计划包含 `version: 1` 和 `source_identity`，后者由 `source_identity(manifest)` 根据全部旁白单元的 ID 与文本计算。`visual_events` 指定已声明图片和明确的裁切轨迹；`cards` 指定独立于字幕的标题；`sound` 包含原声增益和氛围段。事件时间由 `{ "unit_id": "...", "text_start": 0 }` 锚定原文，并解析到当前强制对齐的词边界。开头和结尾可以使用 `{ "at": "start" }` / `{ "at": "end" }`。

源文本变化、未观察到的词边界、未声明的图片、越界/重叠的声音事件均拒绝编译。视觉和声音仍由既有 `production_v2` 渲染和验收，不建立第二套旁白时间轴，不套用 legacy 的镜头长度限制。渲染回执绑定原始计划、编译后的标题文件、时间轴和源码哈希。

标题新增可选 `fade_in_ms`，设置为 `0` 时第一帧可见；未设置时保留原淡入淡出行为与原有序列化字段。

## 重建独立样片

使用 `.venv-core/Scripts/python.exe bin/build_directed_preview.py`，依次执行 `prepare`、`align`、`render`，每次提供同一独立 `--destination`；准备时指定 `--episode xlc-01 --seconds 84`。目标目录须在正式 `episodes/` 之外，结尾必须位于完整旁白单元之间。

准备之后、渲染之前放入与该片段 source_identity 绑定的 `directing.json`。现成范例在 `state/creative-pilots/xlc-01-20261018/directing.json`；`author_plan.py` 记录此样片的创作参数。不要把片段计划直接复制成整集计划。

对齐与渲染应按项目约定脱离工具调用运行，输出日志，短间隔读取进度。运行前将 `tools/ffmpeg/bin` 加入 PATH。重建使用新的样片目录，保留已有候选片及其证据。

## 声音控制的实际边界

这版实现的是原声后期控制，尚未解决“同音色重新表演”。本地 Qwen clone 服务不消费 instruct；调用脚本现在会在网络请求前拒绝 clone + instruct，默认仍保留 clone_kai 与原参考音频，不会静默切换 CustomVoice。声音的感染力仍需要用户试听判断，不能由音量曲线或单元测试代替。

## 验证证据

- `scripts/ai-project-check.ps1 -ProjectRoot D:\hv -EvidencePath <独立路径>` 的 production 验收：250 passed、311 deselected。证据：样片目录的 `acceptance.json`、`acceptance.log`。
- 审计回归测试改用独立临时分集，不再读取前几个历史分集，也不向真实分集写入 synthetic 文件或占用生产锁。
- `final-verification.json`：保护文件、输入哈希、原声样本、字幕原文与几何、当前对齐证据、混音峰值。
- `review/verification.json`：两版媒体参数与完整解码结果；`review/comparison-contact.jpg`：10 个时点的对比；`review/comparison-page.png`：浏览器实测界面。
- 浏览器实测通过播放、暂停、47 秒跳转、排他声音切换。网页同步允许小量播放时钟漂移；抽测约 0.136 秒，不将其视为逐帧同步测量工具。

以上证明本次候选片技术有效，不代表艺术效果、历史内容、听感或整集发布已获批准。下一步应根据这段对比的实际反馈，调整首帧文字密度、镜头频率和声音强弱，再向整集扩展。
