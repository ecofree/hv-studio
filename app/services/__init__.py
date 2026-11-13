"""业务服务集中导出（惰性）。

H 批（2026-09-05）：从「顶层 import 全部子模块」改成 PEP 562 惰性导入。

为什么要改
----------
原先这里写着 `from . import (bgm, compose, image, motion, ...)`，而 compose
在模块级 import 了 managers / motion / upscale / config / numpy / PIL ——
结果是**任何**一条 `from app.services.<anything> import X` 都会先把这条
重依赖链整个拉起来，哪怕你只想拿一个常量。

这直接破坏了 `app/services/spec.py` 的存在意义。spec 的文档写着
「本模块只依赖标准库，任何模块 import 它都不会产生循环」——但只要它挂在
`app.services` 这个包里，而包 `__init__` 顶层 import 了 compose，这句承诺
就是假的：从导入链早期（例如 `app.models`）去引 spec，会炸成循环导入。

实测踩到的链路：
    app.managers → app.config.config → app.models → app.models.relay
                 → app.services.spec →【触发本 __init__】→ compose
                 → app.managers（尚未初始化完）→ ImportError:
                   cannot import name 'save' from partially initialized module

改成惰性后
----------
- `from app.services import compose` 语义不变：首次访问属性时才真正导入。
- `from app.services.spec import FPS` 不再拉起 compose 等重模块。
- `import app.services.compose` 照常。

D2（2026-09-05）：alignment / subtitle 已删——两个都是空壳，零调用点，
只在 __init__ 里被导出。真正在做的是 compose 里的烧字幕与 script 里的切分。
"""
from __future__ import annotations

import importlib

# app/services/ 下的全部子模块。列在这里 ≠ 立刻导入。
_LAZY = (
    "bgm", "compose", "cosyvoice", "image", "motion", "polyphone",
    "script", "shot_builder", "spec", "timing_assert", "upscale",
)

__all__ = list(_LAZY)


def __getattr__(name: str):
    if name in _LAZY:
        mod = importlib.import_module(f".{name}", __name__)
        globals()[name] = mod      # 缓存进模块字典，后续访问不再走 __getattr__
        return mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(_LAZY))
