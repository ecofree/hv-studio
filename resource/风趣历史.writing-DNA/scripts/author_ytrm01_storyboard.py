# -*- coding: utf-8 -*-
"""Author the ytrm-01 storyboard: one source-bound image per narration unit.

Every prompt restates the recurring character appearance because each image
request is independent; the model has no memory between calls.
"""
import json
from pathlib import Path

EP = Path(r"D:/hv/episodes/ytrm-01")
PLAN = Path(r"D:/hv/episodes/ytrm-01/production/v2/plans/1fb2b2df8d21bd63b2ff0551bb313fbae5d0521c8488a2d67d4c66f48d23f1b3/plan.json")

M_YOUNG = ("the lean Chinese monk with a shaved head, sun-darkened skin "
           "and a worn ochre kasaya")
M_MID = ("the weathered Chinese monk with a shaved head "
         "and a travel-stained ochre kasaya")
M_OLD = ("the aged, gaunt Chinese monk with a shaved head "
         "and a patched ochre kasaya")
HORSE = "a gaunt reddish-brown horse with a shaggy mane"
GUIDE = "a Hu frontier guide in a fur-trimmed leather coat with a curved knife"
KING = "a stout Central Asian king in a brocade robe"

# index: (prompt template, monk descriptor)
PROMPTS = {
    0: ("A storyteller's idealized tableau on a dark wooden table: an ornate purple-gold alms bowl, "
        "a sealed imperial pass on silk, a folding fan and a cup of tea, warm lamp light, "
        "the legendary version of a journey, vertical still life.", None),
    1: ("A cold Tang dynasty frontier archive at night: a clerk's desk with bamboo slips, sealed warrants "
        "and a bronze paperweight, one arrest notice lying open under moonlight through a lattice window, "
        "dust suspended in the air.", None),
    2: ("Turkic cavalry silhouettes on the horizon beyond a Tang frontier pass, beacon smoke rising from "
        "watchtowers, soldiers on the ramparts, wind-torn banners, late afternoon dust, wide vertical vista.", None),
    3: ("A dim government office: a young monk's petition on a desk, an official's brush laid across it unread, "
        "{monk} standing small at the threshold before tall shelves of records, grey light.", M_YOUNG),
    4: ("Before dawn in a monastery courtyard, {monk} sits alone on frost-covered stone steps, prayer beads in "
        "his hand, eyes fixed on the distant gate, blue hour stillness.", M_YOUNG),
    5: ("A scripture hall argument: two senior monks gesture sharply across a low table piled with scrolls, "
        "younger monks listening in rows, shafts of dusty light from high windows, doctrinal division.", None),
    6: ("Sun-baked farmland in Guanzhong with cracked earth and dry millet stalks, carts of grain outside a relief "
        "granary in the distance, officials directing a queue of travelers, pale sky, quiet endurance.", None),
    7: ("A crowded city gate at dusk: a stream of famine refugees in ragged clothes, among them {monk} with his "
        "head lowered and a bundle on his back, slipping past guards in the dust.", M_YOUNG),
    8: ("A desert post station at night: {monk} changes horses, a fresh mount led out, an oil lamp swinging, "
        "a frontier wall and watchtower behind, cold stars, urgency.", M_YOUNG),
    9: ("A frontier governor's office in lamplight: a sealed arrest warrant pinned beside a map of the western "
        "roads, a bronze seal and brush on the desk, the shadow of window bars across the wall.", None),
    10: ("In a private back hall, a Tang magistrate in dark official robes tears a sealed warrant in half, {monk} "
         "standing before him in travel-stained robes, a single candle between them, tense and humane.", M_YOUNG),
    11: ("A vast Gobi at dusk with five isolated beacon towers strung across the horizon, each with a small spring "
         "below, no vegetation, arrow slits and watchmen silhouettes, immense empty sky.", None),
    12: ("A ruined temple courtyard: {guide} kneels before {monk} to receive ordination, {horse} tied nearby, "
         "incense smoke and late sun.", M_YOUNG),
    13: ("Night at the desert edge: {guide} crouches by a small fire, face torn between fear and calculation, "
         "hand on his knife, {monk} sitting calm beyond the flames, cold blue wind.", M_YOUNG),
    14: ("Moonlit desert camp: {guide} stands over {monk}, who sits calmly with both hands empty and resting on his "
         "knees; only the guide holds a drawn short knife, body bent, circling, long shadows on sand, {horse} "
         "watching, cold blade light, no gore.", M_YOUNG),
    15: ("An extreme close view of a short knife blade trembling in the dark, moonlight running along the edge, "
         "a blurred ember behind, grains of sand drifting in the wind.", None),
    16: ("{monk} sits upright facing west in open desert, robes straightened, eyes closed, prayer beads moving "
         "through his fingers; the knife-wielding silhouette stands a few paces away, cold moonlight, immense stillness.", M_YOUNG),
    17: ("Two figures frozen in a night standoff on the sand, {monk} seated in meditation, {guide} standing with a "
         "lowered knife, {horse} between them, stars and wind, tension like a drawn string.", M_YOUNG),
    18: ("Grey dawn on the Gobi: {guide} sheathes his knife, bows with hands clasped, then leads his horse away; "
         "{monk} stands watching, thin light on their faces, tracks in the sand.", M_YOUNG),
    19: ("An immense desert under a huge sky, {monk} with {horse} walking west, tiny against the dunes, long shadows, "
         "dust haze, vertical composition emphasizing solitude.", M_YOUNG),
    20: ("Night at the base of a Tang beacon tower: {monk} crouches by a spring filling a water bag, arrows strike "
         "the sand around him, soldiers with bows above in torchlight, alarm.", M_YOUNG),
    21: ("Inside a beacon tower room, a Tang colonel in leather armor pours tea for {monk}, a map between them, "
         "his finger pointing to a route around two towers, lamplight, guarded sympathy.", M_YOUNG),
    22: ("At the fourth beacon, a Buddhist frontier officer presses the hands of {monk} in farewell, a small sutra "
         "charm on the table, night wind and torchlight, {horse} waiting outside.", M_YOUNG),
    23: ("An endless sea of dunes under a white-hot sky, no birds, only old camel bones and bleached skulls, "
         "{monk} and {horse} small at the center, heat shimmer, desolation.", M_YOUNG),
    24: ("A wall of yellow sandstorm swallows the desert, {monk} pulling his robe over his face, {horse} turning away, "
         "sun blazing through dust, dramatic apocalyptic light.", M_YOUNG),
    25: ("{monk} on horseback fumbles at a leather water bag, fingers trembling; the bag slips and falls toward the "
         "sand in a puff of dust, his cracked lips and desperate eyes.", M_YOUNG),
    26: ("Spilled water darkens and vanishes into scorching sand, steam rising, the empty leather bag lying flat, "
         "{monk} standing over the dry stain, pitiless noon light.", M_YOUNG),
    27: ("{monk} on his knees in the vast desert, hands empty, head bowed; {horse} stands over him, ribs showing, "
         "heat waves, a sense of physiological dread, no gore.", M_YOUNG),
    28: ("{monk} yanks the reins around, the head of {horse} pulled sideways, dust kicking up, his face set with "
         "shame and fear, westward dunes behind him.", M_YOUNG),
    29: ("{horse} gallops through deep sand toward the east, {monk} bent low over its neck, a long trail of hoofprints "
         "behind, urgent motion, dust cloud, low sun.", M_YOUNG),
    30: ("On a knife-edge dune ridge, {monk} hauls {horse} to a stop; below, a line of hoofprints leads back the way "
         "he came; wind-driven sand, vast empty horizon, a moment of decision.", M_YOUNG),
    31: ("{monk} stands beside {horse} on the ridge, head bowed, one hand pressed to his chest, wind tearing at his "
         "robe, the eastward tracks visible below, conscience and shame.", M_YOUNG),
    32: ("{monk} stands alone on a wind-scoured dune top, one hand raised toward the west in a vow, head bowed, robe "
         "whipping in the wind, {horse} standing by, dusk light, remorse turning to resolve.", M_YOUNG),
    33: ("{monk} turns {horse} back toward the west and steps into deepening sand, his back to the east, wind erasing "
         "the old tracks, resolve in his posture.", M_YOUNG),
    34: ("{monk} collapsed beside {horse} under a freezing night sky, lips cracked, eyes sunken, the animal lying down "
         "to shelter him, immense stars, frost on the sand.", M_YOUNG),
    35: ("At dawn in a low hollow, {horse} tugs at the sleeve of {monk} toward a patch of green grass and a small "
         "spring; he crawls forward, hands reaching the water, first light.", M_YOUNG),
    36: ("A torch-lit road to a Central Asian palace, envoys bowing, {monk} arriving with {horse}, high mud-brick walls, "
         "silk banners, night sky, grandeur.", M_MID),
    37: ("Inside a richly painted Gaochang hall, {king} personally offers a tray of food to {monk}, attendants kneeling, "
         "lamplight and silk, tense courtesy.", M_MID),
    38: ("{king} stands at the head of the hall, one hand on a sword hilt, gesturing to guards at the door; {monk} sits "
         "calm at a low table, the room divided by shadow.", M_MID),
    39: ("{monk} alone before a table laden with gold, silk and delicacies, the king's offer spread out; his hands rest "
         "empty in his lap, candlelight, moral weight.", M_MID),
    40: ("{monk} looks steadily at {king} and speaks, one hand open; the king's face hardens; painted walls and a gilded "
         "Buddha behind them, low warm light.", M_MID),
    41: ("{monk} sits in full lotus in a bare chamber, food trays untouched at the door, his face grown gaunt, a single "
         "high window casting hard light on the floor.", M_MID),
    42: ("{king} rushes into the chamber, courtiers behind him, {monk} slumped but upright, lips black, face grey; the "
         "king's composure breaking, dawn light.", M_MID),
    43: ("{king} kneels at the bedside and bows his head in apology, the thin hand of {monk} resting on the coverlet, "
         "the chamber soft with morning light, relief and shame.", M_MID),
    44: ("A long caravan assembles outside the Gaochang gate: thirty horses, twenty attendants, bundles of silk and "
         "sealed letters, {monk} in new robes at the head, dawn dust.", M_MID),
    45: ("A column of tiny figures crossing a vast glacier wall, blue ice cliffs and snow bridges, crevasses below, "
         "wind plumes, scale and danger, cold palette.", None),
    46: ("At a cave camp in the ice, {monk} kneels beside a frozen companion wrapped in cloth, exhausted men and horses "
         "behind, grey light, grief without gore.", M_MID),
    47: ("After four years of travel, {monk} stands before the great gateway of Nalanda monastery, brick towers and "
         "monks descending the steps, warm Indian light, arrival.", M_MID),
    48: ("An ancient Indian abbot with a white beard, one hundred and six years old, seated on a low throne, tears on "
         "his cheeks as he reaches toward {monk}, young monks watching, lamplight.", M_MID),
    49: ("In a dim library, {monk} reads a palm-leaf manuscript by oil lamp, surrounded by stacks of bundled leaves, "
         "gesturing in argument with invisible opponents, deep concentration.", M_MID),
    50: ("{monk} traces a line on an old palm-leaf manuscript under a small oil lamp, dust motes in the light, "
         "night stillness, a sense of resolution.", M_MID),
    51: ("A vast Indian assembly ground beside a river: eighteen kings under canopies, thousands of monks and scholars "
         "seated in tiers, banners, elephants, hazy golden light, enormous scale.", None),
    52: ("{monk} hangs a silk-covered panel at the gate of the assembly, a crowd watching, the panel turned away from "
         "view, torchlight, a formal challenge.", M_MID),
    53: ("The assembly hall over many days: rows of seated scholars in silence, the silk panel at the center, {monk} "
         "standing alone before it, no one rising, long shadows.", M_MID),
    54: ("Kings and monks bow before {monk}, silk scarves and gold offerings presented, he receives them with a slight "
         "bow, warm dusk light, triumph without arrogance.", M_MID),
    55: ("{monk} stands on the assembly steps looking back at the crowd, a young man's journey written in his weathered "
         "face, banners and dust, quiet pride.", M_MID),
    56: ("In a monastery cell, {monk} packs bundled palm-leaf manuscripts into wooden boxes, a caravan waiting outside, "
         "he does not look at the gifts behind him, dawn light.", M_MID),
    57: ("The great Zhuque Gate of Chang'an crowded with citizens, {monk} and a caravan of scriptures entering, "
         "officials and monks in procession, banners, spring haze, scale.", M_MID),
    58: ("In the Luoyang Yiluan Hall, the Tang emperor in dark gold-trimmed robes questions {monk}, maps of the western "
         "regions spread on a low table, courtiers at a distance, lamplight and authority.", M_MID),
    59: ("The emperor offers a seal of office on a lacquered tray; {monk} raises one hand in refusal, the two men alone "
         "in a vast hall, moonlight and candlelight, quiet resolve.", M_MID),
    60: ("{monk} sits at a plain desk with brush and paper, dawn light through a lattice window, stacks of manuscripts, "
         "his face at peace, a simple cell.", M_MID),
    61: ("A new translation hall at Ci'en Temple under construction, scaffolding and craftsmen, imperial commissioners "
         "presenting plans, {monk} bowing in thanks, spring light.", M_MID),
    62: ("Late night in the translation hall: {monk}, now aged with grey stubble and deep lines, writes at a worn desk "
         "by a single lamp, stacks of finished manuscripts around him, snow beyond the window, one continuous scene, "
         "quiet endurance.", M_MID),
    63: ("Close view of an aged monk's ink-stained sleeve and a brush resting beside a sheet of paper, a small oil "
         "lamp, stacks of bound manuscripts, deep night, quiet discipline.", None),
    64: ("A quiet chamber, {monk} lies on a simple bed, robes folded, disciples kneeling, cold winter light, serenity, "
         "no distress.", M_OLD),
    65: ("A vast procession of people in white robes outside the walls of Chang'an, moving slowly toward the Zhongnan "
         "Mountains under a grey sky, banners lowered, immense scale, collective grief.", None),
    66: ("A plain empty desert ridge at dusk, a single set of footprints and hoofprints, no gods or weapons in the sky, "
         "only wind and sand, austere realism.", None),
    67: ("The silhouette of {monk} on a dune at dusk, one hand raised as if remembering a slap, the vast desert behind, "
         "footprints leading west, timeless closure.", M_OLD),
}


