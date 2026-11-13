"""Build a review-only visual proposal from existing, unchanged episode inputs.

This does not call providers, edit the workflow, or produce playable media.
"""
from pathlib import Path
import hashlib
import json
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.services import studio_workflow as W

OUT = Path(__file__).resolve().parent
EP = ROOT / 'workspace/episodes/ytrm-01-user-20261022'
RUN = json.loads((EP / 'production/v2/run.json').read_text(encoding='utf-8'))
TIMELINE = json.loads(Path(RUN['timeline_path']).read_text(encoding='utf-8'))
ALIGN = json.loads(Path(RUN['alignment_path']).read_text(encoding='utf-8'))
DOC = W.document('ytrm-01-user-20261022')
BASE = json.loads((OUT / 'evidence/baseline.json').read_text(encoding='utf-8'))
SR = ALIGN['sample_rate']
RECEIPT = json.loads(Path(RUN['receipt_path']).read_text(encoding='utf-8'))
TOTAL = RECEIPT['samples']
FRAMES = RECEIPT['expected_frames']


def dump(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda: f.read(1048576), b''):
            h.update(b)
    return h.hexdigest()


def anchor(unit, quote):
    u = DOC['units'][unit]
    text = u['source_text']
    assert text.count(quote) == 1, (unit, quote)
    start = text.index(quote)
    word = next(w for w in ALIGN['words'][u['unit_id']] if w['text_start'] <= start < w['text_end'])
    return {'unit_id': u['unit_id'], 'unit_index': unit, 'text_start': start,
            'text_end': start + len(quote), 'quote': quote, 'sample': word['sample_start'],
            'timing_source': 'existing_acoustic_alignment_not_new_listening_approval'}


def beat(unit, quote, title, action, layers, asset, local, guard):
    return {'anchor': anchor(unit, quote), 'title': title, 'action': action,
            'layer_design': layers, 'asset_requirement': asset,
            'local_motion': local, 'guard': guard}


