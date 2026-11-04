"""评测台：合成集生成 + 指标执行（M2）。

真实会话没有标注，合成集用来把算法调到及格线；真实数据用来观察漂移。
"""

from __future__ import annotations

__all__ = ["synthetic", "runner"]
