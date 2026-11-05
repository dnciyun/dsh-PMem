"""PMem Core 的对外实现：加、取、改、删、查、统计。

铁律（PLAN.md §1）：

- 原文只追加：``text_raw/text_norm/created_at/provenance`` 由触发器钉死，纠错只能新开一行；
- 结论必有证据：派生字段（confidence / evidence_*）随时可重算；
- 单写者：所有写操作走 ``Database.tx()``（BEGIN IMMEDIATE）。
"""

from __future__ import annotations

import json
from typing import Any

from ..base import events
from ..base.clock import day_key, now_iso, parse_iso
from ..base.config import Config
from ..base.db import Database
from ..base.errors import ConflictError, DeniedError, NotFoundError, ValidationError
from ..base.ids import ulid
from . import graph as graph_mod
from . import groups as group_mod
from . import scoring
from . import vector as vector_mod
from .retrieval import channels as channels_mod
from .retrieval import lexical as lexical_mod
from .retrieval import search as search_mod
# 兼容：拆分前 ``VISIBLE_STATUS`` 是 store 模块面的常量（vector.py 的注释仍按旧位置引用）。
from .retrieval.channels import VISIBLE_STATUS
from .text import claim_sig, is_mostly_ascii, normalize, sanitize, sha256_hex, tokenize

DEFAULT_SCOPE = "project:manual"


