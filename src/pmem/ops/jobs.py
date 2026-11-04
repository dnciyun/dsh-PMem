"""常驻任务队列（M9）：复用 ``job`` 表，单写者、可恢复、只追加事件。

调度器不保存"上次运行时间"这种影子状态——``job.next_run_at`` 就是唯一状态。
一个任务成功后，在同一个事务里把 ``next_run_at`` 推到下一个时点；进程崩在
中间也不会留下两个同类型的任务。

约定：

- 任务 id 固定为 ``sched:<kind>``，一个 kind 只有一条排队记录；
- ``state`` 只用 pending / running / dead；``done`` 不用于常驻任务；
- 失败最多 ``max_attempts`` 次，退避后重试；超过转 dead，由 monitor 报出来；
- 运行中的任务超过 ``stale_minutes`` 视为进程崩溃，回到 pending。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from ..base import events
from ..base.clock import now_iso, to_iso
from ..base.db import Database

JOB_TYPES = ("sleep", "reflect_week", "backup", "monitor", "monthly")


def _payload(value: dict[str, Any] | None) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse(row: Any) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        payload = {}
    return {
        "id": row["id"],
        "type": row["type"],
        "payload": payload if isinstance(payload, dict) else {},
        "next_run_at": row["next_run_at"],
        "state": row["state"],
        "attempts": row["attempts"],
        "last_error": row["last_error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def ensure(
    db: Database,
    *,
    kind: str,
    next_run_at: datetime,
    payload: dict[str, Any] | None = None,
) -> bool:
    """登记一个常驻任务；已存在则不动。返回是否新建。"""
    job_id = f"sched:{kind}"
    ts = now_iso()
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO job(id, type, payload_json, next_run_at, state, attempts, "
            "last_error, created_at, updated_at) VALUES(?,?,?,?,'pending',0,NULL,?,?)",
            (job_id, kind, _payload(payload), to_iso(next_run_at), ts, ts),
        )
        created = cur.rowcount > 0
        if created:
            events.append(
                conn,
                actor="system:scheduler",
                op="ops.job.schedule",
                target_tbl="job",
                target_id=job_id,
                payload={"kind": kind, "next_run_at": to_iso(next_run_at)},
            )
    return created


def get(db: Database, kind: str) -> dict[str, Any] | None:
    row = db.conn.execute("SELECT * FROM job WHERE id=?", (f"sched:{kind}",)).fetchone()
    return _parse(row) if row else None


def list_jobs(db: Database) -> list[dict[str, Any]]:
    rows = db.conn.execute(
        "SELECT * FROM job ORDER BY CASE state WHEN 'dead' THEN 0 WHEN 'running' THEN 1 ELSE 2 END, "
        "next_run_at, id"
    ).fetchall()
    return [_parse(row) for row in rows]


def claim_next(db: Database, now: datetime) -> dict[str, Any] | None:
    """原子地取一条到期的 pending 任务并置 running。"""
    ts = to_iso(now)
    with db.tx() as conn:
        row = conn.execute(
            "SELECT * FROM job WHERE state='pending' AND next_run_at<=? "
            "ORDER BY next_run_at, id LIMIT 1",
            (ts,),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE job SET state='running', updated_at=? WHERE id=? AND state='pending'",
            (ts, row["id"]),
        )
        events.append(
            conn,
            actor="system:scheduler",
            op="ops.job.start",
            target_tbl="job",
            target_id=row["id"],
            payload={"kind": row["type"], "attempt": row["attempts"] + 1},
        )
        data = _parse(row)
        data["state"] = "running"
        return data


def complete(
    db: Database,
    job: dict[str, Any],
    *,
    next_run_at: datetime,
    result: dict[str, Any] | None = None,
) -> None:
    ts = now_iso()
    payload = dict(job.get("payload") or {})
    payload["last_run_at"] = ts
    payload["last_result"] = result or {}
    with db.tx() as conn:
        conn.execute(
            "UPDATE job SET state='pending', next_run_at=?, attempts=0, last_error=NULL, "
            "payload_json=?, updated_at=? WHERE id=?",
            (to_iso(next_run_at), _payload(payload), ts, job["id"]),
        )
        events.append(
            conn,
            actor="system:scheduler",
            op="ops.job.done",
            target_tbl="job",
            target_id=job["id"],
            payload={
                "kind": job["type"],
                "next_run_at": to_iso(next_run_at),
                "result": result or {},
            },
        )


def fail(
    db: Database,
    job: dict[str, Any],
    *,
    error: str,
    now: datetime,
    max_attempts: int = 3,
    retry_minutes: int = 10,
) -> dict[str, Any]:
    attempts = int(job.get("attempts") or 0) + 1
    dead = attempts >= max(1, max_attempts)
    ts = to_iso(now)
    if dead:
        state = "dead"
        next_run = job["next_run_at"]
    else:
        state = "pending"
        next_run = to_iso(now + timedelta(minutes=max(1, retry_minutes)))
    with db.tx() as conn:
        conn.execute(
            "UPDATE job SET state=?, attempts=?, last_error=?, next_run_at=?, updated_at=? "
            "WHERE id=?",
            (state, attempts, error[:1000], next_run, ts, job["id"]),
        )
        events.append(
            conn,
            actor="system:scheduler",
            op="ops.job.fail",
            target_tbl="job",
            target_id=job["id"],
            payload={"kind": job["type"], "attempts": attempts, "dead": dead, "error": error[:500]},
        )
    return {"id": job["id"], "state": state, "attempts": attempts, "last_error": error}


def recover_stale(
    db: Database, *, now: datetime, stale_minutes: int = 120
) -> list[str]:
    """把长时间 running 的任务放回 pending（进程崩溃/重启后的自愈）。"""
    cutoff = to_iso(now - timedelta(minutes=max(1, stale_minutes)))
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT id FROM job WHERE state='running' AND updated_at<?", (cutoff,)
        ).fetchall()
        ids = [row["id"] for row in rows]
        for job_id in ids:
            conn.execute(
                "UPDATE job SET state='pending', last_error='stale running job', updated_at=? "
                "WHERE id=?",
                (to_iso(now), job_id),
            )
            events.append(
                conn,
                actor="system:scheduler",
                op="ops.job.recover",
                target_tbl="job",
                target_id=job_id,
                payload={"reason": "stale"},
            )
    return ids


def requeue_dead(db: Database, *, now: datetime, kind: str | None = None) -> list[str]:
    """人工把 dead 任务放回队列；不重置 attempts 之外的调度配置。"""
    sql = "SELECT id FROM job WHERE state='dead'"
    params: list[Any] = []
    if kind:
        sql += " AND type=?"
        params.append(kind)
    rows = db.conn.execute(sql, params).fetchall()
    ids = [row["id"] for row in rows]
    if not ids:
        return []
    with db.tx() as conn:
        for job_id in ids:
            conn.execute(
                "UPDATE job SET state='pending', attempts=0, last_error=NULL, next_run_at=?, "
                "updated_at=? WHERE id=?",
                (to_iso(now), to_iso(now), job_id),
            )
            events.append(
                conn,
                actor="system:scheduler",
                op="ops.job.requeue",
                target_tbl="job",
                target_id=job_id,
                payload={},
            )
    return ids


__all__ = [
    "JOB_TYPES",
    "ensure",
    "get",
    "list_jobs",
    "claim_next",
    "complete",
    "fail",
    "recover_stale",
    "requeue_dead",
]
