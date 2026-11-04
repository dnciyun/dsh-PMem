"""M9 把评测结果写进 ``metrics``，让 monitor 能看「有没有变差」。

评测本身跑在临时库（合成集），这里只把结果抄回当前库；不是把合成记忆写进真实库。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ..base import events
from ..base.clock import now_iso
from ..base.db import Database

METRIC_NAMES = {
    "eval.recall_at_5": ("recall", "lexical_recall_at_k"),
    "eval.ndcg_at_5": ("recall", "ndcg_at_k"),
    "eval.lexical_mrr": ("recall", "lexical_mrr"),
    "eval.dedup_precision": ("dedup", "precision"),
    "eval.dedup_recall": ("dedup", "recall"),
    "eval.conflict_f1": ("conflict", "f1"),
    "eval.poison_escapes": ("poison", "count"),
    "eval.latency_p50_ms": ("recall", "latency_p50_ms"),
}


def record_eval(db: Database, metrics: dict[str, Any], *, subject: str = "self") -> dict[str, Any]:
    """把一次评测的标量指标写入当天 metrics；同名重复记录保留历史行。"""
    day = datetime.now().astimezone().date().isoformat()
    ts = now_iso()
    written: dict[str, float] = {}
    for name, (group, key) in METRIC_NAMES.items():
        node = metrics.get(group) or {}
        value = node.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        written[name] = float(value)
    if not written:
        return {"ok": False, "written": {}, "reason": "没有可记录的标量指标"}
    with db.tx() as conn:
        for name, value in written.items():
            conn.execute(
                "INSERT INTO metrics(ts, day_key, name, value, labels_json) VALUES(?,?,?,?,?)",
                (ts, day, name, value, '{"source":"eval"}'),
            )
        events.append(
            conn,
            actor="system:eval",
            op="ops.eval.record",
            target_tbl="day",
            target_id=day,
            subject_id=subject,
            payload={"metrics": written},
        )
    return {"ok": True, "day": day, "written": written}


TOKEN_METRIC = "pmem.token_per_turn"


def record_token_turn(
    db: Database,
    *,
    tokens: int,
    session_id: str | None = None,
    subject: str = "self",
) -> dict[str, Any]:
    """记一次「每轮注入的 tokens」。

    门是均值 ≤600 / p95 ≤800（`docs/05-evaluation.md`）。这里只存原始样本，
    分位数在 ``memory.receipt.token_stats`` 里算——聚合值不能覆盖原始值，
    否则换了统计口径就没法重算（和 usage 重放同一个道理）。
    """
    value = float(max(0, int(tokens)))
    day = datetime.now().astimezone().date().isoformat()
    labels = {"source": "plugin", "session": session_id or ""}
    db.conn.execute(
        "INSERT INTO metrics(ts, day_key, name, value, labels_json) VALUES(?,?,?,?,?)",
        (now_iso(), day, TOKEN_METRIC, value, json.dumps(labels, ensure_ascii=False)),
    )
    return {"ok": True, "day": day, "tokens": value, "session_id": session_id}


__all__ = ["record_eval", "record_token_turn", "METRIC_NAMES", "TOKEN_METRIC"]
