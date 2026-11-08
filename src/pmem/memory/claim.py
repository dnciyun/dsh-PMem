"""断言层（claim）：把「一段叙述」拆成可核对的最小事实，只做存与取。

铁律：
- claim 不是记忆的替代品，是**并存**的最小事实层（subject / predicate / object /
  polarity / status）；默认不参与检索（`claim.enabled=false` 时既有路径零变化）。
- 只降级不删除：软删只改 status，事件日志留墓碑（不含原文）。
- 同一 `(source_line, predicate, object, polarity)` 幂等：重跑只更新、不追加；
  同一 `(subject, predicate, object, polarity, status)` 合并成一条，带 use_count/last_seen。
- 版本链（`claim.version.enabled`）：同一 subject+predicate+kind 下 object/polarity 变了
  就开新 seq，旧行 superseded，`claim_link` 记改口原因；默认关。
"""

from __future__ import annotations

import json
from typing import Any

from ..base import events
from ..base.clock import now_iso
from ..base.ids import ulid
from ..base.errors import ValidationError
from .text import normalize, tokenize

CLAIM_KINDS = ("fact", "preference", "date", "third_party", "boundary")
# claim.create 的结构守卫（任务要求：长度 + 标点；不合规直接拒绝，不做猜测性规范化）。
_SENTENCE_PUNCT = "。！？；\n，,"
_PREDICATE_BAD = {"别", "不", "没", "也", "就", "还", "很", "太", "都", "才", "又", "再", "更", "最"}
_MAX_PREDICATE = 8
_MAX_OBJECT = 30
CLAIM_STATUSES = (
    "happened",
    "planned",
    "hypothetical",
    "rumored",
    "unknown",
    "candidate",
)
# 可直接入库的「明确层」状态（candidate 也能存，但不进人物卡）。
EXPLICIT_STATUSES = ("happened", "planned", "hypothetical", "rumored")
LIVE_STATUSES = EXPLICIT_STATUSES + ("candidate",)


def _row(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["polarity"] = int(item.get("polarity") or 0)
    item["seq"] = int(item.get("seq") or 1)
    item["use_count"] = int(item.get("use_count") or 0)
    item["confidence"] = float(item.get("confidence") or 0.0)
    return item


def _clamp(value: Any, default: float = 0.8) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.0, min(1.0, number))


def _check_structure(predicate: str, object_: str) -> None:
    """create 层结构守卫：predicate ≤8 字且不是单个副词；object ≤30 字且不含句读。

    为什么在这层拦而不是只在规则层拦：host 抽取也会给出「别 / 一上来太熟」这种
    predicate/object 错位；结构守卫是最后一道，宁可直接拒绝也不把垃圾写进库。
    """
    predicate_text = str(predicate or "").strip()
    object_text = str(object_ or "").strip()
    if not predicate_text or len(predicate_text) > _MAX_PREDICATE:
        raise ValidationError(
            f"claim.predicate 必须非空且 ≤{_MAX_PREDICATE} 字", field="predicate"
        )
    if predicate_text in _PREDICATE_BAD:
        raise ValidationError("claim.predicate 不能是单个副词", field="predicate")
    if any(char in _SENTENCE_PUNCT for char in predicate_text):
        raise ValidationError("claim.predicate 不能含句读", field="predicate")
    if not object_text or len(object_text) > _MAX_OBJECT:
        raise ValidationError(f"claim.object 必须非空且 ≤{_MAX_OBJECT} 字", field="object")
    if any(char in _SENTENCE_PUNCT for char in object_text):
        raise ValidationError("claim.object 不能含句读", field="object")


def _validate(kind: str, status: str, polarity: Any, object_: str) -> tuple[str, str, int, str]:
    kind = str(kind or "").strip()
    if kind not in CLAIM_KINDS:
        raise ValidationError(f"claim.kind 只能是 {CLAIM_KINDS}", field="kind")
    status = str(status or "happened").strip()
    if status not in CLAIM_STATUSES:
        raise ValidationError(f"claim.status 只能是 {CLAIM_STATUSES}", field="status")
    try:
        polarity_int = int(polarity)
    except (TypeError, ValueError):
        raise ValidationError("claim.polarity 必须是 0/1", field="polarity") from None
    if polarity_int not in (0, 1):
        raise ValidationError("claim.polarity 必须是 0/1", field="polarity")
    object_text = str(object_ or "").strip()
    if not object_text:
        raise ValidationError("claim.object 不能为空", field="object")
    return kind, status, polarity_int, object_text


