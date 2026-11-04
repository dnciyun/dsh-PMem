"""自我层（M17）：宪法 + 叙事 + 未闭环承诺。

对外只暴露一个模块 ``soul``，因为它就是一份可读的文本契约：
``constitution.md`` / ``narrative.md`` + ``history/`` 版本快照。
"""

from __future__ import annotations

from . import soul

__all__ = ["soul"]
