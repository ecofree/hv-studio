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
