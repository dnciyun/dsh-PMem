"""兼容层：M6 起特质推导在 `pmem.trait.derive`。

保留这个模块是为了不动 M4 的 import 路径；新代码请直接 import `pmem.trait`。
"""

from __future__ import annotations

from ..trait.derive import derive, strength

__all__ = ["derive", "strength"]