scenes = [
    {
        'id': 'opening', 'number': '01', 'title': '首帧与开场反差', 'tagline': '先看见玄奘，再看见处境翻转。',
        'before_images': ['unit-000.png', 'unit-001.png', 'unit-009.png'],
        'before': '前 17.47 秒两镜都用器物静物图，主要是轻推与角落光效；随后仍是文书静物。已渲染标题写“玄奘真实西行”，但首段旁白明确在讲神魔演义。人物和反差出现得不够直观。',
        'after': '首帧把既有玄奘人物设定、钵盂和送行空间放进同一构图，明确标出“在后世的神魔演义里”；到原文“但你要是”才切现实文书，接“相反”转向边关追捕压力。保留原顺序，不把后文危险强塞进第一句。',
        'old_shots': [0, 1, 2, 3], 'end_sample': TIMELINE['shots'][4]['sample_start'],
        'layout': ['上方 8%–22%：既有标题＋原文演义标识', '中部 28%–68%：玄奘轮廓与钵盂；右侧留出入口', '下部 72%–90%：字幕预留区；器物不压字幕', '深度：前景钵盂／中景人物／后景殿门'],
        'reuse': '复用 unit-000 的器物、灯光与材质，unit-001／009 的文书与门窗，人物沿用 unit-016／025 及既有人物设定。旧图保留原文件。',
        'needed': '补一张人物与器物同框的首帧母版，准备钵盂、人物、殿门、干净背景层；人物不得从其他图直接硬贴。',
        'movement': '每镜的虚拟摄影机只走一个方向：前景约 10 px、中景约 4 px、背景约 2 px（1080 宽，设计上限）；脸和手不做局部拉伸。灯光只在灯罩内轻变。',
        'accept': ['第一帧即可辨认玄奘与送行语境，标题不遮脸。', '演义标识在首段持续可读，17.47 秒之后才进入现实叙述。', '道具无伪造可读公文；可读字均使用原文的后期文字层。'],
        'beats': [
            beat(0, '在后世的神魔演义里', '人物先进入故事', '玄奘与钵盂同框，殿门形成纵深；镜头轻向人物靠近，保留“演义”提示。', '钵盂前景／玄奘中景／门柱后景；脸手独立保护。', 'OPEN-01：新构图母版＋4 层，待准备。', '灯罩内微亮暗；不添加烟雾或飘落物。', '不改旁白；不展示现实追兵。'),
            beat(0, '天子端着御酒', '送行的具体道具', '沿人物视线切到御酒与钵盂；用镜头关注变化表现待遇，避免让整张静图漂移。', '杯与钵前景／桌面中景／殿门背景。', '复用 unit-000 道具；需要干净背景。', '器物高光随视差轻移，幅度不超过 1%；无闪白。', '不添加原文之外的对白或仪式。'),
            beat(0, '通关文牒', '为反转埋下文书', '聚焦文牒与印记，再回到人物和器物的关系；承接下一段同类文书。', '文书前景／手与袖中景／桌面背景。', 'OPEN-02：文书近景分层，原图不能提供时再准备。', '前景缓移、背景慢移；切换前停稳 0.3 秒。', '不让生成模型绘制可读字，不伪造具体通关文件。'),
            beat(1, '但你要是', '同一文书，意义翻转', '沿相近构图切至现实环境的文书；去掉演义标识，人物从受礼位置变成面对关口的位置。', '文书／人物背影／关门；保持光色来自既有画风。', 'OPEN-03：沿用 unit-001／009 的环境；人物关口关系待补。', '切换本身承担转折；不加闪光或快速旋转。', '只画文书与关口压力，不画已被捕或遭殴打。'),
            beat(1, '相反', '把威胁落到空间', '从文书近景移到关口环境，远处人影受门框压迫；让观众理解“出不去”。', '近处门柱／中景玄奘／远处关口。', '与 OPEN-03 共用母版和人物层。', '门柱横移 10 px、人物 4 px、远景 2 px。', '不改变人物衣着，不新增追逐动作。'),
        ],
    },
    {
        'id': 'knife', 'number': '02', 'title': '夜间向导持刀', 'tagline': '危险来自距离、犹豫和等待。',
        'before_images': ['unit-017.png', 'unit-015.png', 'unit-016.png'],
        'before': '第一张夜景 unit-017 里两人近距离相对，与“几十步”不符；unit-015 的刃身特写偏长且带火光；unit-016 才有前后距离。现有镜头缺少拔刀、停步、僵持、退回的连续变化。',
        'after': '用同一空间轴线组织铺草→拔刀→远处停步→端坐→僵持→收刀退回。人始终分处前后景，旁白说“要是”时保持悬念，不演成实际行凶。',
        'old_shots': [24, 25, 26, 27], 'end_sample': TIMELINE['shots'][28]['sample_start'],
        'layout': ['玄奘位于左下中景；向导位于右上远景', '两人之间保留大片空沙地；相对位置全段一致', '短刀与持刀手在中景特写中交代，不靠近玄奘颈部', '月光与云在背景；风沙只过地面，不盖脸'],
        'reuse': '优先复用 unit-016 的远近关系与玄奘形象；unit-017 留作旧版对照，不能直接用作“几十步”主镜；unit-015 的长刃与火光不进入新方案。',
        'needed': '同一夜景干净背景、玄奘卧／坐两个状态、向导持刀／收刀／退回三个状态、匹配原文短刀的手部近景。各状态同装束、同体型、同一光向。',
        'movement': '环境动、人物关键时刻停：地面薄沙横移，月后云慢移；前沙 12 px／玄奘 3 px／向导 2 px／远景 1 px。角色动作靠准备好的姿态，不平移僵硬人形假装走路。',
        'accept': ['两人始终有明显远近与空地间隔；不量化虚构精确步数。', '短刀始终属于向导；没有刺杀、流血、尸体和火焰升级。', '收刀后才后退，机位不越轴；原文“无声地耗着”处让镜头真正慢下来。'],
        'beats': [
            beat(18, '半夜里', '先交代两处草铺', '同一宽景交代玄奘与向导分处两端；留出中间空沙地。', '近沙／玄奘卧姿／远处向导／月下荒原。', 'NIGHT-01：同轴夜景母版与两处草铺，待准备。', '地面少量薄沙平移；月后云慢移，不加暴风。', '旧 unit-017 近距离构图不能直接沿用。'),
            beat(18, '石磐陀拔出了腰里的短刀', '拔刀，不贴身', '切向导腰部与持刀手；用刀柄出鞘、短刀下垂两个状态交代动作，再回到距离关系。', '手与短刀／向导衣袖／暗背景。', 'NIGHT-02：两张短刀动作状态；不是长刀亮刃旧图。', '刀面只有一次低幅反光，随后稳定。', '不加火光，不把短刀放到玄奘身旁。'),
            beat(18, '走到距离玄奘几十步的地方停住脚', '停步与犹豫', '向导仍在远景；由两三个朝向状态交代徘徊，再停住。玄奘留在近处草铺。', '沙地前景／玄奘近层／向导远层／地平线。', 'NIGHT-03：向导持刀的朝向状态，共用 NIGHT-01。', '镜头慢推 1%；人物停止后环境还在极轻移动。', '不画到玄奘跟前；距离通过构图表达。'),
            beat(19, '黑灯瞎火的大漠深处', '让危险留在可能中', '保持远景向导与空地，转到玄奘视角的距离；不出现旁白假设中的伤害结果。', '近处草铺边缘／远处向导／天空。', '共用 NIGHT-01／03。', '减少移动，刀面不持续闪烁。', '“要是”是条件句，不画尸体、埋沙或杀人。'),
            beat(20, '玄奘既没喊救命', '先给不动的反应', '近景看玄奘没有逃跑；短暂停住画面，让“既没……也没……”听清楚。', '玄奘头肩／草铺／远景。', '复用 unit-016 人物形象；新卧姿到坐姿连续状态。', '面部完全静止；风沙不覆盖眼口。', '不新增惊叫、哭泣或夸张表情。'),
            beat(20, '他慢慢坐直身子', '坐直与端坐', '由起身半坐→盘腿坐稳两个状态切换，镜头缓靠近衣角和稳定姿态；不使用全身橡皮拉伸。', '衣角与手／玄奘／远处向导。', 'NIGHT-04：两个人物姿态，身份沿用现有形象。', '坐稳后停止人物层；不做口型。', '只表现原文动作，不追加抵抗或搏斗。'),
            beat(20, '刀刃在几十步外晃动', '距离里对峙', '回到同轴宽景：近处玄奘稳定，远处向导改变朝向；保持双方尺寸与位置连续。', '近沙／玄奘／远处向导／月与云。', '复用 NIGHT-01／03／04。', '前景沙缓动，背景微移；不靠抖屏制造危险。', '不把远景人放大到贴身，不使用频闪。'),
            beat(20, '两个人就在夜色里无声地耗着', '留一段真正的等待', '保持同一空间，不新增切镜；让空地成为画面的中心。', '同轴四层；人物不动。', '复用同一分层母版。', '只保留最低幅度背景变化；移动强度约前段的 1/3。', '不压缩现有音频停顿，不改变字幕时间。'),
            beat(20, '石磐陀终于叹了口气', '收刀，退回原处', '先给收刀状态，再回宽景让向导退向自己的草铺；到下一句天亮才进入后续画面。', '近处玄奘／远处向导／草铺／荒原。', 'NIGHT-05：收刀与退回状态，共用原空间。', '环境继续缓动；避免人物在沙面上无步态滑行。', '不得提前转白天，不新增台词与声效。'),
        ],
    },
    {
        'id': 'water', 'number': '03', 'title': '沙漠失水', 'tagline': '让观众看清，水是怎么失去的。',
        'before_images': ['unit-025.png', 'unit-026.png'],
        'before': 'unit-025 一张“伸手取袋”的定格承担约 16.36 秒；之后跳到 unit-026 的失水结果。掉落、触沙、渗水和水痕消失缺少过程，主要仍靠旁白解释。',
        'after': '按原句安排取袋→脱手→落地→清水渗沙→只剩暗痕。失手发生在手与水袋的连接处，后果发生在地面，两个特写由同一水袋和落点连接。',
        'old_shots': [36, 37, 38], 'end_sample': TIMELINE['shots'][39]['sample_start'],
        'layout': ['主动作放在画面中下部 42%–68%，避开字幕区', '手与水袋位于同一焦点，马与人物不拆得支离破碎', '落地后切近景：同一个袋口、同一个沙地落点', '前景细沙／水袋与水痕／人物与马／远沙丘'],
        'reuse': 'unit-025 的人物、马、手和皮水袋是连续性参考；unit-026 的空袋与沙地作结果参考。保留原稿规定的意外失手。',
        'needed': '准备水袋脱手、触沙两个状态及干净地面；袋口水流、湿沙形状和干沙覆盖分别成层。不能直接把整幅 unit-025 向下挪动模拟掉落。',
        'movement': '取袋时保持镜头稳定；掉袋约 0.4–0.6 秒、触沙停 0.15 秒（待动作素材试排），不加夸张弹跳。水痕先出现再淡出；地面细沙仅在背景流动。',
        'accept': ['同一袋型、绳结、手位、光向贯穿取袋到落地。', '水只从袋口出来，按重力向下；水痕不穿过人物或马。', '渗水之后才出现干痕；动作时长在既有旁白窗口内，不改速度、不补读。'],
        'beats': [
            beat(27, '就在走入大沙漠', '迷路，环境压住人物', '先用人物与老马在沙地中的位置交代迷路；镜头逐步靠近马背水袋，为动作建立位置。', '前沙／人物与马／远丘。', '复用 unit-025 形象与马；同构图背景清理层待准备。', '远处轻微热扰动仅属后续可选效果；本批不实现，不扭曲人物。', '不新增倒地、昏迷或沙尘暴。'),
            beat(27, '玄奘伸手去取马背上的皮水袋', '手、绳与水袋', '切近景，先让手接触袋带，再展示握持；袋型和绳结要与后一镜完全一致。', '手与绳／水袋／马背背景。', 'WATER-01：握袋状态与干净马背；沿用原人物、马和袋型。', '身体不漂移；只移动准备好的手部动作状态。', '不把取水改成主动倒水。'),
            beat(27, '连日疲惫，手上一滑', '脱手的瞬间', '手与袋带分离，水袋离开原来挂点；先看清脱手，再进入下落镜头。', '手层／袋层／绳层／马背背景。', 'WATER-02：脱手状态，独立皮袋与绳结层。', '脱手约 0.3 秒、后续下落约 0.4–0.6 秒；以已确认旁白为时间基准。', '不拉长手指，不让袋带穿过手掌。'),
            beat(27, '水袋啪的一声掉在沙地上', '落地必须看得见', '同方向切低机位，水袋触沙；落地位置明确，袋口朝向与后续水流连续。', '前沙／皮袋／湿沙准备层／干净地面。', 'WATER-03：落地状态与地面层，不能复用整张伸手定格代替动作。', '落地轻扬少量细沙，停 0.15 秒后稳定；不加弹跳。', '不新增撞击音效，不改现有声音方案。'),
            beat(27, '这是穿过数百里死海唯一的救命水', '让损失停一下', '停在袋与地面的近景，视线落到袋口；保留旁白强调的时长，下一句才展开渗沙。', '空袋／袋口／沙地。', '复用 WATER-03。', '画面基本静止，不让水持续喷涌成泉。', '不增加字幕外的新结论。'),
            beat(28, '在滚烫干燥的沙堆上', '看清落点', '同一地面和水袋，切到袋口附近的局部；保持沙纹与光向连贯。', '皮袋边缘／湿沙／干沙。', 'WATER-04：一套干湿沙层与袋口流体遮罩。', '沙粒的局部运动不盖住关键水痕。', '不把单薄水迹画成池塘。'),
            beat(28, '清水瞬间渗得无影无踪', '水进入沙地', '水流在袋口下方短暂可见，随后进入湿沙区；用轮廓扩散和透明度表现渗入。', '袋口水流／湿沙轮廓／干沙。', 'WATER-04；需要新遮罩动画能力，当前渲染器没有。', '水流约 0.4 秒消失；湿沙最多扩到预设局部区域，不全屏变色。', '水的路径必须连续，不向上飘、不重复倒流。'),
            beat(28, '只留下一片迅速晒干的暗褐色泥沙', '痕迹也没了', '保留暗褐色水痕，局部缓缓变浅；最后留空袋和人物的距离关系，接回头求生。', '空袋／逐渐变浅的湿沙／远处人物与马。', 'WATER-05：结果镜头，复用 unit-026 环境与形象参考。', '约 1.2–1.6 秒渐淡，保留最后约 0.8 秒看清结果；不做闪切。', '不改变下一幕“往回走”的入口与字幕。'),
        ],
    },
    {
        'id': 'ending', 'number': '04', 'title': '结尾与粗竹席', 'tagline': '五万里路，最后落在一件遗物上。',
        'before_images': ['unit-062.png', 'unit-064.png', 'unit-065.png'],
        'before': '译经图已有局部动效，后段主要为病榻与送葬静图。最后 3.898 秒虽然标成 new-bamboo-mat，实际仍指向 unit-065 送葬人群；旁白落在“粗竹席”，画面却没有竹席。',
        'after': '译经细节→薄葬嘱咐→送葬远景→行程与经卷回望→粗竹席近景。保留大场面到一件遗物的收束，最后一句只留竹席、空处和轻光，不再回人群。',
        'old_shots': [74, 75, 76, 77, 78, 79], 'end_sample': TOTAL,
        'layout': ['译经：手与卷在中景，灯与窗各自为层', '末句：竹席纹理居中偏下，上部保留安静空处', '人物不加佛光，不表现升天，不新造葬礼细节', '结尾末 2 秒逐步减速至静止，结束时长仍为 18:04.448'],
        'reuse': 'unit-062／063 的译经与经卷、unit-064 的室内关系、unit-065 的送葬远景保留；但送葬图只用在对应旁白，不能冒充竹席镜头。',
        'needed': 'END-01 粗竹席近景是真正缺失的画面，需单独准备；沿用室内材质与光色，不需新人物。译经要手／笔／纸三个状态时才补局部动作素材。',
        'movement': '译经段灯焰局部轻动、窗外背景慢移，纸页只有具备独立层才轻翻；最后竹席最多推近 1%，最后 2 秒收住。环境动效随故事收束逐步减弱。',
        'accept': ['片尾资产名与真实画面一致：必须真的看见粗竹席。', '不显示伪造可读梵文或古文；卷数信息只用用户原文。', '末句字幕与竹席互不遮挡；不增加结尾口号、关注提示或延长声音。'],
        'beats': [
            beat(57, '在此后的十九年里', '从时代转回案头', '以现有译经室内建立安静空间，逐步靠近案台。', '灯／书案与玄奘／窗与庭院。', '复用 unit-062，清理背景后分层。', '灯罩或灯焰内微亮暗；窗外变化不穿过窗框。', '不把窗外天气细节当作史实再扩写。'),
            beat(57, '一句梵文配一句汉话', '把工作落在手与纸', '以笔、手、经卷的局部切镜表现逐字对勘；不靠画面文字堆积代替动作。', '笔手／纸与卷／桌面。', 'END-02：如实施写字，需手与笔的连续状态；旧图只能静态特写。', '局部停笔、对照、落笔；不用纸面整块扭动。', '不得生成可读伪经文，不改原句。'),
            beat(58, '据《旧唐书》统计', '成果让人听清', '经卷近景承接数字段；镜头慢退看见积累，不另加数据翻滚。', '近卷／案台／书架。', '复用 unit-063；文字不另加。', '前景约 6 px、背景 1 px；不要快速数字动画。', '原文数字只由现有旁白与字幕表达。'),
            beat(59, '麟德元年春天', '病榻与嘱咐', '保留现有室内画面，调整关注点到人物与身旁弟子，逐步减少运动。', '前景席与床沿／人物／窗。', '复用 unit-064，脸部与身体保护。', '窗外背景轻移，人物保持静止。', '不加喘息、哭声或新的情绪表演。'),
            beat(59, '用一领粗竹席', '提前交代遗物', '短切粗竹席纹理与卷边，再回到安静室内；这一镜为最终落点建立视觉记忆。', '席边／席面／素地。', 'END-01：真正的粗竹席资产，当前缺失。', '侧向约 4 px 的低幅视差，纹理不拉伸。', '不展示遗体包裹细节，不把草席替代粗竹席。'),
            beat(60, '玄奘安详圆寂', '停止动作', '回到既有安静室内，保持画面，不追加强烈光效。', '同一室内分层，运动归零。', '复用 unit-064。', '静止。', '不画升天，不加钟声，不延长停顿。'),
            beat(61, '唐高宗闻讯罢朝痛哭', '由个人走向送别', '先保留静室或已存在且匹配的宫廷静景，待“出殡那天”再切人群。', '室内现有层。', '复用 unit-064；无需新增帝王哭泣特写。', '保持低运动。', '不靠虚构帝王表情增加戏剧性。'),
            beat(61, '出殡那天', '人群只用在送葬句', '使用 unit-065 的送葬远景，镜头慢退，避免从近处正脸营造新增具体人物。', '前景队列／城门／远山。', '复用 unit-065，深度层待准备。', '前景 6 px／城门 2 px／远山 1 px；没有循环迈步素材就不伪造行走。', '保持现有画风与服饰，不新增仪仗。'),
            beat(61, '贞观初年那个混在饥民堆里溜出长安的年轻人', '回望来路', '回用已经出现过的离城与行路画面作为回望，接经卷；切镜由原句触发。', '既有行路图层；不混合成同一时空。', '复用现有 new-refugee-gate 与 unit-019，先核对人物；不新增服装。', '各镜微推；不做跨场景光流变形。', '这是回望剪辑，不新增历史事件。'),
            beat(61, '走了五万里路', '路与经卷', '短接行路远景，再落经卷；为末句从宏大数量转到小物件腾出注意力。', '行路／经卷分别成镜，不叠成奇观。', '复用 unit-019 与 unit-063。', '转场按句意切换，末镜减速。', '不加数字或口号。'),
            beat(61, '最后带走的', '只剩那一领粗竹席', '从第一字即切到竹席近景；让席面纹理与上方空处成为结尾，直到原音轨结束。', '席边前景／席面中景／空背景。', 'END-01：缺失资产必须补齐，禁止拿 unit-065 代替。', '最多推近 1%；最后 2 秒归零，不新增尾帧时长。', '不添加关注文案；结束时间、字幕、声音保持。'),
        ],
    },
]

