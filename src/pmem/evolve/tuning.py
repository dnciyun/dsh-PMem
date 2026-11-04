"""参数自调（M5，docs/03 §14）。

两条纪律，缺一不可：

1. **没有足够标注就不调**：最近 ``days`` 天内的反馈 < ``min_labels`` → 直接跳过；
2. **只接受评估提升**：候选配置的 nDCG@5 相对提升 ≥ ``accept_gain``，且
   recall / dedup / conflict 没有一项下降超过 ``max_drop``，否则写 ``rejected``，
   不动线上配置。

这里只调 4 个有界参数中的 2 个（w_rrf / mmr_lambda）作为第一版；参数结构不动。
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from ..base.db import Database
from ..base.clock import now, now_iso, to_iso
from ..base.config import Config
from ..base.ids import ulid

Evaluator = Callable[[dict[str, Any]], dict[str, Any]]

# 只调这两个；范围按 docs/03 §14.1
CANDIDATES: list[dict[str, Any]] = [
    {"retrieval": {"w_rrf": w, "mmr_lambda": m}}
    for w in (0.45, 0.55, 0.65)
    for m in (0.6, 0.7, 0.8)
]

METRIC_KEYS = ("recall", "dedup", "conflict")


def label_count(db: Database, *, subject: str = "self", days: int = 30) -> int:
    cutoff = to_iso(now() - timedelta(days=days))
    return int(
        db.conn.execute(
            "SELECT COUNT(*) c FROM feedback WHERE subject_id=? AND ts>=? "
            "AND signal IN ('useful','irrelevant','wrong','confirm','deny')",
            (subject, cutoff),
        ).fetchone()["c"]
    )


def _default_evaluate(overrides: dict[str, Any], *, seed: int = 7) -> dict[str, Any]:
    """在合成集上重跑一遍检索；返回调参关心的四个指标。"""
    from ..eval import runner

    out_dir = Path(".tmp/evolve/tuning")
    out_dir.mkdir(parents=True, exist_ok=True)
    metrics = runner.run(
        seed=seed,
        out_dir=out_dir,
        k=5,
        max_groups=4,
        config_overrides=overrides or None,
    )
    return {
        "ndcg": metrics["recall"]["ndcg_at_k"],
        "recall": metrics["recall"]["lexical_recall_at_k"],
        "dedup": metrics["dedup"]["precision"],
        "conflict": metrics["conflict"]["f1"],
    }


def _accept(baseline: dict[str, Any], best: dict[str, Any], *, gain: float, max_drop: float) -> tuple[bool, str]:
    base_ndcg = float(baseline.get("ndcg") or 0.0)
    best_ndcg = float(best.get("ndcg") or 0.0)
    if base_ndcg <= 0:
        return False, "baseline_ndcg_zero"
    if best_ndcg < base_ndcg * (1.0 + gain):
        return False, f"ndcg_gain_below_{gain}"
    for key in METRIC_KEYS:
        if float(best.get(key) or 0.0) < float(baseline.get(key) or 0.0) - max_drop:
            return False, f"metric_drop:{key}"
    return True, "improved"


def tune(
    db: Database,
    *,
    subject: str = "self",
    config: Config | None = None,
    evaluate: Evaluator | None = None,
    persist: bool = True,
    seed: int = 7,
) -> dict[str, Any]:
    """跑一次调参：先验标注量，再评估候选，只在提升时生效。"""
    cfg = config or Config()
    min_labels = int(cfg.get("evolve.tuning.min_labels", 50))
    gain = float(cfg.get("evolve.tuning.accept_gain", 0.02))
    max_drop = float(cfg.get("evolve.tuning.max_drop", 0.01))
    days = int(cfg.get("evolve.tuning.days", 30))
    labels = label_count(db, subject=subject, days=days)
    if labels < min_labels:
        return {
            "status": "skipped",
            "reason": "not_enough_labels",
            "labels": labels,
            "min_labels": min_labels,
        }

    ev = evaluate or _default_evaluate
    baseline = ev({})
    results: list[dict[str, Any]] = []
    for candidate in CANDIDATES:
        metrics = ev(candidate)
        results.append({"params": candidate, "metrics": metrics})
    best = max(results, key=lambda item: float(item["metrics"].get("ndcg") or 0.0))
    accepted, reason = _accept(baseline, best["metrics"], gain=gain, max_drop=max_drop)
    outcome: dict[str, Any] = {
        "status": "accepted" if accepted else "rejected",
        "reason": reason,
        "labels": labels,
        "baseline": baseline,
        "best": best,
        "candidates": len(results),
        "applied": False,
    }
    if not persist:
        outcome["dry_run"] = True
        return outcome

    with db.tx() as conn:
        if accepted:
            conn.execute(
                "UPDATE config_version SET status='superseded' WHERE status='active'"
            )
            version_id = ulid()
            conn.execute(
                "INSERT INTO config_version(id, created_at, params_json, baseline_json, "
                "metrics_json, status, reason) VALUES(?,?,?,?,?, 'active', ?)",
                (
                    version_id,
                    now_iso(),
                    json.dumps(best["params"], ensure_ascii=False, sort_keys=True),
                    json.dumps(baseline, ensure_ascii=False, sort_keys=True),
                    json.dumps(
                        {"best": best["metrics"], "labels": labels, "n": len(results)},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    reason,
                ),
            )
            outcome["applied"] = True
            outcome["version_id"] = version_id
        else:
            conn.execute(
                "INSERT INTO config_version(id, created_at, params_json, baseline_json, "
                "metrics_json, status, reason) VALUES(?,?,?,?,?, 'rejected', ?)",
                (
                    ulid(),
                    now_iso(),
                    json.dumps(best["params"], ensure_ascii=False, sort_keys=True),
                    json.dumps(baseline, ensure_ascii=False, sort_keys=True),
                    json.dumps(
                        {"best": best["metrics"], "labels": labels, "n": len(results)},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    reason,
                ),
            )
    return outcome


def active(db: Database) -> dict[str, Any] | None:
    row = db.conn.execute(
        "SELECT * FROM config_version WHERE status='active' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "params": json.loads(row["params_json"] or "{}"),
        "reason": row["reason"],
    }


def status(db: Database) -> dict[str, Any]:
    counts = {
        row["status"]: row["c"]
        for row in db.conn.execute(
            "SELECT status, COUNT(*) c FROM config_version GROUP BY status"
        ).fetchall()
    }
    return {"active": active(db), "versions": counts}


def rollback(db: Database) -> dict[str, Any]:
    """撤销已生效的调参版本，回到默认配置；历史保留。"""
    current = active(db)
    if current is None:
        return {"rolled_back": False, "reason": "no_active_version"}
    with db.tx() as conn:
        conn.execute(
            "UPDATE config_version SET status='superseded', reason='rollback' WHERE id=?",
            (current["id"],),
        )
    return {"rolled_back": True, "version_id": current["id"]}
