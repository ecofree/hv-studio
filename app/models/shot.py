"""
Shot 数据模型。

Shot 是流水线最核心的工作单元：
  - 来自 Frame 切分（按音频时长 + 250ms 呼吸间隙）
  - 决定走 KEN_BURNS 还是 I2V
  - 记录 prompt、参考图、产物路径、校验结果

motion: MotionSpec 决定 Ken Burns 的具体曲线（drift/pan/push/pull）。
character_ref_pack: 角色定妆包路径，注入到 image[] 字段做一致性。
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field


class ShotType(str, Enum):
    STILL = "still"
    KEN_BURNS = "ken_burns"
    I2V = "i2v"


class MotionSpec(BaseModel):
    """
    Ken Burns 运镜参数。所有坐标归一化到 [0, 1]（以输入图为参考）。
    实际像素由 app/services/motion.py 基于 upscale 后尺寸算。
    """
    geometry_version: str = "legacy_pixel_crop_v1"
    max_zoom: float = 1.6
    type: str = "drift"                  # drift | pan | push | pull | static
    intensity: float = 0.08              # 推/拉/移的强度
    start_xy: tuple[float, float] | None = None  # (x, y) 起点
    end_xy: tuple[float, float] | None = None
    start_zoom: float = 1.0               # 机位：1.0 全景 / 1.18 中景 / 1.38 近景 / 1.6 特写
    end_zoom: float = 1.0
    easing: str = "ease_in_out"          # ease_in_out | linear | ease_in | ease_out
    grade: str | None = None             # 光线调色：dawn | noon | dusk | night | fire
                                         # None = 不调色（保持生图原色调，hw-001 基线行为）


class RefImage(BaseModel):
    """角色定妆包中的一张参考图。"""
    path: str
    angle: str = "front"                 # front | left | right | three_quarter_left | back
    expression: str = "neutral"
    notes: str = ""


class CharacterRefPack(BaseModel):
    """
    一个角色的多角度定妆照集。
    同一集同一角色共享同一 RefPack，注入到所有含该角色的 shot.image[] 字段。
    """
    character_id: str
    character_name: str
    images: list[RefImage] = Field(default_factory=list)
    anchor_face_path: str | None = None  # 标准锚脸（人脸相似度比对基准）
    face_embedding: list[float] | None = None  # 预计算的特征向量


class Shot(BaseModel):
    id: str                              # shot-001
    frame_id: str                        # 所属 frame
    index: int                           # 在 frame 内的序号（从 0）
    type: ShotType = ShotType.KEN_BURNS
    is_hero: bool = False                # 英雄镜头 → I2V 候选

    # ---------- 叙事语义（M2 固化，供编排/人工校对） ----------
    narration: str = ""                  # 本镜口播原文（对应字幕）
    shot_type: str = "medium"            # wide | medium | close_up | insert（构图语义）
    # D2（2026-09-05）：cast_refs / keywords / scene_ref 已删——三个字段全仓只有
    # 这里定义 + 测试 fixture 里当字面量填过，从未被读。Shot 是 extra=ignore（
    # pydantic v2 默认），旧 episode.json 里带这些键仍可正常载入，只是被忽略。

    duration_ms: int                     # 含 250ms 呼吸间隙（100 head + 150 tail）
    audio_start_ms: int                  # 在 frame audio 内的起始
    audio_end_ms: int
    audio_segment_path: str | None = None  # 切分后这一 shot 的独立 wav

    # ---------- 生图 prompt（已组装） ----------
    prompt: str
    negative_prompt: str = ""
    expression: str = ""                 # 显式表情（防 ref 表情污染）
    pose: str = ""                       # 显式姿态
    ref_pack_id: str | None = None       # 角色定妆包 ID
    ref_pack_paths: list[str] = Field(default_factory=list)

    # ---------- 产物路径 ----------
    image_path: str | None = None                # subrouter 返回的原始图
    image_actual_size: tuple[int, int] | None = None  # 真实像素（不是请求 size）
    upscaled_path: str | None = None
    motion_path: str | None = None               # Ken Burns 输出 mp4
    i2v_task_id: str | None = None
    i2v_path: str | None = None

    # ---------- 字幕（一行或多行） ----------
    subtitle_text: str = ""

    # ---------- 校验 ----------
    face_sim: float | None = None
    face_sim_warn: bool = False
    retry_count: int = 0
    last_error: str | None = None

    # ---------- stage 推进记录（M2 固化：单镜可重建的依据） ----------
    # 例：{"image": "done", "motion": "done"}。编排器读写，防重复重建。
    stage: dict[str, str] = Field(default_factory=dict)

    # ---------- 运镜 ----------
    motion: MotionSpec = Field(default_factory=MotionSpec)


    # C3（2026-09-05）：needs_regen() 已删——全仓零调用点，且判据之一 face_sim
    # 属于随 character_lock.py / face.py 一并删除的人脸一致性能力。
