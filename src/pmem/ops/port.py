"""JSONL 导出 / 导入（04 §5.9，M8）。

导出的是**记忆原文与元数据**，不含向量（模型绑定、体积大）；导入后由检索层按需重算。
导入规则：

1. 首行 header 校验 `type/version/schema_version`，高版本拒绝；
2. `id` 已存在按 `on_conflict` 处理（skip/merge/fail）；
3. `provenance` 保持原值，不因为导入而升级；
4. 整批一个事务，失败整体回滚；
5. 向量不导入，`memory_vec_plain` 留空。

只处理 `type=memory` 与 `type=event`（事件行是可选的整链快照，见 ``import_jsonl``）；
entity/relation/trait 行会记入 `ignored`，由 net/sleep 重算。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..base import events
from ..base.clock import day_key, now_iso, parse_iso
from ..base.db import Database
from ..base.errors import ConflictError, ValidationError
from ..memory import groups as group_mod
from ..memory.text import claim_sig, is_mostly_ascii, normalize, sha256_hex, tokenize

DEFAULT_SCOPE = "project:manual"

_KINDS = {"episodic", "semantic", "person", "relation", "experience", "goal", "meta"}
_PROV = {"user_explicit", "user_confirmed", "external", "observed", "inferred", "unconfirmed"}
_SENS = {"public", "personal", "secret"}
_STATUS = {
    "candidate",
    "active",
    "dormant",
    "archived",
    "contested",
    "superseded",
    "discarded",
    "deleted",
}


def _canonical(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _version_tuple(value: str) -> tuple[int, ...]:
    parts = tuple(int(x) for x in re.findall(r"\d+", value or ""))
    return parts or (0,)


def parse_jsonl(text: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """解析 header + 记录；空行跳过，坏行直接报错。"""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    if not lines:
        raise ValidationError("导入内容为空", field="data")
    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise ValidationError(f"首行不是合法 JSON：{exc}", field="data") from exc
    if not isinstance(header, dict) or header.get("type") != "pmem_export":
        raise ValidationError("首行必须是 pmem_export header", field="data")

    records: list[dict[str, Any]] = []
    for number, line in enumerate(lines[1:], start=2):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"第 {number} 行不是合法 JSON：{exc}", field="data") from exc
        if not isinstance(record, dict):
            raise ValidationError(f"第 {number} 行不是对象", field="data")
        records.append(record)
    return header, records


def _check_header(db: Database, header: dict[str, Any]) -> None:
    if header.get("version") != "v1":
        raise ValidationError(f"不支持的导出格式 {header.get('version')!r}", field="data")
    current = str(db.meta("schema_version") or "0")
    incoming = str(header.get("schema_version") or "0")
    if _version_tuple(incoming) > _version_tuple(current):
        raise ValidationError(
            f"导出 schema {incoming} 高于当前工具 {current}，请先升级 PMem",
            field="data",
        )


def _row_from_record(record: dict[str, Any], *, subject: str) -> dict[str, Any]:
    text = record.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValidationError("导入记录缺少 text", field="data")
    text = text.strip()
    memory_id = str(record.get("id") or "").strip()
    if not memory_id:
        raise ValidationError("导入记录缺少 id", field="data")

    norm = normalize(text)
    created = str(record.get("created_at") or now_iso())
    try:
        day = str(record.get("day") or day_key(parse_iso(created)))
    except ValueError:
        day = day_key()
    source = record.get("source") if isinstance(record.get("source"), dict) else {}
    extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
    status = str(record.get("status") or "active")
    kind = str(record.get("kind") or "semantic")
    provenance = str(record.get("provenance") or "external")
    sensitivity = str(record.get("sensitivity") or "personal")
    if kind not in _KINDS:
        raise ValidationError(f"非法 kind：{kind}", field="data")
    if provenance not in _PROV:
        raise ValidationError(f"非法 provenance：{provenance}", field="data")
    if sensitivity not in _SENS:
        raise ValidationError(f"非法 sensitivity：{sensitivity}", field="data")
    if status not in _STATUS:
        raise ValidationError(f"非法 status：{status}", field="data")
    deleted_at = now_iso() if status == "deleted" else None
    return {
        "id": memory_id,
        "subject_id": subject,
        "scope_key": str(record.get("scope_key") or DEFAULT_SCOPE),
        "kind": kind,
        "text_raw": text,
        "text_norm": norm,
        "text_seg": tokenize(text),
        "text_sha256": sha256_hex(norm),
        "lang": "en" if is_mostly_ascii(norm) else "zh",
        "status": status,
        "importance": float(record.get("importance", 0.5)),
        "confidence": float(record.get("confidence", 0.5)),
        "provenance": provenance,
        "evidence_count": int(record.get("evidence_count", 1)),
        "evidence_div": float(record.get("evidence_div", 0.0)),
        "source_kind": str(source.get("kind") or "import"),
        "source_ref": source.get("ref"),
        "episode_id": None,
        "session_id": source.get("session"),
        "claim_sig": claim_sig(text, str(record.get("kind") or "semantic")),
        "sensitivity": sensitivity,
        "pinned": 1 if record.get("pinned") else 0,
        "created_at": created,
        "recorded_at": created,
        "valid_from": record.get("valid_from"),
        "valid_to": record.get("valid_to"),
        "day_key": day,
        "canonical_id": record.get("canonical_id"),
        "supersedes_id": record.get("supersedes_id"),
        "superseded_by": record.get("superseded_by"),
        "cluster_id": record.get("cluster_id"),
        "use_count": int(record.get("use_count", 0)),
        "last_used_at": record.get("last_used_at"),
        "decay_score": float(record.get("decay_score", 1.0)),
        "extra_json": _canonical(extra),
        "deleted_at": deleted_at,
    }


_COLUMNS = (
    "id,subject_id,scope_key,kind,text_raw,text_norm,text_seg,text_sha256,lang,status,"
    "importance,confidence,provenance,evidence_count,evidence_div,source_kind,source_ref,"
    "episode_id,session_id,claim_sig,sensitivity,pinned,created_at,recorded_at,valid_from,"
    "valid_to,day_key,canonical_id,supersedes_id,superseded_by,cluster_id,use_count,"
    "last_used_at,decay_score,extra_json,deleted_at"
)

# ---- 事件日志导出/导入（M29） ---------------------------------------------
# 导出行：{"type": "event", "seq": ..., ..., "payload": {...}}
# 为什么 payload 用 JSON 值而不是原始字符串：外部工具/人读都方便。导入时按
# ``events.append`` 同一套 canonical 规则重新序列化——event_log 的 payload_json
# 一律由 events.append 写成 canonical JSON，所以「导出→导入」后逐字节相同，
# 哈希链可原样验证。若有人手改过 payload_json（非 canonical），导入端会在
# verify_chain 处明确报错，而不是悄悄放过。
_EVENT_FIELDS = (
    "seq",
    "id",
    "ts",
    "actor",
    "op",
    "target_tbl",
    "target_id",
    "subject_id",
    "idem_key",
    "run_id",
    "prev_hash",
    "hash",
)
_EVENT_SELECT = "SELECT " + ",".join(_EVENT_FIELDS) + ",payload_json FROM event_log ORDER BY seq"
_EVENT_INSERT = (
    "INSERT INTO event_log(seq,id,ts,actor,op,target_tbl,target_id,subject_id,payload_json,"
    "idem_key,run_id,prev_hash,hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _payload_value(raw: Any) -> Any:
    try:
        return json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {"_raw": raw}


def _event_payload_json(record: dict[str, Any]) -> str:
    payload = record.get("payload")
    if payload is None:
        return "{}"
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def export_event_records(db: Database, *, limit: int | None = None) -> list[dict[str, Any]]:
    """把 event_log 转成可导出的 ``type=event`` 行（按 seq 升序）。"""
    sql = _EVENT_SELECT + (" LIMIT ?" if limit else "")
    rows = db.conn.execute(sql, (int(limit),) if limit else ()).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = {"type": "event"}
        for field in _EVENT_FIELDS:
            item[field] = row[field]
        item["payload"] = _payload_value(row["payload_json"])
        out.append(item)
    return out


def _event_insert_row(record: dict[str, Any]) -> tuple[Any, ...]:
    raw_seq = record.get("seq")
    if isinstance(raw_seq, bool) or not isinstance(raw_seq, int):
        try:
            raw_seq = int(str(raw_seq))
        except (TypeError, ValueError):
            raise ValidationError("事件记录缺少合法的 seq", field="data") from None
    if raw_seq <= 0:
        raise ValidationError("事件记录的 seq 必须为正整数", field="data")
    event_id = str(record.get("id") or "").strip()
    if not event_id:
        raise ValidationError("事件记录缺少 id", field="data")
    for required in ("ts", "actor", "op", "hash"):
        if not str(record.get(required) or "").strip():
            raise ValidationError(f"事件记录缺少 {required}", field="data")

    def opt(name: str) -> str | None:
        value = record.get(name)
        return None if value is None or value == "" else str(value)

    return (
        raw_seq,
        event_id,
        str(record["ts"]),
        str(record["actor"]),
        str(record["op"]),
        opt("target_tbl"),
        opt("target_id"),
        opt("subject_id"),
        _event_payload_json(record),
        opt("idem_key"),
        opt("run_id"),
        opt("prev_hash"),
        str(record["hash"]),
    )


def import_jsonl(
    db: Database,
    data: str,
    *,
    subject: str = "self",
    on_conflict: str = "skip",
    dry_run: bool = False,
    actor: str = "import",
) -> dict[str, Any]:
    if on_conflict not in ("skip", "merge", "fail"):
        raise ValidationError("on_conflict 只能是 skip/merge/fail", field="on_conflict")
    header, records = parse_jsonl(data)
    _check_header(db, header)

    memory_records = [record for record in records if record.get("type") == "memory"]
    event_records = [record for record in records if record.get("type") == "event"]
    event_rows = sorted(
        (_event_insert_row(record) for record in event_records), key=lambda row: row[0]
    )
    event_ids = [row[1] for row in event_rows]
    if len(set(event_ids)) != len(event_ids):
        raise ValidationError("导出文件里事件 id 重复", field="data")
    if event_rows:
        # 哈希链按 seq 首尾相连：只能整条链落进空日志。往已有事件后面追加另一条链，
        # 从第一条开始 prev_hash 就对不上——与其导入后链断，不如当场拒绝。
        existing_events = int(
            db.conn.execute("SELECT COUNT(*) c FROM event_log").fetchone()["c"]
        )
        if existing_events:
            raise ValidationError(
                "目标库已有事件日志，不能把另一条哈希链合并进来",
                hint=(
                    "事件日志按 seq 串成哈希链，只能导入到空库；整库迁移请直接用备份文件"
                    "（pmem backup / snapshot），或导出时不带事件、只导入记忆。"
                ),
                field="event_log",
            )

    stats = {
        "input": len(records),
        "inserted": 0,
        "merged": 0,
        "skipped": 0,
        "failed": 0,
        "ignored": len(records) - len(memory_records) - len(event_records),
        "events_inserted": 0,
        "events_input": len(event_rows),
        "dry_run": dry_run,
    }
    seen: set[str] = set()

    try:
        with db.tx() as conn:
            # 事件先写：后面导入记忆时补发的事件会接到这条链的尾巴上，链仍然连续。
            for row in event_rows:
                conn.execute(_EVENT_INSERT, row)
            if event_rows:
                stats["events_inserted"] = len(event_rows)
            for record in memory_records:
                row = _row_from_record(record, subject=subject)
                memory_id = row["id"]
                if memory_id in seen:
                    raise ValidationError(f"导入批次里 id 重复：{memory_id}", field="data")
                seen.add(memory_id)

                existing = conn.execute("SELECT * FROM memory WHERE id=?", (memory_id,)).fetchone()
                if existing is not None:
                    if on_conflict == "fail":
                        stats["failed"] += 1
                        raise ConflictError(f"记忆已存在：{memory_id}", field="id")
                    if on_conflict == "skip":
                        stats["skipped"] += 1
                        continue
                    conn.execute(
                        "UPDATE memory SET importance=?, confidence=?, pinned=?, sensitivity=?, "
                        "extra_json=? WHERE id=?",
                        (
                            float(row["importance"]),
                            float(row["confidence"]),
                            row["pinned"],
                            row["sensitivity"],
                            row["extra_json"],
                            memory_id,
                        ),
                    )
                    stats["merged"] += 1
                    continue

                values = tuple(row[column] for column in _COLUMNS.split(","))
                conn.execute(
                    f"INSERT INTO memory({_COLUMNS}) VALUES({','.join('?' for _ in values)})",
                    values,
                )
                if not dry_run:
                    # 事件日志随导出一起导入时，源库的 memory.add 事件已经记过这些记忆；
                    # 再补 memory.import 会让「导出 N 条事件 → 导入 N 条」对不上。分组/等级
                    # 是本地新动作，它们自己的事件照写（链会继续往后接）。
                    if not event_rows:
                        events.append(
                            conn,
                            actor=actor,
                            op="memory.import",
                            target_tbl="memory",
                            target_id=memory_id,
                            subject_id=subject,
                            payload={
                                "source": header.get("created_at"),
                                "schema": header.get("schema_version"),
                            },
                        )
                    groups = record.get("groups")
                    if isinstance(groups, list):
                        for path in groups:
                            if path:
                                group_mod.assign(
                                    db,
                                    target_tbl="memory",
                                    target_id=memory_id,
                                    path=str(path),
                                    assigned_by="import",
                                )
                    grade = record.get("grade")
                    if grade:
                        group_mod.set_grade(
                            db,
                            target_tbl="memory",
                            target_id=memory_id,
                            grade=str(grade),
                            assigned_by="import",
                        )
                stats["inserted"] += 1

            if event_rows:
                chain_ok, chain_detail = events.verify_chain(conn)
                if not chain_ok:
                    raise ValidationError(
                        f"导入的事件哈希链校验失败：{chain_detail}",
                        hint="导出文件可能被修改或不完整；请重新导出，或关掉 --with-events 只导入记忆。",
                        field="event_log",
                    )
            if dry_run:
                raise _DryRun(stats)
    except _DryRun as exc:
        return exc.stats

    return stats


class _DryRun(Exception):
    """内部信号：dry_run 时回滚事务，但仍返回统计。"""

    def __init__(self, stats: dict[str, Any]) -> None:
        super().__init__("dry-run")
        self.stats = stats
