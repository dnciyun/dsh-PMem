"""反思（s7，docs/03 §13）：七个问题，只读产出。

硬约束（G7）：``reflection`` 不允许被 ``memory`` 写入路径引用。
反思里可以有推测，但推测只能待在这里，要变成记忆必须由用户显式确认。

同一 ``period`` 的反思是**幂等**的：内容没变就复用旧行；
内容变了才追加新行（保留历史，不覆盖）。
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from ..base.clock import now, now_iso, to_iso
from ..base.db import Database
from ..base.ids import ulid
from ..reflect.audit import audit
from .common import canonical, day_bounds_iso

# 七问的键固定，值是给人看的短句
QUESTIONS = (
    "what_happened",
    "what_changed",
    "what_unresolved",
    "what_learned",
    "what_uncertain",
    "what_to_watch",
    "what_to_ask",
)


def _scalar(db: Database, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = db.conn.execute(sql, params).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _metrics(db: Database, *, subject: str, start_iso: str, end_iso: str) -> dict[str, Any]:
    status = {
        row["status"]: row["c"]
        for row in db.conn.execute(
            "SELECT status, COUNT(*) c FROM memory WHERE subject_id=? GROUP BY status",
            (subject,),
        ).fetchall()
    }
    kinds = {
        row["kind"]: row["c"]
        for row in db.conn.execute(
            "SELECT kind, COUNT(*) c FROM memory WHERE subject_id=? AND status<>'deleted' "
            "GROUP BY kind",
            (subject,),
        ).fetchall()
    }
    return {
        "memories": sum(v for k, v in status.items() if k != "deleted"),
        "memories_by_status": status,
        "memories_by_kind": kinds,
        "new_today": _scalar(
            db,
            "SELECT COUNT(*) FROM memory WHERE subject_id=? AND created_at>=? AND created_at<? "
            "AND status<>'deleted'",
            (subject, start_iso, end_iso),
        ),
        "conflicts_open": _scalar(
            db, "SELECT COUNT(*) FROM conflict WHERE subject_id=? AND status='open'", (subject,)
        ),
        "patterns": _scalar(
            db,
            "SELECT COUNT(*) FROM pattern WHERE subject_id=? AND status IN ('candidate','confirmed')",
            (subject,),
        ),
        "traits_established": _scalar(
            db,
            "SELECT COUNT(*) FROM trait WHERE subject_id=? AND status='established'",
            (subject,),
        ),
        "traits_hypothesis": _scalar(
            db,
            "SELECT COUNT(*) FROM trait WHERE subject_id=? AND status='hypothesis'",
            (subject,),
        ),
        "entities": _scalar(
            db, "SELECT COUNT(*) FROM entity WHERE subject_id=? AND status='active'", (subject,)
        ),
        "relations": _scalar(
            db, "SELECT COUNT(*) FROM relation WHERE status IN ('active','dormant')"
        ),
        "recalls_today": _scalar(
            db,
            "SELECT COUNT(*) FROM recall_log WHERE subject_id=? AND ts>=? AND ts<?",
            (subject, start_iso, end_iso),
        ),
        "feedback_today": _scalar(
            db,
            "SELECT COUNT(*) FROM feedback WHERE subject_id=? AND ts>=? AND ts<?",
            (subject, start_iso, end_iso),
        ),
    }


def compose(
    db: Database,
    *,
    subject: str = "self",
    day_key: str,
    due_days: int = 30,
    freshness_days: int = 90,
    recall_days: int = 30,
) -> dict[str, Any]:
    """生成七个问题的答案、发现、假设与建议（不写库）。"""
    start_iso, end_iso = day_bounds_iso(day_key)
    metrics = _metrics(db, subject=subject, start_iso=start_iso, end_iso=end_iso)

    open_conflicts = db.conn.execute(
        "SELECT id, kind, slot, severity FROM conflict WHERE subject_id=? AND status='open' "
        "ORDER BY severity DESC, created_at LIMIT 20",
        (subject,),
    ).fetchall()
    candidates = db.conn.execute(
        "SELECT id, text_raw FROM memory WHERE subject_id=? AND status='candidate' "
        "ORDER BY importance DESC LIMIT 10",
        (subject,),
    ).fetchall()
    patterns = db.conn.execute(
        "SELECT kind, key, support FROM pattern WHERE subject_id=? "
        "AND status IN ('candidate','confirmed') ORDER BY support DESC LIMIT 10",
        (subject,),
    ).fetchall()
    changed_today = _scalar(
        db,
        "SELECT COUNT(*) FROM memory WHERE subject_id=? AND created_at>=? AND created_at<? "
        "AND status IN ('superseded','contested')",
        (subject, start_iso, end_iso),
    )
    learned_today = _scalar(
        db,
        "SELECT COUNT(*) FROM experience WHERE subject_id=? AND created_at>=? AND created_at<?",
        (subject, start_iso, end_iso),
    )
    due = db.conn.execute(
        "SELECT m.id, m.text_raw FROM memory m JOIN target_grade g "
        "ON g.target_tbl='memory' AND g.target_id=m.id "
        "WHERE m.subject_id=? AND m.status='active' AND g.grade IN ('A','B') "
        "AND (m.last_used_at IS NULL OR m.last_used_at < ?) "
        "ORDER BY m.importance DESC LIMIT 5",
        (subject, to_iso(now() - timedelta(days=due_days))),
    ).fetchall()

    quality = audit(
        db,
        subject=subject,
        due_days=due_days,
        freshness_days=freshness_days,
        recall_days=recall_days,
        vector_enabled=True,
    )

    questions = {
        "what_happened": f"这一天新增 {metrics['new_today']} 条记忆，库内共 {metrics['memories']} 条。",
        "what_changed": f"有 {changed_today} 条记忆被替代或进入争议。",
        "what_unresolved": f"{len(open_conflicts)} 条冲突待裁决。",
        "what_learned": f"新增 {learned_today} 张经验卡。",
        "what_uncertain": (
            f"{len(candidates)} 条候选记忆、{metrics['traits_hypothesis']} 条假设特质尚未确认。"
        ),
        "what_to_watch": (
            f"{len(patterns)} 条规律待观察，{len(due)} 条 A/B 级记忆久未使用。"
            + (
                f" 最大缺陷 {quality['defects'][0]['code']}（{quality['defects'][0]['count']} 条）。"
                if quality["defects"]
                else ""
            )
        ),
        "what_to_ask": (
            f"有 {len(open_conflicts)} 条冲突需要你拍板，要现在看吗？"
            if open_conflicts
            else ("最近有没有哪条记忆让我记错了？" if metrics["memories"] else "")
        ),
    }

    findings = [
        {
            "kind": "conflict",
            "id": row["id"],
            "slot": row["slot"],
            "severity": row["severity"],
        }
        for row in open_conflicts
    ] + [{"kind": "defect", **item} for item in quality["defects"]]
    hypotheses = [
        {"kind": row["kind"], "key": row["key"], "support": row["support"]}
        for row in patterns
        if row["support"] < 5
    ]
    suggestions: list[dict[str, Any]] = []
    if open_conflicts:
        suggestions.append(
            {
                "action": "review_conflicts",
                "reason": f"{len(open_conflicts)} 条冲突未裁决",
                "command": "pmem conflicts list",
            }
        )
    if candidates:
        suggestions.append(
            {
                "action": "review_candidates",
                "reason": f"{len(candidates)} 条候选记忆还没被使用过",
                "command": "pmem search --kind semantic",
            }
        )
    if due:
        suggestions.append(
            {
                "action": "refresh_due",
                "reason": "有 A/B 级记忆超过 30 天没被想起",
                "command": "pmem surface --session review",
            }
        )

    # 质量审计的建议更具体（带缺陷码、修复命令），放在前面；同一 action 去重。
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [*quality["suggestions"], *suggestions]:
        key = str(item.get("action") or item.get("target") or "")
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)

    metrics = dict(metrics)
    if quality["quality"] is not None:
        metrics["quality"] = quality["quality"]
    if quality["actionable_rate"] is not None:
        metrics["actionable_rate"] = quality["actionable_rate"]
    for name, comp in quality["components"].items():
        if comp["value"] is not None:
            metrics[f"part.{name}"] = comp["value"]

    return {
        "questions": questions,
        "findings": findings,
        "hypotheses": hypotheses,
        "metrics": metrics,
        "suggestions": merged,
        "quality": quality["quality"],
        "weight_used": quality["weight_used"],
        "components": quality["components"],
        "defects": quality["defects"],
        "actionable_rate": quality["actionable_rate"],
    }


def _same(existing: Any, payload: dict[str, Any]) -> bool:
    try:
        return (
            json.loads(existing["questions_json"]) == payload["questions"]
            and json.loads(existing["findings_json"]) == payload["findings"]
            and json.loads(existing["hypotheses_json"]) == payload["hypotheses"]
            and json.loads(existing["metrics_json"]) == payload["metrics"]
            and json.loads(existing["suggestions_json"]) == payload["suggestions"]
        )
    except (json.JSONDecodeError, TypeError):
        return False


def write(
    db: Database,
    *,
    subject: str = "self",
    day_key: str,
    run_id: str = "",
    due_days: int = 30,
    freshness_days: int = 90,
    recall_days: int = 30,
) -> dict[str, Any]:
    """幂等写入某天的反思：内容相同复用，内容变化追加新行。"""
    payload = compose(
        db,
        subject=subject,
        day_key=day_key,
        due_days=due_days,
        freshness_days=freshness_days,
        recall_days=recall_days,
    )
    period = f"day:{day_key}"
    existing = db.conn.execute(
        "SELECT * FROM reflection WHERE subject_id=? AND period=? ORDER BY created_at DESC LIMIT 1",
        (subject, period),
    ).fetchone()
    if existing is not None and _same(existing, payload):
        return {
            **payload,
            "id": existing["id"],
            "period": period,
            "created": False,
        }

    reflection_id = ulid()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO reflection(id, subject_id, period, questions_json, findings_json, "
            "hypotheses_json, metrics_json, suggestions_json, created_at, run_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                reflection_id,
                subject,
                period,
                canonical(payload["questions"]),
                canonical(payload["findings"]),
                canonical(payload["hypotheses"]),
                canonical(payload["metrics"]),
                canonical(payload["suggestions"]),
                now_iso(),
                run_id,
            ),
        )
        # 指标落到 metrics 表，供长期曲线使用；按 day+name 覆盖，重跑不增行。
        conn.execute(
            "DELETE FROM metrics WHERE day_key=? AND name LIKE 'sleep.%'", (day_key,)
        )
        ts = now_iso()
        for name, value in payload["metrics"].items():
            if isinstance(value, (int, float)):
                conn.execute(
                    "INSERT INTO metrics(ts, day_key, name, value, labels_json) VALUES(?,?,?,?,?)",
                    (ts, day_key, f"sleep.{name}", float(value), "{}"),
                )
    return {
        **payload,
        "id": reflection_id,
        "period": period,
        "created": True,
    }