def _insert(
    db: Any,
    *,
    subject_id: str,
    kind: str,
    predicate: str,
    object_: str,
    polarity: int,
    status: str,
    event_date: str | None,
    mentioned_at: str,
    event_end: str | None,
    scope: str | None,
    confidence: float,
    source_line: str | None,
    source_turn: int | None,
    evidence_quote: str,
    extractor: str,
    claim_group: str | None,
    seq: int = 1,
    superseded_by: str | None = None,
) -> dict[str, Any]:
    claim_id = ulid()
    db.conn.execute(
        "INSERT INTO claim(id, subject_id, kind, predicate, object, polarity, status, "
        "event_date, mentioned_at, event_end, scope, claim_group, seq, superseded_by, "
        "confidence, source_line, source_turn, evidence_quote, extractor, schema_ver, "
        "use_count, last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            claim_id,
            subject_id,
            kind,
            predicate,
            object_,
            polarity,
            status,
            event_date,
            mentioned_at,
            event_end,
            scope,
            claim_group,
            seq,
            superseded_by,
            confidence,
            source_line,
            source_turn,
            evidence_quote,
            extractor,
            "1",
            1,
            mentioned_at,
        ),
    )
    return {"id": claim_id, "claim_group": claim_group, "seq": seq}


def _open_chain(
    db: Any, *, subject_id: str, kind: str, predicate: str
) -> dict[str, Any] | None:
    """同 subject+predicate+kind 下当前未 superseded 的那条（版本链的链头）。"""
    row = db.conn.execute(
        "SELECT * FROM claim WHERE subject_id=? AND kind=? AND predicate=? "
        "AND superseded_by IS NULL AND status NOT IN ('superseded','deleted') "
        "ORDER BY last_seen DESC, seq DESC LIMIT 1",
        (subject_id, kind, predicate),
    ).fetchone()
    return _row(row) if row is not None else None


def _touch(
    db: Any,
    existing: dict[str, Any],
    *,
    evidence_quote: str,
    confidence: float,
    mentioned_at: str,
    event_date: str | None,
    status: str,
    last_seen: str,
) -> None:
    """合并：只动计数/时间/证据，不新开行。"""
    incoming = evidence_quote.strip()
    old_quote = str(existing.get("evidence_quote") or "")
    # 证据片段只在「新的更短且仍被旧片段包含」时不换，其余情况保留最近一次说法。
    quote = incoming if incoming and incoming not in old_quote else old_quote or incoming
    db.conn.execute(
        "UPDATE claim SET use_count=use_count+1, last_seen=?, mentioned_at=?, "
        "confidence=MAX(confidence, ?), evidence_quote=?, "
        "event_date=COALESCE(event_date, ?), status=? WHERE id=?",
        (
            last_seen,
            mentioned_at,
            confidence,
            quote,
            event_date,
            status if status in LIVE_STATUSES else existing["status"],
            existing["id"],
        ),
    )


