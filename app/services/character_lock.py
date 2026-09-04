"""
角色一致性服务（M2 实装：insightface buffalo_l embed 门控）。

职责（见 .workbuddy/memory/2026-09-03.md 实测结论）：
  1. resource/ref_packs/{character_id}/ 定妆包管理：load_pack
  2. check_consistency：生成图人脸 embed vs 锚图余弦相似度 → face_sim
  3. shot 产图后登记 face_sim / face_sim_warn，供 needs_regen() 判重做
  4. （可选）把锚图作为参考图注入 edits 图生图，增强保人

M2 实测结论（2026-09-03，务必读）：
  - insightface buffalo_l 能可靠检测 AI 画图人脸 + 出 512d embed
  - 同人不同镜头（文生图随机）sim 约 0.17~0.41 —— 远低于 0.5
  - 单参考图 edits 图生图 sim 约 0.44 —— 有提升但不足 0.6
  - 锚图质量决定上限：shot-000(5脸群像/最大脸72px) 是差锚；
    需专用正脸大特写定妆照做锚
  - 所以 character_lock 的可靠门槛是「高质量锚」+「embed 门控自动重做」
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from app.models import CharacterRefPack, RefImage, Shot

RESOURCE = Path(__file__).resolve().parent.parent.parent / "resource"
REF_PACKS = RESOURCE / "ref_packs"

# 一致性判定阈值（cosine）。ArcFace 惯例：>0.6 同人，<0.4 判不同人。
# 竖屏 AI 画图多姿态场景放宽：<0.35 明确判不同人(重做)，0.35~0.45 存疑告警
SIM_ACCEPT = 0.45
SIM_REJECT = 0.35


def pack_dir(character_id: str) -> Path:
    return REF_PACKS / character_id


def load_pack(character_id: str) -> CharacterRefPack | None:
    """从 resource/ref_packs/{id}/ 加载定妆包。目录不存在返回 None。"""
    d = pack_dir(character_id)
    meta_p = d / "meta.json"
    if not meta_p.exists():
        return None
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    images: list[RefImage] = []
    for img in meta.get("images", []):
        p = d / img["file"]
        if not p.exists():
            continue
        images.append(RefImage(
            path=str(p).replace("\\", "/"),
            angle=img.get("angle", "front"),
            expression=img.get("expression", "neutral"),
        ))
    anchor_f = meta.get("anchor_face")
    anchor_path = str(d / anchor_f).replace("\\", "/") if anchor_f else None
    return CharacterRefPack(
        character_id=character_id,
        character_name=meta.get("character_name", character_id),
        images=images,
        anchor_face_path=anchor_path,
    )


def _norm(e: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(e)
    return e / n if n > 0 else e


def anchor_embedding(pack: CharacterRefPack) -> np.ndarray | None:
    """
    计算锚脸 embed（缓存到 pack.face_embedding）。
    从 anchor_face_path（首选）或 images[0] 检测人脸提 embed。
    """
    from app.services.face import embed_face

    if pack.face_embedding is not None:
        return np.array(pack.face_embedding)
    candidates = []
    if pack.anchor_face_path:
        candidates.append(pack.anchor_face_path)
    candidates += [i.path for i in pack.images]
    for p in candidates:
        e, meta = embed_face(p)
        if e is not None:
            pack.face_embedding = e.tolist()
            return e
    return None


def check_consistency(shot: Shot, anchor_embed: np.ndarray | None = None) -> float:
    """
    检测 shot.image_path 人脸 embed，与锚图比余弦相似度。
    返回 sim；并写 shot.face_sim / shot.face_sim_warn / shot.last_error。
    - 无人脸 → 返回 0.0 且 face_sim_warn=True（构图无人/脸太小）
    - 有 embed → cosine
    """
    from app.services.face import cosine, embed_face

    if not shot.image_path:
        shot.last_error = "shot 无 image_path"
        return 0.0
    if anchor_embed is None:
        # 尝试从 shot.ref_pack 找锚
        pack = load_pack(shot.ref_pack_id) if shot.ref_pack_id else None
        if pack is None and shot.ref_pack_paths:
            pack = CharacterRefPack(
                character_id=shot.ref_pack_id or "anon",
                character_name="anon",
                images=[RefImage(path=p) for p in shot.ref_pack_paths],
            )
        if pack is None:
            shot.face_sim = 0.0
            shot.face_sim_warn = True
            shot.last_error = "无锚图可比"
            return 0.0
        anchor_embed = anchor_embedding(pack)
        if anchor_embed is None:
            shot.face_sim = 0.0
            shot.face_sim_warn = True
            shot.last_error = "锚图无人脸"
            return 0.0

    emb, meta = embed_face(shot.image_path)
    if emb is None:
        shot.face_sim = 0.0
        shot.face_sim_warn = True
        shot.last_error = "生成图无人脸（构图缺人/脸太小）"
        return 0.0

    sim = cosine(_norm(anchor_embed), emb)
    shot.face_sim = round(sim, 4)
    shot.face_sim_warn = sim < SIM_REJECT
    shot.last_error = None
    return sim


def scan_episode_consistency(ep) -> dict[str, float]:
    """
    对整集所有 shot 做一致性扫描（用于回归/质检）。
    需要每 shot 的 ref_pack_id 指向同一角色；anchor 取该角色定妆包。
    返回 {shot_id: sim}。
    """
    res: dict[str, float] = {}
    packs: dict[str, CharacterRefPack] = {}
    for sid, shot in ep.shots.items():
        if not shot.image_path or not Path(shot.image_path).exists():
            res[sid] = 0.0
            continue
        pid = shot.ref_pack_id or "zhangqian"
        if pid not in packs:
            packs[pid] = load_pack(pid)
            if packs[pid] is not None:
                anchor_embedding(packs[pid])
        p = packs.get(pid)
        emb = anchor_embedding(p) if p else None
        res[sid] = check_consistency(shot, emb)
    return res


def should_override_expression() -> bool:
    """是否需要在 prompt 中显式覆盖表情。"""
    return True
