"""冲突检测与裁决（M2）。

只处理**可判定**的冲突（`docs/03` §7）：

- ``slot``：同一 (holder, predicate)、值不同、时间窗**重叠**；
- ``negation``：同一 (holder, predicate, value)、正负相反；
- ``temporal``：值不同但时间窗**不重叠** → 不是冲突，是先后替代（跳过）。

M2 的策略是**只标记不自动裁决**：命中即两条都置 ``contested`` + 建 conflict 行，
等用户用 ``pmem conflicts resolve`` 拍板。自动裁决（证据多者胜/新者胜）留到 M5，
因为"自动判对"本身需要标注数据验证。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ..base import events
from ..base.clock import now_iso, parse_iso
from ..base.db import Database
from ..base.ids import ulid

SEVERITY = {"slot": 0.5, "negation": 0.7, "evidence": 0.6, "temporal": 0.2, "identity": 0.8}

# 只有"功能性谓词"（一个人只能有一个值）才有槽位冲突。
# 喜欢/使用/认识是多值的：既喜欢咖啡又喜欢茶完全正常，不该算冲突。
FUNCTIONAL_PREDS = frozenset({"lives_in", "works_at", "born_in", "has", "age"})


def parse_sig(sig: str | None) -> dict[str, str] | None:
    """解析 ``slot:holder|pred|obj|neg``；不是槽位签名就返回 None。"""
    if not sig or not sig.startswith("slot:"):
        return None
    body = sig[len("slot:") :]
    parts = body.split("|")
    if len(parts) != 4:
        return None
    holder, pred, obj, neg = parts
    return {"holder": holder, "pred": pred, "obj": obj, "neg": neg}


def _overlap(left: Any, right: Any) -> bool:
    """两段时间是否重叠；缺失视为 ±∞。"""
    lf = left["valid_from"]
    lt = left["valid_to"]
    rf = right["valid_from"]
    rt = right["valid_to"]
    if lf and rt and lf >= rt:
        return False
    if rf and lt and rf >= lt:
        return False
    return True


def _pair_order(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def detect(
    db: Database,
    *,
    subject: str = "self",
    kinds: tuple[str, ...] = ("slot", "negation"),
    max_items: int = 5000,
) -> list[dict[str, Any]]:
    """扫描活跃记忆，返回新发现的冲突（不写库）。"""
    rows = db.conn.execute(
        "SELECT id, claim_sig, kind, text_raw, valid_from, valid_to, created_at, evidence_count "
        "FROM memory WHERE subject_id=? AND status IN ('active','candidate','contested') "
        "ORDER BY created_at LIMIT ?",
        (subject, max_items),
    ).fetchall()

    buckets: dict[tuple[str, str], list[Any]] = {}
    for row in rows:
        parsed = parse_sig(row["claim_sig"])
        if parsed is None:
            continue
        buckets.setdefault((parsed["holder"], parsed["pred"]), []).append((row, parsed))

    found: list[dict[str, Any]] = []
    for (_holder, _pred), items in buckets.items():
        if len(items) < 2:
            continue
        for i in range(len(items)):
            left, lp = items[i]
            for j in range(i + 1, len(items)):
                right, rp = items[j]
                if left["id"] == right["id"]:
                    continue
                if lp["obj"] != rp["obj"]:
                    if "slot" not in kinds or lp["pred"] not in FUNCTIONAL_PREDS:
                        continue  # 多值谓词：同时喜欢咖啡和茶不是冲突
                    if not _overlap(left, right):
                        continue  # 时间不重叠 = 先后替代，不是冲突
                    kind = "slot"
                    detail = {"left_value": lp["obj"], "right_value": rp["obj"], "predicate": lp["pred"]}
                elif lp["neg"] != rp["neg"]:
                    if "negation" not in kinds:
                        continue
                    kind = "negation"
                    detail = {"value": lp["obj"], "left_neg": lp["neg"], "right_neg": rp["neg"]}
                else:
                    continue
                a, b = _pair_order(left["id"], right["id"])
                found.append(
                    {
                        "kind": kind,
                        "left_id": a,
                        "right_id": b,
                        "slot": f"{lp['holder']}|{lp['pred']}",
                        "severity": SEVERITY[kind],
                        "detail": detail,
                    }
                )
    return found


def persist(
    db: Database,
    conflicts: list[dict[str, Any]],
    *,
    subject: str = "self",
    actor: str = "system:conflict",
) -> dict[str, Any]:
    """写入 conflict 表并把双方置为 contested（重复的跳过）。"""
    created = 0
    skipped = 0
    with db.tx() as conn:
        for item in conflicts:
            existing = conn.execute(
                "SELECT id FROM conflict WHERE left_tbl='memory' AND left_id=? AND right_tbl='memory' "
                "AND right_id=? AND kind=?",
                (item["left_id"], item["right_id"], item["kind"]),
            ).fetchone()
            if existing:
                skipped += 1
                continue
            conflict_id = ulid()
            conn.execute(
                "INSERT INTO conflict(id, subject_id, kind, left_tbl, left_id, right_tbl, right_id, slot, "
                "severity, detail_json, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,'open',?)",
                (
                    conflict_id,
                    subject,
                    item["kind"],
                    "memory",
                    item["left_id"],
                    "memory",
                    item["right_id"],
                    item["slot"],
                    item["severity"],
                    json.dumps(item["detail"], ensure_ascii=False, sort_keys=True),
                    now_iso(),
                ),
            )
            for memory_id in (item["left_id"], item["right_id"]):
                conn.execute(
                    "UPDATE memory SET status='contested' WHERE id=? AND status IN ('active','candidate')",
                    (memory_id,),
                )
            events.append(
                conn,
                actor=actor,
                op="conflict.detect",
                target_tbl="conflict",
                target_id=conflict_id,
                subject_id=subject,
                payload={"kind": item["kind"], "slot": item["slot"]},
            )
            created += 1
    return {"created": created, "skipped": skipped}


def list_conflicts(db: Database, *, status: str = "open", limit: int = 20) -> list[dict[str, Any]]:
    rows = db.conn.execute(
        "SELECT * FROM conflict WHERE status=? ORDER BY severity DESC, created_at DESC LIMIT ?",
        (status, limit),
    ).fetchall()
    out = []
    for row in rows:
        detail = {}
        try:
            detail = json.loads(row["detail_json"])
        except json.JSONDecodeError:
            pass
        out.append(
            {
                "id": row["id"],
                "kind": row["kind"],
                "slot": row["slot"],
                "severity": row["severity"],
                "status": row["status"],
                "left": row["left_id"],
                "right": row["right_id"],
                "detail": detail,
                "created_at": row["created_at"],
            }
        )
    return out


def resolve(
    db: Database,
    conflict_id: str,
    decision: str,
    *,
    note: str | None = None,
    actor: str = "user",
) -> dict[str, Any]:
    """裁决一条冲突。

    ``decision``：
    - ``prefer_left`` / ``prefer_right``：输的一方降级为 archived（仍可按 id 取回），赢的一方恢复 active；
    - ``keep_both``：双方保持 contested，但冲突关闭，不再提醒；
    - ``dismissed``：双方恢复 active，冲突作废。
    """
    if decision not in ("prefer_left", "prefer_right", "keep_both", "dismissed"):
        raise ValueError(f"unknown decision: {decision}")
    row = db.conn.execute("SELECT * FROM conflict WHERE id=?", (conflict_id,)).fetchone()
    if row is None:
        raise KeyError(conflict_id)

    winner = loser = None
    if decision == "prefer_left":
        winner, loser = row["left_id"], row["right_id"]
    elif decision == "prefer_right":
        winner, loser = row["right_id"], row["left_id"]

    with db.tx() as conn:
        if winner and loser:
            conn.execute("UPDATE memory SET status='archived' WHERE id=?", (loser,))
            conn.execute("UPDATE memory SET status='active' WHERE id=?", (winner,))
        elif decision == "dismissed":
            conn.execute(
                "UPDATE memory SET status='active' WHERE id IN (?,?) AND status='contested'",
                (row["left_id"], row["right_id"]),
            )
        conn.execute(
            "UPDATE conflict SET status=?, resolved_by=?, resolved_at=? WHERE id=?",
            (decision, actor, now_iso(), conflict_id),
        )
        events.append(
            conn,
            actor=actor,
            op="conflict.resolve",
            target_tbl="conflict",
            target_id=conflict_id,
            payload={"decision": decision, "note": note},
        )
    return {"id": conflict_id, "decision": decision, "winner": winner, "loser": loser}


def run(db: Database, *, subject: str = "self") -> dict[str, Any]:
    """检测 + 落库，一步到位。"""
    found = detect(db, subject=subject)
    stats = persist(db, found, subject=subject)
    stats["found"] = len(found)
    stats["at"] = now_iso()
    return stats
