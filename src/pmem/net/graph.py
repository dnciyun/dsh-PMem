"""2 跳图查询（M3 / PLAN M3 Gate：100k 边 2 跳 < 150 ms）。

实现取向：**不做通用图引擎**，只做两种最有用的问法——

- ``src=X``：X 能到谁（1/2 跳），按路径分数排序；
- ``src=X, dst=Y``：X 与 Y 之间有什么路径（直接边或共同邻居）；
- 只给 ``dst=Y``：谁指向 Y。

为什么不用递归 CTE：SQLite 的递归 CTE 在 10 万边上会把中间结果落临时表，
深度 2 的 fan-out 一旦超过几千就退化成秒级。分层取邻接（每层一条
``IN (...)`` 查询，配合 ``idx_rel_src``/``idx_rel_dst``）能把每跳控制在毫秒级，
并且天然带 fan-out 上限，不会因为某个超级节点把内存打爆。
"""

from __future__ import annotations

from typing import Any

from ..base.errors import ConflictError, ValidationError
from . import entities

MAX_FANOUT = 200
MAX_EDGES_PER_LEVEL = 4000


def _entity_brief(row: Any) -> dict[str, Any]:
    return {"id": row["id"], "name": row["name"], "type": row["type"]}


def _endpoint(db: Any, subject: str, value: str, field: str) -> Any:
    row = db.conn.execute(
        "SELECT * FROM entity WHERE id=? AND status='active'", (value,)
    ).fetchone()
    if row is not None:
        return row
    found = [r for r in entities.find(db, subject=subject, name=value) if r["status"] == "active"]
    if not found:
        return None
    # 同一个裸名对应多个身份：不猜，让调用方选
    distinct = {r["name_norm"] for r in found}
    if len(distinct) > 1:
        raise ConflictError(
            f"实体名 {value!r} 对应 {len(found)} 个身份，请用实体 id 指定",
            field=field,
            hint="先调用 memory_relation action=list_entities 或传 id",
            details={
                "candidates": [
                    {"id": r["id"], "name": r["name"], "type": r["type"],
                     "disambig": _disambig(r)}
                    for r in found
                ]
            },
        )
    return found[0]


def endpoint(db: Any, subject: str, value: str, field: str = "src") -> Any:
    """公开的端点解析：id 或名字 → 实体行；找不到返回 None。"""
    return _endpoint(db, subject, value, field)


def _disambig(row: Any) -> list[str]:
    import json

    try:
        data = json.loads(row["extra_json"])
    except (json.JSONDecodeError, TypeError):
        return []
    return sorted(data.get("disambig") or []) if isinstance(data, dict) else []


def _status_clause(include_dormant: bool) -> str:
    return "status IN ('active','contested','dormant')" if include_dormant else "status IN ('active','contested')"


def _edges(
    db: Any,
    *,
    column: str,
    ids: list[str],
    predicate: str | None,
    include_dormant: bool,
) -> list[Any]:
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    sql = (
        f"SELECT * FROM relation WHERE {column} IN ({placeholders}) AND {_status_clause(include_dormant)}"
    )
    params: list[Any] = list(ids)
    if predicate:
        sql += " AND predicate=?"
        params.append(predicate)
    sql += " ORDER BY weight DESC LIMIT ?"
    params.append(MAX_EDGES_PER_LEVEL)
    return list(db.conn.execute(sql, params).fetchall())


def _valid_at(row: Any, as_of: str | None) -> bool:
    if not as_of:
        return True
    if row["valid_from"] and row["valid_from"] > as_of:
        return False
    if row["valid_to"] and row["valid_to"] < as_of:
        return False
    return True


def _edge_item(row: Any, src: Any, dst: Any, *, scale: float = 1.0) -> dict[str, Any]:
    return {
        "relation_id": row["id"],
        "src": _entity_brief(src),
        "dst": _entity_brief(dst),
        "predicate": row["predicate"],
        "hops": 1,
        "weight": round(row["weight"] * scale, 4),
        "confidence": round(row["confidence"], 4),
        "derivation": row["derivation"],
        "evidence": _evidence(row["evidence_json"]),
        "path": [src["name"], dst["name"]],
        "predicates": [row["predicate"]],
    }


def _evidence(raw: str) -> list[str]:
    import json

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [str(item["memory_id"]) for item in data if isinstance(item, dict) and item.get("memory_id")]


