"""打分：重要度、可信度、时效、RRF 融合、MMR 去冗（docs/03-algorithms.md §3–§5）。

本模块是纯函数集合，不碰数据库，方便单测与后续调参。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Callable

from .text import estimate_tokens

# 来源先验（03 §4 / PLAN §7.1）
SRC_PRIOR: dict[str, float] = {
    "user_explicit": 0.90,
    "user_confirmed": 0.85,
    "external": 0.80,
    "observed": 0.55,
    "inferred": 0.35,
    "unconfirmed": 0.25,
}

# 证据权重
TIER_W: dict[str, float] = {
    "user_explicit": 1.0,
    "user_confirmed": 0.9,
    "external": 0.8,
    "observed": 0.5,
    "inferred": 0.3,
    "unconfirmed": 0.2,
}

EXPLICITNESS: dict[str, float] = {
    "user_explicit": 1.0,
    "user_confirmed": 0.85,
    "external": 0.7,
    "observed": 0.45,
    "inferred": 0.25,
    "unconfirmed": 0.2,
}

CAP_ABSOLUTE = 0.95
CAP_SINGLE_SESSION = 0.70  # 仅约束弱来源；用户明说的一句话不该被压到 0.7（见 03 §4）
CAP_T3_ONLY = 0.70
CAP_T4_T5_ONLY = 0.60
CAP_AUTO_TRAIT = 0.75

STRONG_TIERS = frozenset({"user_explicit", "user_confirmed", "external"})

# 反证惩罚系数（03 §4）：出现反证时 confidence *= 0.5
CONF_DEMOTION = 0.5

EVIDENCE_K = 0.45


def _clip(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def length_fit(length: int) -> float:
    if 20 <= length <= 300:
        return 1.0
    if length < 20:
        return 0.6
    # >300：线性衰减到 0.5
    return _clip(1.0 - (length - 300) / 3000.0, 0.5, 1.0)


def emphasis_score(text: str) -> float:
    """强调词 / 反复叮嘱 → 1.0，否则 0。"""
    markers = ("记住", "重要", "别忘", "必须", "永远", "一定要", "下次", "千万别")
    hits = sum(1 for m in markers if m in text)
    return 1.0 if hits else 0.0


def importance(
    *,
    provenance: str,
    text: str,
    emphasis: bool | None = None,
    entity_count: int = 0,
    novelty: float = 0.5,
    pinned: bool = False,
) -> float:
    """重要度（03 §3）。``novelty`` 无向量时由调用方给近似值。"""
    if pinned:
        return 1.0
    p_src = SRC_PRIOR.get(provenance, 0.35)
    expl = EXPLICITNESS.get(provenance, 0.25)
    cov = min(1.0, entity_count / 3.0)
    emph = (1.0 if emphasis_score(text) else 0.0) if emphasis is None else (1.0 if emphasis else 0.0)
    lf = length_fit(len(text))
    score = (
        0.35 * p_src
        + 0.20 * expl
        + 0.15 * cov
        + 0.15 * emph
        + 0.10 * _clip(novelty)
        + 0.05 * lf
    )
    return round(_clip(score), 4)


def evidence_diversity(tiers: Iterable[tuple[str, str | None]]) -> float:
    """D = min(2.0, Σ w(tier))，按 (tier, session) 去重 —— 同会话复读不算新证据。"""
    seen: set[tuple[str, str | None]] = set()
    total = 0.0
    for tier, session in tiers:
        key = (tier, session)
        if key in seen:
            continue
        seen.add(key)
        total += TIER_W.get(tier, 0.2)
    return min(2.0, total)


def confidence_from_tiers(provenance: str, tiers: Sequence[str]) -> float:
    """按"每个不同 session 一条 tier"的列表算可信度（03 §4）。

    ``tiers`` 里同 tier 出现两次 = 两个来源同类型证据，权重叠加但受 D 上限约束。
    """
    prior = SRC_PRIOR.get(provenance, 0.35)
    d = evidence_diversity([(t, f"session{i}") for i, t in enumerate(tiers)])
    value = prior + (CAP_ABSOLUTE - prior) * (1.0 - math.exp(-EVIDENCE_K * d))
    tier_set = set(tiers)
    if tier_set <= {"inferred", "unconfirmed"}:
        value = min(value, CAP_T4_T5_ONLY)
    elif tier_set <= {"observed", "inferred", "unconfirmed"}:
        value = min(value, CAP_T3_ONLY)
    if len(tiers) <= 1 and tier_set.isdisjoint(STRONG_TIERS):
        value = min(value, CAP_SINGLE_SESSION)
    return round(min(value, CAP_ABSOLUTE), 4)


def confidence(
    provenance: str,
    tiers: Sequence[tuple[str, str | None]],
    *,
    auto_trait: bool = False,
) -> float:
    """可信度（03 §4）：先验 + 证据增长，再套封顶规则。"""
    prior = SRC_PRIOR.get(provenance, 0.35)
    d = evidence_diversity(tiers)
    value = prior + (CAP_ABSOLUTE - prior) * (1.0 - math.exp(-EVIDENCE_K * d))

    tier_set = {t for t, _ in tiers}
    if tier_set <= {"inferred", "unconfirmed"}:
        value = min(value, CAP_T4_T5_ONLY)
    elif tier_set <= {"observed", "inferred", "unconfirmed"}:
        value = min(value, CAP_T3_ONLY)
    sessions = {s for _, s in tiers if s}
    if len(sessions) <= 1 and tier_set.isdisjoint(STRONG_TIERS):
        value = min(value, CAP_SINGLE_SESSION)
    if auto_trait:
        value = min(value, CAP_AUTO_TRAIT)
    return round(min(value, CAP_ABSOLUTE), 4)


def recency(age_days: float, half_life_days: float) -> float:
    if half_life_days <= 0:
        return 1.0
    return math.exp(-math.log(2) * max(0.0, age_days) / half_life_days)


def rrf(
    rank_lists: Sequence[Sequence[int]],
    k: int = 60,
    weights: Sequence[float] | None = None,
) -> dict[int, float]:
    """Reciprocal Rank Fusion：只吃名次，免标定、对分数量纲免疫。

    ``weights`` 是每路的贡献权重（默认全 1.0，不传时与旧行为**逐位相同**）：
    第 ``i`` 路贡献 ``weights[i] / (k + rank)``。给「补充通道」降权时用它，
    不要去改 rank 列表本身——那同时会改候选池里谁先进，副作用比调权大。
    """
    if weights is not None and len(weights) != len(rank_lists):
        raise ValueError(
            f"weights 与 rank_lists 数量不一致: {len(weights)} != {len(rank_lists)}"
        )
    scores: dict[int, float] = {}
    for index, ranks in enumerate(rank_lists):
        weight = 1.0 if weights is None else float(weights[index])
        if weight <= 0.0:
            continue
        for idx, seq in enumerate(ranks, start=1):
            scores[seq] = scores.get(seq, 0.0) + weight / (k + idx)
    return scores


def rrf_normalize(
    score: float,
    channels: int,
    k: int = 60,
    weights: Sequence[float] | None = None,
) -> float:
    """RRF 归一化到 0..1。

    ``weights`` 缺省 = 每路 1.0（分母 ``channels/(k+1)``，与旧行为相同）。
    传了就按**实际权重和**算分母（``sum(weights)/(k+1)``），归一化尺子不因调权而变。
    """
    if channels <= 0 and not weights:
        return 0.0
    if weights:
        total = sum(max(0.0, float(w)) for w in weights)
    else:
        total = float(channels)
    ceiling = total / (k + 1)
    return _clip(score / ceiling) if ceiling else 0.0


# 软淡忘（task-40）：等级权重只是排序里的一档，不是淘汰；S 永不衰减等级档。
DECAY_GRADE_FACTORS: dict[str, float] = {
    "S": 1.0,
    "A": 0.95,
    "B": 0.9,
    "C": 0.8,
    "D": 0.7,
}


def decay_factor(
    *,
    idle_days: float,
    half_life_days: float = 30.0,
    min_factor: float = 0.35,
    grade: str | None = None,
    use_count: int = 0,
    grade_weight: bool = True,
    use_count_weight: bool = True,
) -> float:
    """软淡忘的排序因子：**只用于最终注入列表的重排**（不改状态/选中集合/注入条数）。

    1. 时间：``factor = max(min_factor, 0.5 ** (idle_days / half_life_days))``，
       ``idle_days`` 用 ``last_used_at or created_at``（与 lifecycle 同口径）；
    2. 等级：低等级再乘一档（S 1.0 / A 0.95 / B 0.9 / C 0.8 / D 0.7）；
    3. 使用次数：``1 + min(0.10, 0.03 * ln(1 + use_count))`` 的轻微加成（log，不是阈值）。

    ``min_factor`` 是下限：久不用的记忆在排序里最多只损失 ``1 - min_factor`` 的权重，
    但**不会因此落选**——候选池/MMR/预算都用未衰减分数，因子只在最后对已注入的
    那批条目排先后。
    """
    floor = max(0.0, min(1.0, float(min_factor)))
    half = float(half_life_days)
    idle = max(0.0, float(idle_days))
    if half > 0 and idle > 0:
        factor = 0.5 ** (idle / half)
    else:
        factor = 1.0
    factor = max(floor, factor)
    if grade_weight:
        factor *= DECAY_GRADE_FACTORS.get(str(grade or "").upper(), 1.0)
    if use_count_weight:
        factor *= 1.0 + min(0.10, 0.03 * math.log1p(max(0, int(use_count))))
    return factor


def final_score(
    *,
    rrf_norm: float,
    importance_v: float,
    confidence_v: float,
    recency_v: float,
    graph_affinity: float,
    w_rrf: float = 0.55,
    w_importance: float = 0.15,
    w_confidence: float = 0.12,
    w_recency: float = 0.10,
    w_graph: float = 0.08,
) -> float:
    return round(
        w_rrf * rrf_norm
        + w_importance * importance_v
        + w_confidence * confidence_v
        + w_recency * recency_v
        + w_graph * graph_affinity,
        6,
    )


def mmr_select(
    candidates: Sequence[tuple[int, float]],
    similarity: Callable[[int, int], float],
    *,
    limit: int,
    lam: float = 0.7,
) -> list[tuple[int, float]]:
    """MMR：相关性高、又和已选的不太像。

    ``candidates`` 为 [(seq, relevance)]；``similarity(a_seq, b_seq) -> [0,1]``。
    没有向量时用字符 3-gram Jaccard 当相似度（03 §5.2）。
    """
    pending = list(candidates)
    selected: list[tuple[int, float]] = []
    while pending and len(selected) < limit:
        best_idx = 0
        best_val = -1e9
        for i, (seq, rel) in enumerate(pending):
            redundancy = 0.0
            if selected:
                redundancy = max(similarity(seq, s_seq) for s_seq, _ in selected)
            val = lam * rel - (1.0 - lam) * redundancy
            if val > best_val:
                best_val = val
                best_idx = i
        selected.append(pending.pop(best_idx))
    return selected


def fit_budget(items: Sequence[dict], budget: int, *, per_item_overhead: int = 12, truncate_at: int = 120) -> tuple[list[dict], int]:
    """按 token 预算裁剪；超长条目截断并标 ``truncated``。"""
    out: list[dict] = []
    used = 0
    for item in items:
        text = item.get("text", "")
        cost = estimate_tokens(text) + per_item_overhead
        if used + cost <= budget:
            out.append(item)
            used += cost
            continue
        remaining = budget - used - per_item_overhead
        if remaining < 40:
            break
        head_chars = max(20, min(len(text), truncate_at))
        clipped = dict(item)
        clipped["text"] = text[:head_chars] + "…"
        clipped["truncated"] = True
        cost = estimate_tokens(clipped["text"]) + per_item_overhead
        if used + cost <= budget:
            out.append(clipped)
            used += cost
        break
    return out, used