def create(
    db: Any,
    *,
    subject_id: str = "self",
    kind: str,
    predicate: str,
    object: str,
    polarity: Any = 1,
    status: str = "happened",
    event_date: str | None = None,
    mentioned_at: str | None = None,
    event_end: str | None = None,
    scope: str | None = None,
    confidence: Any = 0.8,
    source_line: str | None = None,
    source_turn: int | None = None,
    evidence_quote: str,
    extractor: str = "host",
    claim_group: str | None = None,
    version: bool = False,
    actor: str = "system:claim",
) -> dict[str, Any]:
    """写入一条 claim（幂等/合并/可选版本链）；返回 ``{action, claim, dropped?}``。"""
    kind, status, polarity_int, object_text = _validate(kind, status, polarity, object)
    predicate = str(predicate or "").strip()
    quote = str(evidence_quote or "").strip()
    if not predicate or not quote:
        raise ValidationError("claim.predicate/evidence_quote 不能为空", field="predicate")
    _check_structure(predicate, object_text)
    mentioned = str(mentioned_at or now_iso())
    confidence_value = _clamp(confidence)
    last_seen = now_iso()
    subject_text = str(subject_id or "self").strip() or "self"

    with db.tx() as conn:
        # 1) 幂等键：同一条原话 + 谓词 + 客体 + 极性 → 只更新。
        if source_line:
            dedup = db.conn.execute(
                "SELECT * FROM claim WHERE subject_id=? AND source_line=? AND predicate=? "
                "AND object=? AND polarity=? ORDER BY seq DESC LIMIT 1",
                (subject_text, source_line, predicate, object_text, polarity_int),
            ).fetchone()
            if dedup is not None:
                existing = _row(dedup)
                _touch(
                    db,
                    existing,
                    evidence_quote=quote,
                    confidence=confidence_value,
                    mentioned_at=mentioned,
                    event_date=event_date,
                    status=status,
                    last_seen=last_seen,
                )
                events.append(
                    conn,
                    actor=actor,
                    op="claim.update",
                    target_tbl="claim",
                    target_id=existing["id"],
                    subject_id=subject_text,
                    payload={"via": "dedup"},
                    idem_key=f"claim-update:{existing['id']}:{last_seen}",
                )
                return {"action": "updated", "claim_id": existing["id"]}

        # 2) 合并键：不同原话但同一事实 → 留一条，计数 +1。
        merged = db.conn.execute(
            "SELECT * FROM claim WHERE subject_id=? AND predicate=? AND object=? "
            "AND polarity=? AND status=? AND superseded_by IS NULL "
            "AND status NOT IN ('superseded','deleted') "
            "ORDER BY last_seen DESC LIMIT 1",
            (subject_text, predicate, object_text, polarity_int, status),
        ).fetchone()
        if merged is not None:
            existing = _row(merged)
            _touch(
                db,
                existing,
                evidence_quote=quote,
                confidence=confidence_value,
                mentioned_at=mentioned,
                event_date=event_date,
                status=status,
                last_seen=last_seen,
            )
            return {"action": "merged", "claim_id": existing["id"]}

        # 3) 版本链：同一 subject+predicate+kind 下 object/polarity 变了 → 开新 seq。
        chain = _open_chain(db, subject_id=subject_text, kind=kind, predicate=predicate) if version else None
        if chain is not None and (
            chain["object"] == object_text and int(chain["polarity"]) == polarity_int
        ):
            _touch(
                db,
                chain,
                evidence_quote=quote,
                confidence=confidence_value,
                mentioned_at=mentioned,
                event_date=event_date,
                status=status,
                last_seen=last_seen,
            )
            return {"action": "merged", "claim_id": chain["id"]}
        group = claim_group or (chain or {}).get("claim_group") or ulid()
        seq = 1
        if chain is not None and (
            chain["object"] != object_text or int(chain["polarity"]) != polarity_int
        ):
            group = chain.get("claim_group") or group
            seq = int(chain.get("seq") or 1) + 1
            reason = (
                "polarity_changed"
                if int(chain["polarity"]) != polarity_int
                else "object_changed"
            )
            inserted = _insert(
                db,
                subject_id=subject_text,
                kind=kind,
                predicate=predicate,
                object_=object_text,
                polarity=polarity_int,
                status=status,
                event_date=event_date,
                mentioned_at=mentioned,
                event_end=event_end,
                scope=scope,
                confidence=confidence_value,
                source_line=source_line,
                source_turn=source_turn,
                evidence_quote=quote,
                extractor=extractor,
                claim_group=group,
                seq=seq,
            )
            db.conn.execute(
                "UPDATE claim SET superseded_by=?, status='superseded' WHERE id=?",
                (inserted["id"], chain["id"]),
            )
            db.conn.execute(
                "INSERT INTO claim_link(claim_id, prev_id, reason, changed_at) VALUES(?,?,?,?)",
                (inserted["id"], chain["id"], reason, last_seen),
            )
            events.append(
                conn,
                actor=actor,
                op="claim.supersede",
                target_tbl="claim",
                target_id=inserted["id"],
                subject_id=subject_text,
                payload={"prev": chain["id"], "reason": reason, "seq": seq},
                idem_key=f"claim-supersede:{inserted['id']}",
            )
            return {
                "action": "versioned",
                "claim_id": inserted["id"],
                "prev_id": chain["id"],
                "reason": reason,
            }
        inserted = _insert(
            db,
            subject_id=subject_text,
            kind=kind,
            predicate=predicate,
            object_=object_text,
            polarity=polarity_int,
            status=status,
            event_date=event_date,
            mentioned_at=mentioned,
            event_end=event_end,
            scope=scope,
            confidence=confidence_value,
            source_line=source_line,
            source_turn=source_turn,
            evidence_quote=quote,
            extractor=extractor,
            claim_group=group,
            seq=seq,
        )
        events.append(
            conn,
            actor=actor,
            op="claim.create",
            target_tbl="claim",
            target_id=inserted["id"],
            subject_id=subject_text,
            payload={"kind": kind, "status": status},
            idem_key=f"claim-create:{inserted['id']}",
        )
        return {"action": "inserted", "claim_id": inserted["id"], "claim_group": group, "seq": seq}


