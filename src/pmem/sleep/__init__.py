"""PMem Sleep（M4）：海马体的 replay 半环。

白天写入的是**经历**（原文 + 实体 + 关系），夜里做的是**巩固**：

    s1 收集 → s2 去重 → s3 关系 → s4 冲突 → s5 规律/经验 →
    s6 特质 → s7 反思/生命周期 → s8 快照

三条纪律：

1. **不产出新事实**：这一层只重组、浓缩、标记，不把推测写成事实；
2. **幂等可续跑**：每阶段一个 checkpoint，崩溃后从最后成功阶段继续；
3. **可回滚**：``--dry-run`` 全流程计算后整体回滚，只留报告。

对外入口是 :func:`pmem.sleep.orchestrator.run`；CLI ``pmem sleep``、
MCP ``memory_sleep``、SDK ``PMem.sleep`` 都只是它的薄壳。
"""

from __future__ import annotations

from .orchestrator import STAGES, catchup, run, status

__all__ = ["STAGES", "run", "catchup", "status"]
