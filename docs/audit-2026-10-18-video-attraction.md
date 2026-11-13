# hv 视频吸引力审计与改善方案

日期：2026 年 10 月 18 日。代码基线：`5b7e5397b4c4ad929e06a8df65f0e4e5bcaac0d1`，包含审计开始时已有的未提交文件。本文是只读审计及改造提案，不是成片批准，也没有执行付费生成或修改生产代码。

**主要结论：项目已有可靠生产链和部分视听组件，但缺少把剧情意图落实到画面、表演、配乐的统一执行计划。** 首帧覆盖不足、底图内变化单一、声音指令丢失，都有具体证据。优先改造这些连接处，比全量更换模型或重写渲染器更有价值。

## 审计证据与适用范围

| 对象 | 本轮检查 | 可支持的结论 |
|---|---|---|
| 当前源代码 | 默认 v2、legacy 预览、脚本入口、导演提示、时间轴、叠加层、TTS、BGM、发布与缓存 | 当前组件的实际能力和缺口 |
| `xlc-01` | 当前 run 指针关联的计划、母带清单、时间轴、455 项输入收据、MP4 哈希、12 个采样时刻、整片解码、响度 | 现存样片的视觉结构及技术指标 |
| `caomin-02` | 当前源文件、overlay 旁车文件、现存 legacy MP4、12 个采样时刻、整片解码、响度 | 标题和幕卡确实出现在样片中；不证明按当前修改后的源文件重建一致 |
| `xlc-08` | 最新剧集的导演稿、口播稿、62 个 unit 的计划 | 导演要求如何进入生产；尚无本轮可核验的成片 |
| `caomin-01` | 场景计划和 overlay 是否存在 | 局部结构参照；没有全片视觉评审 |
| 本地 Qwen 生成支路 | 仓库内生成脚本、启动器及其指向的 `D:/models/qwen3-tts/app.py` | 默认模式实际是否消费表演指令 |
| 外部项目 | 官方仓库、API、模型卡和许可证 | 可借鉴能力；没有安装或试听这些模型 |

没有遍历全部历史剧集，没有进行完整人工听审、逐项史实考证或平台留存实验。图片观察来自成片抽帧，不以测试数量代替观感。

证据目录：[审计证据](/D:/hv/state/audit-attraction-20261018)。结构统计见 [sample-metrics.json](/D:/hv/state/audit-attraction-20261018/sample-metrics.json)，媒体测量见 [media-evidence.json](/D:/hv/state/audit-attraction-20261018/media-evidence.json)。

## 核心问题与修复方向

以下 P1 表示直接影响用户提出的创作目标，P2 表示支撑能力不足；不将艺术取舍描述成系统故障。

### P1 首帧没有成为独立设计和验收对象

**事实。** `xlc-01` 没有 `overlays.json`，第 0 帧只有病榻场景。`caomin-02` 已有“公摊面积 75%”标题及三张幕卡，但标题从 0 到 400ms 淡入；[overlay.py:97](/D:/hv/app/services/overlay.py:97) 在起点计算 alpha=0，[263 行](/D:/hv/app/services/overlay.py:263) 因而不画文字。实际第 0 帧也没有标题。

**影响判断。** 现有标题卡能改善播放中的信息提示，却不能保证首帧截图清楚表达人物、冲突和观看理由。`caomin-02` 标题承诺“土地缩水”，首句却在回顾上一集，视听承诺分散。

**修复。** 将静态封面、实际第 0 帧、前 10–15 秒分别设计。建立可复用的“人物或关键动作＋冲突物件＋一句观看理由”构图；首帧所需文字立即可见，后续装饰可动画。标题由排版层生成，底图继续无字并沿用固定风格。

**验收。** 抽取真实 t=0、0.5、2、5、10 秒画面；手机缩略尺寸检查可读性、主体遮挡、字幕与平台 UI 安全区；让目标观众用自己的话说出“讲谁、冲突是什么”。不以“有 title_card 文件”视为通过。

### P1 开场反转出现偏晚

**事实。** `xlc-01` 全长 1312 秒，首张底图持续 27.94 秒。旁白先交代日期、天气、托孤，36.56 秒仍在讲“最温情的契约”，47.30 秒出现“不是倚仗”，51.70 秒说到“政治枷锁”。