def merge(db: Any, **kwargs: Any) -> dict[str, Any]:
    """兼容入口：合并语义内建在 ``create`` 里；这里显式表示「合并/更新」路径。"""
    kwargs.setdefault("version", False)
    return create(db, **kwargs)


def supersede(
    db: Any,
    claim_id: str,
    *,
    new_claim: dict[str, Any],
    reason: str = "changed",
    actor: str = "system:claim",
) -> dict[str, Any]:
    """显式改口：新 claim 入库，旧行标 superseded，claim_link 记原因。"""
    payload = dict(new_claim)
    payload.setdefault("version", True)
    result = create(db, **payload, actor=actor)
    new_id = result.get("claim_id")
    if not new_id or new_id == claim_id:
        return result
    with db.tx() as conn:
        db.conn.execute(
            "UPDATE claim SET superseded_by=?, status='superseded' WHERE id=?",
            (new_id, claim_id),
        )
        db.conn.execute(
            "INSERT INTO claim_link(claim_id, prev_id, reason, changed_at) VALUES(?,?,?,?)",
            (new_id, claim_id, str(reason or "changed"), now_iso()),
        )
        events.append(
            conn,
            actor=actor,
            op="claim.supersede",
            target_tbl="claim",
            target_id=new_id,
            subject_id=str(payload.get("subject_id") or "self"),
            payload={"prev": claim_id, "reason": str(reason or "changed")},
            idem_key=f"claim-supersede:{new_id}",
        )
    return result


def get(db: Any, claim_id: str) -> dict[str, Any] | None:
    row = db.conn.execute("SELECT * FROM claim WHERE id=?", (claim_id,)).fetchone()
    return _row(row) if row is not None else None


