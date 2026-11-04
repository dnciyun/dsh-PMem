"""不变量自检（`docs/02` §5 I1–I19）。

``pmem doctor`` 调它。每条返回 ``{id, ok, detail}``；失败不抛异常 —— 体检要能一次看全。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..base import events
from ..base.db import Database

SCOPE_RE = re.compile(r"^project:[0-9a-f]{8,64}$|^project:manual$")


def _scalar(conn: Any, sql: str, params: tuple = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def check_all(db: Database, *, subject: str = "self") -> list[dict[str, Any]]:
    conn = db.conn
    results: list[dict[str, Any]] = []

    def add(check_id: str, ok: bool, detail: str = "") -> None:
        results.append({"id": check_id, "ok": bool(ok), "detail": detail})

    # I1 原文不可变：触发器必须在
    trigger = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='mem_immutable'"
    ).fetchone()
    add("I1", trigger is not None, "mem_immutable 触发器存在" if trigger else "缺少不可变触发器")

    # I2 活跃记忆必须有来源与置信度
    bad = _scalar(
        conn,
        "SELECT COUNT(*) FROM memory WHERE status IN ('active','contested') "
        "AND (confidence <= 0 OR provenance IS NULL OR provenance='')",
    )
    add("I2", bad == 0, f"{bad} 条活跃记忆缺来源/置信度")

    # I3 派生行必须带证据
    empty_evidence = 0
    for table in ("relation", "trait", "pattern", "experience"):
        empty_evidence += _scalar(
            conn, f"SELECT COUNT(*) FROM {table} WHERE evidence_json IS NULL OR evidence_json IN ('','[]','{{}}')"
        )
    add("I3", empty_evidence == 0, f"{empty_evidence} 条派生行没有证据指针")

    # I4 证据指针不能指向已被真删的记忆
    dangling = 0
    for row in conn.execute("SELECT evidence_json FROM relation UNION ALL SELECT evidence_json FROM trait"):
        try:
            payload = json.loads(row["evidence_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        items = payload if isinstance(payload, list) else payload.get("evidence", [])
        for item in items if isinstance(items, list) else []:
            memory_id = item.get("memory_id") if isinstance(item, dict) else None
            if memory_id and conn.execute("SELECT 1 FROM memory WHERE id=?", (memory_id,)).fetchone() is None:
                dangling += 1
    add("I4", dangling == 0, f"{dangling} 个悬空证据指针")

    # I5 特质版本链不能成环
    cycles = 0
    rows = conn.execute("SELECT id, prev_id FROM trait").fetchall()
    prev = {r["id"]: r["prev_id"] for r in rows}
    for start in prev:
        seen = set()
        node = start
        while node and node in prev:
            if node in seen:
                cycles += 1
                break
            seen.add(node)
            node = prev[node]
    add("I5", cycles == 0, f"{cycles} 个特质版本链成环")

    # I6 事件哈希链
    chain_ok, chain_detail = events.verify_chain(conn)
    add("I6", chain_ok, chain_detail)

    # I8 supersede 双向一致
    inconsistent = _scalar(
        conn,
        "SELECT COUNT(*) FROM memory a JOIN memory b ON a.superseded_by=b.id "
        "WHERE b.supersedes_id IS NOT a.id",
    ) + _scalar(
        conn,
        "SELECT COUNT(*) FROM memory a JOIN memory b ON a.supersedes_id=b.id "
        "WHERE b.superseded_by IS NOT a.id",
    )
    add("I8", inconsistent == 0, f"{inconsistent} 条替代关系单向")

    # I9 向量行不能指向不存在的记忆
    orphan_vec = _scalar(
        conn,
        "SELECT COUNT(*) FROM memory_vec_plain v LEFT JOIN memory m ON v.memory_seq=m.seq WHERE m.seq IS NULL",
    )
    add("I9", orphan_vec == 0, f"{orphan_vec} 条孤儿向量")

    # I12 派生行必须有 derivation（experience 表按设计没有该列，故不查）
    missing_derivation = 0
    for table in ("relation", "trait", "pattern"):
        missing_derivation += _scalar(
            conn, f"SELECT COUNT(*) FROM {table} WHERE derivation IS NULL OR derivation=''"
        )
    add("I12", missing_derivation == 0, f"{missing_derivation} 条派生行缺 derivation")

    # I13 scope_key 形状
    rows = conn.execute("SELECT DISTINCT scope_key FROM memory").fetchall()
    bad_scope = [r["scope_key"] for r in rows if not SCOPE_RE.match(r["scope_key"] or "")]
    add("I13", not bad_scope, f"非法 scope_key：{bad_scope[:3]}")

    # I14 关系权重不得超过谓词上限（防止手工/派生写越界）
    over_weight = _scalar(
        conn,
        "SELECT COUNT(*) FROM relation r JOIN relation_type t ON t.predicate=r.predicate "
        "WHERE r.weight > t.max_weight + 1e-9",
    )
    add("I14", over_weight == 0, f"{over_weight} 条关系权重超过谓词上限")

    # I15 实体提及的 span 必须落在正文内
    bad_span = _scalar(
        conn,
        "SELECT COUNT(*) FROM entity_mention em JOIN memory m ON m.seq=em.memory_seq "
        "WHERE em.span_start < 0 OR em.span_end <= em.span_start "
        "OR em.span_end > length(m.text_norm)",
    )
    add("I15", bad_span == 0, f"{bad_span} 条提及 span 越界")

    # I16 活跃关系不允许自环
    self_loop = _scalar(
        conn, "SELECT COUNT(*) FROM relation WHERE src_id=dst_id AND status IN ('active','contested')"
    )
    add("I16", self_loop == 0, f"{self_loop} 条关系自环")

    # I17 分组路径/深度自洽；成员只能指向「还存在」的宿主
    # 软删是停放不是消失：记忆行、分组、等级都留着等恢复，成员关系必须跟着留，
    # 否则捞回来的时候它悄悄掉出原来的分组。所以这里只查宿主是不是真的没了。
    # 实体不同：合并会把成员迁到保留的那一侧，留在非活跃实体上的成员就是没迁干净的残留。
    bad_group = _scalar(
        conn,
        "SELECT COUNT(*) FROM grp g LEFT JOIN grp p ON p.id=g.parent_id "
        "WHERE g.status='active' AND ("
        "g.depth <> 1 + length(g.path) - length(replace(g.path,'/','')) "
        "OR (g.parent_id IS NULL AND g.path <> g.name) "
        "OR (g.parent_id IS NOT NULL AND (p.id IS NULL OR g.path <> p.path || '/' || g.name)))",
    )
    orphan_member = _scalar(
        conn,
        "SELECT COUNT(*) FROM grp_member gm JOIN grp g ON g.id=gm.grp_id "
        "WHERE g.status <> 'active'",
    )
    parked = _scalar(
        conn,
        "SELECT COUNT(*) FROM grp_member gm JOIN memory m ON m.id=gm.target_id "
        "WHERE gm.target_tbl='memory' AND m.status='deleted'",
    )
    orphan_target = _scalar(
        conn,
        "SELECT COUNT(*) FROM grp_member gm WHERE "
        "(gm.target_tbl='memory' AND NOT EXISTS (SELECT 1 FROM memory m WHERE m.id=gm.target_id)) "
        "OR (gm.target_tbl='entity' AND NOT EXISTS "
        "  (SELECT 1 FROM entity e WHERE e.id=gm.target_id AND e.status='active'))",
    )
    detail = (
        f"{bad_group} 个分组路径/深度不一致，{orphan_member} 条成员指向非活跃分组，"
        f"{orphan_target} 条成员指向已消失的目标"
    )
    if parked:
        detail += f"（另有 {parked} 条挂在停放记忆上，属软删保留，不报错）"
    add("I17", bad_group == 0 and orphan_member == 0 and orphan_target == 0, detail)

    # I18 关联图边规范化：a_seq < b_seq（无向边只存一份）、无自环、不指向已消失的记忆
    bad_edge = _scalar(conn, "SELECT COUNT(*) FROM memory_edge WHERE a_seq >= b_seq")
    dangling_edge = _scalar(
        conn,
        "SELECT COUNT(*) FROM memory_edge e "
        "LEFT JOIN memory a ON a.seq=e.a_seq LEFT JOIN memory b ON b.seq=e.b_seq "
        "WHERE a.seq IS NULL OR b.seq IS NULL",
    )
    add(
        "I18",
        bad_edge == 0 and dangling_edge == 0,
        f"{bad_edge} 条边未规范化，{dangling_edge} 条边指向不存在的记忆",
    )

    # I19 向量表自洽：维度>0、blob 长度 = dim×2（fp16）或 dim×4（老 fp32）、不指向已消失的记忆
    bad_vec = _scalar(
        conn,
        "SELECT COUNT(*) FROM memory_vec_plain "
        "WHERE dim <= 0 OR length(vec) NOT IN (dim*2, dim*4)",
    )
    dangling_vec = _scalar(
        conn,
        "SELECT COUNT(*) FROM memory_vec_plain v "
        "LEFT JOIN memory m ON m.seq=v.memory_seq WHERE m.seq IS NULL",
    )
    add(
        "I19",
        bad_vec == 0 and dangling_vec == 0,
        f"{bad_vec} 条向量维度不自洽，{dangling_vec} 条向量指向不存在的记忆",
    )

    return results


def summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [r for r in results if not r["ok"]]
    return {
        "total": len(results),
        "passed": len(results) - len(failed),
        "failed": failed,
        "ok": not failed,
    }
