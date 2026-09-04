"""
字幕烧录服务（STUB，待 M1 实装）。

输入：frame.subtitle: list[SubtitleLine]（来自 char_align 切片）
输出：srt 字幕文件 + 烧录到 mp4

样式：见 config.yaml 的 profile.subtitle
  - 字体：思源宋体
  - 主色 &H00FFFFFF（白），描边 4px 黑色
  - 底部 320px、顶部 200px 安全区
  - 关键词高亮：&H0000D7FF（亮青），font_size 80

抖音 UI 遮挡实测：底部约 280-350px，顶部约 200px
"""
from __future__ import annotations

from app.models import Frame


def render_ass(frames: list[Frame]) -> str:
    raise NotImplementedError("M1 实装：生成 .ass 字幕，含样式 + 关键词高亮")


def burn_in(mp4_path: str, ass_path: str, dst_path: str) -> str:
    raise NotImplementedError("M1 实装：ffmpeg ass 滤镜烧录到 mp4")