def _json_load(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def _json_dump(data: dict[str, Any] | None) -> str:
    # 和 ``memory/retrieval/channels.py`` 里的同名实现必须保持一致：
    # channels 不能 import store（会循环），所以只能复制这一份；
    # 改 canonical JSON 口径时两处一起改。
    return json.dumps(data or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_EVIDENCE_JSON_TABLES = ("trait", "pattern", "experience")


def _drop_evidence_ref(conn: Any, table: str, memory_id: str) -> tuple[int, int]:
    """从派生表的 ``evidence_json`` 里摘掉指向 ``memory_id`` 的条目。

    返回 ``(updated_rows, deleted_rows)``。摘完变空的行**整行删除**：派生行
    没有证据就是无源结论（违反 I3），留着比删掉更糟；它们都能从原文重建。
    重新序列化用 canonical JSON，和 ``sleep.common.evidence`` / ``trait.derive``
    的写法一致（重跑比较不抖动）。
    """
    updated = 0
    deleted = 0
    rows = conn.execute(
        f"SELECT id, evidence_json FROM {table} WHERE evidence_json LIKE ?",
        (f"%{memory_id}%",),
    ).fetchall()
    for row in rows:
        try:
            items = json.loads(row["evidence_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(items, list):
            continue
        kept = [
            item
            for item in items
            if not (isinstance(item, dict) and item.get("memory_id") == memory_id)
        ]
        if len(kept) == len(items):
            continue
        updated += 1
        if kept:
            conn.execute(
                f"UPDATE {table} SET evidence_json=? WHERE id=?",
                (
                    json.dumps(kept, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    row["id"],
                ),
            )
        else:
            # trait 的版本链有自引用外键（prev_id REFERENCES trait(id)）：先断开
            # 指向它的版本，再删，否则硬删会被 FOREIGN KEY 卡住。
            if table == "trait":
                conn.execute("UPDATE trait SET prev_id=NULL WHERE prev_id=?", (row["id"],))
            conn.execute(f"DELETE FROM {table} WHERE id=?", (row["id"],))
            deleted += 1
    return updated, deleted


class MemoryStore:
    """记忆内核。一个实例绑定一个库 + 一个主体。"""

    def __init__(
        self,
        db: Database,
        *,
        config: Config | None = None,
        subject: str = "self",
        actor: str = "system",
        scope_key: str = DEFAULT_SCOPE,
    ) -> None:
        self.db = db
        self.cfg = config or Config()
        self.subject = subject
        self.actor = actor
        self.scope_key = scope_key
        # 全库 df 缓存：覆盖率要的是「这个词在整个记忆里有多稀罕」，
        # 不是「在候选集里有多稀罕」（后者反向偏置，见 query.coverage_idf_sets）。
        self._df_cache: dict[str, int] = {}
        self._memory_total: int | None = None

    # ------------------------------------------------------------------ 写
    def add(
        self,
        text: str,
        *,
        kind: str = "semantic",
        provenance: str = "user_explicit",
        source_kind: str = "dsh_session",
        source_ref: str | None = None,
        session_id: str | None = None,
        episode_id: str | None = None,
        scope_key: str | None = None,
        valid_from: str | None = None,
        valid_to: str | None = None,
        sensitivity: str = "personal",
        pinned: bool = False,
        extra: dict[str, Any] | None = None,
        groups: list[str] | None = None,
        grade: str | None = None,
        grade_reason: str | None = None,
        idem_key: str | None = None,
        actor: str | None = None,
        created_at: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        self._validate_add(text, kind, provenance, sensitivity, pinned, valid_from, valid_to)
        if isinstance(groups, str):
            groups = [groups]
        if grade:
            group_mod.validate_grade(grade)
        actor = actor or self.actor
        scope = scope_key or self.scope_key

        if idem_key:
            prior = events.idem_lookup(self.db.conn, idem_key)
            if prior is not None:
                row = self._row(prior["target_id"])
                if row is not None:
                    return {
                        "id": row["id"],
                        "status": row["status"],
                        "importance": row["importance"],
                        "confidence": row["confidence"],
                        "duplicate_of": None,
                        "sanitized": False,
                        "idempotent_replay": True,
                    }

        clean, flags = sanitize(text) if self.cfg.get("sanitize.enabled", True) else (text, [])
        clean = clean.strip()
        if not clean:
            raise ValidationError("消毒后正文为空", hint="这条内容可能全是伪装的指令标记")
        norm = normalize(clean)
        digest = sha256_hex(norm)
        session = session_id or f"adhoc-{ulid()[:10]}"

        if dry_run:
            existing = self.db.conn.execute(
                "SELECT * FROM memory WHERE subject_id=? AND text_sha256=? AND status<>'deleted' "
                "ORDER BY created_at LIMIT 1",
                (self.subject, digest),
            ).fetchone()
            if existing is not None:
                return self._merge_existing(existing, provenance, session, actor, idem_key, dry_run=True)

        imp = scoring.importance(
            provenance=provenance,
            text=clean,
            entity_count=len([e for e in (extra or {}).get("entities", [])]),
            novelty=float((extra or {}).get("novelty", 0.5)),
            pinned=pinned,
        )
        extra_payload = dict(extra or {})
        extra_payload["evidence_tiers"] = [provenance]
        extra_payload["evidence_sessions"] = 1
        if flags:
            extra_payload["sanitized"] = flags
        conf = scoring.confidence(provenance, [(provenance, session)])

        status = "active" if self._activates(imp, provenance, pinned) else "candidate"
        mem_id = ulid()
        ts = created_at or now_iso()
        row = {
            "id": mem_id,
            "subject_id": self.subject,
            "scope_key": scope,
            "kind": kind,
            "text_raw": clean,
            "text_norm": norm,
            "text_seg": tokenize(clean),
            "text_sha256": digest,
            "lang": "en" if is_mostly_ascii(norm) else "zh",
            "status": status,
            "importance": imp,
            "confidence": conf,
            "provenance": provenance,
            "evidence_count": 1,
            "evidence_div": scoring.evidence_diversity([(provenance, session)]),
            "source_kind": source_kind,
            "source_ref": source_ref,
            "episode_id": episode_id,
            "session_id": session,
            "claim_sig": claim_sig(clean, kind),
            "sensitivity": sensitivity,
            "pinned": 1 if pinned else 0,
            "created_at": ts,
            "recorded_at": ts,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "day_key": day_key(parse_iso(ts)) if created_at else day_key(),
            "extra_json": _json_dump(extra_payload),
        }
        if dry_run:
            return {
                "id": None,
                "status": status,
                "importance": imp,
                "confidence": conf,
                "duplicate_of": None,
                "sanitized": bool(flags),
                "dry_run": True,
                "would_insert": row["text_raw"][:200],
                "groups": list(groups or []),
                "grade": grade,
            }

        with self.db.tx() as conn:
            # 去重检查必须在 BEGIN IMMEDIATE **之后**：并发同文时后到者能看见先提交的行，
            # 于是走「并证据」而不是各插一条（审计实测 16 线程同文会写出 1~9 条重复）。
            existing = conn.execute(
                "SELECT * FROM memory WHERE subject_id=? AND text_sha256=? AND status<>'deleted' "
                "ORDER BY created_at LIMIT 1",
                (self.subject, digest),
            ).fetchone()
            if existing is not None:
                result = self._merge_existing(
                    existing, provenance, session, actor, idem_key, dry_run=False
                )
                result.update(self._attach_meta(existing["id"], groups, grade, grade_reason))
                return result
            self._insert(conn, row)
            seq = int(conn.execute("SELECT seq FROM memory WHERE id=?", (mem_id,)).fetchone()["seq"])
            # 关联图增量连边：新记忆和谁说过同一件事，入库时就定下来。
            # 放在同一个事务里，图不会出现「记忆在、边还没连」的中间态。
            self._memory_total = None  # 库变大了，全库 df/总数缓存作废
            if self.cfg.get("retrieval.graph.enabled", True):
                try:
                    graph_mod.link_new(
                        conn,
                        seq=seq,
                        text_seg=row["text_seg"],
                        max_df=int(self.cfg.get("retrieval.graph.max_df", 24) or 24),
                        per_node=int(self.cfg.get("retrieval.graph.neighbours", 8) or 8),
                        min_weight=float(self.cfg.get("retrieval.graph.min_weight", 0.12) or 0.12),
                    )
                except Exception:  # noqa: BLE001 图是增强能力，连边失败不能挡住记忆写入
                    pass
            if self.cfg.get("net.auto_extract", True):
                from ..net import relations as net_relations

                net_relations.ingest_memory(
                    self.db,
                    subject=self.subject,
                    memory_id=mem_id,
                    memory_seq=seq,
                    text_norm=norm,
                    valid_from=valid_from,
                    valid_to=valid_to,
                )
            if groups or grade:
                self._attach_meta(mem_id, groups, grade, grade_reason)
            events.append(
                conn,
                actor=actor,
                op="memory.add",
                target_tbl="memory",
                target_id=mem_id,
                subject_id=self.subject,
                payload={"kind": kind, "provenance": provenance, "status": status},
                idem_key=idem_key,
            )
            if idem_key:
                events.idem_remember(conn, idem_key, "memory.add", mem_id)

        # 向量通道（M13）：入库顺手编码一条，下次检索就不用先补。放在事务**外面**：
        # ONNX 推理几毫秒，不该占着写锁；失败也无所谓，``vector.ensure`` 会兜底补齐。
        vector_mod.embed_new(self.db, seq, row["text_raw"], self.cfg)

        return {
            "id": mem_id,
            "status": status,
            "importance": imp,
            "confidence": conf,
            "duplicate_of": None,
            "sanitized": bool(flags),
            "idempotent_replay": False,
            "groups": group_mod.groups_for(self.db, "memory", mem_id),
            "grade": group_mod.grade_of(self.db, "memory", mem_id),
        }

    def _attach_meta(
        self,
        memory_id: str,
        groups: list[str] | None,
        grade: str | None,
        grade_reason: str | None,
    ) -> dict[str, Any]:
        for path in groups or []:
            group_mod.assign(
                self.db,
                target_tbl="memory",
                target_id=memory_id,
                path=path,
                assigned_by="ai",
            )
        if grade:
            group_mod.assign(
                self.db,
                target_tbl="memory",
                target_id=memory_id,
                grade=grade,
                rationale=grade_reason,
                evidence=[memory_id],
                assigned_by="ai",
            )
        return {
            "groups": group_mod.groups_for(self.db, "memory", memory_id),
            "grade": group_mod.grade_of(self.db, "memory", memory_id),
        }

    def _merge_existing(
        self,
        existing: Any,
        provenance: str,
        session: str,
        actor: str,
        idem_key: str | None,
        dry_run: bool,
    ) -> dict[str, Any]:
        """同一条原文再次入库：不新增行，只并证据（同会话不重复计）。

        读-改-写整体在同一个 ``tx()`` 里：并发合并时后到者读到的是前者提交后的
        ``extra_json``，不会把 ``evidence_tiers/evidence_sessions`` 覆盖回旧值
        （审计实测 16 并发合并曾出现 count=17 但 sessions=4 的丢更新）。
        """

        def _compute(row: Any) -> dict[str, Any]:
            extra = _json_load(row["extra_json"])
            tiers: list[str] = list(extra.get("evidence_tiers") or [row["provenance"]])
            same_session = row["session_id"] == session
            if not same_session:
                tiers.append(provenance)
            conf = scoring.confidence_from_tiers(row["provenance"], tiers)
            new_evidence = 0 if same_session else 1
            extra.update({"evidence_tiers": tiers, "evidence_sessions": len(tiers)})
            return {"extra": extra, "tiers": tiers, "conf": conf, "new_evidence": new_evidence}

        if dry_run:
            calc = _compute(existing)
            return {
                "id": existing["id"],
                "status": existing["status"],
                "importance": existing["importance"],
                "confidence": calc["conf"],
                "duplicate_of": existing["id"],
                "sanitized": False,
                "idempotent_replay": False,
            }

        with self.db.tx() as conn:
            fresh = conn.execute(
                "SELECT * FROM memory WHERE id=? AND status<>'deleted'", (existing["id"],)
            ).fetchone()
            if fresh is None:
                # 极小概率：并证据前这条被另一个进程真删了。别把幽灵 id 写进事件。
                raise NotFoundError(f"记忆 {existing['id']} 在合并前已被删除", field="id")
            calc = _compute(fresh)
            conn.execute(
                "UPDATE memory SET evidence_count=evidence_count+?, confidence=?, extra_json=? WHERE id=?",
                (calc["new_evidence"], calc["conf"], _json_dump(calc["extra"]), fresh["id"]),
            )
            events.append(
                conn,
                actor=actor,
                op="memory.evidence",
                target_tbl="memory",
                target_id=fresh["id"],
                subject_id=self.subject,
                payload={"new_evidence": calc["new_evidence"], "sessions": len(calc["tiers"])},
                idem_key=idem_key,
            )
            if idem_key:
                events.idem_remember(conn, idem_key, "memory.evidence", fresh["id"])
            result = {
                "id": fresh["id"],
                "status": fresh["status"],
                "importance": fresh["importance"],
                "confidence": calc["conf"],
                "duplicate_of": fresh["id"],
                "sanitized": False,
                "idempotent_replay": False,
            }
        return result

    def _insert(self, conn: Any, row: dict[str, Any]) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        conn.execute(f"INSERT INTO memory({cols}) VALUES({marks})", tuple(row.values()))

    def _activates(self, imp: float, provenance: str, pinned: bool) -> bool:
        threshold = self.cfg.get("score.active_threshold", 0.45)
        return bool(pinned or imp >= threshold or provenance in {"user_explicit", "user_confirmed"})

    # ------------------------------------------------------------------ 读
    def get(self, memory_id: str, *, include_deleted: bool = False) -> dict[str, Any]:
        row = self._row(memory_id)
        if row is None:
            raise NotFoundError(f"找不到记忆 {memory_id}", field="id")
        if row["status"] == "deleted" and not include_deleted:
            raise NotFoundError(f"记忆 {memory_id} 已被删除", hint="如需恢复请用 update --status active")
        item = self._to_item(row, include_extra=True)
        # 恢复过的记忆不该还挂着「已撤回」：撤回态就是 archived + 撤回事件，
        # 用 update 改回 active 之后，这两条都要同时不成立。
        item["retracted"] = row["status"] == "archived" and self._is_retracted(memory_id)
        return item

    def _is_retracted(self, memory_id: str) -> bool:
        """这条记忆是不是被「撤回」过（不等于普通归档：归档没有撤回事件）。"""
        row = self.db.conn.execute(
            "SELECT 1 FROM event_log WHERE op='memory.retract' AND target_id=? LIMIT 1",
            (memory_id,),
        ).fetchone()
        return row is not None

    def _row(self, memory_id: str) -> Any | None:
        return self.db.conn.execute("SELECT * FROM memory WHERE id=?", (memory_id,)).fetchone()

    def _to_item(self, row: Any, *, include_extra: bool = False) -> dict[str, Any]:
        item = {
            "id": row["id"],
            "kind": row["kind"],
            "text": row["text_raw"],
            "status": row["status"],
            "importance": row["importance"],
            "confidence": row["confidence"],
            "provenance": row["provenance"],
            "sensitivity": row["sensitivity"],
            "pinned": bool(row["pinned"]),
            "day": row["day_key"],
            "created_at": row["created_at"],
            "use_count": row["use_count"],
            "last_used_at": row["last_used_at"],
            "last_surfaced_at": row["last_surfaced_at"],
            "evidence_count": row["evidence_count"],
            "claim_sig": row["claim_sig"],
            "scope_key": row["scope_key"],
            "canonical_id": row["canonical_id"],
            "supersedes_id": row["supersedes_id"],
            "superseded_by": row["superseded_by"],
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
            "groups": group_mod.groups_for(self.db, "memory", row["id"]),
            "grade": group_mod.grade_of(self.db, "memory", row["id"]),
        }
        if include_extra:
            item["extra"] = _json_load(row["extra_json"])
            item["source"] = {"kind": row["source_kind"], "ref": row["source_ref"], "session": row["session_id"]}
        return item

    # ------------------------------------------------------------------ 改
    def update(
        self,
        memory_id: str,
        *,
        set_fields: dict[str, Any] | None = None,
        supersede_with: dict[str, Any] | None = None,
        reason: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        row = self._row(memory_id)
        if row is None:
            raise NotFoundError(f"找不到记忆 {memory_id}", field="id")
        actor = actor or self.actor
        mutable = {
            "importance",
            "pinned",
            "sensitivity",
            "status",
            "kind",
            "valid_from",
            "valid_to",
            "decay_score",
            "cluster_id",
        }
        set_fields = set_fields or {}
        unknown = set(set_fields) - mutable
        if unknown:
            raise ValidationError(
                f"这些字段不允许改：{sorted(unknown)}",
                hint="原文相关字段不可变；改内容请用 supersede_with 新开一行",
                field=",".join(sorted(unknown)),
            )
        if "status" in set_fields and set_fields["status"] not in {
            "candidate",
            "active",
            "dormant",
            "archived",
            "contested",
            "discarded",
            "deleted",
        }:
            raise ValidationError("status 非法", field="status")
        if "sensitivity" in set_fields and set_fields["sensitivity"] not in {"public", "personal", "secret"}:
            raise ValidationError("sensitivity 非法", field="sensitivity")

        if supersede_with:
            new = self.add(
                supersede_with.get("text", ""),
                kind=supersede_with.get("kind", row["kind"]),
                provenance=supersede_with.get("provenance", "user_explicit"),
                source_kind=supersede_with.get("source_kind", "correction"),
                session_id=supersede_with.get("session_id") or f"fix-{ulid()[:10]}",
                sensitivity=supersede_with.get("sensitivity", row["sensitivity"]),
                actor=actor,
            )
            with self.db.tx() as conn:
                conn.execute(
                    "UPDATE memory SET status='superseded', superseded_by=? WHERE id=?", (new["id"], memory_id)
                )
                conn.execute("UPDATE memory SET supersedes_id=? WHERE id=?", (memory_id, new["id"]))
                events.append(
                    conn,
                    actor=actor,
                    op="memory.supersede",
                    target_tbl="memory",
                    target_id=new["id"],
                    subject_id=self.subject,
                    payload={"old": memory_id, "reason": reason},
                )
            return {"id": new["id"], "superseded": memory_id}

        if not set_fields:
            raise ValidationError("没有要改的字段", field="set")

        assignments = ", ".join(f"{k}=?" for k in set_fields)
        values = list(set_fields.values())
        if set_fields.get("status") == "deleted":
            # CHECK(status<>'deleted' OR deleted_at IS NOT NULL) 要求同一条语句里满足
            assignments += ", deleted_at=?"
            values.append(now_iso())
        elif row["status"] == "deleted" and "status" in set_fields:
            # 从停放里捞回来：删除时间必须一起清掉，否则一条活着的记忆还挂着「几时删的」，
            # 将来想按删除时间清理墓碑时会误伤。
            assignments += ", deleted_at=NULL"
        with self.db.tx() as conn:
            conn.execute(f"UPDATE memory SET {assignments} WHERE id=?", (*values, memory_id))
            events.append(
                conn,
                actor=actor,
                op="memory.update",
                target_tbl="memory",
                target_id=memory_id,
                subject_id=self.subject,
                payload={"set": set_fields, "reason": reason},
            )
        return self.get(memory_id, include_deleted=True)

    def delete(self, memory_id: str, *, hard: bool = False, reason: str | None = None, actor: str | None = None) -> dict[str, Any]:
        row = self._row(memory_id)
        if row is None:
            raise NotFoundError(f"找不到记忆 {memory_id}", field="id")
        actor = actor or self.actor
        if not hard:
            with self.db.tx() as conn:
                conn.execute("UPDATE memory SET status='deleted', deleted_at=? WHERE id=?", (now_iso(), memory_id))
                events.append(
                    conn,
                    actor=actor,
                    op="memory.delete",
                    target_tbl="memory",
                    target_id=memory_id,
                    subject_id=self.subject,
                    payload={"hard": False, "reason": reason},
                )
            return {"deleted": 1, "hard": False, "cascaded": {}}

        with self.db.tx() as conn:
            cascaded: dict[str, int] = {}
            # 真删会级联清掉 mention（FK ON DELETE CASCADE），但证据是 JSON，
            # 必须手动摘掉悬空指针；先记下受影响实体，删完重算热度。
            affected_entities = [
                str(item["entity_id"])
                for item in conn.execute(
                    "SELECT DISTINCT entity_id FROM entity_mention WHERE memory_seq=?", (row["seq"],)
                ).fetchall()
            ]
            cascaded["entity_mention"] = int(
                conn.execute(
                    "SELECT COUNT(*) c FROM entity_mention WHERE memory_seq=?", (row["seq"],)
                ).fetchone()["c"]
            )
            cascaded["relation_refs"] = int(
                conn.execute(
                    "SELECT COUNT(*) c FROM relation WHERE evidence_json LIKE ?",
                    (f"%{memory_id}%",),
                ).fetchone()["c"]
            )
            for rel in conn.execute(
                "SELECT id, evidence_json FROM relation WHERE evidence_json LIKE ?",
                (f"%{memory_id}%",),
            ).fetchall():
                try:
                    items = json.loads(rel["evidence_json"])
                except (json.JSONDecodeError, TypeError):
                    items = []
                kept = [
                    item
                    for item in (items if isinstance(items, list) else [])
                    if not (isinstance(item, dict) and item.get("memory_id") == memory_id)
                ]
                if kept:
                    conn.execute(
                        "UPDATE relation SET evidence_json=? WHERE id=?",
                        (json.dumps(kept, ensure_ascii=False), rel["id"]),
                    )
                else:
                    conn.execute("DELETE FROM relation WHERE id=?", (rel["id"],))

            # 派生行里的证据指针（trait/pattern/experience）：摘条目；摘空则删行。
            for table in _EVIDENCE_JSON_TABLES:
                updated, deleted = _drop_evidence_ref(conn, table, memory_id)
                cascaded[f"{table}_refs"] = updated
                if deleted:
                    cascaded[f"{table}_deleted"] = deleted

            # conflict 的 left_id/right_id 是 NOT NULL（schema 约束），不能置 NULL；
            # 冲突是派生状态，直接删行（重跑 sleep 会重新检测），避免留下悬空引用。
            cascaded["conflicts"] = int(
                conn.execute(
                    "SELECT COUNT(*) c FROM conflict WHERE "
                    "(left_tbl='memory' AND left_id=?) OR (right_tbl='memory' AND right_id=?)",
                    (memory_id, memory_id),
                ).fetchone()["c"]
            )
            conn.execute(
                "DELETE FROM conflict WHERE "
                "(left_tbl='memory' AND left_id=?) OR (right_tbl='memory' AND right_id=?)",
                (memory_id, memory_id),
            )

            # feedback.target_id 也是 NOT NULL：同样只能删行（反馈是对这条记忆的表态，
            # 记忆真删后它已经没有意义）。
            cascaded["feedback"] = int(
                conn.execute(
                    "SELECT COUNT(*) c FROM feedback WHERE target_tbl='memory' AND target_id=?",
                    (memory_id,),
                ).fetchone()["c"]
            )
            conn.execute(
                "DELETE FROM feedback WHERE target_tbl='memory' AND target_id=?", (memory_id,)
            )

            # 幂等键：普通写入的幂等键随记忆一起清掉；
            # ``journal:*`` 键必须保留——repair 靠它判断「派生过、后来被删 → 不复活」
            # （tests/test_journal_repair.py 的两条用例就是守这个语义）。
            cascaded["idem"] = int(
                conn.execute(
                    "SELECT COUNT(*) c FROM idem WHERE target_id=? AND key NOT LIKE 'journal:%'",
                    (memory_id,),
                ).fetchone()["c"]
            )
            conn.execute(
                "DELETE FROM idem WHERE target_id=? AND key NOT LIKE 'journal:%'", (memory_id,)
            )
            cascaded["journal_idem_kept"] = int(
                conn.execute(
                    "SELECT COUNT(*) c FROM idem WHERE target_id=? AND key LIKE 'journal:%'",
                    (memory_id,),
                ).fetchone()["c"]
            )

            # superseded_by 没有外键，需要手动清；supersedes_id 有 ON DELETE SET NULL，自动清。
            cascaded["superseded_cleared"] = int(
                conn.execute(
                    "SELECT COUNT(*) c FROM memory WHERE superseded_by=?", (memory_id,)
                ).fetchone()["c"]
            )
            conn.execute("UPDATE memory SET superseded_by=NULL WHERE superseded_by=?", (memory_id,))

            conn.execute("DELETE FROM memory WHERE id=?", (memory_id,))
            conn.execute(
                "DELETE FROM grp_member WHERE target_tbl='memory' AND target_id=?", (memory_id,)
            )
            conn.execute(
                "DELETE FROM target_grade WHERE target_tbl='memory' AND target_id=?", (memory_id,)
            )
            conn.execute(
                "DELETE FROM surface_log WHERE target_tbl='memory' AND target_id=?", (memory_id,)
            )

            # 实体热度重算：mention 行被级联删了，mention_count 必须跟着降。
            for entity_id in affected_entities:
                conn.execute(
                    "UPDATE entity SET mention_count=(SELECT COUNT(*) FROM entity_mention "
                    "WHERE entity_id=?) WHERE id=?",
                    (entity_id, entity_id),
                )
            cascaded["entities_recounted"] = len(affected_entities)

            events.append(
                conn,
                actor=actor,
                op="memory.hard_delete",
                target_tbl="memory",
                target_id=memory_id,
                subject_id=self.subject,
                payload={"reason": reason, "tombstone": True, "cascaded": cascaded},
            )
        return {"deleted": 1, "hard": True, "cascaded": cascaded}

    def retract(
        self,
        memory_id: str,
        *,
        reason: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """撤回一条记忆：**不是删除**。

        语义对齐原话层的墓碑：这条说过，但不算数了。实现上落到 ``status='archived'``
        ——「不算数」的记忆不该再进检索/醒来卡，但原文、证据、事件链一个字都不改，
        想恢复就 ``update --status active``。和 ``memory_delete`` 的区别写在这里，
        因为这是最容易混的一处：

        - 撤回：留痕、可恢复、默认看不见（``memory.retract`` 事件）；
        - 删除：清账（软删 7 天可恢复，``--hard`` 连行一起没）。
        """
        row = self._row(memory_id)
        if row is None:
            raise NotFoundError(f"找不到记忆 {memory_id}", field="id")
        actor = actor or self.actor
        if row["status"] == "deleted":
            raise ValidationError(
                "已删除的记忆不能撤回",
                hint="删除是清账，撤回是表态；已被删除的请先 update --status active 再决定。",
                field="target",
            )
        if row["status"] == "archived" and self._is_retracted(memory_id):
            return {
                "ok": True,
                "already": True,
                "id": memory_id,
                "status": "archived",
                "restore_hint": "update --status active 可恢复",
            }
        previous = row["status"]
        with self.db.tx() as conn:
            conn.execute("UPDATE memory SET status='archived' WHERE id=?", (memory_id,))
            events.append(
                conn,
                actor=actor,
                op="memory.retract",
                target_tbl="memory",
                target_id=memory_id,
                subject_id=self.subject,
                payload={"from": previous, "reason": reason},
            )
        return {
            "ok": True,
            "already": False,
            "id": memory_id,
            "status": "archived",
            "from": previous,
            "restore_hint": "update --status active 可恢复",
        }


    def history(self, target: str, *, limit: int = 50) -> list[dict[str, Any]]:
        if ":" in target:
            tbl, ident = target.split(":", 1)
        else:
            tbl, ident = "memory", target
        rows = self.db.conn.execute(
            "SELECT * FROM event_log WHERE target_id=? ORDER BY seq DESC LIMIT ?", (ident, limit)
        ).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "event": r["id"],
                    "ts": r["ts"],
                    "actor": r["actor"],
                    "op": r["op"],
                    "table": r["target_tbl"],
                    "payload": _json_load(r["payload_json"]),
                }
            )
        return out

    # ------------------------------------------------------------------ 查

    def search(
        self,
        query: str,
        *,
        k: int | None = None,
        budget: int | None = None,
        mode: str = "auto",
        filters: dict[str, Any] | None = None,
        client: str | None = None,
        record: bool = True,
    ) -> dict[str, Any]:
        # 编排在 memory/retrieval/search.py；签名/返回值/异常语义与拆分前逐字一致。
        return search_mod.run_search(self, query, k=k, budget=budget, mode=mode,
                                     filters=filters, client=client, record=record)

    # ------------------------------------------- 旧私有名（一行委托，实现在 retrieval/）
    # search 编排与已有测试（test_vector/test_graph_spread）仍按旧名调用，所以保留。

    def _chan_tokens(self, q: str, *, drop: frozenset[str] | set[str] | None = None) -> list[int]:
        return lexical_mod.chan_tokens(self, q, drop=drop)

    def _chan_terms(self, terms: list[str], *, limit: int = 50) -> list[int]:
        return lexical_mod.chan_terms(self, terms, limit=limit)

    def _chan_trigram(self, q: str) -> list[int]:
        return lexical_mod.chan_trigram(self, q)

    def _chan_like(self, q: str) -> list[int]:
        return lexical_mod.chan_like(self, q)

    def _term_weights(self, terms: list[str]) -> dict[str, float]:
        return lexical_mod.term_weights(self, terms)

    def last_memory_at(self) -> str | None:
        return lexical_mod.last_memory_at(self)

    def _memory_count(self) -> int:
        return lexical_mod.memory_count(self)

    @staticmethod
    def _seed_weights(pending, cov_by_seq, limit):
        return channels_mod.seed_weights(pending, cov_by_seq, limit)

    def _graph_mass(self, seeds, graph_cfg):
        return channels_mod.graph_mass(self, seeds, graph_cfg)

    def _graph_append(self, *, mass, picked, graph_cfg=None):
        return channels_mod.graph_append(self, mass=mass, picked=picked, graph_cfg=graph_cfg)

    def _temporal_append(self, *, q, fused, payload_seq, filters, temporal_cfg, update_q):
        return channels_mod.temporal_append(self, q=q, fused=fused, payload_seq=payload_seq,
                                            filters=filters, temporal_cfg=temporal_cfg,
                                            update_q=update_q)

    def _entity_append(self, cand, *, filters):
        return channels_mod.entity_append(self, cand, filters=filters)

    def _vector_append(self, *, q, cov_by_seq, picked, filters, vec_cfg):
        return channels_mod.vector_append(self, q=q, cov_by_seq=cov_by_seq, picked=picked,
                                          filters=filters, vec_cfg=vec_cfg)

    def _append_items(self, items, *, payload, payload_seq, payload_cov, k, budget, spent,
                      policy="zero_cover", head=4):
        return channels_mod.append_items(self, items, payload=payload, payload_seq=payload_seq,
                                         payload_cov=payload_cov, k=k, budget=budget,
                                         spent=spent, policy=policy, head=head)

    def _raw_fallback(self, *, q, terms, k, cov_best, got, have_graph=False):
        return channels_mod.raw_fallback(self, q=q, terms=terms, k=k, cov_best=cov_best,
                                         got=got, have_graph=have_graph)

    def _fetch_scored(self, fused, filters, *, limit):
        return channels_mod.fetch_scored(self, fused, filters, limit=limit)

    def _record_search(self, *, query, mode, channels, payload, used, latency_ms, client,
                       uncertainty=None):
        channels_mod.record_search(self, query=query, mode=mode, channels=channels,
                                   payload=payload, used=used, latency_ms=latency_ms,
                                   client=client, uncertainty=uncertainty)

    # ------------------------------------------------------------------ 统计
    def stats(self) -> dict[str, Any]:
        conn = self.db.conn
        by_status = {r["status"]: r["c"] for r in conn.execute(
            "SELECT status, COUNT(*) c FROM memory GROUP BY status"
        )}
        by_kind = {r["kind"]: r["c"] for r in conn.execute(
            "SELECT kind, COUNT(*) c FROM memory GROUP BY kind"
        )}
        total = sum(by_status.values())
        detail = self.db.integrity_detail()
        integrity: dict[str, Any] = {"integrity_ok": detail.get("ok")}
        if detail.get("errors"):
            integrity["integrity_errors"] = detail["errors"]
        if detail.get("error"):
            integrity["integrity_error"] = detail["error"]
        size = 0
        if self.db.path != ":memory:":
            from pathlib import Path

            p = Path(self.db.path)
            if p.exists():
                size = p.stat().st_size
        return {
            "subject": self.subject,
            "memory_total": total,
            "by_status": by_status,
            "by_kind": by_kind,
            "event_count": conn.execute("SELECT COUNT(*) c FROM event_log").fetchone()["c"],
            # 「最近记了什么」要有确定答案：按写入顺序取最后几条，别让人拿相关性检索去猜。
            "recent": [
                {
                    "id": row["id"],
                    "day": row["day_key"],
                    "kind": row["kind"],
                    "status": row["status"],
                    "text": str(row["text_raw"] or "")[:80],
                }
                for row in conn.execute(
                    "SELECT id, day_key, kind, status, text_raw FROM memory "
                    "WHERE status<>'deleted' ORDER BY seq DESC LIMIT 5"
                )
            ],
            "schema_version": self.db.meta("schema_version"),
            "db_size_bytes": size,
            # 带上库路径：出问题时第一件要确认的是「它读的是不是同一个库」。
            "db_path": str(self.db.path or ""),
            **integrity,
        }

    def capabilities(self) -> dict[str, Any]:
        import pmem

        jieba_ok = True
        try:
            import jieba  # noqa: F401
        except Exception:
            jieba_ok = False
        vec_rows = self.db.conn.execute("SELECT COUNT(*) c FROM memory_vec_plain").fetchone()["c"]
        vec_ready, vec_reason = vector_mod.ready(self.cfg)
        degraded: list[str] = []
        # jieba 不是必需件：主词法是自带的双字切分，索引也是按它建的。
        # 把「没装 jieba」报成「降级」会让人以为系统不完整（实测有人当成异常来问）。
        # 真正该报的是「用的是哪套切词」，所以改成 features.tokenizer。
        if vec_rows == 0:
            degraded.append("no_embedding")
        return {
            "api": "v1",
            "server": pmem.__version__,
            "schema_version": pmem.SCHEMA_VERSION,
            "name": {"cn": pmem.__name_cn__, "en": pmem.__name_en__, "code": pmem.__codename__},
            "features": {
                "vector": bool(vec_ready and vec_rows > 0),
                # 主词法：装了 jieba 用它，否则用自带的双字切分（默认、索引也按它建）。
                "tokenizer": "jieba" if jieba_ok else "bigram",
                "jieba": jieba_ok,
                "net": True,
                "surface": True,
                "vec0": False,
                "llm_t2": False,
                "rest": bool(self.cfg.get("gate.rest_enabled", False)),
                "auth": bool(self.cfg.get("gate.token", "")),
                "rate_limit": int(self.cfg.get("gate.rate_limit_per_min", 0) or 0),
                "mcp_transports": ["stdio"],
            },
            "vector_state": {
                "enabled": bool(vector_mod.settings(self.cfg).get("enabled", False)),
                "ready": vec_ready,
                "reason": vec_reason,
                "vectors": int(vec_rows),
            },
            "degraded": degraded,
            "limits": {
                "max_text_chars": self.cfg.get("limits.max_text_chars", 8000),
                "max_k": self.cfg.get("limits.max_k", 50),
                "max_batch": self.cfg.get("limits.max_batch", 200),
            },
            "budget": {"daily_llm_tokens": 0, "remaining": 0},
        }

    # ------------------------------------------------------------------ 校验
    def _validate_add(
        self,
        text: str,
        kind: str,
        provenance: str,
        sensitivity: str,
        pinned: bool,
        valid_from: str | None,
        valid_to: str | None,
    ) -> None:
        from ..base.errors import KINDS, PROVENANCE, SENSITIVITY

        if not isinstance(text, str) or not text.strip():
            raise ValidationError("text 不能为空", field="text")
        max_chars = self.cfg.get("limits.max_text_chars", 8000)
        if len(text) > max_chars:
            raise ValidationError(
                f"text 超过 {max_chars} 字符",
                hint="请分段提交，并在 extra.segment 标注",
                field="text",
            )
        if kind not in KINDS:
            raise ValidationError(f"kind 非法：{kind}", hint=f"可选 {KINDS}", field="kind")
        if provenance not in PROVENANCE:
            raise ValidationError(f"provenance 非法：{provenance}", hint=f"可选 {PROVENANCE}", field="provenance")
        if sensitivity not in SENSITIVITY:
            raise ValidationError(f"sensitivity 非法：{sensitivity}", field="sensitivity")
        if sensitivity == "secret" and not self.cfg.get("policy.allow_secret", False):
            raise DeniedError(
                "secret 级内容默认不入库",
                hint="把 policy.allow_secret 设为 true 才会存；否则请只存指针",
            )
        if valid_from and valid_to and valid_to < valid_from:
            raise ConflictError("valid_to 早于 valid_from", field="valid_to")
