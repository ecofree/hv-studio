"""输出规格常量——全项目唯一真源（整改 C1，审计 P1-1）。

为什么要单独一个模块
--------------------
此前这些常量分散在多处各写一份：
  HEAD_MS/TAIL_MS  compose.py / shot_builder.py / timing_assert.py 三份 100/150
  FPS              motion.py / timing_assert.py 两份 25
  NARRATION_SR     compose.py / voice._mp3_to_wav 默认值 两份 16000

它们是「跨模块一致」型常量：只要两份不同步，出的问题不会报错，而是**静默的
时间轴错位**——shot_builder 按 100ms 算的隔离间隙与 compose 实际加的 150ms 不一致，
成片就整体偏移，且帧指纹门禁查不出来（它只比帧数）。

原方案是「以 compose 为唯一真源，其余 import compose」。改成独立模块的原因：
compose 模块级 import 了 managers / motion / upscale / config / numpy / PIL，
让 timing_assert 和 shot_builder 去 import 它，等于把一条重量级依赖链塞进两个
纯逻辑模块，还引入了循环导入的可能。规格常量本质是**数据**，不该挂在
一个重量级模块的命名空间上——这正是 MIN_FRAG_CHARS 那次「由门禁反推而非手写」
同一个思路：能推就推，能共享就共享，不要复制。

本模块只依赖标准库，任何模块 import 它都不会产生循环。
"""
from __future__ import annotations

import math

# ---- 输出规格 ----
FPS = 25
W = 1080
H = 1920

# ---- 旁白隔离间隙（呼吸感）----
# 组间（不同底图之间）前加 HEAD_MS、后加 TAIL_MS，避免两句旁白贴在一起。
HEAD_MS = 100
TAIL_MS = 150

# ---- 旁白 wav 采样率 ----
# 与 voice._mp3_to_wav 的落盘口径（16k mono PCM）必须一致；
# 由 tests 的漂移守卫锁住，防止两边各改各的。
NARRATION_SR = 16000

# ---- 单镜时长门禁 ----
# H 批（2026-09-05）：从 timing_assert.py 收口到此。它们是「输出规格」而非
# 断言逻辑，且 script.py 的切分下限要由它们反推（跟 MIN_FRAG_CHARS 同一个
# 思路：能推就推，不手写魔数）。
MIN_FRAMES = 40            # ≥ 1.6s —— 短于此会来不及看清画面
MAX_FRAMES = 200           # ≤ 8s  —— 长于此单镜会闷，且撞上硬门禁必然挂

MIN_SHOT_MS = MIN_FRAMES * 1000 // FPS     # 1600ms
MAX_SHOT_MS = MAX_FRAMES * 1000 // FPS     # 8000ms

# 切分软上限：比硬门禁 MAX_SHOT_MS 留 25% 冗余，超了就该拆，别等撞门禁。
# 真源原是 config profile.rhythm.max_shot_ms=6000 —— 那一段全仓零消费者
# （H 批已删），这里让它变成有依据的常量。
SPLIT_THRESHOLD_MS = 6000

# ---- subrouter 出图尺寸模型 ----
# H 批（2026-09-05）：此前 1024*1536 这个数在两处各写一份
# （app/config/config.py 的 MP_FIXED、app/models/relay.py 的
# SUBROUTER_PIXEL_CAP），尺寸预测公式也照抄了两遍。改一处忘一处就会出现
# 「hv config 打印的尺寸」和「relay 实际按这个尺寸算配额」对不上。
#
# 实测：无论请求什么 size，中转站返回的图像素总量固定在这个数（≈1.5MP），
# 只是长宽比按请求来。样本：1024x1024→1254x1254、1024x1536→1024x1536、
# 1024x1792→948x1659。
SUBROUTER_PIXEL_CAP = 1024 * 1536


def group_of(frame_id: str) -> str:
    """帧 id 所属的「源句子组」。

    呼吸间隙只在**组边界**插入（组首 HEAD_MS、组尾 TAIL_MS），所以「哪些帧
    属于同一句」是时间轴的一等事实，而不是某处的私有约定。compose 按它算
    实际镜头时长，timing_assert 的花钱前预检要复刻同一口径 —— 两者一旦各写
    一份，预检就会与成片产生静默偏差（正是本模块开头警告的那类事故）。
    """
    parts = frame_id.split("-")
    return parts[1] if len(parts) > 1 else frame_id


def predict_actual_dims(w: int, h: int) -> tuple[int, int]:
    """按请求的宽高比，预测中转站实际返回的像素 (w_actual, h_actual)。"""
    ratio = w / h
    h_actual = int(math.sqrt(SUBROUTER_PIXEL_CAP / ratio))
    w_actual = int(ratio * h_actual)
    return w_actual, h_actual


def predict_actual_dims_of(size: str) -> tuple[int, int]:
    """predict_actual_dims 的 "WxH" 字符串入口（如 "1080x1920"）。"""
    w, h = size.split("x")
    return predict_actual_dims(int(w), int(h))