for scene in scenes:
    scene['start_sample'] = scene['beats'][0]['anchor']['sample']
    scene['start_seconds'] = scene['start_sample'] / SR
    scene['end_seconds'] = scene['end_sample'] / SR
    scene['render_ready'] = False
    scene['before_evidence'] = [TIMELINE['shots'][i] for i in scene['old_shots']]
    for i, b in enumerate(scene['beats']):
        start = b['anchor']['sample']
        end = scene['beats'][i+1]['anchor']['sample'] if i+1 < len(scene['beats']) else scene['end_sample']
        b.update(id=f"{scene['id']}-{i+1:02}", start_sample=start, end_sample=end,
                 start_seconds=start/SR, end_seconds=end/SR,
                 start_frame=round(start*25/SR), end_frame=min(FRAMES, round(end*25/SR)),
                 status='design_only_assets_pending')

plan = {
    'schema': 'hv.visual_proposal.v1', 'id': 'xz-four-scenes-20261023',
    'episode_id': 'ytrm-01-user-20261022', 'status': 'draft_for_visual_review',
    'active': False, 'render_ready': False, 'visual_approved': False,
    'scope': 'four_scene_plan_only', 'source_sha256': DOC['source_sha256'],
    'source_binding': {'path': str(EP/'editorial/source-binding.json'), 'sha256': sha(EP/'editorial/source-binding.json')},
    'base_timeline': {'path': RUN['timeline_path'], 'sha256': sha(RUN['timeline_path'])},
    'base_workflow': {'path': str(EP/'studio/workflow.json'), 'sha256': sha(EP/'studio/workflow.json')},
    'audio': {'path': RUN['audio_master_path'], 'sha256': sha(RUN['audio_master_path']),
              'sample_rate': SR, 'samples': TOTAL, 'speed': 'existing_1.2x_do_not_apply_again',
              'contract_path': str(EP/'studio/voice-contract.json'), 'contract_sha256': sha(EP/'studio/voice-contract.json')},
    'alignment': {'path': RUN['alignment_path'], 'sha256': sha(RUN['alignment_path']), 'all_timings_from_existing_alignment': True},
    'timebase': {'fps': 25, 'existing_frame_count': FRAMES, 'existing_duration_seconds': TOTAL/SR,
                 'rounding': 'nearest frame; terminal boundary clamped to existing receipt frame count; audio remains sample-exact'},
    'policy': {'rewrite_narration': False, 'change_voice': False, 'change_style': False, 'change_character_identity': False,
               'overwrite_assets': False, 'run_tts': False, 'run_asr': False, 'render': False, 'new_provider_calls': 0},
    'opening_overlay_proposal': {'kind': 'title_card', 'title': '西行五万里',
        'replace_subtitle': {'old_rendered': '玄奘真实西行', 'new': '在后世的神魔演义里'},
        'title_duration_ms': 4000, 'context_label_until_sample': 419280,
        'context_label_followup': 'title fades after 4s; small context label remains to 17.47s',
        'anchor_y': 0.15, 'status': 'not_applied', 'all_existing_act_cards_preserved': True},
    'capabilities': {
        'existing': 'scene_animation v1 只有原平面图局部平移视差、rain、glow；可处理小幅遮罩环境动效，不能拆出真实人物或补全背景。',
        'requires_assets': '真实分层视差需前中后景透明层与干净背景，移动范围须有足够补边。姿态切换需同身份的连续状态。',
        'requires_future_implementation': ['独立透明图层合成与遮挡顺序', '动作状态编排', '袋口水流与湿沙遮罩变化', '局部风沙／云层（不得把 rain 改名当沙）'],
        'fallback': '仅静态切镜与低幅推移可复用现有引擎；没有动作素材时标记待制作，不能称人物动作或真实 2.5D 已完成。'},
    'geometry': {'canvas': [1080, 1920], 'reserved_subtitle_box_norm': [0.07, 0.72, 0.85, 0.90],
                 'right_platform_controls_exclusion': [0.88, 0.40, 1.0, 0.94],
                 'note': '这是画面规划安全区，不修改现有字幕几何；未来必须按真实字幕渲染框复核，不是已通过字幕检验。',
                 'portrait_policy': 'face/hands protected; preserve source aspect; no elastic body warp'},
    'layer_asset_contract': {'required_fields': ['asset_id', 'source_image_sha256', 'role', 'z_order', 'alpha_path', 'clean_background_path',
                                                'protected_face_hand_mask', 'overscan_margin_px', 'motion_range_px', 'identity_reference', 'review_status'],
                             'overscan_rule': '至少覆盖最大位移加羽化宽度；人物边缘、刀柄、绳结必须单独检查。',
                             'acceptance': ['无露底、重影、黑边、人物轮廓残留', '静态视差不冒充姿态动画', '人物脸手、文字与字幕不随环境扭曲']},
    'scenes': scenes,
    'next_steps': ['先审四处动作顺序与构图方案', '只准备缺失的母版、人物姿态和透明层；保留全部旧素材',
                   '实现当前引擎缺少的图层／遮罩能力后，按已有音轨做少量局部试排',
                   '视听审核后才接入新版本；不覆盖现有 workflow、directing、音轨和视频'],
}
assert DOC['source_sha256'] == RUN['source_sha256'] == BASE['source_sha256']
source_binding = json.loads((EP/'editorial/source-binding.json').read_text(encoding='utf-8'))
source_checks = []
for item in source_binding['inputs']:
    matches = {key: sha(item[key]) == item['sha256'] for key in ['path', 'original_path', 'snapshot']}
    assert all(matches.values()), item['path']
    source_checks.append({'role': item['role'], 'path': item['path'], 'sha256': item['sha256'], 'matches': matches})
