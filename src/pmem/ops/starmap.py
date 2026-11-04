"""记忆海马体星图（M9）：真实库的**关系星云**，不硬套脑/海马形状。

星图不是装饰：位置、亮度、连线都由库里的字段决定——

- **没有边界**：黑底星空 + 星云 + 三档星点 + 暗角；形状由关系自己长出来；
- **细胞** = 半透明记忆胞体 + 光晕 + 短树突；S/A 有十字星芒；休眠变暗；
- **颜色** = 有分级用等级色，无分级用类型色；
- **连线** = 相似记忆、共同实体、同组相邻、实体关系、特质证据；没有连线 = 还没建立关系；
- **簇心** = 分组/类型；`我们` 在上方，`我` 在侧边；位置是确定性的，不做随机力导向。

输出：交互 HTML（canvas，无外部依赖）、静态 SVG、JSON、可选 PNG（Pillow）。
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from ..base.clock import now_iso
from ..base.db import Database
from ..memory.groups import grade_map, group_map
from ..memory.text import char_ngrams
from . import starmap_brain, starmap_space

GRADE_COLOR = {
    "S": "#ffd27a",
    "A": "#ffe9c7",
    "B": "#8fe3ff",
    "C": "#b7a6ff",
    "D": "#7c8aa5",
    None: "#9fb4cc",
}
# 日记派生色：不是等级也不是类型，而是「来源」。
# 用琥珀而不复用任何 GRADE_COLOR / KIND_COLOR：同一等级下日记来的那句
# 和平时写下的那句，在图上必须是两种颜色，人才能在扫一眼时区分。
JOURNAL_COLOR = "#e9b872"
GRADE_LABEL = {"S": "核心", "A": "长期", "B": "有用", "C": "情境", "D": "琐碎", None: "未分级"}
GRADE_ORBIT = {"S": -8, "A": 14, "B": 45, "C": 80, "D": 120, None: 32}
KIND_COLOR = {
    "episodic": "#7fc8ff",
    "semantic": "#ffe2a8",
    "person": "#ff9fc4",
    "relation": "#8ff0d0",
    "experience": "#ffc27a",
    "goal": "#9ef0a8",
    "meta": "#c9a7ff",
}
TYPE_COLOR = {
    "person": "#ff9fc4",
    "place": "#9ef0a8",
    "org": "#ffd27a",
    "time": "#b7a6ff",
    "event": "#8fe3ff",
    "knowledge": "#c9a7ff",
    "tool": "#8ff0d0",
    "goal": "#9ef0a8",
    "concept": "#ffe9c7",
    "trait": "#c9a7ff",
    "cognition": "#c9a7ff",
}
GROUP_PALETTE = [
    "#ffd27a", "#8fe3ff", "#b7a6ff", "#8ff0d0", "#ff9fc4",
    "#9ef0a8", "#ffc27a", "#c9a7ff", "#7fc8ff", "#ffe9c7",
]
PREDICATE_COLOR = {
    "likes": "#ff9fc4",
    "dislikes": "#ff8a8a",
    "works_at": "#ffd27a",
    "lives_in": "#9ef0a8",
    "uses": "#8ff0d0",
    "part_of": "#8fe3ff",
    "related_to": "#7c8aa5",
    "works_on": "#b7a6ff",
    "knows": "#ffc27a",
    "located_in": "#8ff0d0",
    "studies_at": "#ffe9c7",
    "born_in": "#ff9fc4",
}


def _hash(value: str) -> int:
    import hashlib

    return int(hashlib.sha256(value.encode("utf-8")).hexdigest()[:12], 16)


def _short(text: str, limit: int = 80) -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _grade_key(grade: str | None) -> int:
    return {"S": 0, "A": 1, "B": 2, "C": 3, "D": 4}.get(grade or "", 5)


def _node_color(node: dict[str, Any]) -> str:
    if node["type"] == "entity":
        return TYPE_COLOR.get(node.get("entity_type") or "", "#9fb4cc")
    if node["type"] == "group":
        return GROUP_PALETTE[_hash(node["id"]) % len(GROUP_PALETTE)]
    if node["type"] == "trait":
        return "#c9a7ff"
    if node["type"] == "core":
        return "#ffe9c7"
    # 来源优先于等级：日记派生的记忆即便有 S/A 分级，也先亮成琥珀色，
    # 否则「这句是从那天日记来的」就被等级色盖掉了。
    if node.get("type") == "memory" and node.get("source_kind") == "journal":
        return JOURNAL_COLOR
    if node.get("type") == "memory" and node.get("grade"):
        return GRADE_COLOR.get(node["grade"], "#9fb4cc")
    return KIND_COLOR.get(node.get("kind") or "", "#9fb4cc")


def _latest_quality(db: Database) -> float | None:
    row = db.conn.execute(
        "SELECT metrics_json FROM reflection WHERE period LIKE 'day:%' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["metrics_json"] or "{}").get("quality")
    except (json.JSONDecodeError, TypeError):
        return None


def collect(
    db: Database,
    *,
    subject: str = "self",
    redact: bool = True,
    limit: int = 3000,
) -> dict[str, Any]:
    """读取真实库，生成星图数据（未布局）。"""
    mem_rows = db.conn.execute(
        "SELECT seq,id,text_raw,kind,status,importance,confidence,day_key,created_at,"
        "use_count,last_used_at,pinned,sensitivity,source_kind FROM memory "
        "WHERE subject_id=? AND status<>'deleted' ORDER BY day_key, seq LIMIT ?",
        (subject, max(1, limit)),
    ).fetchall()
    total_memories = int(
        db.conn.execute(
            "SELECT COUNT(*) c FROM memory WHERE subject_id=? AND status<>'deleted'", (subject,)
        ).fetchone()["c"]
    )
    ids = [row["id"] for row in mem_rows]
    groups = group_map(db, "memory", ids)
    grades = grade_map(db, "memory", ids)

    group_rows = db.conn.execute(
        "SELECT g.*, (SELECT COUNT(*) FROM grp_member gm WHERE gm.grp_id=g.id) AS members "
        "FROM grp g WHERE g.subject_id=? AND g.status='active' ORDER BY g.path",
        (subject,),
    ).fetchall()

    entity_rows = db.conn.execute(
        "SELECT id,name,type,mention_count,confidence FROM entity "
        "WHERE subject_id=? AND status='active' ORDER BY mention_count DESC, name",
        (subject,),
    ).fetchall()
    entity_ids = {row["id"] for row in entity_rows}
    memory_ids = set(ids)

    mention_rows = db.conn.execute(
        "SELECT em.entity_id, m.id AS memory_id FROM entity_mention em "
        "JOIN memory m ON m.seq=em.memory_seq "
        "WHERE m.subject_id=? AND m.status<>'deleted'",
        (subject,),
    ).fetchall()
    mentions: list[dict[str, str]] = []
    mentioned_entities: set[str] = set()
    for row in mention_rows:
        if row["entity_id"] in entity_ids and row["memory_id"] in memory_ids:
            mentions.append({"memory": row["memory_id"], "entity": row["entity_id"]})
            mentioned_entities.add(row["entity_id"])

    rel_rows = db.conn.execute(
        "SELECT r.id,r.src_id,r.dst_id,r.predicate,r.weight,r.status "
        "FROM relation r JOIN entity s ON s.id=r.src_id "
        "WHERE s.subject_id=? AND r.status IN ('active','dormant','contested')",
        (subject,),
    ).fetchall()
    relations = [
        dict(row)
        for row in rel_rows
        if row["src_id"] in entity_ids and row["dst_id"] in entity_ids
    ]

    trait_rows = db.conn.execute(
        "SELECT id,type,key,value_json,strength,confidence,evidence_json,status "
        "FROM trait WHERE subject_id=? AND status IN ('hypothesis','established') "
        "ORDER BY strength DESC LIMIT 80",
        (subject,),
    ).fetchall()

    display_row = db.conn.execute(
        "SELECT display FROM subject WHERE id=?", (subject,)
    ).fetchone()
    display = display_row["display"] if display_row else "我"

    nodes: list[dict[str, Any]] = [
        {
            "id": "core",
            "type": "core",
            "label": display,
            "sub": "主体",
            "orbit": 0,
        }
    ]
    for row in group_rows:
        path = row["path"]
        nodes.append(
            {
                "id": f"grp:{path}",
                "type": "group",
                "label": path.split("/")[-1],
                "path": path,
                "depth": row["depth"],
                "members": row["members"],
                "kind": "group",
                "sub": f"{row['members']} 项",
            }
        )
    for row in mem_rows:
        text = row["text_raw"] or ""
        if redact and row["sensitivity"] == "secret":
            text = "（秘密，默认隐去）"
        memory_groups = groups.get(row["id"], [])
        nodes.append(
            {
                "id": f"mem:{row['id']}",
                "type": "memory",
                "label": _short(text, 86),
                "text": _short(text, 260),
                "kind": row["kind"],
                "status": row["status"],
                "grade": grades.get(row["id"]),
                "groups": memory_groups,
                "importance": round(float(row["importance"]), 3),
                "confidence": round(float(row["confidence"]), 3),
                "day": row["day_key"],
                "created_at": row["created_at"],
                "use_count": int(row["use_count"]),
                "last_used_at": row["last_used_at"],
                "pinned": bool(row["pinned"]),
                "sensitivity": row["sensitivity"],
                # 来源是「这句从哪来」：journal = 当天日记派生。
                # 老 JSON 没有这个字段，前端按缺省处理，不影响渲染。
                "source_kind": row["source_kind"],
                "seq": int(row["seq"]),
            }
        )
    for row in entity_rows:
        nodes.append(
            {
                "id": f"ent:{row['id']}",
                "type": "entity",
                "label": row["name"],
                "entity_type": row["type"],
                "mention_count": int(row["mention_count"]),
                "confidence": round(float(row["confidence"]), 3),
                "kind": "entity",
                "sub": row["type"],
            }
        )
    for row in trait_rows:
        try:
            value = json.loads(row["value_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            value = {}
        label = value.get("label") or row["key"]
        nodes.append(
            {
                "id": f"trait:{row['id']}",
                "type": "trait",
                "label": _short(str(label), 28),
                "trait_type": row["type"],
                "strength": round(float(row["strength"]), 3),
                "confidence": round(float(row["confidence"]), 3),
                "status": row["status"],
                "kind": "trait",
                "sub": row["type"],
            }
        )

    links: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def link(source: str, target: str, kind: str, **extra: Any) -> None:
        key = (source, target, kind)
        if key in seen:
            return
        seen.add(key)
        links.append({"source": source, "target": target, "type": kind, **extra})

    for node in nodes:
        if node["type"] != "memory":
            continue
        for path in node.get("groups") or []:
            if any(item["id"] == f"grp:{path}" for item in nodes):
                link(node["id"], f"grp:{path}", "group", weight=0.35)
    for item in mentions:
        link(f"mem:{item['memory']}", f"ent:{item['entity']}", "mention", weight=0.25)
    for item in relations:
        link(
            f"ent:{item['src_id']}",
            f"ent:{item['dst_id']}",
            "relation",
            weight=float(item["weight"]),
            predicate=item["predicate"],
            status=item["status"],
        )
    for row in trait_rows:
        try:
            evidence = json.loads(row["evidence_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            evidence = []
        if isinstance(evidence, list):
            for memory_id in evidence[:8]:
                if isinstance(memory_id, str) and memory_id in memory_ids:
                    link(f"trait:{row['id']}", f"mem:{memory_id}", "evidence", weight=0.3)

    # 记忆之间的关联：相似文本 + 共同实体 + 同组相邻。
    # 0 条 relation 的真实库也能长出神经网络，而不是只剩分组线。
    memory_nodes = [node for node in nodes if node["type"] == "memory"]
    similar_count = 0
    if len(memory_nodes) >= 2:
        grams: dict[str, set[str]] = {}
        for node in memory_nodes:
            text = node.get("text") or node.get("label") or ""
            grams[node["id"]] = char_ngrams(text, 3) or char_ngrams(text, 2)
        df: Counter[str] = Counter()
        for values in grams.values():
            df.update(values)
        cutoff = max(6, int(len(memory_nodes) * 0.18))
        postings: dict[str, list[str]] = defaultdict(list)
        for node_id, values in grams.items():
            for gram in values:
                if 2 <= df[gram] <= cutoff:
                    postings[gram].append(node_id)
        candidates: Counter[tuple[str, str]] = Counter()
        for ids in postings.values():
            if len(ids) < 2 or len(ids) > 18:
                continue
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    a, b = ids[i], ids[j]
                    if a == b:
                        continue
                    candidates[(min(a, b), max(a, b))] += 1
        scored: list[tuple[float, str, str]] = []
        for (a, b), overlap in candidates.items():
            union = len(grams[a]) + len(grams[b]) - overlap
            if union <= 0:
                continue
            sim = overlap / union
            if sim >= 0.16:
                scored.append((sim, a, b))
        scored.sort(reverse=True)
        degree: Counter[str] = Counter()
        for sim, a, b in scored:
            if degree[a] >= 3 or degree[b] >= 3:
                continue
            link(a, b, "similar", weight=round(sim, 3))
            degree[a] += 1
            degree[b] += 1
            similar_count += 1

    by_entity: dict[str, list[str]] = defaultdict(list)
    for item in mentions:
        by_entity[f"ent:{item['entity']}"].append(f"mem:{item['memory']}")
    co_count = 0
    for ids in by_entity.values():
        unique = list(dict.fromkeys(ids))[:8]
        for i in range(len(unique)):
            for j in range(i + 1, len(unique)):
                a, b = unique[i], unique[j]
                link(min(a, b), max(a, b), "co_mention", weight=0.35)
                co_count += 1

    by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in memory_nodes:
        for path in node.get("groups") or []:
            by_group[path].append(node)
    group_chain_count = 0
    for path, members in by_group.items():
        members.sort(key=lambda n: (n.get("day") or "", n.get("seq") or 0))
        for left, right in zip(members, members[1:]):
            link(min(left["id"], right["id"]), max(left["id"], right["id"]), "group_chain", weight=0.22)
            group_chain_count += 1

    quality = _latest_quality(db)
    sleep = db.conn.execute(
        "SELECT day_key,status,finished_at FROM sleep_run WHERE subject_id=? "
        "ORDER BY started_at DESC LIMIT 1",
        (subject,),
    ).fetchone()
    stats = db.conn.execute(
        "SELECT status, COUNT(*) c FROM memory WHERE subject_id=? GROUP BY status", (subject,)
    ).fetchall()
    kinds = db.conn.execute(
        "SELECT kind, COUNT(*) c FROM memory WHERE subject_id=? GROUP BY kind", (subject,)
    ).fetchall()

    return {
        "meta": {
            "title": "PMem · 海马体星图",
            "subtitle": "记忆不是被存下来的，是被不断重塑的。",
            "generated_at": now_iso(),
            "subject": display,
            "redacted": bool(redact),
            "truncated": total_memories > len(mem_rows),
            "shown": len(mem_rows),
            "total": total_memories,
            "quality": quality,
            "last_sleep": dict(sleep) if sleep else None,
            "counts": {
                "memories": len(mem_rows),
                "entities": len(entity_rows),
                "relations": len(relations),
                "groups": len(group_rows),
                "traits": len(trait_rows),
                "mentions": len(mentions),
                "similar": sum(1 for l in links if l["type"] == "similar"),
                "co_mention": sum(1 for l in links if l["type"] == "co_mention"),
                "group_chain": sum(1 for l in links if l["type"] == "group_chain"),
                "links": len(links),
            },
            "by_status": {row["status"]: row["c"] for row in stats},
            "by_kind": {row["kind"]: row["c"] for row in kinds},
            "entity_total": len(entity_rows),
            "mentioned_entities": len(mentioned_entities),
        },
        "nodes": nodes,
        "links": links,
        "dust": _dust(db, subject=subject, memory_ids=memory_ids, limit=limit),
        "history": _history(db, subject=subject),
    }


def _history(db: Database, *, subject: str = "self") -> list[dict[str, Any]]:
    """每天写了多少条记忆。时间回放按这个放星。"""
    rows = db.conn.execute(
        "SELECT day_key, COUNT(*) c, MIN(seq) a, MAX(seq) b FROM memory "
        "WHERE subject_id=? AND status<>'deleted' AND day_key IS NOT NULL "
        "GROUP BY day_key ORDER BY day_key",
        (subject,),
    ).fetchall()
    total = 0
    out: list[dict[str, Any]] = []
    for row in rows:
        total += int(row["c"])
        out.append(
            {
                "day": row["day_key"],
                "count": int(row["c"]),
                "cumulative": total,
                "seq_from": int(row["a"]),
                "seq_to": int(row["b"]),
            }
        )
    return out


_DUST_KINDS = ("event", "recall", "metric", "reflect", "sleep")
_DUST_OPS = {
    "memory.add": "写下",
    "memory.update": "改写",
    "memory.delete": "删除",
    "memory.grade": "定级",
    "surface.emit": "浮现",
    "recall.query": "回想",
    "sleep.run": "睡眠",
    "relation.add": "连起",
    "entity.merge": "合并",
    "trait.update": "形成",
    "feedback.record": "反馈",
}


def _dust(
    db: Database,
    *,
    subject: str = "self",
    memory_ids: set[str],
    limit: int = 3000,
) -> list[dict[str, Any]]:
    """把「不是记忆、但确实活过」的痕迹收成星尘。

    每一条都带 ``k``（哪一类）、``t``（什么时候）、``m``（贴着哪条记忆）、
    ``l``（一句话说明）。前端据此决定颜色、衰减和能不能点开。
    """
    out: list[dict[str, Any]] = []
    cap = max(200, min(6000, limit * 2))

    # 1) 事件流：写入、定级、浮现、睡眠……星图的密度主要来自它。
    rows = db.conn.execute(
        "SELECT seq,ts,actor,op,target_tbl,target_id,payload_json FROM event_log "
        "WHERE subject_id IS NULL OR subject_id=? ORDER BY seq DESC LIMIT ?",
        (subject, cap),
    ).fetchall()
    for row in rows:
        target = row["target_id"] if row["target_tbl"] == "memory" else None
        if target and target not in memory_ids:
            target = None
        op = row["op"] or ""
        out.append(
            {
                "id": f"ev:{row['seq']}",
                "k": "event",
                "t": row["ts"],
                "m": target,
                "l": f"{_DUST_OPS.get(op, op)} · {row['actor'] or ''}".strip(" ·"),
            }
        )

    # 2) 召回：一次回想就是一次闪光。
    rows = db.conn.execute(
        "SELECT id,ts,query,returned_json FROM recall_log WHERE subject_id=? "
        "ORDER BY ts DESC LIMIT ?",
        (subject, cap),
    ).fetchall()
    for row in rows:
        target = None
        try:
            returned = json.loads(row["returned_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            returned = []
        if isinstance(returned, dict):
            returned = returned.get("items") or returned.get("ids") or []
        if isinstance(returned, list):
            for item in returned[:6]:
                candidate = (
                    item.get("id") if isinstance(item, dict) else item if isinstance(item, str) else None
                )
                if isinstance(candidate, str):
                    candidate = candidate[4:] if candidate.startswith("mem:") else candidate
                    if candidate in memory_ids:
                        target = candidate
                        break
        out.append(
            {
                "id": f"rc:{row['id']}",
                "k": "recall",
                "t": row["ts"],
                "m": target,
                "l": f"回想 · {_short(row['query'] or '', 40)}",
            }
        )

    # 3) 指标：每一天的体重、心率。
    rows = db.conn.execute(
        "SELECT ts,day_key,name,value FROM metrics ORDER BY ts DESC LIMIT ?", (cap,)
    ).fetchall()
    for index, row in enumerate(rows):
        out.append(
            {
                "id": f"mt:{index}:{row['ts']}",
                "k": "metric",
                "t": row["ts"],
                "m": None,
                "l": f"{row['name']} {float(row['value']):.2f} · {row['day_key']}",
            }
        )

    # 4) 反思与睡眠：少，但每一粒都该亮。
    for row in db.conn.execute(
        "SELECT id,period,created_at FROM reflection WHERE subject_id=? "
        "ORDER BY created_at DESC LIMIT 120",
        (subject,),
    ).fetchall():
        out.append(
            {
                "id": f"rf:{row['id']}",
                "k": "reflect",
                "t": row["created_at"],
                "m": None,
                "l": f"反思 · {row['period']}",
            }
        )
    for row in db.conn.execute(
        "SELECT id,day_key,started_at,status FROM sleep_run WHERE subject_id=? "
        "ORDER BY started_at DESC LIMIT 120",
        (subject,),
    ).fetchall():
        out.append(
            {
                "id": f"sl:{row['id']}",
                "k": "sleep",
                "t": row["started_at"],
                "m": None,
                "l": f"睡眠 · {row['day_key']} · {row['status']}",
            }
        )
    return out



def layout(graph: dict[str, Any], prev: dict[str, Any] | None = None) -> dict[str, Any]:
    """深空布局：分组定扇区、时间定半径、相似度做局部松弛。

    实现见 ``starmap_space``；``starmap_brain`` 只剩下 SVG / PNG 的 2D 输出。
    """
    return starmap_space.layout(graph, prev=prev)


def render_svg(graph: dict[str, Any]) -> str:
    """静态 SVG（实现见 ``starmap_brain``）。"""
    return starmap_brain.render_svg(graph)


def render_png(graph: dict[str, Any], path: str | Path, *, size: int = 1400) -> dict[str, Any]:
    """可选 PNG（实现见 ``starmap_brain``）。"""
    return starmap_brain.render_png(graph, path, size=size)


def build(
    db: Database,
    *,
    subject: str = "self",
    redact: bool = True,
    limit: int = 3000,
    prev: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """采集 + 布点。``prev`` 给上一次的星图就切到增量布点（旧星不动）。"""
    return layout(collect(db, subject=subject, redact=redact, limit=limit), prev=prev)


def write(
    db: Database,
    *,
    out: str | Path = "data/starmap",
    subject: str = "self",
    redact: bool = True,
    png: bool = True,
    limit: int = 3000,
    incremental: bool = False,
) -> dict[str, Any]:
    """写星图。

    ``incremental=True``（M9.7c）：读上一次的 JSON 当参照，**已经画过的记忆
    位置不动**，只给新记忆和的新星尘找位置；而且只写 JSON。
    页面读的就是 JSON；HTML / SVG / PNG 是离线产物，每 1.5 秒重画一遍
    一份 370 KB 的单文件没有任何意义。
    """
    from .starmap_html import render_html

    target = Path(out)
    if target.suffix.lower() in (".html", ".htm", ".svg", ".json", ".png"):
        target = target.with_suffix("")
    target.parent.mkdir(parents=True, exist_ok=True)
    html_path = target.with_suffix(".html")
    svg_path = target.with_suffix(".svg")
    json_path = target.with_suffix(".json")

    prev: dict[str, Any] | None = None
    if incremental:
        try:
            candidate = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            candidate = None
        if isinstance(candidate, dict) and candidate.get("nodes"):
            prev = candidate

    graph = build(db, subject=subject, redact=redact, limit=limit, prev=prev)
    partial = bool(incremental and prev is not None)
    png_result: dict[str, Any] = {"ok": False, "skipped": True}
    if not partial:
        html_path.write_text(render_html(graph), encoding="utf-8")
        svg_path.write_text(render_svg(graph), encoding="utf-8")
        if png:
            png_result = render_png(graph, target.with_suffix(".png"))
    json_path.write_text(
        json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "ok": True,
        "html": None if partial else str(html_path),
        "svg": None if partial else str(svg_path),
        "json": str(json_path),
        "png": png_result,
        "incremental": partial,
        "meta": graph["meta"],
    }


__all__ = ["collect", "layout", "build", "render_svg", "render_png", "write"]
