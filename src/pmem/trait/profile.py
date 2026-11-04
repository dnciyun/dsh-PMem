"""记忆画像（M6）：给宿主一个"我现在认为你是什么样"的低成本摘要。

只读，不产生新结论；数据来自已建立的 trait + goals，不再临时推断。
预算按 token 估算，超出就截断，并在输出里显式标 `truncated`。
"""

from __future__ import annotations

import json
from typing import Any

from ..base.db import Database
from ..memory.text import estimate_tokens


def build(db: Database, *, subject: str = "self", budget: int = 300) -> dict[str, Any]:
    traits = db.conn.execute(
        "SELECT * FROM trait WHERE subject_id=? AND status IN ('hypothesis','established') "
        "ORDER BY CASE status WHEN 'established' THEN 0 ELSE 1 END, strength DESC",
        (subject,),
    ).fetchall()
    goals = db.conn.execute(
        "SELECT id, text_raw, importance, day_key FROM memory WHERE subject_id=? "
        "AND kind='goal' AND status IN ('active','candidate') ORDER BY importance DESC LIMIT 5",
        (subject,),
    ).fetchall()

    items: list[dict[str, Any]] = []
    tokens = 0
    for row in traits:
        value = json.loads(row["value_json"] or "{}")
        text = value.get("label") or row["key"]
        cost = estimate_tokens(text) + 12
        if tokens + cost > budget:
            break
        tokens += cost
        items.append(
            {
                "kind": "trait",
                "id": row["id"],
                "type": row["type"],
                "key": row["key"],
                "label": text,
                "status": row["status"],
                "strength": row["strength"],
                "confidence": row["confidence"],
                "evidence_days": row["evidence_days"],
            }
        )
    goal_items: list[dict[str, Any]] = []
    for row in goals:
        cost = estimate_tokens(row["text_raw"]) + 8
        if tokens + cost > budget:
            break
        tokens += cost
        goal_items.append(
            {
                "id": row["id"],
                "text": row["text_raw"],
                "importance": row["importance"],
                "day": row["day_key"],
            }
        )
    return {
        "traits": items,
        "goals": goal_items,
        "tokens": tokens,
        "budget": budget,
        "truncated": len(items) + len(goal_items) < len(traits) + len(goals),
    }


def render(data: dict[str, Any]) -> str:
    lines: list[str] = []
    if data.get("traits"):
        lines.append("画像：")
        for item in data["traits"]:
            mark = "已确立" if item["status"] == "established" else "假设"
            lines.append(f"- {item['label']}（{mark}，强度 {item['strength']:.2f}）")
    if data.get("goals"):
        lines.append("目标：")
        for item in data["goals"]:
            lines.append(f"- {item['text']}")
    if not lines:
        return "— 还没有稳定的画像"
    return "\n".join(lines)