plan['latest_source_checks'] = source_checks
reference_ids = ['unit-000', 'unit-001', 'unit-009', 'unit-012', 'unit-015', 'unit-016', 'unit-017',
                 'unit-019', 'unit-025', 'unit-026', 'unit-062', 'unit-063', 'unit-064', 'unit-065', 'new-refugee-gate']
plan['existing_asset_references'] = []
for image_id in reference_ids:
    raw_path = RUN['image_assets'][image_id]
    image_path = raw_path if isinstance(raw_path, str) else raw_path['path']
    plan['existing_asset_references'].append({'id': image_id, 'path': image_path, 'sha256': sha(image_path),
                                              'role': 'unchanged_existing_reference_not_new_asset'})
draft = EP / 'studio/visual-revisions/xz-four-scenes-20261023/plan.json'
dump(draft, plan)
dump(OUT/'plan.json', plan)

# Deterministic checks of the proposal, not artistic or media acceptance.
checks = []
for s in scenes:
    assert s['start_sample'] == TIMELINE['shots'][s['old_shots'][0]]['sample_start']
    assert s['end_sample'] == TIMELINE['shots'][s['old_shots'][-1]]['sample_end']
    for i, b in enumerate(s['beats']):
        u = DOC['units'][b['anchor']['unit_index']]
        assert u['source_text'][b['anchor']['text_start']:b['anchor']['text_end']] == b['anchor']['quote']
        assert b['start_sample'] < b['end_sample'] <= TOTAL
        assert 0 <= b['start_frame'] < b['end_frame'] <= FRAMES
        if i: assert s['beats'][i-1]['end_sample'] == b['start_sample']
        checks.append({'id': b['id'], 'verbatim_anchor': True, 'existing_audio_boundary': True, 'nonempty_frame_interval': True})
