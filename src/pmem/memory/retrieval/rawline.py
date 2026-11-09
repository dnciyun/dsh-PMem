"""原话通道（task-53，默认关）：把 journal_line 当一等检索源。

为什么需要它：派生记忆会把长原话截断/改人称/摘要，714 题里大量「证据其实在库里」
的题，证据只在原话行里，现有 raw_fallback 只在覆盖率 <0.30 时补 ≤3 条，够不到。
但原话不能直接塞：探针显示 top-8 里 35% 是 >45 天旧行、14.6% 是别的问题的证据，
直接注会喂出「无关旧事/违反禁止」。所以这里做三件事：

1. **同一把字面尺子**：复用 ``query.content_terms`` / ``query.bigrams`` / 
   ``coverage_idf_sets`` 与现成的 ``store._term_weights``（全库 IDF），不另造一套分；
2. **时间衰减**：``w = max(0.2, 0.5 ** (age_days / max_age_days * 2))``，
   老行不丢，只是权重低；
3. **只追加、不去挤**：``pick`` 对已有 payload 做 12 字窗口去重，并受
   ``budget_share`` 字符预算约束；search 在正常 payload 之后追加，原有条目顺序/数量不变。

默认 ``retrieval.rawline.enabled=false``；关时 search 不调用本模块、不新增 SQL。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ...base import clock
from .. import query as query_mod
from ..text import normalize

DEFAULT_COUNT = 8
DEFAULT_MAX_AGE_DAYS = 45
# 字面分下限：**绝对下限要小**。`coverage_idf_sets` 的分母是「问句里在库中出现过的双字」
# 的 IDF 和，一条原话通常只覆盖其中两三个词，分数天然在 0.1~0.4 量级；实测 0.25 会把
# 全部候选都滤掉（candidates=0，A/B 变成两侧同构的空跑）。这里把它当「噪声地板」用，
# 真正的排序交给分数×时间衰减。
DEFAULT_MIN_SCORE = 0.10
DEFAULT_BUDGET_SHARE = 0.4
DEFAULT_DEDUPE_OVERLAP = 0.6
# 只有相隔 ≤N 天的近似文本才算「重复」；默认 1（同日 + 次日）。
DEFAULT_DEDUPE_SAME_DAY_DAYS = 1


def settings(cfg: Any) -> dict[str, Any]:
    raw = None
    try:
        raw = cfg.get("retrieval.rawline") if cfg is not None else None
    except Exception:  # noqa: BLE001 坏 config 当作缺省
        raw = None
    data = raw if isinstance(raw, dict) else {}
    return {
        "enabled": bool(data.get("enabled", False)),
        "count": max(1, int(data.get("count", DEFAULT_COUNT) or DEFAULT_COUNT)),
        "max_age_days": max(1.0, float(data.get("max_age_days", DEFAULT_MAX_AGE_DAYS) or DEFAULT_MAX_AGE_DAYS)),
        "min_score": max(0.0, float(data.get("min_score", DEFAULT_MIN_SCORE) or 0.0)),
        "budget_share": max(0.0, min(1.0, float(data.get("budget_share", DEFAULT_BUDGET_SHARE) or 0.0))),
        "dedupe_overlap": max(0.0, min(1.0, float(data.get("dedupe_overlap", DEFAULT_DEDUPE_OVERLAP) or 0.0))),
        # 跨天的近似文本**不是**重复：同一件事在 4/1 和 7/20 各说一次，是「两个事件/
        # 两个时间点」，问「最早是什么时候」时恰恰需要两条都在。只有同一天（同日复述、
        # 助手回声）才当成重复去掉。0 = 只删同一天，N = 相隔 ≤N 天也算同一次。
        "dedupe_same_day_days": max(0, int(data.get("dedupe_same_day_days", DEFAULT_DEDUPE_SAME_DAY_DAYS) or 0)),
    }


def _now() -> datetime:
    # 必须走产品 clock（测试/回放会 set_now），不能用 datetime.now()。
    return clock.now()


def _age_days(ts: str, now: datetime) -> float:
    try:
        moment = clock.parse_iso(str(ts or ""))
    except Exception:  # noqa: BLE001 坏 ts 当作很老
        return 9999.0
    if moment.tzinfo is None:
        from datetime import UTC

        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (now - moment).total_seconds() / 86400.0)


def _time_weight(age_days: float, max_age_days: float) -> float:
    return max(0.2, 0.5 ** (age_days / max_age_days * 2.0))


def candidates(
    store: Any, query: str, *, cutoff_ts: str, cfg: Any
) -> list[dict[str, Any]]:
    """cutoff 前的 journal_line 候选，按和 lexical 同一把覆盖率尺子打分 × 时间衰减。"""
    conf = settings(cfg)
    if not conf["enabled"]:
        return []
    terms = query_mod.content_terms(str(query or ""))
    if not terms:
        return []
    # cutoff 用 datetime 比、不在 SQL 里比字符串：ts 可能是 Z、+08:00、+00:00 混写，
    # 字符串比较会把同一时刻排错（实测会漏掉整批行）。
    try:
        cutoff_dt = clock.parse_iso(str(cutoff_ts or ""))
    except Exception:  # noqa: BLE001 坏 cutoff 就当没有截断
        cutoff_dt = None
    sql = (
        "SELECT jl.id, jl.text, jl.ts, jl.role, jl.day_key, jl.seq "
        "FROM journal_line jl "
        "WHERE jl.subject_id=? "
        "AND NOT EXISTS (SELECT 1 FROM journal_retraction r "
        "WHERE r.subject_id=jl.subject_id AND r.line_id=jl.id) "
        "ORDER BY jl.seq"
    )
    try:
        rows = store.db.conn.execute(sql, (str(store.subject),)).fetchall()
    except Exception:  # noqa: BLE001 老库/只读：原话通道不存在也不报错
        return []
    if not rows:
        return []
    if cutoff_dt is not None:
        kept = []
        for row in rows:
            try:
                moment = clock.parse_iso(str(row["ts"] or ""))
            except Exception:  # noqa: BLE001
                continue
            if moment.tzinfo is None:
                from datetime import UTC

                moment = moment.replace(tzinfo=UTC)
            if moment <= cutoff_dt:
                kept.append(row)
        rows = kept
    if not rows:
        return []
    texts = [str(row["text"] or "") for row in rows]
    sets = [query_mod.bigrams(text) for text in texts]
    weights = store._term_weights(terms)  # 和 memory 打分同一个全库 IDF 字典
    now = _now()
    out: list[dict[str, Any]] = []
    for row, text, grams in zip(rows, texts, sets):
        if not text:
            continue
        base_score = query_mod.coverage_idf_sets(
            terms, sets, grams, weights or None
        )
        if base_score <= 0:
            continue
        age = _age_days(str(row["ts"] or ""), now)
        # 地板看**原始覆盖率**（时间衰减只影响排序，不该把一条年轻但有依据的行判成噪声）。
        if base_score < conf["min_score"]:
            continue
        score = base_score * _time_weight(age, conf["max_age_days"])
        out.append(
            {
                "line_id": str(row["id"]),
                "text": text,
                "ts": str(row["ts"] or ""),
                "day": str(row["day_key"] or ""),
                "role": str(row["role"] or "user"),
                "seq": int(row["seq"] or 0),
                "score": round(float(score), 6),
                "base_score": round(float(base_score), 6),
                "age_days": round(float(age), 3),
            }
        )
    out.sort(key=lambda item: (-item["score"], -item["seq"]))
    return out


def _windows(text: str, width: int = 12) -> set[str]:
    body = normalize(text)
    if len(body) <= width:
        return {body} if body else set()
    return {body[index : index + width] for index in range(len(body) - width + 1)}


def _day_gap(left: str, right: str) -> int | None:
    """两个 day_key 相差几天；解析不了就返回 None（当同一天处理，保守去重）。"""
    try:
        from datetime import date

        return abs((date.fromisoformat(str(left)[:10]) - date.fromisoformat(str(right)[:10])).days)
    except Exception:  # noqa: BLE001 坏日期不参与跨天判定
        return None


def _duplicates(
    payload: list[dict[str, Any]],
    text: str,
    overlap: float,
    *,
    day: str | None = None,
    same_day_days: int = DEFAULT_DEDUPE_SAME_DAY_DAYS,
) -> bool:
    """12 字窗口重叠率：候选窗口里有多少已出现在任一 payload 条目里。

    跨天的近似文本不算重复：同一件事在不同日期各说一次，是**两个时间点**，
    问「最早/原来的说法」时两条都要在场。只有相隔 ≤same_day_days 天的才算同一次
    表述（同日复述、助手回声）。
    """
    grams = _windows(text)
    if not grams:
        return True
    for item in payload:
        existing = _windows(str(item.get("text") or ""))
        if not existing:
            continue
        hit = sum(1 for gram in grams if gram in existing)
        if hit / len(grams) > overlap:
            other_day = str(item.get("day") or "")
            if day and other_day and same_day_days >= 0:
                gap = _day_gap(day, other_day)
                if gap is not None and gap > same_day_days:
                    continue
            return True
    return False


def pick(
    payload: list[dict[str, Any]],
    query: str,
    *,
    cfg: Any,
    candidates: list[dict[str, Any]] | None = None,
    budget: int | None = None,
) -> list[dict[str, Any]]:
    """从候选里选 ≤count 条：去重、min_score、budget_share 字符上限。

    返回的条目结构与现有 payload 一致（kind=journal / day / source_line_id / role /
    text），这样 exam.render_recall 不改也能按 ``[日期][原话]`` 渲染。
    """
    conf = settings(cfg)
    if not conf["enabled"] or not candidates:
        return []
    char_cap = None
    if budget is not None:
        char_cap = max(0.0, float(budget) * conf["budget_share"])
    out: list[dict[str, Any]] = []
    used_chars = 0.0
    for candidate in candidates:
        if len(out) >= conf["count"]:
            break
        text = str(candidate.get("text") or "").strip()
        if not text:
            continue
        if _duplicates(
            payload + out,
            text,
            conf["dedupe_overlap"],
            day=str(candidate.get("day") or ""),
            same_day_days=int(conf.get("dedupe_same_day_days", DEFAULT_DEDUPE_SAME_DAY_DAYS)),
        ):
            continue
        if char_cap is not None and used_chars + len(text) > char_cap:
            continue
        item = {
            "id": str(candidate.get("line_id") or ""),
            "text": text,
            "score": round(float(candidate.get("score") or 0.0), 4),
            "kind": "journal",
            "day": str(candidate.get("day") or ""),
            "valid_from": str(candidate.get("ts") or ""),
            "role": str(candidate.get("role") or "user"),
            "importance": 0.5,
            "confidence": 0.6,
            "provenance": "user_explicit"
            if str(candidate.get("role") or "user") == "user"
            else "observed",
            "channels": ["rawline"],
            "truncated": False,
            "groups": [],
            "grade": None,
            "source": "journal",
            "source_line_id": str(candidate.get("line_id") or ""),
        }
        out.append(item)
        used_chars += len(text)
    return out
