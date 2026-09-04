"""
Episode / Frame / Subtitle 数据模型。

整个 episode.json 用一个 Episode 对象描述，包含：
  - frames: 原始脚本切句（可能一句话切多个 frame）
  - shots: 镜头（frame 进一步切分到音频时长 + 呼吸间隙粒度）

所有路径用 POSIX 风格（/），与 ffmpeg 一致。
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from .shot import Shot


class EpisodeStatus(str, Enum):
    """
    状态机（线性、不可跳跃回退，回退通过显式 reset()）：
        DRAFT         → 仅有原始脚本
        SCRIPTED      → 切分 frame 完成
        TTS_ALIGNED   → TTS 生成 + 强制对齐完成
        IMAGE_LOCKED  → 所有 shot 配图完成（含一致性校验）
        UPSCALED      → RealESRGAN 2× 完成
        COMPOSED      → 视频合成 + 字幕烧录完成
        DONE          → 全流程通过
        FAILED        → 任一步骤失败，需人工介入
    """
    DRAFT = "draft"
    SCRIPTED = "scripted"
    TTS_ALIGNED = "tts_aligned"
    IMAGE_LOCKED = "image_locked"
    UPSCALED = "upscaled"
    COMPOSED = "composed"
    DONE = "done"
    FAILED = "failed"

    @classmethod
    def valid_transition(cls, frm: "EpisodeStatus", to: "EpisodeStatus") -> bool:
        """线性状态机。回退显式走 reset()。"""
        order = [
            cls.DRAFT, cls.SCRIPTED, cls.TTS_ALIGNED, cls.IMAGE_LOCKED,
            cls.UPSCALED, cls.COMPOSED, cls.DONE,
        ]
        # FAILED 可从任何状态进入
        if to == cls.FAILED:
            return True
        try:
            return order.index(to) == order.index(frm) + 1
        except ValueError:
            return False


class SubtitleLine(BaseModel):
    """一行字幕，对应一段对齐后的字符/词。"""
    text: str
    start_ms: int
    end_ms: int
    keyword: str | None = None  # 关键词高亮（用 subtitle.keyword 配置）


class CharToken(BaseModel):
    """MMS_FA 字符级强对齐的一个 token。"""
    char: str
    start_ms: int
    end_ms: int
    conf: float = 1.0


class Frame(BaseModel):
    """一段脚本（一句话或一个完整意思），可能切为多个 shot。"""
    id: str                              # frame-001
    text: str                            # 原文（未做 polyphone 替换）
    narration_text: str | None = None    # 已做 polyphone 替换、TTS 用的文本
    audio_path: str | None = None        # TTS 输出 wav
    audio_duration_ms: int | None = None
    char_align: list[CharToken] = Field(default_factory=list)  # MMS_FA
    subtitle: list[SubtitleLine] = Field(default_factory=list)
    shot_ids: list[str] = Field(default_factory=list)          # 引用 Shot.id


class Episode(BaseModel):
    """
    整集 = 一个 Episode 对象。可序列化为 episodes/{id}.json。
    任何阶段产物都登记在对应字段；shots 用 dict[id, Shot] 而非 list，
    保证 O(1) 查找。
    """
    id: str                              # episode-001
    schema_version: str = "1.0"          # M2 固化：episode.json schema 版本
    title: str
    series: str = "汉武大帝"
    episode_number: int = 1
    status: EpisodeStatus = EpisodeStatus.DRAFT
    script_path: str | None = None
    frames: list[Frame] = Field(default_factory=list)
    shots: dict[str, Shot] = Field(default_factory=dict)
    profile: str = "ancient-realistic"  # 覆盖 config.yaml default_profile
    notes: str = ""
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    def transition_to(self, new: EpisodeStatus) -> None:
        if not EpisodeStatus.valid_transition(self.status, new):
            raise ValueError(f"非法状态迁移: {self.status} → {new}")
        self.status = new
        self.updated_at = datetime.now()

    def reset(self, to: EpisodeStatus = EpisodeStatus.DRAFT) -> None:
        """显式回退（用于重做某阶段）。"""
        self.status = to
        self.updated_at = datetime.now()
