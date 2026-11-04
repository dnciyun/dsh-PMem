"""记忆动力学（M9.65）：浮现概率是算出来的，不是存出来的。

一个不带时间的标量没法表达遗忘：`0.4` 在一小时后和六个月后意思完全一样。
所以这里**不存强度**，只存"什么时候被想起过"（`surface_log` 与 `created_at`），
强度由时钟推出来：

    B = ln( Σ_j (t - t_j)^-d ),  d = 0.5          # ACT-R base-level activation
    R = 1 / (1 + exp(-(B - τ) / s))               # 可提取概率

为什么是 ACT-R 而不是 FSRS：

- FSRS 需要真实复习评分（Again / Hard / Good / Easy），我们还没有；
- ACT-R 只需要访问时间戳，而访问时间戳本来就在日志里，零迁移；
- 两者都表达同一件事——**强度是时间的函数**，只是精度不同。
  等积累出几千条真实浮现回执（M9.65b）再谈升级，现在拟合就是过拟合。

三个性质是这套东西存在的理由：

1. 常被想起 → B 变大 → 更牢；
2. 久不想起 → B 变小 → 更淡；
3. **再想起 → B 立刻回升**（第 3 条是原来那套"按等级 + 闲置天数"的阶梯没有的）。

注意：这里的 R 是**浮现概率**，不是"重要性"。淡忘不是独立机制，
它就是 R 随时间下降，所以一套参数同时管"该不该浮现"和"沉不沉下去"。
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Iterable

from ..base.clock import now as clock_now, parse_iso

# ACT-R 的衰减指数。0.5 是 ACT-R 默认值，也是幂律遗忘的标准取值。
D = 0.5

# 两次访问之间至少按 1 分钟算，避免同一秒内的重复访问把 B 推到无穷。
FLOOR_MINUTES = 1.0

# logistic 的中点与斜率。τ=-1.5 让"单次访问、7 天前"落在 R≈0.71，
# "30 天前"落在 R≈0.42，"一年没碰"落在 R≈0.08。
TAU = -1.5
SCALE = 0.6

# 等级权重：等级是"它值不值得占上下文"，不是"它会不会被忘"。
# D 级为 0：从来没打算浮现的东西，不该因为词法撞上就冒出来。
GRADE_WEIGHT = {"S": 1.0, "A": 0.9, "B": 0.75, "C": 0.5, "D": 0.0}
NO_GRADE_WEIGHT = 0.6

# 线索命中的下限：你刚刚提到它，就不该因为"旧"而沉默。
CUE_FLOOR = 0.6

# turn 模式的浮现门限：低于它就不打扰（浮现有代价，要 precision 不是 recall）。
TURN_THRESHOLD = 0.25

# 「快忘了」的窗口：R 掉进这个区间才值得主动提一次。
# 下限压到 0.02 是故意的——已经淡到快没了的高等级记忆，正是最该被"防御性复习"的；
# 但同一个会话最多主动提 3 条，否则就变成噪音。
RHYTHM_LOW = 0.10
RHYTHM_HIGH = 0.45
RHYTHM_MIN_P = 0.03
RHYTHM_PER_SESSION = 3

# 公平车道：低优先级记忆不能因为「分数永远不够」而被饿死。
# 闲置超过 FAIR_AFTER_DAYS 天的记忆，每个会话允许强制提一次（最多 1 条）。
# 它保证的是「有界最迟召回」，不是「经常出现」——所以阈值给得很宽。
FAIR_AFTER_DAYS = 30
FAIR_PER_SESSION = 1


def _as_datetime(value: datetime | str) -> datetime:
    return value if isinstance(value, datetime) else parse_iso(str(value))


def _days_since(moment: datetime, stamp: datetime) -> float:
    return max(0.0, (moment - stamp).total_seconds() / 86400.0)


def activation(
    times: Iterable[datetime | str],
    *,
    now: datetime | str | None = None,
    d: float = D,
    floor_minutes: float = FLOOR_MINUTES,
) -> float:
    """ACT-R base-level activation：``B = ln( Σ (t - t_j)^-d )``。

    没有访问记录时返回 ``-inf``（可提取概率为 0）。
    """
    moment = _as_datetime(now) if now is not None else clock_now()
    floor_days = max(floor_minutes, 1e-9) / 1440.0
    total = 0.0
    for stamp in times:
        try:
            at = _as_datetime(stamp)
        except (TypeError, ValueError):
            continue
        elapsed = max(floor_days, _days_since(moment, at))
        total += elapsed ** -d
    return math.log(total) if total > 0 else float("-inf")


def squash(b: float, *, tau: float = TAU, scale: float = SCALE) -> float:
    """把 base-level 压到 [0,1] 的可提取概率。"""
    if b == float("-inf"):
        return 0.0
    if scale <= 0:
        return 0.0
    x = (b - tau) / scale
    if x >= 60:
        return 1.0
    if x <= -60:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


def retrievability(
    times: Iterable[datetime | str],
    *,
    now: datetime | str | None = None,
) -> float:
    """``R``：此刻还能不能想起来。空记录 → 0。"""
    return squash(activation(times, now=now))


def grade_weight(grade: str | None) -> float:
    return GRADE_WEIGHT.get((grade or "").upper(), NO_GRADE_WEIGHT)


def score(
    grade: str | None,
    times: Iterable[datetime | str],
    *,
    now: datetime | str | None = None,
    floor: float = 0.0,
    boost: float = 1.0,
) -> float:
    """浮现概率 ``p = max(R, floor) × 等级权重 × boost``，夹在 [0,1]。"""
    r = retrievability(times, now=now)
    if floor > 0:
        r = max(r, floor)
    return max(0.0, min(1.0, r * grade_weight(grade) * float(boost)))


def describe(
    grade: str | None,
    times: Iterable[datetime | str],
    *,
    now: datetime | str | None = None,
    floor: float = 0.0,
    boost: float = 1.0,
) -> dict[str, Any]:
    """可解释版本：把 B / R / 权重一起带出来，方便 `pmem surface --explain`。"""
    marks = list(times)
    moment = _as_datetime(now) if now is not None else clock_now()
    b = activation(marks, now=moment)
    r = squash(b)
    effective = max(r, floor) if floor > 0 else r
    p = max(0.0, min(1.0, effective * grade_weight(grade) * float(boost)))
    return {
        "b": None if b == float("-inf") else round(b, 4),
        "r": round(r, 4),
        "weight": grade_weight(grade),
        "p": round(p, 4),
        "accesses": len(marks),
        "floor": floor,
    }


def is_due(r: float, *, low: float = RHYTHM_LOW, high: float = RHYTHM_HIGH) -> bool:
    """R 是否落在「快忘了、现在提一次最值」的窗口里。"""
    return low <= r <= high