def _node_map(db: Any, ids: set[str]) -> dict[str, Any]:
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = db.conn.execute(
        f"SELECT * FROM entity WHERE id IN ({placeholders})", tuple(ids)
    ).fetchall()
    return {row["id"]: row for row in rows}


def query(
    db: Any,
    *,
    subject: str = "self",
    src: str | None = None,
    dst: str | None = None,
    predicate: str | None = None,
    depth: int = 2,
    limit: int = 50,
    include_dormant: bool = False,
    as_of: str | None = None,
) -> dict[str, Any]:
    if not src and not dst:
        raise ValidationError("query 需要 src 或 dst 至少一个", field="src")
    if depth < 1 or depth > 2:
        raise ValidationError("depth 只支持 1 或 2（3 跳误差指数级放大）", field="depth")
    src_row = _endpoint(db, subject, src, "src") if src else None
    dst_row = _endpoint(db, subject, dst, "dst") if dst else None
    if src and src_row is None:
        return {"items": [], "src": None, "dst": _entity_brief(dst_row) if dst_row else None,
                "request": {"src": src, "dst": dst},
                "depth": depth, "scanned": 0, "not_found": src}
    if dst and dst_row is None:
        return {"items": [], "src": _entity_brief(src_row) if src_row else None, "dst": None,
                "request": {"src": src, "dst": dst},
                "depth": depth, "scanned": 0, "not_found": dst}

    items: list[dict[str, Any]] = []
    if src_row is not None and dst_row is not None:
        items = _between(db, src_row, dst_row, predicate, depth, include_dormant, as_of)
    elif src_row is not None:
        items = _forward(db, src_row, predicate, depth, include_dormant, as_of)
    else:
        items = _backward(db, dst_row, predicate, depth, include_dormant, as_of)

    # 去重 + 排序
    dedup: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in items:
        key = (item["src"]["id"], item["dst"]["id"], tuple(item["predicates"]), tuple(item["path"]))
        if key not in dedup or item["weight"] > dedup[key]["weight"]:
            dedup[key] = item
    ordered = sorted(dedup.values(), key=lambda it: (-it["weight"], it["hops"], it["path"]))
    return {
        "items": ordered[: max(1, limit)],
        "src": _entity_brief(src_row) if src_row else None,
        "dst": _entity_brief(dst_row) if dst_row else None,
        "request": {"src": src, "dst": dst},
        "depth": depth,
        "scanned": len(items),
    }


def _forward(
    db: Any,
    start: Any,
    predicate: str | None,
    depth: int,
    include_dormant: bool,
    as_of: str | None,
) -> list[dict[str, Any]]:
    level = _edges(db, column="src_id", ids=[start["id"]], predicate=predicate,
                   include_dormant=include_dormant)
    level = [row for row in level if _valid_at(row, as_of)][:MAX_FANOUT]
    nodes = _node_map(db, {row["dst_id"] for row in level})
    out = [
        _edge_item(row, start, nodes[row["dst_id"]])
        for row in level
        if row["dst_id"] in nodes
    ]
    if depth < 2 or not level:
        return out
    next_ids = list({row["dst_id"] for row in level})[:MAX_FANOUT]
    second = _edges(db, column="src_id", ids=next_ids, predicate=predicate,
                    include_dormant=include_dormant)
    second = [row for row in second if _valid_at(row, as_of)]
    all_nodes = _node_map(db, {row["src_id"] for row in second} | {row["dst_id"] for row in second})
    # 先按 mid 建索引：否则每个二跳边都要扫一遍一跳列表，hub 查询会退化成 O(n²)
    level_by_dst: dict[str, list[Any]] = {}
    for edge in level:
        level_by_dst.setdefault(edge["dst_id"], []).append(edge)
    for row in second:
        if row["src_id"] not in all_nodes or row["dst_id"] not in all_nodes:
            continue
        if row["dst_id"] == start["id"]:
            continue
        for first in level_by_dst.get(row["src_id"], ()):
            item = _edge_item(row, all_nodes[row["src_id"]], all_nodes[row["dst_id"]], scale=0.8)
            item.update(
                {
                    "hops": 2,
                    "weight": round(first["weight"] * row["weight"] * 0.8, 4),
                    "confidence": round(min(first["confidence"], row["confidence"]), 4),
                    "path": [start["name"], all_nodes[row["src_id"]]["name"], all_nodes[row["dst_id"]]["name"]],
                    "predicates": [first["predicate"], row["predicate"]],
                    "via": [{"relation_id": first["id"], "predicate": first["predicate"]}],
                    "evidence": sorted(set(_evidence(first["evidence_json"]) + _evidence(row["evidence_json"]))),
                }
            )
            out.append(item)
    return out


