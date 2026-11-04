"""ULID：48 位毫秒时间 + 80 位随机，Crockford Base32。

为什么不用 uuid4：ULID 的字典序 = 时间序，分页、按天切分、范围查询都不用额外索引。
同一毫秒内单调递增（随机部分 +1），保证并发写入的 id 仍然有序。
"""

from __future__ import annotations

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_MAX_RAND = (1 << 80) - 1

_last_ms: int = -1
_last_rand: int = 0


def _encode(value: int, length: int) -> str:
    out: list[str] = []
    for _ in range(length):
        out.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def ulid(ts_ms: int | None = None) -> str:
    """生成一个 ULID。``ts_ms`` 仅用于测试与导入（回放历史时间）。"""
    global _last_ms, _last_rand
    ms = int(time.time() * 1000) if ts_ms is None else int(ts_ms)
    if ms > _last_ms:
        _last_ms = ms
        _last_rand = int.from_bytes(os.urandom(10), "big")
    else:
        # 同一毫秒（或时间回拨）：单调 +1，避免 id 逆序
        _last_rand = (_last_rand + 1) & _MAX_RAND
        ms = _last_ms
    return _encode(ms, 10) + _encode(_last_rand, 16)


def ulid_time_ms(value: str) -> int:
    """从 ULID 里取回毫秒时间戳（10 个字符 = 50 位，取低 48 位）。"""
    if len(value) != 26:
        raise ValueError(f"not a ulid: {value!r}")
    n = 0
    for ch in value[:10]:
        n = (n << 5) | _ALPHABET.index(ch)
    return n & ((1 << 48) - 1)


def is_ulid(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 26:
        return False
    return all(c in _ALPHABET for c in value)