**影响判断。** “托孤还是枷锁”是明确的矛盾，但新观众要等待较长铺垫才得到观看承诺。这里不是要求把二十分钟内容压成短视频，而是让长片的入口更明确。

**修复。** 下一版先用矛盾句和关键动作提出问题，再补时地信息。改动口播必须形成新的源稿版本，不能悄悄重排已批准音频或破坏字幕忠实性。

**验收。** 在相同画风、相同主题下比较两个开场，分别测“想不想继续看”和“是否理解矛盾”；上线留存只能在获得真实播放数据后判断。

### P1 视觉变化主要来自机械推拉，缺少每幕的信息推进

**事实。** `xlc-01` 有 159 个时间轴 shot、45 张底图、430 条字幕。159 个场景均无显式 motion，实际为 80 次 push、79 次 pull，全部围绕画面中心、在 1.00–1.06 倍之间变化。连续使用同一底图的段落中位时长 30.36 秒，最长 42.22 秒。见 [production_timeline.py:60](/D:/hv/app/services/production_timeline.py:60)。

![xlc-01 成片抽样](/D:/hv/state/audit-attraction-20261018/xlc-01-contact.jpg)

**视觉判断。** 抽帧中主要依靠人物站坐、宫室、书案和光线变化承载解释；讲疆域扩张时仍是人物画面。底图并不差，问题是缺少“画面新增了什么信息”的设计。

**修复。** 在每幕标注叙事任务：提出矛盾、交代阵营、解释机制、改变局势、兑现反转。选择对应表现：人物反应、关键物件细节、地理路线、关系图、数值对比、短暂留白。先用已有底图重新构图及局部图层，再决定需要新增哪些素材。

**验收。** 每个主要剧情转折都能指出一个对应的视觉变化；同图长时间停留要有内容上的理由。长 shot 本身不是错误，不能把 legacy 的 40–200 帧限制套进 v2。保留 narration unit 与其原文映射，在其内部设计子事件。

### P1 幕标题工具与新 v2 剧集的结构脱节

**事实。** 未跟踪工具 [build_overlays.py:104](/D:/hv/bin/build_overlays.py:104) 用 `ep.frames/ep.shots` 和 legacy `compose.build_timeline` 定位幕卡；`xlc-01` 和 `xlc-08` 的这些集合为空，而内容在 v2 unit/timeline 中。工具的幕标题正则还不匹配它们的“第一幕 (A段)”格式，实测两集匹配数均为 0，`caomin-02` 为 4。

**影响。** 代码里有标题组件，新的主要生产路径仍缺少稳定的自动装配入口；人工做过一集不能自然扩展到下一集。

**修复。** 为 v2 从原文段落或 unit ID 读取幕锚点，再用已观测的词级对齐编译成样本/帧位置。legacy 保留自己的时间轴解析。两者复用叠加层渲染，不共用错误的定位口径。

**验收。** 不生成真实剧集的 frames、不重跑 splitter；在隔离 fixture 中验证 A/B 幕标题、重录后幕卡跟随、重复文本歧义拒绝、缺锚点明确报错。

### P1 导演稿中的要求没有进入可执行计划

**事实。** `xlc-08/script_director.md` 有画面、音效、语气描述，当前口播计划却是 62/62 个 `neutral`、空 instruction、0 个 delivery cue；`xlc-01` 是 159/159、0 cue。生产入口加载 `ep.script_path`/口播源，没有发现消费 `script_director.md` 的生产代码。即使有 cue，[delivery.py:9](/D:/hv/app/services/delivery.py:9) 也只将段首、含“语气”、不含“配乐”的受支持提示编成 TTS instruction，其余多为待处置记录。

**影响。** “剧本写了如何演”与“系统实际这样演”之间断开。导演稿写得更复杂，并不会自动让画面或声音更丰富。

**修复。** 建立一份与原文绑定的执行计划，区分 `planned / supported / compiled / rendered / reviewed`。每一条要求明确由哪个组件实现，不支持的指令不能只显示为已接收。

**边界。** `xlc-01` 是 `external_recording` 导入。其 plan 的 neutral 不描述母带里的真实表演，不能据此声称这段声音一定平淡，也没有证据证明它一定由下面的 Qwen 脚本生成。

### P1 本地 Qwen 默认克隆模式没有执行表演指令

