"""特质推导（M6，docs/03 §12）。

与 M4 的简化版相比，M6 补上四件事：

1. **强度公式**：`0.40·norm_days + 0.30·consistency + 0.20·recency + 0.10·(1-contradiction_rate)`；
2. **established 门槛**：跨 ≥3 天、strength ≥0.65、confidence ≥0.70、无未决冲突；
3. **证据全量重算**：每次从当前活跃规律重新取证据，不沿用旧 trait 的 evidence；
4. **上限与降级**：每 (subject, scope) 最多 30 条活跃特质，超限按 strength 保留；
   established 证据不足/有反证立即降为 hypothesis，180 天无支持 → deprecated。

数值微调（strength/confidence 小幅变化）原地更新，只有状态/证据/值变化才新建版本。
这是对 §12.2「每次更新都新版本」的有意收窄：否则 recency 每天变化会把时间线刷爆，
月度 churn 永远超标；状态与证据的变化仍然完整留链。
"""

from __future__ import annotations

import json
import math
from typing import Any

from ..base import events
from ..base.clock import days_between, now_iso
from ..base.config import Config
from ..base.db import Database
from ..base.ids import ulid

_MAP: dict[str, tuple[str, str]] = {
    "frequency": ("habit", "经常发生"),
    "preference": ("interest", "稳定偏好"),
    "periodicity": ("habit", "周期性习惯"),
    "sequence": ("experience", "常见顺序"),
}


