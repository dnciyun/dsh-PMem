"""接口安全（M8）：令牌与滑动窗口限流。

定位：PMem 默认只监听本机/stdio；令牌与限流是"被暴露时"的兜底，不是网络服务替代品。
令牌用常量时间比较，避免时序侧信道；限流按 key（MCP 用全局，REST 用客户端地址）。
"""

from __future__ import annotations

import hmac
import time
from collections import deque
from typing import Deque

from ..base.errors import BudgetError, DeniedError


def token_ok(expected: str, provided: str | None) -> bool:
    if not expected:
        return True
    if not provided:
        return False
    return hmac.compare_digest(expected, provided)


class RateLimiter:
    """滑动窗口：`max_per_min <= 0` 表示不限制。"""

    def __init__(self, max_per_min: int = 0) -> None:
        self.max_per_min = max(0, int(max_per_min))
        self._hits: dict[str, Deque[float]] = {}
        self._blocked = 0

    def check(self, key: str = "global", *, now: float | None = None) -> None:
        if self.max_per_min <= 0:
            return
        stamp = time.monotonic() if now is None else now
        window = self._hits.setdefault(key, deque())
        while window and stamp - window[0] >= 60.0:
            window.popleft()
        if len(window) >= self.max_per_min:
            self._blocked += 1
            raise BudgetError(
                f"请求过快：每分钟最多 {self.max_per_min} 次",
                hint="降低调用频率，或调大 [gate] rate_limit_per_min",
                code="E_RATE_LIMIT",
            )
        window.append(stamp)

    def blocked(self) -> int:
        return self._blocked


def require_token(expected: str, provided: str | None) -> None:
    if not token_ok(expected, provided):
        raise DeniedError(
            "令牌无效",
            hint="在 Authorization: Bearer <token> 或 MCP params._meta.token 中提供",
            code="E_DENIED",
        )
