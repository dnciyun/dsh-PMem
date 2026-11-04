"""进化引擎入口（M5）：把"候选"推进成"可复核、可复用、可回滚"的结构。

四层，可以单独跑，也可以一起跑：

- ``patterns``   应用人工标签：correct → confirmed，wrong → rejected；给出准确率；
- ``conflicts``  超期升级 + 只裁决"一方已被替代"的冲突；
- ``experience`` 经验复用统计（匹配/复用/反馈是显式动作，不在这里批量做）；
- ``tuning``     参数量够才评估，nDCG 提升才生效。

和睡眠一样：``dry_run`` 全流程计算，最后整体回滚。
"""

from __future__ import annotations

import json
from typing import Any

from ..base import events
from ..base.config import Config
from ..base.db import Database
from ..base.ids import ulid
from . import conflicts as conflicts_mod
from . import experience as experience_mod
from . import review as review_mod
from . import tuning as tuning_mod

LAYERS = ("patterns", "conflicts", "experience", "tuning")


class _Rollback(Exception):
    pass


def _run_layers(
    db: Database,
    *,
    subject: str,
    layers: tuple[str, ...],
    config: Config,
    evaluate: Any,
) -> dict[str, Any]:
    stats: dict[str, Any] = {}
    if "patterns" in layers:
        applied = review_mod.apply_labels(db, subject=subject)
        precision = review_mod.precision(db, subject=subject, target_tbl="pattern")
        stats["patterns"] = {
            **applied,
            "precision": precision["precision"],
            "labeled": precision["labeled"],
            "queue": len(review_mod.review_queue(db, subject=subject, target_tbl="pattern")),
        }
    if "conflicts" in layers:
        escalated = conflicts_mod.escalate(
            db,
            subject=subject,
            days=int(config.get("evolve.conflict_escalate_days", 14)),
        )
        auto = conflicts_mod.auto_resolve(
            db,
            subject=subject,
            enabled=bool(config.get("evolve.auto_resolve_conflicts", True)),
        )
        stats["conflicts"] = {
            "escalated": escalated["escalated"],
            "auto_resolved": auto["resolved"],
            "skipped": auto["skipped"],
            "by_rule": auto["by_rule"],
        }
    if "experience" in layers:
        stats["experience"] = experience_mod.stats(db, subject=subject)
    if "tuning" in layers:
        stats["tuning"] = tuning_mod.tune(
            db,
            subject=subject,
            config=config,
            evaluate=evaluate,
            persist=True,
        )
    return stats


def run(
    db: Database,
    *,
    subject: str = "self",
    layers: list[str] | tuple[str, ...] | None = None,
    dry_run: bool = False,
    config: Config | None = None,
    evaluate: Any = None,
) -> dict[str, Any]:
    """跑一轮进化。返回每层统计；tuning 可能 skipped（标注不足）。"""
    cfg = config or Config()
    selected = tuple(layers or LAYERS)
    unknown = [layer for layer in selected if layer not in LAYERS]
    if unknown:
        raise ValueError(f"unknown layers: {unknown}")
    run_id = ulid()
    if dry_run:
        try:
            with db.tx():
                stats = _run_layers(
                    db, subject=subject, layers=selected, config=cfg, evaluate=evaluate
                )
                raise _Rollback()
        except _Rollback:
            pass
        return {
            "run_id": run_id,
            "layers": list(selected),
            "stats": stats,
            "dry_run": True,
            "rolled_back": True,
        }

    stats = _run_layers(
        db, subject=subject, layers=selected, config=cfg, evaluate=evaluate
    )
    with db.tx() as conn:
        events.append(
            conn,
            actor="system:evolve",
            op="evolve.run",
            target_tbl=None,
            target_id=None,
            subject_id=subject,
            payload={"layers": list(selected), "stats": stats},
        )
    return {
        "run_id": run_id,
        "layers": list(selected),
        "stats": stats,
        "dry_run": False,
        "rolled_back": False,
    }


def stats(db: Database, *, subject: str = "self") -> dict[str, Any]:
    """进化层总览：标签、规律、冲突、经验、调参版本。"""
    return {
        "review": review_mod.stats(db, subject=subject),
        "conflicts": conflicts_mod.stats(db, subject=subject),
        "experience": experience_mod.stats(db, subject=subject),
        "tuning": tuning_mod.status(db),
        "labels": db.conn.execute(
            "SELECT COUNT(*) c FROM evolve_label WHERE subject_id=?", (subject,)
        ).fetchone()["c"],
    }


def render(stats: dict[str, Any]) -> str:
    return json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True)
