"""可塑性记忆 PMemories (PMem).

一个独立于大语言模型的持久记忆与自主进化系统。

分层（见 PLAN.md §4.1）：

- ``base``   基础设施：DB、事件、ID、时钟、配置、错误
- ``memory`` PMem Core：记忆的真相来源（原文不可变 + 索引 + 打分 + 去重）
- ``net``    PMem Net：实体 / 关系 / 图
- ``sleep``  PMem Sleep：夜间巩固
- ``evolve`` PMem Evolve：规律 / 冲突 / 经验
- ``trait``  PMem Trait：认知特质
- ``reflect`` PMem Reflect：反思（只读）
- ``gate``   PMem Gate：对外接口（MCP / REST / CLI / SDK）
"""

from __future__ import annotations

__version__ = "0.27.4"
__name_cn__ = "可塑性记忆"
__name_en__ = "PMemories"
__codename__ = "PMem"

SCHEMA_VERSION = "1.10.0"


def __getattr__(name: str):  # lazy: 避免 gate → pmem → gate 的循环导入
    if name == "PMem":
        from .gate.sdk import PMem

        return PMem
    raise AttributeError(f"module 'pmem' has no attribute {name!r}")


__all__ = ["__version__", "__name_cn__", "__name_en__", "__codename__", "SCHEMA_VERSION", "PMem"]