def _canonical(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _label(kind: str, key: str, support: int, evidence_days: int) -> dict[str, Any]:
    return {
        "label": f"{_MAP[kind][1]}：{key}",
        "pattern_kind": kind,
        "pattern_key": key,
        "support": support,
        "evidence_days": evidence_days,
    }


def _ids(raw: str | None) -> list[str]:
    try:
        data = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [str(item["memory_id"]) for item in data if isinstance(item, dict) and item.get("memory_id")]


def _evidence(
    db: Database, ids: list[str]
) -> tuple[list[str], int, int, str | None]:
    """返回 (有效 id, 独立天数, 反证数, 最后支持时间)。"""
    ids = sorted(set(ids))
    if not ids:
        return [], 0, 0, None
    marks = ",".join("?" for _ in ids)
    rows = db.conn.execute(
        f"SELECT id, day_key, status, created_at FROM memory WHERE id IN ({marks}) "
        "AND status<>'deleted'",
        ids,
    ).fetchall()
    valid = [row["id"] for row in rows]
    days = len({row["day_key"] for row in rows})
    contradicted = {row["id"] for row in rows if row["status"] == "contested"}
    for row in db.conn.execute(
        f"SELECT DISTINCT target_id FROM feedback WHERE target_tbl='memory' "
        f"AND target_id IN ({marks}) AND signal IN ('wrong','deny')",
        ids,
    ).fetchall():
        contradicted.add(row["target_id"])
    last = max((row["created_at"] for row in rows), default=None)
    return valid, days, len(contradicted & set(valid)), last


def strength(
    *,
    evidence_days: int,
    supports: int,
    contradictions: int,
    last_support_at: str | None,
    recency_half_life_days: int = 120,
) -> float:
    """docs/03 §12.1 的强度公式（无向量，全部来自计数与时间）。"""
    if evidence_days <= 0:
        return 0.0
    norm_days = min(1.0, evidence_days / 10.0)
    total = supports + contradictions
    consistency = supports / total if total else 0.0
    contradiction_rate = contradictions / total if total else 0.0
    age = days_between(last_support_at) if last_support_at else 999.0
    recency = math.exp(-math.log(2) * age / max(1, recency_half_life_days))
    value = (
        0.40 * norm_days
        + 0.30 * consistency
        + 0.20 * recency
        + 0.10 * (1.0 - contradiction_rate)
    )
    return round(max(0.0, min(1.0, value)), 4)


def _current(db: Database, *, subject: str, trait_type: str, key: str) -> Any | None:
    return db.conn.execute(
        "SELECT * FROM trait WHERE subject_id=? AND scope='self' AND type=? AND key=? "
        "AND status IN ('hypothesis','established')",
        (subject, trait_type, key),
    ).fetchone()


def _latest(db: Database, *, subject: str, trait_type: str, key: str) -> Any | None:
    return db.conn.execute(
        "SELECT * FROM trait WHERE subject_id=? AND scope='self' AND type=? AND key=? "
        "ORDER BY version DESC LIMIT 1",
        (subject, trait_type, key),
    ).fetchone()


def _retire_orphans(db: Database, *, subject: str, retired: list[str]) -> None:
    rows = db.conn.execute(
        "SELECT * FROM trait WHERE subject_id=? AND status IN ('hypothesis','established') "
        "AND derivation LIKE 'pattern:%'",
        (subject,),
    ).fetchall()
    for row in rows:
        pattern_id = row["derivation"].split(":", 1)[1]
        pattern = db.conn.execute(
            "SELECT status FROM pattern WHERE id=?", (pattern_id,)
        ).fetchone()
        if pattern is not None and pattern["status"] in ("candidate", "confirmed"):
            continue
        db.conn.execute(
            "UPDATE trait SET status='deprecated', retired_reason=?, updated_at=? WHERE id=?",
            ("pattern_gone", now_iso(), row["id"]),
        )
        retired.append(row["id"])


def _cap(db: Database, *, subject: str, max_active: int, retired: list[str]) -> int:
    capped = 0
    for scope in ("self", "person", "topic"):
        rows = db.conn.execute(
            "SELECT id FROM trait WHERE subject_id=? AND scope=? "
            "AND status IN ('hypothesis','established') ORDER BY strength DESC, version DESC",
            (subject, scope),
        ).fetchall()
        for row in rows[max_active:]:
            db.conn.execute(
                "UPDATE trait SET status='deprecated', retired_reason='capacity', updated_at=? "
                "WHERE id=?",
                (now_iso(), row["id"]),
            )
            retired.append(row["id"])
            capped += 1
    return capped


def _collect_candidates(db: Database, *, subject: str) -> dict[str, dict[str, Any]]:
    patterns = db.conn.execute(
        "SELECT * FROM pattern WHERE subject_id=? AND status IN ('candidate','confirmed') "
        "ORDER BY kind, key",
        (subject,),
    ).fetchall()
    grouped: dict[str, dict[str, Any]] = {}
    for pattern in patterns:
        kind = pattern["kind"]
        if kind not in _MAP:
            continue
        key = f"pattern:{kind}:{pattern['key']}"
        bucket = grouped.setdefault(
            key,
            {
                "kind": kind,
                "pattern_key": pattern["key"],
                "ids": set(),
                "support": 0,
                "pattern_id": pattern["id"],
            },
        )
        bucket["ids"].update(_ids(pattern["evidence_json"]))
        bucket["support"] = max(bucket["support"], int(pattern["support"]))
        bucket["pattern_id"] = pattern["id"]
    return grouped


def derive(
    db: Database,
    *,
    subject: str = "self",
    day_key: str,
    run_id: str = "",
    config: Config | None = None,
) -> dict[str, Any]:
    """把规律提升/降级为特质；证据每次全量重算。"""
    cfg = config or Config()
    max_active = int(cfg.get("trait.max_active", 30))
    promote_days = int(cfg.get("trait.promote_days", 3))
    promote_strength = float(cfg.get("trait.promote_strength", 0.65))
    min_confidence = float(cfg.get("trait.min_confidence", 0.70))
    demote_strength = float(cfg.get("trait.demote_strength", 0.45))
    stale_days = int(cfg.get("trait.stale_days", 180))
    half_life = int(cfg.get("trait.recency_half_life_days", 120))

    candidates = _collect_candidates(db, subject=subject)
    created: list[str] = []
    versioned: list[str] = []
    demoted: list[str] = []
    unchanged = 0
    updated_in_place = 0
    retired: list[str] = []
    ts = now_iso()

    with db.tx() as conn:
        for key, bucket in sorted(candidates.items()):
            kind = bucket["kind"]
            trait_type = _MAP[kind][0]
            valid, evidence_days, contradictions, last_support = _evidence(
                db, list(bucket["ids"])
            )
            if not valid:
                continue
            current = _current(db, subject=subject, trait_type=trait_type, key=key)
            latest = _latest(db, subject=subject, trait_type=trait_type, key=key)

            # 硬门槛：任何活跃特质都要能追溯到 ≥3 条记忆；证据塌缩就退役
            if len(valid) < 3:
                if current is not None:
                    conn.execute(
                        "UPDATE trait SET status='deprecated', "
                        "retired_reason='insufficient_evidence', updated_at=? WHERE id=?",
                        (ts, current["id"]),
                    )
                    retired.append(current["id"])
                continue

            # 180 天无新支持：退役，不再产生新特质
            if last_support and days_between(last_support) >= stale_days:
                if current is not None:
                    conn.execute(
                        "UPDATE trait SET status='deprecated', retired_reason='stale', "
                        "updated_at=? WHERE id=?",
                        (ts, current["id"]),
                    )
                    retired.append(current["id"])
                continue

            supports = len(valid)
            contradiction_rate = contradictions / max(1, supports + contradictions)
            consistency = 1.0 - contradiction_rate
            value = strength(
                evidence_days=evidence_days,
                supports=supports,
                contradictions=contradictions,
                last_support_at=last_support,
                recency_half_life_days=half_life,
            )
            # confidence 用证据天数与一致性；不拿 support 天数冒充
            confidence = round(
                min(0.95, 0.55 + 0.06 * evidence_days + 0.10 * consistency), 4
            )
            established = (
                evidence_days >= promote_days
                and value >= promote_strength
                and confidence >= min_confidence
                and contradictions == 0
            )
            desired = "established" if established else "hypothesis"
            value_json = _canonical(
                _label(kind, bucket["pattern_key"], bucket["support"], evidence_days)
            )
            evidence_json = _canonical([{"memory_id": mid} for mid in valid])
            first_seen = min(
                (
                    row["day_key"]
                    for row in db.conn.execute(
                        "SELECT day_key FROM memory WHERE id IN (%s)"
                        % ",".join("?" for _ in valid),
                        valid,
                    ).fetchall()
                ),
                default=day_key,
            )

            if current is not None:
                same_shape = (
                    current["status"] == desired
                    and current["value_json"] == value_json
                    and current["evidence_json"] == evidence_json
                    and current["evidence_days"] == evidence_days
                )
                if same_shape:
                    if (
                        abs(current["strength"] - value) < 0.02
                        and abs(current["confidence"] - confidence) < 0.02
                    ):
                        unchanged += 1
                        continue
                    conn.execute(
                        "UPDATE trait SET strength=?, confidence=?, last_support_at=?, "
                        "updated_at=? WHERE id=?",
                        (value, confidence, last_support or current["last_support_at"], ts, current["id"]),
                    )
                    updated_in_place += 1
                    continue

            version = int(latest["version"]) + 1 if latest is not None else 1
            prev_id = latest["id"] if latest is not None else None
            if current is not None:
                conn.execute(
                    "UPDATE trait SET status='deprecated', retired_reason='superseded', "
                    "updated_at=? WHERE id=?",
                    (ts, current["id"]),
                )
            trait_id = ulid()
            conn.execute(
                "INSERT INTO trait(id, subject_id, scope, type, key, value_json, strength, "
                "confidence, status, evidence_json, evidence_days, first_seen, last_support_at, "
                "version, prev_id, derivation, created_at, updated_at) "
                "VALUES(?,?,'self',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    trait_id,
                    subject,
                    trait_type,
                    key,
                    value_json,
                    value,
                    confidence,
                    desired,
                    evidence_json,
                    evidence_days,
                    latest["first_seen"] if latest is not None else first_seen,
                    last_support or ts,
                    version,
                    prev_id,
                    f"pattern:{bucket['pattern_id']}",
                    ts,
                    ts,
                ),
            )
            created.append(trait_id)
            if current is not None:
                versioned.append(trait_id)
                if current["status"] == "established" and desired == "hypothesis":
                    demoted.append(trait_id)
                op = "trait.demote" if desired == "hypothesis" else "trait.version"
            else:
                op = "trait.promote"
            events.append(
                conn,
                actor="system:trait",
                op=op,
                target_tbl="trait",
                target_id=trait_id,
                subject_id=subject,
                payload={
                    "type": trait_type,
                    "key": key,
                    "version": version,
                    "status": desired,
                    "strength": value,
                    "confidence": confidence,
                    "evidence_days": evidence_days,
                    "contradictions": contradictions,
                    "run_id": run_id,
                },
            )

        _retire_orphans(db, subject=subject, retired=retired)
        capped = _cap(db, subject=subject, max_active=max_active, retired=retired)

    active = db.conn.execute(
        "SELECT COUNT(*) c FROM trait WHERE subject_id=? AND status IN ('hypothesis','established')",
        (subject,),
    ).fetchone()["c"]
    established_count = db.conn.execute(
        "SELECT COUNT(*) c FROM trait WHERE subject_id=? AND status='established'", (subject,)
    ).fetchone()["c"]
    return {
        "candidates": len(candidates),
        "created": len(created),
        "versioned": len(versioned),
        "demoted": len(demoted),
        "unchanged": unchanged,
        "updated_in_place": updated_in_place,
        "retired": len(retired),
        "capacity_retired": capped,
        "active": int(active),
        "established": int(established_count),
        "ids": created,
    }
