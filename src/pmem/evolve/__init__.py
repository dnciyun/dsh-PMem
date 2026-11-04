"""PMem Evolve（M5）：从长期数据里找规律、复核候选、复用经验、校准参数。

分工：

- ``review``    人工标签 / 复核队列 / 准确率
- ``experience`` 情境匹配、复用、反馈
- ``conflicts`` 超期升级与保守裁决
- ``tuning``    标注量足够时的参数自调（只在评估提升时生效）

不改写原文，不直接改 Trait（交 Trait 模块），不把推测写成事实。
"""

from __future__ import annotations

from .runner import LAYERS, run, stats

__all__ = ["LAYERS", "run", "stats"]