def _backward(
    db: Any,
    end: Any,
    predicate: str | None,
    depth: int,
    include_dormant: bool,
    as_of: str | None,
) -> list[dict[str, Any]]:
    level = _edges(db, column="dst_id", ids=[end["id"]], predicate=predicate,
                   include_dormant=include_dormant)
    level = [row for row in level if _valid_at(row, as_of)][:MAX_FANOUT]
    nodes = _node_map(db, {row["src_id"] for row in level})
    out = [
        _edge_item(row, nodes[row["src_id"]], end)
        for row in level
        if row["src_id"] in nodes
    ]
    if depth < 2 or not level:
        return out
    prev_ids = list({row["src_id"] for row in level})[:MAX_FANOUT]
    second = _edges(db, column="dst_id", ids=prev_ids, predicate=predicate,
                    include_dormant=include_dormant)
    second = [row for row in second if _valid_at(row, as_of)]
    all_nodes = _node_map(db, {row["src_id"] for row in second} | {row["dst_id"] for row in second})
    level_by_src: dict[str, list[Any]] = {}
    for edge in level:
        level_by_src.setdefault(edge["src_id"], []).append(edge)
    for row in second:
        if row["src_id"] not in all_nodes or row["dst_id"] not in all_nodes:
            continue
        if row["src_id"] == end["id"]:
            continue
        for last in level_by_src.get(row["dst_id"], ()):
            item = _edge_item(row, all_nodes[row["src_id"]], all_nodes[row["dst_id"]], scale=0.8)
            item.update(
                {
                    "hops": 2,
                    "weight": round(row["weight"] * last["weight"] * 0.8, 4),
                    "confidence": round(min(row["confidence"], last["confidence"]), 4),
                    "path": [all_nodes[row["src_id"]]["name"], all_nodes[row["dst_id"]]["name"], end["name"]],
                    "predicates": [row["predicate"], last["predicate"]],
                    "via": [{"relation_id": last["id"], "predicate": last["predicate"]}],
                    "evidence": sorted(set(_evidence(row["evidence_json"]) + _evidence(last["evidence_json"]))),
                }
            )
            out.append(item)
    return out


def _between(
    db: Any,
    src_row: Any,
    dst_row: Any,
    predicate: str | None,
    depth: int,
    include_dormant: bool,
    as_of: str | None,
) -> list[dict[str, Any]]:
    forward = _edges(db, column="src_id", ids=[src_row["id"]], predicate=predicate,
                     include_dormant=include_dormant)
    backward = _edges(db, column="dst_id", ids=[dst_row["id"]], predicate=predicate,
                      include_dormant=include_dormant)
    forward = [row for row in forward if _valid_at(row, as_of)]
    backward = [row for row in backward if _valid_at(row, as_of)]
    nodes = _node_map(db, {row["dst_id"] for row in forward} | {row["src_id"] for row in backward}
                      | {src_row["id"], dst_row["id"]})
    out: list[dict[str, Any]] = []
    for row in forward:
        if row["dst_id"] == dst_row["id"] and row["dst_id"] in nodes:
            out.append(_edge_item(row, src_row, dst_row))
    if depth < 2:
        return out
    backward_by_src: dict[str, list[Any]] = {}
    for edge in backward:
        backward_by_src.setdefault(edge["src_id"], []).append(edge)
    for first in forward:
        mid = first["dst_id"]
        if mid == dst_row["id"] or mid not in nodes:
            continue
        for second in backward_by_src.get(mid, ()):
            item = _edge_item(second, nodes[mid], dst_row, scale=0.8)
            item.update(
                {
                    "hops": 2,
                    "weight": round(first["weight"] * second["weight"] * 0.8, 4),
                    "confidence": round(min(first["confidence"], second["confidence"]), 4),
                    "path": [src_row["name"], nodes[mid]["name"], dst_row["name"]],
                    "predicates": [first["predicate"], second["predicate"]],
                    "via": [{"relation_id": first["id"], "predicate": first["predicate"]}],
                    "evidence": sorted(set(_evidence(first["evidence_json"]) + _evidence(second["evidence_json"]))),
                }
            )
            out.append(item)
    return out