unchanged = {p: sha(p) == h for p, h in BASE['files'].items()}
assert all(unchanged.values()), [p for p, same in unchanged.items() if not same]
for x in BASE['assets']:
    assert sha(x['path']) == sha(x['copy'])
issues = [
    {'scene': 'opening', 'finding': 'current_rendered_context_label_mismatch', 'status': 'proposal_only'},
    {'scene': 'knife', 'finding': 'unit_017_close_distance_conflicts_with_narration', 'status': 'replacement_layout_needed'},
    {'scene': 'water', 'finding': 'missing_intermediate_action_layers', 'status': 'assets_and_compositor_needed'},
    {'scene': 'ending', 'finding': 'new_bamboo_mat_resolves_to_funeral_image', 'status': 'real_bamboo_mat_asset_missing'},
]
verification = {'status': 'plan_integrity_checks_passed_not_media_approval', 'source_sha256': DOC['source_sha256'],
                'scene_count': len(scenes), 'beat_count': len(checks), 'checks': checks,
                'latest_source_checks': source_checks,
                'protected_file_count': len(unchanged), 'protected_unchanged': unchanged,
                'asset_copies_identical': True, 'preserved_subtitle_count': len(TIMELINE['subtitles']),
                'audio_samples_unchanged': TOTAL, 'existing_frames_unchanged': FRAMES,
                'provider_calls': 0, 'tts_runs': 0, 'asr_runs': 0, 'render_runs': 0,
                'current_workflow_untouched': True, 'draft_registered_path': str(draft),
                'project_quick_acceptance': 'not_run_start_rejected_by_automatic_approval_policy',
                'browser_preview': 'not_verified_file_protocol_rejected_by_browser_policy',
                'remaining': issues, 'human_visual_review': 'pending',
                'limitations': '检查为文案锚点、范围、文件保留及方案结构检查；未检查新视频像素、未试播新声音、未声称史实或艺术通过。'}