def main():
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    units = plan["units"]
    assert len(units) == 68, len(units)
    assert sorted(PROMPTS) == list(range(68)), sorted(PROMPTS)

    prompts, unit_scenes = {}, []
    for i, unit in enumerate(units):
        template, monk = PROMPTS[i]
        text = template
        text = text.replace("{monk}", monk) if monk else text
        text = text.replace("{horse}", HORSE).replace("{guide}", GUIDE).replace("{king}", KING)
        assert "{" not in text and "}" not in text, (i, text)
        key = "unit-%03d" % i
        prompts[key] = {"en": text, "unit_id": unit["unit_id"], "order": i}
        unit_scenes.append({"image": key, "unit_id": unit["unit_id"]})

    scene = {
        "episode": "ytrm-01",
        "authored": True,
        "authored_by": "ytrm-01: 68 authored plates, one per narration unit, character continuity restated per prompt",
        "review_required": True,
        "unit_scenes": unit_scenes,
    }
    (EP / "image_prompts.json").write_text(json.dumps(prompts, ensure_ascii=False, indent=2), encoding="utf-8")
    (EP / "scene_plan.json").write_text(json.dumps(scene, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote", len(prompts), "prompts and", len(unit_scenes), "unit scenes")
    lengths = [len(v["en"]) for v in prompts.values()]
    print("prompt chars: min %d  median %d  max %d" % (min(lengths), sorted(lengths)[len(lengths)//2], max(lengths)))


if __name__ == "__main__":
    main()
