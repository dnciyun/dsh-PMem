"""反思（M7）：只读质量审计 + 周报。

对外只暴露四个入口：

- ``audit``：六维质量分、缺陷清单、可执行建议（纯 SELECT）；
- ``reflect``：日审计 / 周报，``persist=True`` 时只写 ``reflection`` + ``metrics``；
- ``history``：历史反思周期与质量分；
- ``render``：人读文本。

G7：本包不 import ``memory.store`` 的写接口，反思结果永远不能直接变成事实。
"""

from __future__ import annotations

from .audit import WEIGHTS, audit as run_audit
from .report import week_bounds, week_key
from .service import history, reflect, render

__all__ = [
    "WEIGHTS",
    "history",
    "reflect",
    "render",
    "run_audit",
    "week_bounds",
    "week_key",
]
