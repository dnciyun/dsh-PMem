"""PMem Trait（M6）：稳定认知特征的版本化存储。

- ``derive`` 从规律推导特质，证据每次全量重算；
- ``history`` 版本链 / 事件时间线 / 指标（trace_rate、churn）；
- ``profile`` 只读画像，供宿主低成本注入。

硬约束：无证据不建特质；established 必须 ≥3 天证据、strength/confidence 达标、
无未决反证；只降级不删除；每次状态变化保留版本链。
"""

from __future__ import annotations

from .derive import derive as derive_trait
from .derive import strength
from .history import list_traits, metrics, timeline, versions
from .profile import build as build_profile

__all__ = [
    "derive_trait",
    "strength",
    "versions",
    "timeline",
    "list_traits",
    "metrics",
    "build_profile",
]