dump(OUT/'evidence/plan-checks.json', verification)

def ts(x):
    return f'{int(x//60):02}:{x%60:06.3f}'

lines = ['# 玄奘四处画面方案：修改对比与检查结果', '',
         '范围：新项目 D:/hv-studio；仅新增独立画面方案草稿。用户正文、已确认声音、字幕、当前导演文件与全部旧素材不改。', '',
         '本次没有配音、识别、生成新图片或渲染。对比页里的图片均是旧素材；新构图是规划示意，不能当成成片截图。', '',
         f'基于当前全文 {len(DOC["units"])} 段、{len(TIMELINE["shots"])} 镜、{len(TIMELINE["subtitles"])} 条字幕；音轨 {TOTAL/SR:.3f} 秒、{SR} Hz、{TOTAL} 采样；旧视频登记 {FRAMES} 帧。', '',
         '| 位置 | 之前 | 本次方案 |', '|---|---|---|']
for s in scenes: lines.append(f'| {s["title"]} | {s["before"]} | {s["after"]} |')
for s in scenes:
    lines += ['', f'## {s["number"]} {s["title"]}｜{ts(s["start_seconds"])}–{ts(s["end_seconds"])}', '', s['tagline'], '',
              '**画面分层：** ' + '；'.join(s['layout']), '', '**复用：** '+s['reuse'], '', '**待准备：** '+s['needed'], '',
              '**运动控制：** '+s['movement'], '', '| 原音轨时间 | 原文锚点（不改字） | 动作与画面 | 素材／边界 |', '|---|---|---|---|']
    for b in s['beats']:
        lines.append(f'| {ts(b["start_seconds"])}–{ts(b["end_seconds"])} | {b["anchor"]["quote"]} | {b["action"]}<br>{b["local_motion"]} | {b["asset_requirement"]}<br>{b["guard"]} |')
    lines += ['', '**验收要求**', ''] + ['- '+x for x in s['accept']]
