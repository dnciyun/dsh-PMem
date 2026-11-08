"""claim 索引（task-45）：claim 表的 FTS 通道，默认关。

设计：
- **独立 FTS 表** ``claim_fts(text_seg)``：claim 是短句，索引 predicate+object+evidence_quote
  的双字切分（和 memory_fts_tok 同一套 ``tokenize``），不新增依赖、不动 memory 表；
- 同步只从 claim 表重建（claims 量小）；用 ``claim_index_marker``（行数+最新 last_seen）
  短路，没变就不重建；
- ``claim.index.enabled=false`` 时本模块**一次也不会被调用**（search 侧 gate），
  既有 SQL / 返回体逐字节不变。
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from ...base.clock import now_iso
from ..text import normalize, tokenize

INDEX_VERSION = "1"
MARKER_KEY = "claim_index_marker"
_FTS_DDL = """CREATE VIRTUAL TABLE IF NOT EXISTS claim_fts USING fts5(
  text_seg,
  tokenize="unicode61 remove_diacritics 2"
)"""


_ISO_DATE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}")
_MD_DATE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_CN_DATE_HINT = re.compile(r"[一二三四五六七八九十]{1,2}\s*月\s*[一二三四五六七八九十]{1,2}\s*[日号]")


def _date_forms(text: str) -> list[str]:
    """日期形态互通：ISO ↔ 4月19号/4月19日；只做确定性展开，不猜年份。"""
    value = str(text or "").strip()
    forms: list[str] = []
    iso_match = _ISO_DATE.search(value)
    iso = iso_match.group(0) if iso_match else ""
    if iso:
        forms.append(iso)
    month = day = None
    md = _MD_DATE.search(value)
    if md:
        month, day = int(md.group(1)), int(md.group(2))
    elif iso:
        try:
            iso_year, iso_month, iso_day = (int(part) for part in iso.split("-"))
            month, day = iso_month, iso_day
        except ValueError:
            month = day = None
    if month and day:
        forms.extend([f"{month}月{day}号", f"{month}月{day}日"])
        if iso:
            year = iso.split("-")[0]
            forms.append(f"{year}年{month}月{day}号")
            forms.append(f"{year}年{month}月{day}日")
    # 去重保序
    return list(dict.fromkeys(form for form in forms if form))


def _token_seg(claim: dict[str, Any]) -> str:
    text = " ".join(
        str(claim.get(key) or "")
        for key in ("subject_id", "kind", "predicate", "object", "evidence_quote")
    )
    extra = " ".join(_date_forms(str(claim.get("event_date") or claim.get("object") or "")))
    raw = tokenize(normalize(text))
    tokenized = raw if isinstance(raw, str) else " ".join(str(item) for item in raw)
    return " ".join(part for part in (tokenized, extra) if part)


def ensure(db: Any) -> bool:
    """幂等建 FTS 表（只在 claim.index.enabled 打开时由 search 调用）。"""
    try:
        db.conn.execute(_FTS_DDL)
        db.conn.execute(
            "INSERT INTO schema_meta(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            ("claim_index_version", INDEX_VERSION, now_iso()),
        )
        return True
    except sqlite3.DatabaseError:
        return False


def _marker(db: Any) -> str:
    row = db.conn.execute(
        "SELECT COUNT(*) c, COALESCE(MAX(last_seen), '') m, COALESCE(MAX(rowid),0) r "
        "FROM claim WHERE status NOT IN ('deleted','superseded') AND superseded_by IS NULL"
    ).fetchone()
    return f"{row['c']}:{row['m']}:{row['r']}"


def _stored_marker(db: Any) -> str | None:
    row = db.conn.execute(
        "SELECT value FROM schema_meta WHERE key=?", (MARKER_KEY,)
    ).fetchone()
    return str(row["value"]) if row is not None else None


def rebuild(db: Any) -> int:
    """全量重建 claim FTS；返回索引条数。"""
    ensure(db)
    rows = db.conn.execute(
        "SELECT rowid, kind, predicate, object, evidence_quote FROM claim "
        "WHERE status NOT IN ('deleted','superseded') AND superseded_by IS NULL"
    ).fetchall()
    with db.tx() as conn:
        conn.execute("DELETE FROM claim_fts")
        for row in rows:
            conn.execute(
                "INSERT INTO claim_fts(rowid, text_seg) VALUES(?,?)",
                (int(row["rowid"]), _token_seg(dict(row))),
            )
        conn.execute(
            "INSERT INTO schema_meta(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (MARKER_KEY, _marker(db), now_iso()),
        )
    return len(rows)


def sync(db: Any) -> int:
    """按 marker 增量判断：变了就重建，没变直接返回当前索引数。"""
    ensure(db)
    if _stored_marker(db) != _marker(db):
        return rebuild(db)
    row = db.conn.execute("SELECT COUNT(*) c FROM claim_fts").fetchone()
    return int(row["c"]) if row is not None else 0


def verify(db: Any) -> dict[str, Any]:
    """索引自检：行数/标记是否与 claim 表一致（不写库）。"""
    expected = int(
        db.conn.execute(
            "SELECT COUNT(*) c FROM claim WHERE status NOT IN ('deleted','superseded') "
            "AND superseded_by IS NULL"
        ).fetchone()["c"]
    )
    try:
        indexed = int(db.conn.execute("SELECT COUNT(*) c FROM claim_fts").fetchone()["c"])
    except sqlite3.DatabaseError:
        indexed = -1
    return {
        "ok": indexed == expected and _stored_marker(db) == _marker(db),
        "expected": expected,
        "indexed": indexed,
        "marker_ok": _stored_marker(db) == _marker(db),
    }


def _match_query(query: str) -> str:
    raw_query = normalize(query or "")
    tokens = tokenize(raw_query)
    parts = (
        [str(token) for token in tokens if str(token).strip()]
        if isinstance(tokens, (list, tuple))
        else [token for token in str(tokens).split() if token.strip()]
    )
    # 日期两形互通：用户说「4月19号」也要能命中索引里的 2026-04-19（反之亦然）。
    date_parts: list[str] = []
    for match in _ISO_DATE.finditer(raw_query):
        date_parts.extend(_date_forms(match.group(0)))
    for match in _MD_DATE.finditer(raw_query):
        date_parts.extend(_date_forms(match.group(0)))
    for match in _CN_DATE_HINT.finditer(raw_query):
        date_parts.append(match.group(0))
    parts = list(dict.fromkeys([*parts, *date_parts]))
    # 双字 OR：任一切片命中即可进 claim 通道（RRF 只吃名次，不吃 BM25 量纲）。
    return " OR ".join(f'"{part}"' for part in parts[:32])


def search(
    db: Any,
    query: str,
    *,
    subject: str = "self",
    kinds: list[str] | None = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """claim FTS 命中（subject/kind 过滤），按 bm25 排序返回 claim 行 + score。"""
    sync(db)
    match = _match_query(query)
    if not match:
        return []
    sql = (
        "SELECT c.*, bm25(claim_fts) AS rank FROM claim_fts "
        "JOIN claim c ON c.rowid = claim_fts.rowid "
        "WHERE claim_fts MATCH ? AND c.subject_id=? "
        "AND c.status NOT IN ('deleted','superseded') AND c.superseded_by IS NULL"
    )
    params: list[Any] = [match, str(subject)]
    if kinds:
        sql += " AND c.kind IN (" + ",".join("?" for _ in kinds) + ")"
        params.extend(kinds)
    sql += " ORDER BY rank ASC, c.use_count DESC, c.last_seen DESC LIMIT ?"
    params.append(max(1, int(limit or 1)))
    try:
        rows = db.conn.execute(sql, params).fetchall()
    except sqlite3.DatabaseError:
        return []
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["score"] = round(1.0 / (1.0 + max(0.0, float(row["rank"] or 0.0))), 4)
        # 命中回查原话：短句只当索引/路由，注入时以原文为主。
        item["source_text"] = original_text(db, item)
        result.append(item)
    return result


def boundary_claims(
    db: Any, query: str, *, subject: str = "self", limit: int = 3
) -> list[dict[str, Any]]:
    """boundary claim：按与当前话题的 token 重叠排序，最多 limit 条。"""
    sync(db)
    rows = db.conn.execute(
        "SELECT * FROM claim WHERE subject_id=? AND kind='boundary' "
        "AND status NOT IN ('deleted','superseded') AND superseded_by IS NULL "
        "ORDER BY last_seen DESC LIMIT 200",
        (str(subject),),
    ).fetchall()
    q_tokens = {
        str(token)
        for token in (
            tokenize(normalize(query or "")).split()
            if isinstance(tokenize(normalize(query or "")), str)
            else []
        )
        if token
    }

    def score(row: Any) -> tuple[int, str]:
        text = normalize(f"{row['predicate']} {row['object']}")
        return (sum(1 for token in q_tokens if token in text), str(row["last_seen"] or ""))

    ranked = sorted(rows, key=score, reverse=True)
    return [dict(row) for row in ranked[: max(1, int(limit or 1))]]


def original_text(db: Any, claim: dict[str, Any], *, max_chars: int = 120) -> str:
    """用 claim.source_line 回查 journal_line 原文（截 ~120 字）；没有就退 evidence_quote。"""
    line_id = str(claim.get("source_line") or "").strip()
    text = ""
    if line_id:
        row = db.conn.execute(
            "SELECT text FROM journal_line WHERE id=? LIMIT 1", (line_id,)
        ).fetchone()
        if row is not None:
            text = str(row["text"] or "")
    if not text:
        text = str(claim.get("evidence_quote") or "")
    return text[: max(1, int(max_chars or 120))]


def inject_text(claim: dict[str, Any], original: str) -> str:
    """claim 只做索引/路由：注入「claim 摘要｜原话片段（你说过：日期）」。"""
    summary = claim_text(claim)
    body = str(original or "").strip()
    if body and body not in summary:
        return f"{summary}｜原话：{body}"
    return summary


def _bigrams(text: str) -> set[str]:
    raw = normalize(text)
    return {raw[index : index + 2] for index in range(max(0, len(raw) - 1)) if raw[index : index + 2].strip()}


def source_covered(
    claim: dict[str, Any],
    payload: list[dict[str, Any]],
    memory_refs: dict[str, str] | None = None,
) -> bool:
    """同来源已进 top-k 就不重复注入（source_line↔source_ref，或原话 bigram≥0.8）。"""
    source_line = str(claim.get("source_line") or "")
    refs = memory_refs or {}
    if source_line and source_line in refs.values():
        return True
    original = str(claim.get("source_text") or claim.get("evidence_quote") or "")
    grams = _bigrams(original)
    if not grams:
        return False
    for item in payload:
        if item.get("source") == "claim":
            continue
        item_grams = _bigrams(str(item.get("text") or ""))
        if not item_grams:
            continue
        coverage = sum(1 for gram in grams if gram in item_grams) / len(grams)
        if coverage >= 0.8:
            return True
    return False


def claim_text(claim: dict[str, Any]) -> str:
    """claim 注入文本：按 kind 渲染成人话，不把机器谓词/ISO 直接塞给模型。

    - date：``4月19号（你说过：2026-04-19）``（中文形态 + ISO 括注，两形都进索引）
    - boundary：``「别提画册」``（禁止项，不参与普通事实叙述）
    - 其他：``不吃香菜（你说过：2026-05-02）``
    """
    predicate = str(claim.get("predicate") or "").strip()
    obj = str(claim.get("object") or "").strip()
    day = str(claim.get("event_date") or claim.get("mentioned_at") or "")[:10]
    kind = str(claim.get("kind") or "")
    if kind == "date":
        iso = str(claim.get("event_date") or "")
        if not iso and _ISO_DATE.match(obj):
            iso = obj
        if iso:
            try:
                _year, month, day_num = (int(part) for part in iso[:10].split("-"))
                label = f"{month}月{day_num}号"
            except (ValueError, TypeError):
                label = obj
            return f"{label}（你说过：{iso[:10]}）"
        return f"{obj}（你说过：{day}）" if day else obj
    if kind == "boundary":
        body = f"「{predicate}{obj}」"
        return f"{body}（你说过：{day}）" if day else body
    body = f"{predicate}{obj}".strip()
    if predicate in {"event_on", "date", "日期"}:
        body = obj or predicate
    subject_id = str(claim.get("subject_id") or "").strip()
    if kind == "third_party" and subject_id and subject_id not in {"self", "assistant"}:
        body = f"{subject_id}{body}"
    return f"{body}（你说过：{day}）" if day else body


def as_row(claim: dict[str, Any], seq: int) -> dict[str, Any]:
    """把 claim 包成 search 内部可消费的合成 memory row（负 seq 只在本轮有效）。"""
    text = claim_text(claim)
    day = str(claim.get("event_date") or claim.get("mentioned_at") or "")[:10]
    stamp = str(claim.get("mentioned_at") or claim.get("last_seen") or now_iso())
    return {
        "seq": int(seq),
        "id": str(claim.get("id") or ""),
        "subject_id": str(claim.get("subject_id") or "self"),
        "scope_key": "",
        "kind": "claim",
        "text_raw": text,
        "text_norm": normalize(text),
        "text_seg": "",
        "text_sha256": "",
        "lang": "zh",
        "status": "active",
        "importance": 0.5,
        "confidence": float(claim.get("confidence") or 0.8),
        "provenance": "user_explicit",
        "evidence_count": 1,
        "evidence_div": 0.0,
        "source_kind": "claim",
        "source_ref": str(claim.get("id") or ""),
        "episode_id": None,
        "session_id": None,
        "claim_sig": None,
        "claim_group": claim.get("claim_group"),
        "sensitivity": "personal",
        "pinned": 0,
        "created_at": stamp,
        "recorded_at": stamp,
        "valid_from": day or None,
        "valid_to": None,
        "day_key": day,
        "canonical_id": None,
        "supersedes_id": None,
        "superseded_by": None,
        "cluster_id": None,
        "use_count": int(claim.get("use_count") or 1),
        "last_used_at": str(claim.get("last_seen") or stamp),
        "last_surfaced_at": None,
        "decay_score": None,
        "deleted_at": None,
        "extra_json": "{}",
        "claim_id": str(claim.get("id") or ""),
        "claim_kind": str(claim.get("kind") or ""),
        "mentioned_at": claim.get("mentioned_at"),
        "event_date": claim.get("event_date"),
        "boundary": bool(str(claim.get("kind") or "") == "boundary"),
        "scope": claim.get("scope"),
        **({"original": str(claim.get("source_text") or "")[:120]} if use_original else {}),
    }


def payload_item(
    claim: dict[str, Any], *, score: float = 1.0, use_original: bool = False
) -> dict[str, Any]:
    """claim 注入用 payload 条目（回查原话；boundary 仍用短句）。"""
    day = str(claim.get("event_date") or claim.get("mentioned_at") or "")[:10]
    text = (
        inject_text(claim, str(claim.get("source_text") or claim.get("evidence_quote") or ""))
        if use_original
        else claim_text(claim)
    )
    return {
        "id": str(claim.get("id") or ""),
        "text": text,
        "score": round(float(score), 4),
        "kind": "claim",
        "status": "active",
        "day": day,
        "valid_from": claim.get("event_date") or claim.get("mentioned_at"),
        "importance": 0.5,
        "confidence": float(claim.get("confidence") or 0.8),
        "provenance": "user_explicit",
        "channels": ["claim"],
        "truncated": False,
        "groups": [],
        "grade": None,
        "source": "claim",
        "claim_id": str(claim.get("id") or ""),
        "claim_kind": str(claim.get("kind") or ""),
        "mentioned_at": claim.get("mentioned_at"),
        "event_date": claim.get("event_date"),
        "boundary": bool(str(claim.get("kind") or "") == "boundary"),
        "scope": claim.get("scope"),
        **({"original": str(claim.get("source_text") or "")[:120]} if use_original else {}),
    }
