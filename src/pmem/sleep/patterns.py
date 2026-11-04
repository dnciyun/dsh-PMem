"""规律挖掘（s5，docs/03 §10）。

四类规律，全部由**已有记忆**统计而来，不引入新事实：

- ``frequency``   同一 (holder, predicate, object) 独立出现在 ≥3 天；
- ``preference``  第一人称喜欢/不喜欢同一对象的正负比 ≥3:1（样本 ≥5）；
- ``periodicity`` 写入时间的星期分布显著偏离均匀（卡方，df=6）；
- ``sequence``    同一 episode 内的 kind 二元/三元序列（support ≥3，P ≥0.6）。

规律只是 ``candidate``/``confirmed`` 的假设；成为特质要再过 s6，
而且特质也仍然是 ``hypothesis``，除非跨 ≥3 天有证据。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any

from ..base import events
from ..base.clock import now_iso, parse_iso, to_iso
from ..base.db import Database
from ..base.ids import ulid
from .common import canonical, day_bounds_iso, evidence as evidence_json

# df=6 的卡方临界值（p=0.05 / 0.01 / 0.001）
CHI2_CRITICAL = ((12.59, 0.80), (16.81, 0.90), (22.46, 0.95))


def parse_slot(sig: str | None) -> dict[str, str] | None:
    """``slot:holder|pred|obj|neg`` → dict；其它签名返回 None。"""
    if not sig or not sig.startswith("slot:"):
        return None
    parts = sig[len("slot:") :].split("|")
    if len(parts) != 4:
        return None
    return {"holder": parts[0], "pred": parts[1], "obj": parts[2], "neg": parts[3]}


def _chi_square(counts: list[int]) -> float:
    total = sum(counts)
    if total == 0:
        return 0.0
    expected = total / len(counts)
    return sum((c - expected) ** 2 / expected for c in counts)


def _confidence_from_chi2(chi2: float) -> float:
    confidence = 0.0
    for critical, value in CHI2_CRITICAL:
        if chi2 >= critical:
            confidence = value
    return confidence


def _window(from_day: str, to_day: str) -> str:
    return canonical({"from": from_day, "to": to_day})


def _upsert(
    db: Database,
    *,
    subject: str,
    kind: str,
    key: str,
    support: int,
    confidence: float,
    from_day: str,
    to_day: str,
    evidence: str,
    derivation: str,
    created: list[str],
    updated: list[str],
) -> bool:
    conn = db.conn
    status = "confirmed" if support >= 5 else "candidate"
    existing = conn.execute(
        "SELECT * FROM pattern WHERE subject_id=? AND kind=? AND key=? "
        "AND status IN ('candidate','confirmed')",
        (subject, kind, key),
    ).fetchone()
    window = _window(from_day, to_day)
    ts = now_iso()
    if existing is None:
        pattern_id = ulid()
        conn.execute(
            "INSERT INTO pattern(id, subject_id, kind, key, support, confidence, window_json, "
            "evidence_json, status, first_seen, last_seen, derivation, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                pattern_id,
                subject,
                kind,
                key,
                support,
                confidence,
                window,
                evidence,
                status,
                from_day,
                to_day,
                derivation,
                ts,
            ),
        )
        created.append(pattern_id)
        return True
    new_status = "confirmed" if existing["status"] == "confirmed" or support >= 5 else "candidate"
    if (
        existing["support"] == support
        and abs(existing["confidence"] - confidence) < 1e-9
        and existing["window_json"] == window
        and existing["evidence_json"] == evidence
        and existing["last_seen"] == to_day
        and existing["status"] == new_status
    ):
        return False
    conn.execute(
        "UPDATE pattern SET support=?, confidence=?, window_json=?, evidence_json=?, "
        "status=?, first_seen=MIN(first_seen, ?), last_seen=?, derivation=? WHERE id=?",
        (support, confidence, window, evidence, new_status, from_day, to_day, derivation, existing["id"]),
    )
    updated.append(existing["id"])
    return True


def mark_stale(db: Database, *, subject: str, stale_days: int = 30, today: str | None = None) -> int:
    """超过 ``stale_days`` 没有新支持的规律 → ``stale``（不删除，可被新证据复活）。"""
    base = datetime.fromisoformat(today) if today else datetime.now().astimezone()
    cutoff = (base - timedelta(days=stale_days)).date().isoformat()
    rows = db.conn.execute(
        "SELECT id FROM pattern WHERE subject_id=? AND status IN ('candidate','confirmed') "
        "AND last_seen < ?",
        (subject, cutoff),
    ).fetchall()
    if not rows:
        return 0
    with db.tx() as conn:
        for row in rows:
            conn.execute("UPDATE pattern SET status='stale' WHERE id=?", (row["id"],))
        events.append(
            conn,
            actor="system:sleep",
            op="pattern.stale",
            target_tbl="pattern",
            target_id=None,
            subject_id=subject,
            payload={"count": len(rows), "cutoff": cutoff},
        )
    return len(rows)


def mine(
    db: Database,
    *,
    subject: str = "self",
    day_key: str,
    run_id: str = "",
    window_days: int = 90,
    stale_days: int = 30,
) -> dict[str, Any]:
    """跑一轮规律挖掘。返回新增/更新的规律与按类型统计。"""
    start_iso, end_iso = day_bounds_iso(day_key)
    from_iso = to_iso(parse_iso(end_iso) - timedelta(days=window_days))
    rows = db.conn.execute(
        "SELECT id, kind, claim_sig, created_at, day_key, episode_id, status FROM memory "
        "WHERE subject_id=? AND status<>'deleted' AND created_at>=? AND created_at<? "
        "ORDER BY created_at",
        (subject, from_iso, end_iso),
    ).fetchall()

    slots: dict[tuple[str, str, str], dict[str, Any]] = {}
    likes: dict[str, dict[str, Any]] = {}
    weekdays: Counter[int] = Counter()
    weekday_days: dict[int, set[str]] = defaultdict(set)
    weekday_memories: dict[int, list[str]] = defaultdict(list)

    for row in rows:
        _start_of_day = row["day_key"]
        local = parse_iso(row["created_at"]).astimezone()
        weekdays[local.weekday()] += 1
        weekday_days[local.weekday()].add(_start_of_day)
        weekday_memories[local.weekday()].append(row["id"])

        parsed = parse_slot(row["claim_sig"])
        if parsed is None:
            continue
        group_key = (parsed["holder"], parsed["pred"], parsed["obj"])
        bucket = slots.setdefault(
            group_key, {"days": set(), "ids": [], "neg": parsed["neg"] == "not"}
        )
        bucket["days"].add(_start_of_day)
        bucket["ids"].append(row["id"])
        if parsed["holder"] == "self" and parsed["pred"] == "likes":
            pref = likes.setdefault(parsed["obj"], {"pos": [], "neg": []})
            pref["neg" if parsed["neg"] == "not" else "pos"].append(row["id"])

    created: list[str] = []
    updated: list[str] = []
    by_kind: Counter[str] = Counter()

    # frequency
    for (holder, pred, obj), bucket in slots.items():
        if pred == "likes":
            continue  # 喜欢/不喜欢交给 preference，避免同一件事被算两次
        support = len(bucket["days"])
        if support < 3:
            continue
        days = sorted(bucket["days"])
        confidence = min(0.9, 0.5 + 0.05 * support)
        if _upsert(
            db,
            subject=subject,
            kind="frequency",
            key=f"{holder}|{pred}|{obj}",
            support=support,
            confidence=round(confidence, 4),
            from_day=days[0],
            to_day=days[-1],
            evidence=evidence_json(bucket["ids"]),
            derivation=f"slot:{holder}|{pred}|{obj}",
            created=created,
            updated=updated,
        ):
            by_kind["frequency"] += 1

    # preference（第一人称喜欢 / 不喜欢）
    for obj, bucket in likes.items():
        pos = len(bucket["pos"])
        neg = len(bucket["neg"])
        total = pos + neg
        if total < 5:
            continue
        if pos >= 3 * neg and pos >= 3:
            direction, ids = "positive", bucket["pos"]
        elif neg >= 3 * pos and neg >= 3:
            direction, ids = "negative", bucket["neg"]
        else:
            continue
        days = sorted(
            {
                row["day_key"]
                for row in db.conn.execute(
                    "SELECT day_key FROM memory WHERE id IN (%s)" % ",".join("?" for _ in ids),
                    ids,
                ).fetchall()
            }
        )
        if len(days) < 2:
            continue
        if _upsert(
            db,
            subject=subject,
            kind="preference",
            key=f"self|likes|{obj}|{direction}",
            support=total,
            confidence=round(min(0.95, 0.5 + 0.05 * total), 4),
            from_day=days[0],
            to_day=days[-1],
            evidence=evidence_json(ids),
            derivation=f"likes:{obj}:{direction}",
            created=created,
            updated=updated,
        ):
            by_kind["preference"] += 1

    # periodicity（星期分布）
    if sum(weekdays.values()) >= 8:
        counts = [weekdays.get(i, 0) for i in range(7)]
        chi2 = _chi_square(counts)
        confidence = _confidence_from_chi2(chi2)
        if confidence > 0:
            for weekday, count in weekdays.items():
                days = sorted(weekday_days[weekday])
                if count < 4 or len(days) < 3:
                    continue
                if _upsert(
                    db,
                    subject=subject,
                    kind="periodicity",
                    key=f"weekday:{weekday}",
                    support=len(days),
                    confidence=confidence,
                    from_day=days[0],
                    to_day=days[-1],
                    evidence=evidence_json(weekday_memories[weekday]),
                    derivation=f"weekday:{weekday}:chi2={chi2:.2f}",
                    created=created,
                    updated=updated,
                ):
                    by_kind["periodicity"] += 1

    # sequence（同 episode 内的 kind n-gram）
    episodes = db.conn.execute(
        "SELECT id FROM episode WHERE subject_id=? AND started_at>=? AND started_at<? "
        "AND memory_count>1 ORDER BY started_at",
        (subject, from_iso, end_iso),
    ).fetchall()
    grams_2: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    grams_3: defaultdict[tuple[str, str, str], list[str]] = defaultdict(list)
    first_counts: Counter[str] = Counter()
    for episode in episodes:
        members = db.conn.execute(
            "SELECT id, kind FROM memory WHERE episode_id=? AND status<>'deleted' "
            "ORDER BY created_at, seq",
            (episode["id"],),
        ).fetchall()
        kinds = [row["kind"] for row in members]
        ids = [row["id"] for row in members]
        if len(kinds) < 2:
            continue
        first_counts[kinds[0]] += 1
        for i in range(len(kinds) - 1):
            key = (kinds[i], kinds[i + 1])
            grams_2[key].append(ids[i])
            if i + 2 < len(kinds):
                grams_3[(kinds[i], kinds[i + 1], kinds[i + 2])].append(ids[i])

    def _write_sequence(key: tuple[str, ...], memory_ids: list[str]) -> None:
        support = len(memory_ids)
        if support < 3:
            return
        confidence = support / max(1, first_counts.get(key[0], support))
        if confidence < 0.6:
            return
        days = sorted(
            {
                row["day_key"]
                for row in db.conn.execute(
                    "SELECT day_key FROM memory WHERE id IN (%s)"
                    % ",".join("?" for _ in memory_ids),
                    memory_ids,
                ).fetchall()
            }
        )
        if _upsert(
            db,
            subject=subject,
            kind="sequence",
            key=">".join(key),
            support=support,
            confidence=round(min(0.95, confidence), 4),
            from_day=days[0],
            to_day=days[-1],
            evidence=evidence_json(memory_ids),
            derivation="episode-kind-ngram",
            created=created,
            updated=updated,
        ):
            by_kind["sequence"] += 1

    for key2, ids2 in grams_2.items():
        _write_sequence(key2, ids2)
    for key3, ids3 in grams_3.items():
        _write_sequence(key3, ids3)

    stale = mark_stale(db, subject=subject, stale_days=stale_days)
    return {
        "scanned": len(rows),
        "patterns": len(created) + len(updated),
        "created": len(created),
        "updated": len(updated),
        "stale": stale,
        "by_kind": dict(by_kind),
        "ids": created + updated,
    }