**事实。** [generate_qwen3_local_audio.py:25](/D:/hv/resource/风趣历史.writing-DNA/scripts/generate_qwen3_local_audio.py:25) 默认 `clone`，每个句块向本地服务发送同一全局 instruct。启动器转到仓库外服务；[app.py:151](/D:/models/qwen3-tts/app.py:151) 的 clone 分支调用 Base `generate_voice_clone(..., x_vector_only_mode=True)`，未消费 `req.instruct`；只有 CustomVoice 分支传入 instruct。上游也把 CustomVoice 指令控制和 Base 克隆列为不同能力。[Qwen3-TTS 官方能力与 API](https://github.com/QwenLM/Qwen3-TTS)

**影响。** 修改“更有感情”的提示词，默认克隆路径不一定产生对应控制，因为这条控制根本没有送进模型。这是所查源码路径的确定缺口；本轮没有调用运行中服务验证模型表现，也不据此推断某份历史母带的生成来源。

**修复。** 先明确返回“当前 clone 接口不支持 instruct”，记录 effective controls。不要把不存在的参数强塞给模型，也不能静默切成另一个音色。保留当前旁白身份时，用相同参考身份试验独立情绪控制路线；允许更换音色时再对比 Qwen CustomVoice。

**验收。** 检查最终调用参数与旁车记录；用同一文本对比紧张、反讽、悲悯、收束等短样本，听审自然度、情绪命中、连续性。HTTP 200 与“参数出现在请求 JSON 中”都不能作为表演实现证据。

### P1 本地脚本把精确停顿变成了统一拼接

**事实。** 同一生成脚本 [54 行](/D:/hv/resource/风趣历史.writing-DNA/scripts/generate_qwen3_local_audio.py:54) 把所有 `[pause=…]` 替换为省略号，丢失指定秒数；[158 行](/D:/hv/resource/风趣历史.writing-DNA/scripts/generate_qwen3_local_audio.py:158) 块间统一插入 0.35 秒，[240 行](/D:/hv/resource/风趣历史.writing-DNA/scripts/generate_qwen3_local_audio.py:240) 幕间统一 0.8 秒。代码块之外的表演说明不被这套解析器提取。

**修复。** 在局部生成阶段保留指定停顿和逐段意图，按语义分块。每个停顿绑定原文位置和实际样本数；外部母带导入后继续跳过 v2 的二次停顿插入，防止重复加静音。v2 自带精确 PCM 停顿能力，无需推倒重写。

**验收。** 原稿、raw take、参考音频保持；验证停顿位置与样本数，并听相邻块衔接，避免每个块都像重新开场。

### P1 声音设计只有单轨配乐能力，缺少剧情编排

**事实。** 本次 `assets/bgm` 没有可选文件，`xlc-01` timeline 没有 `bgm_path`。[bgm.py:35](/D:/hv/app/services/bgm.py:35) 按 profile 选择单首音乐；[89 行](/D:/hv/app/services/bgm.py:89) 循环、固定音量、首尾淡入淡出后混音。未发现把导演音效指示转成独立 SFX/环境轨的生产消费者。

**影响。** 当前生产链不能自行实现“悬念铺底—反转撤乐—关键动作音效—结尾收束”。外部母带可能自带音乐，因此不能扩张为“所有历史视频均无配乐”。

**修复。** 先用 FFmpeg 扩展人声、音乐、环境、重点音效的可选轨道及音量包络，让它们共用剧情事件锚点。可直接用人声作为 sidechain 的控制输入，不必先搭建 VAD 系统；现有注释把两者绑定，夸大了最小实现成本。[FFmpeg sidechaincompress](https://www.ffmpeg.org/ffmpeg-filters.html#sidechaincompress)

**验收。** 人声独听、混音后再听；手机扬声器上仍能听清关键词；关键停顿按设计保留；记录素材来源和使用许可。测整体响度、真峰值和动态，但不以动态越大越有感情。两份样片的测量值见后文。

### P2 创作验收没有形成可比较的反馈闭环

**事实。** 工程有媒体完整性、字幕、哈希、双人类评审等保护；首帧是否成立、每幕推进是否清楚、情绪是否命中，还没有与当前样片绑定的统一对比表。`xlc-01` 的 MP4 哈希符合原收据，但 455 项输入中有 19 个源码文件已变化。

**影响。** 不能把现存旧样片说成当前代码重建通过，也不能把机器验收通过说成内容更吸引人。

**修复。** 每次创作实验保留原版和候选，记录只改变哪一项、样本哈希、评审反馈和下一步。增加首帧导出、开场预览、逐幕联系表、声音指令执行覆盖表；它们作为审核材料，继续使用现有候选和批准机制。

## 应保留的能力与本次不成立的旧指控

- 两条管线继续保留；付费生产仍走 v2，legacy-preview 保持原边界。
- 现有原文和字幕一致性、词级对齐、raw take、持久预算、断点续跑、发布双审应继续使用。
- 当前 [video_segment_cache.py:12](/D:/hv/app/services/video_segment_cache.py:12) 已包含 overlay/karaoke/word_timing，实现了相关代码变化的缓存失效；v2 已按段窗口计算 overlay 指纹。10 月 14 日报告中的这些缺陷不能直接照搬为仍未修复。
- 不把“v2 没有多音字替换”或“shot 可超过 200 帧”写成问题。它们是明确的产品约束。
- 不改变项目既定画风；`en` 只描述人物、动作、地点、道具、光线。文字、地图标签和数值由独立图层生成。

## 外部项目选型

以下是能力借鉴和实验优先级，不是音质排名。许可证摘要用于筛选；实际采用时应固定版本并保留对应许可证。

| 项目 | 对本项目的价值 | 接入方式与限制 |
|---|---|---|
| [Remotion](https://www.remotion.dev/docs/renderer/render-still) | 参数化首帧、标题和短开场；同一构图输出静帧与动画 | 先借设计方法；确需复杂构图时才做独立 Node 边缘渲染器，返回确定帧数的素材。其 [许可证](https://github.com/remotion-dev/remotion/blob/main/LICENSE.md) 不是 MIT，免费商用有主体规模条件 |
| [Motion Canvas](https://motioncanvas.io/docs/time-events/) | 用 `waitUntil` 一类命名事件同步旁白和视觉揭示 | 优先借事件思路；少量关系或路线解释段可独立渲染。[MIT](https://github.com/motion-canvas/motion-canvas/blob/main/LICENSE)，不替代现有生产编排 |
| [Manim Community](https://docs.manim.community/en/stable/reference/manim.mobject.graph.Graph.html) | 人物关系、战线、财政和数量变化的逐步解释 | Python 侧局部动画组件，继续使用现有字体、颜色和背景。[MIT](https://github.com/ManimCommunity/manim/blob/main/LICENSE)；不把整片变成课堂演示 |
| [IndexTTS](https://github.com/index-tts/index-tts#-python-api) | 独立音色参考与情绪参考、强度和向量控制，适合测试保留旁白身份下的表演变化 | 优先作为声音对照实验。2 与 2.5 能力要分开；2.5 的 duration_factor 不等于精确毫秒控制。[自定义代码及模型许可](https://github.com/index-tts/index-tts/blob/main/LICENSE)，不能标成 Apache/MIT |
| [CosyVoice 3](https://github.com/QwenAudio/CosyVoice/blob/main/example.py) | 指令式表演、参考声音、流式语音 | 开源本地版与当前云端 v3.5-flash 是不同部署，不可直接等同。[代码许可](https://github.com/QwenAudio/CosyVoice/blob/main/LICENSE) 与所查 [0.5B 权重](https://huggingface.co/FunAudioLLM/Fun-CosyVoice3-0.5B-2512) 标注 Apache-2.0；本机性能未验证 |
| [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS) | 现有基础已在；CustomVoice 支持自然语言 instruct，Base 提供克隆 | 先修正能力标识及有效参数留痕。所查代码与 1.7B Base/CustomVoice 权重为 Apache-2.0；不能将 CustomVoice 的能力套到 Base 克隆接口 |
| [OpenTimelineIO](https://github.com/AcademySoftwareFoundation/OpenTimelineIO) | 导出到人工精剪流程 | 后置，单向导出编辑序列与素材引用；不是渲染器，不成为第二权威时间轴。[Apache-2.0](https://github.com/AcademySoftwareFoundation/OpenTimelineIO/blob/main/LICENSE.txt) |

暂不优先引入 [Auto-Editor](https://github.com/WyattBlue/auto-editor)：自动删静音无法代替戏剧节奏，可能切掉悬念留白。[Fish S2-Pro](https://huggingface.co/fishaudio/s2-pro) 有丰富表达标签，但所查权重许可限制商业使用，且 [官方部署说明](https://github.com/fishaudio/fish-speech/blob/main/docs/en/install.md) 对其 GPU 路径要求至少 24GB 显存；不作为当前首选。仓库新代码许可不能替代旧权重许可。

不建议同时接入三套动画框架、四套 TTS。第一轮只补工程连接和样片；依据明确需求选择一个外部组件。

## 可直接评审的创作方案

### 首帧与前十五秒示例

以 `xlc-01` 的“托孤与权力束缚”作为实验主题。以下是拟议的新版本设计，非史料原话，也不改动当前源稿。

| 时段 | 画面与文字 | 旁白表演与声音 |
|---|---|---|
| 第 0 帧 | 延续原画风；手与托付动作成为焦点；标题“托孤，还是枷锁？”立即可见，保留人物面部与字幕区 | 直接进入矛盾，不先放长片头 |
| 0–4 秒 | 交握的手或权力物件细节；用现有底图裁切验证，必要时才补一张道具图 | 提案句：“父亲留给他的顾命大臣，后来成了皇权的枷锁。”低声、克制，重音落“枷锁” |
| 4–8 秒 | 切年轻李治的反应，揭示身份；短人物标签独立排版 | 轻微留白后提出“他怎样拿回权力？”音乐短暂让位，避免持续轰鸣 |
| 8–15 秒 | 回到托孤全景，交代贞观末年的时空和两位重臣 | 回归正常讲述速度，进入既有叙事。精确句长和时长以后续实测语音为准 |

“拿回权力”的论断与新口播仍需史实及内容审核；艺术性改善不能靠虚构引语或更夸张的断言取得。对于 `caomin-02`，已有“公摊面积 75%”可作为备选钩子，但应先核对统计口径，并让首句与土地问题对齐，减少对上一集的依赖。

### 各幕设计方法

每一幕写五个具体字段：本幕问题、可见的变化、观众此刻应该理解的关系、情绪转折、通往下一幕的问题。幕标题只是其中一个表现手段。

| 幕的任务 | 优先视觉 | 声音方向 |
|---|---|---|
| 建立权力困境 | 托付动作、人物站位、最少必要的关系标签 | 克制、压低，给疑问留白 |
| 解释权力怎样运作 | 人物关系、利益流向、证据物件 | 清晰、略提速度；关键词清楚即可，不全句重音 |
| 危机和行动升级 | 主体反应、地图推进、局部动作插镜 | 张力逐步上升，音乐与对白错峰 |
| 反转及代价 | 先隐藏结果，再揭示关键人物或物件 | 反转前撤乐或短停，反转后收住，避免全靠喊 |
| 结尾回扣 | 回到开场物件，但让其含义改变 | 减速、留空间，结束问题得到回应 |

声音计划需要区分“讲清楚”“冷幽默”“紧张”“愤怒但克制”“悲悯”“收束”。持续使用“厚重、肃穆、低沉”容易把各幕拉成同一种表演意图；这是需通过试听验证的创作假设。

## 工程改善顺序与验收

### 第一阶段 先做一段有证据的改善样片

建议范围：一个首帧主题的两个构图版本、一段 60–90 秒开场、一处中段转折；声音先选 6 个代表性短句。使用独立实验 ID 和目录，引用已有资产；原片和批准材料保留。

交付物：原版与候选对照、真实首帧、开场分镜、逐句表演单、混音前后试听材料、素材和参数清单。先验证创作方向，再决定是否重制整集。付费生成需要另行明确并持久化请求、字符和图片预算；本报告未执行这些调用。

### 第二阶段 补齐执行计划的连接

在现有 plan/scene_plan 上扩展有来源绑定的设计信息，避免再造一套决定时长的权威数据。每个事件绑定 `unit_id + text range`，由已有 alignment 得到 sample，再派生 frame。

- 首帧与幕卡：适配 v2 unit 结构；新增可见性、文本布局、人物遮挡检查。
- 视觉事件：在 narration unit 内描述焦点、局部图层、揭示顺序；不改 legacy 的镜头定义。
- 表演控制：明确 provider/mode 能力；记录 planned 与 effective 的差异；外部母带增加模型、服务、参考资产及分块信息旁车。
- 声音事件：音乐段、音效位置、音量包络与停顿共用锚点，按语音新时长重新编译。
- 缓存：设计数据、字体和渲染器版本纳入指纹；局部事件变更仅影响覆盖的段。重新审核修改后的候选。

建议接入点：[production_timeline.py](/D:/hv/app/services/production_timeline.py)、[overlay.py](/D:/hv/app/services/overlay.py)、[delivery.py](/D:/hv/app/services/delivery.py)、[bgm.py](/D:/hv/app/services/bgm.py)、[external_narration.py](/D:/hv/app/services/external_narration.py) 和本地音频生成器。尽量复用现有组件，不增加第二编排器。

### 第三阶段 验收后扩展

机器门槛：原文覆盖、字幕几何、素材与输入哈希、音视频帧/样本数、事件锚点、停顿样本数、预算、缓存失效和断点恢复。组件修改分别跑相关 production/shared/legacy 文件，最终使用项目 acceptance 脚本。

创作门槛：首帧可理解；开场有明确观看理由；每幕有新信息；旁白自然且身份连续；音效和音乐帮助叙事；手机外放关键词可听清。采用同一题材、相同播放条件的盲比较，记录理由，不代替用户签署批准。

平台门槛：获得真实数据后再比较前几秒继续观看、30 秒留存、主要幕转折处流失和完播。保持长度、受众、发布时间及流量来源尽量可比；小样本只作线索，不承诺提升百分比。

## 本轮技术证据

| 样片 | 视频流 | 整片解码 | 响度与峰值 |
|---|---|---|---|
| `xlc-01` | 1080×1920，25fps，32800 帧，1312 秒 | 退出码 0，无错误输出 | -17.7 LUFS，LRA 5.0 LU，测得 true peak -0.6 dBFS |
| `caomin-02` | 1080×1920，25fps，26229 帧，视频约 1049.16 秒 | 退出码 0，无错误输出 | -17.5 LUFS，LRA 4.7 LU，测得 true peak -0.5 dBFS |

上述数值不能证明情绪自然或混音理想。没有把峰值接近零等同于已经削波；后续混音应留编码余量并重新测量。

![caomin-02 标题及幕卡抽样](/D:/hv/state/audit-attraction-20261018/caomin-02-contact.jpg)

生产组检查通过 `scripts/ai-project-check.ps1 -ProjectRoot D:/hv -RunGates -Suite production -EvidencePath .../acceptance.json` 后台执行，使用 `.venv-core`，FFmpeg 已加入 PATH。结果为 **FAIL：234 passed，4 failed，311 deselected**，耗时 209.31 秒。运行时依赖、导入、字体和 API 健康检查已通过；没有调用真实生成接口。

4 个失败均位于 `test_audit_fixes.py` 的 scoped image 测试，在进入业务断言前碰到真实 `state/production-storage.lock`。源码确认这些测试未使用 `tmp_episodes` 隔离：部分 fixture 还直接在真实 episodes 根创建/清理 synthetic 数据，一个状态形状测试读取前 8 个现存剧集。设置 pytest 临时目录并不能隔离这些硬编码路径。这是需要修复的测试隔离缺陷；本轮不据此判定 4 项图片业务功能损坏，也不停止生产任务或释放它的锁。后续应先迁移到隔离 fixture，再重跑这 4 项。

此外，readiness 报告 `production validation is for another source revision`。因此当前离线验收不能写成通过，现有生产验证也不能直接复用于当前版本。见 [acceptance.json](/D:/hv/state/audit-attraction-20261018/acceptance.json) 和 [完整日志](/D:/hv/state/audit-attraction-20261018/acceptance.log)。

本轮基线覆盖的 218 个代码、配置及选定剧集输入文件，在结束复核时全部哈希未变、无缺失，见 [preservation-check.json](/D:/hv/state/audit-attraction-20261018/preservation-check.json)。仓库外声音服务及相关脚本另存 [来源哈希](/D:/hv/state/audit-attraction-20261018/local-voice-source-hashes.json)。本轮仅新增本报告和审计材料，没有改造生产实现、重写剧集或签署听觉/艺术批准。
