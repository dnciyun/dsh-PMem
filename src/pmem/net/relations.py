"""关系抽取、落边、传递闭包与休眠（M3 / 03 §8.2–8.4）。

三条硬规则，都是为了"宁可漏，不可错"：

1. **宾语必须能定型**（词表命中或已有实体），否则不建边。未知宾语先当没看见，
   等它再次出现、或人工录入后再补——比造一条类型不明的边安全得多。
2. **问句、第三人称、否定**不产生正向边。中文没有形态标记，问句和陈述
   只差一个"吗"，不显式排除就会把"你喜欢咖啡吗"记成"我喜欢咖啡"。
3. **同一条边只增证据，不重复建行**：`uq_relation` 保证 (src,dst,predicate)
   只有一个活跃行，重复出现只提权重与置信度。
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ..base import events
from ..base.clock import now_iso
from ..base.ids import ulid
from ..memory.text import _norm_obj, normalize
from . import entities

# 宾语：不含标点/换行，1–24 字；非贪婪交给 works_at 的"夹在中间"句式
_OBJ = r"(?P<obj>[^，。；！？,.;!?\n]{1,24})"
_OBJ_LAZY = r"(?P<obj>[^，。；！？,.;!?\n]{1,24}?)"

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("dislikes", re.compile(rf"(?:不喜欢|不太喜欢|不喜欢|讨厌|受不了|反感|厌恶){_OBJ}")),
    ("likes", re.compile(rf"(?<![不没])(?:喜欢|爱|偏爱|迷上|爱好){_OBJ}")),
    ("lives_in", re.compile(rf"(?:住在|搬到|定居在?|安居在?){_OBJ}")),
    ("works_at", re.compile(rf"(?:在|于){_OBJ_LAZY}(?:工作|上班|任职)")),
    ("studies_at", re.compile(rf"(?:在|于){_OBJ_LAZY}(?:上学|读书|读研|读博|毕业)")),
    ("uses", re.compile(rf"(?:使用|在用|用){_OBJ}")),
    ("works_on", re.compile(rf"(?:正在)?(?:开发|(?<![喜喜爱讨厌欢])做|(?<![喜喜爱讨厌欢])研究|(?<![喜喜爱讨厌欢])写){_OBJ}")),
)

WEIGHTS = {
    "likes": 0.7,
    "dislikes": 0.7,
    "works_at": 0.75,
    "lives_in": 0.8,
    "uses": 0.6,
    "works_on": 0.8,
    "part_of": 0.7,
    "studies_at": 0.75,
    "related_to": 0.4,
}

_QUESTION_TAIL = re.compile(r"(?:吗|呢|么|嘛)\s*[？?]?$")
_THIRD_PERSON = re.compile(r"[你您他她它]")
_OTHER_SUBJECT = re.compile(r"(朋友|同事|同学|老板|领导|家人|老婆|老公|孩子|父母|室友)")
_CLAUSE = re.compile(r"[^，。；！？,.;!?\n]+")
_FIRST_PERSON = {"我", "我自己", "自己", "用户", "主人"}
_ADVERB_TAIL = ("目前", "现在", "最近", "以前", "之前", "已经", "一直", "通常", "平时", "就", "还")

# 谓词的宾语类型约束（与 relation_type.domain/range 对齐）。
# 不做这一步，"在咖啡工作"这种错位句式会被记成 works_at。
_DOMAIN = {
    "works_at": {"org"},
    "studies_at": {"org"},
    "lives_in": {"place"},
    "uses": {"tool", "org"},
    "works_on": {"concept", "goal", "tool"},
    "likes": {"concept", "tool", "org", "place"},
    "dislikes": {"concept", "tool", "org", "place"},
}


def clauses(text: str) -> list[tuple[int, str]]:
    return [(m.start(), m.group(0)) for m in _CLAUSE.finditer(text)]


def _is_question(clause: str) -> bool:
    stripped = clause.strip()
    return bool(_QUESTION_TAIL.search(stripped) or stripped.endswith(("?", "？")))


def _holder(
    prefix: str,
    mentions: list[dict[str, Any]],
    offset: int,
    clause_start: int,
) -> dict[str, Any] | None:
    """确定一句话的主语。

    优先级：**同一从句内**紧邻谓词的人名 mention > 第一人称 > 省略（默认"我"）。
    出现第三人称代词且没有名字时返回 None（**宁可不记，也不记到"我"头上**）。
    """
    for mention in reversed(mentions):
        if mention.get("entity_id") is None:
            continue
        if mention["type"] != "person":
            continue
        if mention["start"] < clause_start:
            continue  # 上一句的人名不能当这一句的主语
        if mention["end"] <= offset and offset - mention["end"] <= 8:
            return {
                "entity_id": mention["entity_id"],
                "name": mention["surface"],
                "type": "person",
            }
    if _THIRD_PERSON.search(prefix):
        return None
    if _OTHER_SUBJECT.search(prefix):
        return None  # "我朋友在 X 工作" 不是"我"在工作
    cleaned = prefix.strip(" \t，,。.；;：:、!！?？")
    for adverb in sorted(_ADVERB_TAIL, key=len, reverse=True):
        if cleaned.endswith(adverb):
            cleaned = cleaned[: -len(adverb)].strip()
    if not cleaned or cleaned in _FIRST_PERSON or "我" in cleaned:
        return {"entity_id": None, "name": "我", "type": "person"}
    return None


def _object_mention(
    mentions: list[dict[str, Any]], start: int, end: int
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for mention in mentions:
        if mention.get("entity_id") is None:
            continue
        if mention["start"] >= start - 1 and mention["end"] <= end + 1:
            if best is None or (mention["end"] - mention["start"]) > (best["end"] - best["start"]):
                best = mention
    return best


def extract(
    text: str,
    mentions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """从一条记忆里抽候选三元组（不写库）。

    返回 ``{src, predicate, dst, weight, quote, offset}``；``src`` 为
    ``{"entity_id", "name", "type"}``，``dst`` 为实体 id + surface。
    """
    text = normalize(text)
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for clause_start, clause in clauses(text):
        if _is_question(clause):
            continue
        matches: list[tuple[int, str, re.Match[str]]] = []
        for predicate, pattern in PATTERNS:
            for match in pattern.finditer(clause):
                matches.append((match.start("obj"), predicate, match))
        # 同一宾语被多个模板命中时，只取最靠左/最长的那个（"不喜欢"优先于"喜欢"）
        matches.sort(key=lambda item: (item[0], -len(item[2].group("obj"))))
        for _, predicate, match in matches:
            obj = _norm_obj(match.group("obj"))
            if not obj or len(obj) > 24:
                continue
            obj_start = clause_start + match.start("obj")
            obj_end = obj_start + len(match.group("obj"))
            mention = _object_mention(mentions, obj_start, obj_end)
            dst_id = mention["entity_id"] if mention else None
            dst_type = mention["type"] if mention else None
            surface = mention["surface"] if mention else obj
            if dst_id is None:
                from . import lexicon

                dst_type = lexicon.type_of(obj)
                if dst_type is None:
                    continue  # 未知宾语：不建边
                surface = obj
            holder = _holder(clause[: match.start()], mentions, clause_start + match.start(), clause_start)
            if holder is None:
                continue
            allowed = _DOMAIN.get(predicate)
            if allowed and dst_type not in allowed:
                continue
            holder_key = holder["entity_id"] or "self"
            key = (holder_key, predicate, surface)
            if key in seen:
                continue
            seen.add(key)
            found.append(
                {
                    "src": holder,
                    "predicate": predicate,
                    "dst": {"entity_id": dst_id, "name": surface, "type": dst_type},
                    "quote": clause.strip()[:160],
                    "offset": clause_start + match.start(),
                    "weight": WEIGHTS.get(predicate, 0.5),
                }
            )
    return found


def resolve_src(db: Any, subject: str, src: dict[str, Any]) -> Any:
    if src.get("entity_id"):
        row = db.conn.execute(
            "SELECT * FROM entity WHERE id=? AND status='active'", (src["entity_id"],)
        ).fetchone()
        if row is not None:
            return row
    if src.get("name") in ("我", "自己", "用户"):
        return entities.ensure_self(db, subject)
    result = entities.resolve(
        db, subject=subject, name=src["name"], type_hint="person", confidence=0.55
    )
    if result["entity"] is None:
        return None
    return result["entity"]


def resolve_dst(db: Any, subject: str, dst: dict[str, Any]) -> Any:
    if dst.get("entity_id"):
        row = db.conn.execute(
            "SELECT * FROM entity WHERE id=? AND status='active'", (dst["entity_id"],)
        ).fetchone()
        if row is not None:
            return row
    result = entities.resolve(
        db,
        subject=subject,
        name=dst["name"],
        type_hint=dst.get("type"),
        confidence=0.85,
    )
    return result["entity"]


def upsert(
    db: Any,
    *,
    subject: str,
    src_id: str,
    dst_id: str,
    predicate: str,
    weight: float,
    confidence: float,
    memory_id: str,
    quote: str,
    valid_from: str | None = None,
    valid_to: str | None = None,
    derivation: str = "extracted:T0",
) -> str | None:
    """写入/加强一条边。返回关系 id；``None`` 表示被忽略（自环/未知谓词）。"""
    if src_id == dst_id:
        return None
    meta = db.conn.execute(
        "SELECT max_weight FROM relation_type WHERE predicate=?", (predicate,)
    ).fetchone()
    if meta is None:
        return None
    weight = max(0.0, min(weight, float(meta["max_weight"])))
    ts = now_iso()
    existing = db.conn.execute(
        "SELECT * FROM relation WHERE src_id=? AND dst_id=? AND predicate=? "
        "AND status IN ('active','contested','dormant') "
        "ORDER BY CASE status WHEN 'dormant' THEN 1 ELSE 0 END LIMIT 1",
        (src_id, dst_id, predicate),
    ).fetchone()
    if existing is not None:
        evidence = _evidence_ids(existing["evidence_json"])
        if memory_id in evidence:
            return existing["id"]
        evidence.append(memory_id)
        new_conf = min(0.95, max(existing["confidence"], confidence) + 0.03)
        new_weight = min(float(meta["max_weight"]), max(existing["weight"], weight))
        db.conn.execute(
            "UPDATE relation SET weight=?, confidence=?, evidence_json=?, updated_at=?, "
            "last_support_at=?, status='active', "
            "valid_from=COALESCE(?, valid_from), valid_to=COALESCE(?, valid_to) WHERE id=?",
            (
                new_weight,
                new_conf,
                _dump_evidence(evidence, quote),
                ts,
                ts,
                valid_from,
                valid_to,
                existing["id"],
            ),
        )
        return existing["id"]
    relation_id = ulid()
    db.conn.execute(
        "INSERT INTO relation(id, src_id, dst_id, predicate, weight, confidence, provenance, "
        "evidence_json, valid_from, valid_to, created_at, updated_at, last_support_at, status, derivation) "
        "VALUES(?,?,?,?,?,?, 'inferred', ?,?,?,?,?,?, 'active', ?)",
        (
            relation_id,
            src_id,
            dst_id,
            predicate,
            weight,
            max(0.0, min(1.0, confidence)),
            _dump_evidence([memory_id], quote),
            valid_from,
            valid_to,
            ts,
            ts,
            ts,
            derivation,
        ),
    )
    return relation_id


def _evidence_ids(raw: str) -> list[str]:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [str(item["memory_id"]) for item in data if isinstance(item, dict) and item.get("memory_id")]


def _dump_evidence(ids: list[str], quote: str) -> str:
    return json.dumps(
        [{"memory_id": mid, "quote": quote[:160]} for mid in ids],
        ensure_ascii=False,
        sort_keys=True,
    )


def ingest_memory(
    db: Any,
    *,
    subject: str,
    memory_id: str,
    memory_seq: int,
    text_norm: str,
    valid_from: str | None = None,
    valid_to: str | None = None,
) -> dict[str, Any]:
    """一条记忆入库后的网络沉淀：实体 mention + 关系边。"""
    mention_rows = entities.ingest(
        db, subject=subject, memory_seq=memory_seq, text=text_norm
    )
    candidates = extract(text_norm, mention_rows)
    created = 0
    reinforced = 0
    for item in candidates:
        src = resolve_src(db, subject, item["src"])
        dst = resolve_dst(db, subject, item["dst"])
        if src is None or dst is None:
            continue
        before = db.conn.execute(
            "SELECT 1 FROM relation WHERE src_id=? AND dst_id=? AND predicate=? "
            "AND status IN ('active','contested','dormant')",
            (src["id"], dst["id"], item["predicate"]),
        ).fetchone()
        relation_id = upsert(
            db,
            subject=subject,
            src_id=src["id"],
            dst_id=dst["id"],
            predicate=item["predicate"],
            weight=item["weight"],
            confidence=0.7,
            memory_id=memory_id,
            quote=item["quote"],
            valid_from=valid_from,
            valid_to=valid_to,
        )
        if relation_id is None:
            continue
        if before:
            reinforced += 1
        else:
            created += 1
    if created or reinforced:
        events.append(
            db.conn,
            actor="system:net",
            op="net.extract",
            target_tbl="memory",
            target_id=memory_id,
            subject_id=subject,
            payload={"relations_created": created, "relations_reinforced": reinforced,
                     "entities": len(mention_rows)},
        )
    return {
        "entities": len(mention_rows),
        "relations_created": created,
        "relations_reinforced": reinforced,
    }


# ---------------------------------------------------------------- 共现 / 传递 / 休眠
def cooccurrence(
    db: Any,
    *,
    subject: str = "self",
    min_count: int = 5,
    min_pmi: float = 1.5,
    max_mentions_per_memory: int = 12,
) -> dict[str, Any]:
    """共现 PMI → ``related_to`` 弱边（03 §8.2）。

    只统计同一记忆里共同出现的实体对，天然有窗口；不跨记忆硬凑。
    """
    rows = db.conn.execute(
        "SELECT em.memory_seq, em.entity_id, e.type, m.id AS memory_id FROM entity_mention em "
        "JOIN entity e ON e.id=em.entity_id JOIN memory m ON m.seq=em.memory_seq "
        "WHERE e.subject_id=? AND e.status='active' "
        "ORDER BY em.memory_seq, em.seq",
        (subject,),
    ).fetchall()
    per_memory: dict[int, list[str]] = {}
    seq_to_memory: dict[int, str] = {}
    for row in rows:
        seq_to_memory[row["memory_seq"]] = row["memory_id"]
        bucket = per_memory.setdefault(row["memory_seq"], [])
        if row["entity_id"] not in bucket:
            bucket.append(row["entity_id"])
        if len(bucket) >= max_mentions_per_memory:
            continue
    total = db.conn.execute(
        "SELECT COUNT(*) c FROM memory WHERE subject_id=? AND status<>'deleted'", (subject,)
    ).fetchone()["c"]
    if total == 0:
        return {"pairs": 0, "created": 0, "skipped_low": 0}

    pair_count: dict[tuple[str, str], int] = {}
    pair_memories: dict[tuple[str, str], list[str]] = {}
    df: dict[str, int] = {}
    for seq, ids in per_memory.items():
        for eid in ids:
            df[eid] = df.get(eid, 0) + 1
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = sorted((ids[i], ids[j]))
                pair_count[(a, b)] = pair_count.get((a, b), 0) + 1
                memories = pair_memories.setdefault((a, b), [])
                if len(memories) < 20 and seq in seq_to_memory:
                    memories.append(seq_to_memory[seq])

    created = 0
    skipped = 0
    for (a, b), count in pair_count.items():
        if count < min_count:
            skipped += 1
            continue
        pmi = math.log((count * total) / (df[a] * df[b]))
        if pmi < min_pmi:
            skipped += 1
            continue
        weight = max(0.0, min(0.4, (pmi - 1.0) / 4.0))
        exists = db.conn.execute(
            "SELECT id FROM relation WHERE src_id=? AND dst_id=? AND predicate='related_to' "
            "AND status IN ('active','dormant')",
            (a, b),
        ).fetchone()
        if exists:
            continue
        db.conn.execute(
            "INSERT INTO relation(id, src_id, dst_id, predicate, weight, confidence, provenance, "
            "evidence_json, created_at, updated_at, last_support_at, status, derivation) "
            "VALUES(?,?,?, 'related_to', ?, 0.5, 'inferred', ?,?,?,?, 'active', 'pmi')",
            (
                ulid(),
                a,
                b,
                weight,
                json.dumps(
                    [{"memory_id": mid} for mid in pair_memories.get((a, b), [])],
                    ensure_ascii=False,
                ),
                now_iso(),
                now_iso(),
                now_iso(),
            ),
        )
        created += 1
    return {"pairs": len(pair_count), "created": created, "skipped_low": skipped}


def transitive_closure(db: Any, *, subject: str = "self", max_new: int = 5000) -> dict[str, Any]:
    """只对 ``transitive=1`` 的谓词做 2 跳闭包；3 跳不做（03 §8.3）。"""
    types = db.conn.execute(
        "SELECT predicate, max_weight FROM relation_type WHERE transitive=1"
    ).fetchall()
    created = 0
    skipped_existing = 0
    for row in types:
        predicate = row["predicate"]
        max_weight = float(row["max_weight"])
        edges = db.conn.execute(
            "SELECT src_id, dst_id, weight, confidence, evidence_json FROM relation "
            "WHERE predicate=? AND status='active'",
            (predicate,),
        ).fetchall()
        out: dict[str, list[Any]] = {}
        inn: dict[str, list[Any]] = {}
        for edge in edges:
            out.setdefault(edge["src_id"], []).append(edge)
            inn.setdefault(edge["dst_id"], []).append(edge)
        for mid, lefts in out.items():
            for left in lefts:
                for right in inn.get(left["dst_id"], []):
                    if right["dst_id"] == mid:
                        continue
                    if created >= max_new:
                        return {"created": created, "skipped_existing": skipped_existing, "capped": True}
                    exists = db.conn.execute(
                        "SELECT 1 FROM relation WHERE src_id=? AND dst_id=? AND predicate=? "
                        "AND status IN ('active','contested')",
                        (mid, right["dst_id"], predicate),
                    ).fetchone()
                    if exists:
                        skipped_existing += 1
                        continue
                    weight = min(max_weight, left["weight"] * right["weight"] * 0.7)
                    confidence = min(left["confidence"], right["confidence"]) * 0.8
                    evidence = sorted(set(_evidence_ids(left["evidence_json"]) + _evidence_ids(right["evidence_json"])))
                    db.conn.execute(
                        "INSERT INTO relation(id, src_id, dst_id, predicate, weight, confidence, provenance, "
                        "evidence_json, created_at, updated_at, last_support_at, status, derivation) "
                        "VALUES(?,?,?,?,?,?, 'inferred', ?,?,?,?, 'active', 'transitive:2hop')",
                        (
                            ulid(),
                            mid,
                            right["dst_id"],
                            predicate,
                            max(0.0, weight),
                            max(0.0, confidence),
                            json.dumps([{"memory_id": mid_} for mid_ in evidence], ensure_ascii=False),
                            now_iso(),
                            now_iso(),
                            now_iso(),
                        ),
                    )
                    created += 1
    return {"created": created, "skipped_existing": skipped_existing, "capped": False}


def tick(
    db: Any,
    *,
    subject: str = "self",
    days: int = 90,
    weight: float = 0.3,
    now: str | None = None,
) -> dict[str, Any]:
    """关系休眠：90 天无新证据且权重低 → dormant；不删除，可被新证据唤醒。"""
    current = now or now_iso()
    cutoff = _shift(current, -days)
    rows = db.conn.execute(
        "SELECT id FROM relation WHERE status='active' AND weight < ? "
        "AND (last_support_at IS NULL OR last_support_at < ?)",
        (weight, cutoff),
    ).fetchall()
    for row in rows:
        db.conn.execute("UPDATE relation SET status='dormant', updated_at=? WHERE id=?", (current, row["id"]))
    if rows:
        events.append(
            db.conn,
            actor="system:net",
            op="net.dormant",
            target_tbl="relation",
            target_id=None,
            subject_id=subject,
            payload={"count": len(rows), "cutoff": cutoff},
        )
    return {"dormant": len(rows), "cutoff": cutoff}


def _shift(iso: str, days: int) -> str:
    from ..base.clock import parse_iso, to_iso

    try:
        dt = parse_iso(iso)
    except ValueError:
        dt = datetime.now(timezone.utc)
    return to_iso(dt + timedelta(days=days))


def add_edge(
    db: Any,
    *,
    subject: str,
    src_id: str,
    dst_id: str,
    predicate: str,
    weight: float | None = None,
    evidence: list[str] | None = None,
    provenance: str = "user_explicit",
) -> dict[str, Any]:
    """人工/接口写入一条边（04 §5.6 的 ``action=add``）。"""
    if src_id == dst_id:
        raise ValueError("不允许自环")
    meta = db.conn.execute(
        "SELECT max_weight FROM relation_type WHERE predicate=?", (predicate,)
    ).fetchone()
    if meta is None:
        raise KeyError(f"未知谓词 {predicate}")
    max_weight = float(meta["max_weight"])
    value = max_weight if weight is None else max(0.0, min(float(weight), max_weight))
    ids = [str(mid) for mid in (evidence or []) if mid]
    evidence_items = [{"memory_id": mid} for mid in ids] or [{"manual": True}]
    evidence_json = json.dumps(evidence_items, ensure_ascii=False)
    ts = now_iso()
    existing = db.conn.execute(
        "SELECT * FROM relation WHERE src_id=? AND dst_id=? AND predicate=? "
        "AND status IN ('active','contested','dormant') "
        "ORDER BY CASE status WHEN 'dormant' THEN 1 ELSE 0 END LIMIT 1",
        (src_id, dst_id, predicate),
    ).fetchone()
    if existing is not None:
        merged = _evidence_ids(existing["evidence_json"])
        for mid in ids:
            if mid not in merged:
                merged.append(mid)
        new_weight = min(max_weight, max(existing["weight"], value))
        new_conf = min(0.95, max(existing["confidence"], 0.85) + 0.03)
        db.conn.execute(
            "UPDATE relation SET weight=?, confidence=?, evidence_json=?, updated_at=?, "
            "last_support_at=?, status='active', provenance=? WHERE id=?",
            (
                new_weight,
                new_conf,
                json.dumps(
                    [{"memory_id": mid} for mid in merged] or [{"manual": True}],
                    ensure_ascii=False,
                ),
                ts,
                ts,
                provenance,
                existing["id"],
            ),
        )
        return {"id": existing["id"], "created": False, "weight": new_weight, "confidence": new_conf}
    relation_id = ulid()
    db.conn.execute(
        "INSERT INTO relation(id, src_id, dst_id, predicate, weight, confidence, provenance, "
        "evidence_json, created_at, updated_at, last_support_at, status, derivation) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?, 'active', 'manual')",
        (relation_id, src_id, dst_id, predicate, value, 0.9, provenance, evidence_json, ts, ts, ts),
    )
    return {"id": relation_id, "created": True, "weight": value, "confidence": 0.9}


def reset(db: Any, *, subject: str = "self") -> dict[str, Any]:
    """清空该主体的派生网络（实体/别名/提及/关系），供重算。

    这是 P2「派生数据可整层重算」的实现：原文与事件日志不动。
    """
    with db.tx() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM entity WHERE subject_id=?", (subject,))]
        if ids:
            marks = ",".join("?" for _ in ids)
            conn.execute(
                f"DELETE FROM relation WHERE src_id IN ({marks}) OR dst_id IN ({marks})",
                tuple(ids) + tuple(ids),
            )
            conn.execute(f"DELETE FROM entity_mention WHERE entity_id IN ({marks})", tuple(ids))
            conn.execute(f"DELETE FROM entity_alias WHERE entity_id IN ({marks})", tuple(ids))
        conn.execute("DELETE FROM entity WHERE subject_id=?", (subject,))
        events.append(
            conn,
            actor="system:net",
            op="net.reset",
            target_tbl="entity",
            target_id=None,
            subject_id=subject,
            payload={"entities": len(ids)},
        )
    return {"entities": len(ids)}


def backfill(
    db: Any,
    *,
    subject: str = "self",
    limit: int | None = None,
    memory_ids: list[str] | None = None,
) -> dict[str, Any]:
    """对已存在的记忆补抽实体/关系（幂等：重复跑只增证据不建重复行）。"""
    sql = (
        "SELECT id, seq, text_norm, valid_from, valid_to FROM memory "
        "WHERE subject_id=? AND status<>'deleted'"
    )
    params: list[Any] = [subject]
    if memory_ids:
        sql += f" AND id IN ({','.join('?' for _ in memory_ids)})"
        params.extend(memory_ids)
    sql += " ORDER BY seq"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    rows = db.conn.execute(sql, params).fetchall()
    entities_total = 0
    created = 0
    reinforced = 0
    for row in rows:
        stats = ingest_memory(
            db,
            subject=subject,
            memory_id=row["id"],
            memory_seq=row["seq"],
            text_norm=row["text_norm"],
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
        )
        entities_total += stats["entities"]
        created += stats["relations_created"]
        reinforced += stats["relations_reinforced"]
    return {
        "scanned": len(rows),
        "mentions": entities_total,
        "relations_created": created,
        "relations_reinforced": reinforced,
    }


def stats(db: Any, *, subject: str = "self") -> dict[str, Any]:
    by_type = {
        row["type"]: row["c"]
        for row in db.conn.execute(
            "SELECT type, COUNT(*) c FROM entity WHERE subject_id=? AND status='active' GROUP BY type",
            (subject,),
        ).fetchall()
    }
    by_status = {
        row["status"]: row["c"]
        for row in db.conn.execute(
            "SELECT status, COUNT(*) c FROM relation GROUP BY status"
        ).fetchall()
    }
    by_pred = {
        row["predicate"]: row["c"]
        for row in db.conn.execute(
            "SELECT predicate, COUNT(*) c FROM relation WHERE status<>'retracted' GROUP BY predicate "
            "ORDER BY c DESC"
        ).fetchall()
    }
    mentions = db.conn.execute("SELECT COUNT(*) c FROM entity_mention").fetchone()["c"]
    entities_total = sum(by_type.values())
    relations_total = sum(by_status.values())
    return {
        "entities": entities_total,
        "entities_by_type": by_type,
        "mentions": mentions,
        "relations": relations_total,
        "relations_by_status": by_status,
        "relations_by_predicate": by_pred,
    }