lines += ['', '## 实施边界', '', plan['capabilities']['existing'], '', plan['capabilities']['requires_assets'], '',
          '独立人物层、干净背景、姿态变化、流体／湿沙遮罩尚未制作或接入。方案的图层位移是设计目标，不是已运行的引擎参数。所有原旁白顺序、1.2 倍速、采样长度与字幕时间保持。', '',
          '起止点来自当前声学对齐的字级记录，另给出就近帧边界。保留原有“字幕时间待审”状态，未把机器对齐当作人工复核。', '',
          '本次不扩展史实断言，不改写正文。人物动作只采用原稿已有动作；文案中条件句不画成已发生事件。旧图存在不等于史实正确，新画面的艺术与史实审阅仍待进行。', '',
          '## 检查结果', '', f'- {len(scenes)} 处、{len(checks)} 个动作节拍全部绑定到用户正文中的原字句；时间范围连续且落在现有音轨内。',
          f'- 基线中的 {len(unchanged)} 个文件哈希保持一致；{len(BASE["assets"])} 份对照图复制前后字节一致。',
          '- 当前正文、TTS 参考稿和三份创作规则，与原项目及导入快照逐项一致。',
          '- 当前 workflow、directing、声音合同、音轨、时间轴、字幕、已生成视频与审批文件未改。',
          '- 新方案存为独立 visual-revisions 草稿，active=false、render_ready=false、visual_approved=false。',
          '- 首帧新构图、夜景距离及姿态、水袋连续动作、真实竹席资产仍需后续制作；当前不能声称已经获得新版画面。', '',
          '- 后台快速验收与预览服务的启动被自动审批策略拒绝，返回 blocked by policy；本次没有取得 ai-project-check 的新验收凭据。上述通过项仅指本次方案文件检查。', '',
          '- 应用浏览器还拒绝了 file 协议的本地页面预览；未绕过限制，未完成浏览器布局与点击检查。对比文件已内嵌方案数据，可保留本地使用。', '',
          '## 后续顺序', ''] + [f'{i+1}. {x}' for i, x in enumerate(plan['next_steps'])]
(OUT/'修改对比与检查结果.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
html_path = OUT/'index.html'
if html_path.is_file():
    html = html_path.read_text(encoding='utf-8')
    for name, value in [('plan-data', plan), ('checks-data', verification)]:
        data = json.dumps(value, ensure_ascii=False).replace('</', '<\\/')
        pattern = rf'(<script type="application/json" id="{name}">).*?(</script>)'
        html, count = re.subn(pattern, lambda m: m[1] + data + m[2], html, flags=re.DOTALL)
        assert count == 1
    html_path.write_text(html, encoding='utf-8')
print(json.dumps({'scenes': len(scenes), 'beats': len(checks), 'protected_files': len(unchanged), 'draft': str(draft)}, ensure_ascii=False))