def list_claims(
    db: Any,
    *,
    subject_id: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    include_superseded: bool = False,
    include_deleted: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM claim WHERE 1=1"
    params: list[Any] = []
    if subject_id:
        sql += " AND subject_id=?"
        params.append(str(subject_id))
    if kind:
        sql += " AND kind=?"
        params.append(str(kind))
    if status:
        sql += " AND status=?"
        params.append(str(status))
    if not include_superseded:
        sql += " AND status<>'superseded' AND superseded_by IS NULL"
    if not include_deleted:
        sql += " AND status<>'deleted'"
    sql += " ORDER BY last_seen DESC, seq DESC LIMIT ?"
    params.append(max(1, int(limit or 1)))
    return [_row(row) for row in db.conn.execute(sql, params)]


def search(
    db: Any,
    query: str,
    *,
    subject_id: str | None = None,
    kind: str | None = None,
    k: int = 8,
    include_superseded: bool = False,
) -> list[dict[str, Any]]:
    """确定性 claim 检索（先 LIKE 粗筛，再按词命中/使用次数/新鲜度排序）。

    不做 FTS：claim 量小，且索引层（claim.index）默认关；这里只保证「能搜到、顺序稳」。
    """
    q = normalize(query or "").strip()
    if not q:
        return []
    raw_tokens = tokenize(q)
    tokens = (
        [str(token) for token in raw_tokens if str(token).strip()]
        if isinstance(raw_tokens, (list, tuple))
        else [token for token in str(raw_tokens).split() if token.strip()]
    )
    terms = [term for term in sorted({q, *tokens[:8]}, key=len, reverse=True) if term.strip()][:10]
    clauses: list[str] = []
    params: list[Any] = []
    for term in terms:
        clauses.append("(predicate LIKE ? OR object LIKE ? OR evidence_quote LIKE ?)")
        params.extend([f"%{term}%", f"%{term}%", f"%{term}%"])
    sql = "SELECT * FROM claim WHERE (" + " OR ".join(clauses) + ")"
    if subject_id:
        sql += " AND subject_id=?"
        params.append(str(subject_id))
    if kind:
        sql += " AND kind=?"
        params.append(str(kind))
    if not include_superseded:
        sql += " AND status<>'superseded' AND superseded_by IS NULL"
    sql += " AND status<>'deleted' ORDER BY last_seen DESC LIMIT 200"
    rows = [_row(row) for row in db.conn.execute(sql, params)]
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        haystack = normalize(f"{row['predicate']} {row['object']} {row['evidence_quote']}")
        hits = sum(1 for term in terms if term and term in haystack)
        if not hits:
            continue
        score = round(hits + 0.05 * float(row["use_count"]), 4)
        scored.append((score, row))
    scored.sort(key=lambda pair: -pair[0])
    return [{**row, "score": score} for score, row in scored[: max(1, int(k or 1))]]


search_claims = search


def soft_delete(
    db: Any, claim_id: str, *, reason: str = "user", actor: str = "user"
) -> dict[str, Any]:
    """软删：status=deleted；事件里只留 id/原因，**不含原文**。"""
    row = get(db, claim_id)
    if row is None:
        raise ValidationError("claim 不存在", field="claim_id")
    with db.tx() as conn:
        db.conn.execute("UPDATE claim SET status='deleted' WHERE id=?", (claim_id,))
        events.append(
            conn,
            actor=actor,
            op="claim.delete",
            target_tbl="claim",
            target_id=claim_id,
            subject_id=str(row.get("subject_id") or "self"),
            payload={"reason": str(reason or "user")},
            idem_key=f"claim-delete:{claim_id}",
        )
    return {"deleted": True, "claim_id": claim_id}


def person_card(
    db: Any, entity_id: str, *, include_candidate: bool = False
) -> dict[str, Any] | None:
    row = db.conn.execute("SELECT * FROM person_card WHERE entity_id=?", (entity_id,)).fetchone()
    if row is None:
        return None
    data = dict(row)
    for key in ("facts", "boundary", "contested"):
        try:
            data[key] = json.loads(data.get(key) or "[]")
        except (TypeError, ValueError):
            data[key] = []
    return data


def refresh_person_card(
    db: Any, entity_id: str, *, relation: str | None = None
) -> dict[str, Any]:
    """从 claims 聚合人物卡：明确层才收；同谓词相反值两条都留并标 contested。"""
    statuses = EXPLICIT_STATUSES
    rows = [
        _row(row)
        for row in db.conn.execute(
            "SELECT * FROM claim WHERE subject_id=? AND status IN (?,?,?,?) "
            "AND superseded_by IS NULL ORDER BY last_seen DESC",
            (entity_id, *statuses),
        )
    ]
    facts: list[dict[str, Any]] = []
    boundary: list[dict[str, Any]] = []
    by_predicate: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row["kind"] == "boundary":
            boundary.append(
                {
                    "scope": row.get("scope"),
                    "text": row["evidence_quote"],
                    "claim_id": row["id"],
                }
            )
            continue
        entry = {
            "predicate": row["predicate"],
            "object": row["object"],
            "event_date": row.get("event_date"),
            "claim_id": row["id"],
            "polarity": row["polarity"],
            "status": row["status"],
        }
        facts.append(entry)
        by_predicate.setdefault(row["predicate"], []).append(entry)
    contested = [
        {
            "predicate": predicate,
            "objects": sorted({entry["object"] for entry in entries}),
            "claim_ids": [entry["claim_id"] for entry in entries],
        }
        for predicate, entries in by_predicate.items()
        if len({(entry["object"], entry["polarity"]) for entry in entries}) > 1
    ]
    updated = now_iso()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO person_card(entity_id, relation, facts, boundary, contested, updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(entity_id) DO UPDATE SET "
            "relation=COALESCE(excluded.relation, person_card.relation), "
            "facts=excluded.facts, boundary=excluded.boundary, "
            "contested=excluded.contested, updated_at=excluded.updated_at",
            (
                entity_id,
                relation,
                json.dumps(facts, ensure_ascii=False),
                json.dumps(boundary, ensure_ascii=False),
                json.dumps(contested, ensure_ascii=False),
                updated,
            ),
        )
        events.append(
            conn,
            actor="system:claim",
            op="person_card.refresh",
            target_tbl="person_card",
            target_id=entity_id,
            payload={"facts": len(facts), "boundary": len(boundary), "contested": len(contested)},
            idem_key=f"person-card:{entity_id}:{updated}",
        )
    return {"entity_id": entity_id, "facts": len(facts), "boundary": len(boundary), "contested": len(contested)}
