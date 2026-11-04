"""PMem Core 的对外实现：加、取、改、删、查、统计。

铁律（PLAN.md §1）：

- 原文只追加：``text_raw/text_norm/created_at/provenance`` 由触发器钉死，纠错只能新开一行；
- 结论必有证据：派生字段（confidence / evidence_*）随时可重算；
- 单写者：所有写操作走 ``Database.tx()``（BEGIN IMMEDIATE）。
"""

from __future__ import annotations

import json
import math
import os
import time
from typing import Any

from ..base import events
from ..base.clock import day_key, days_between, now, now_iso, parse_iso, to_iso
from ..base.config import Config
from ..base.db import Database
from ..base.errors import ConflictError, DeniedError, NotFoundError, ValidationError
from ..base.ids import ulid
from . import groups as group_mod
from . import scoring
from .text import (
    ALIAS_TOKENS,
    char_ngrams,
    claim_sig,
    estimate_tokens,
    is_mostly_ascii,
    jaccard,
    normalize,
    query_variants,
    sanitize,
    sha256_hex,
    tokenize,
)
from . import query as query_mod
from . import answer_type as answer_type_mod
from . import graph as graph_mod
from . import journal as journal_mod
from . import scope as scope_mod
from . import temporal as temporal_mod
from . import vector as vector_mod

# 检索默认能看到的状态。
#
# 这里曾经只有 ("active", "contested")，于是「休眠」等于「从检索里消失」——
# 30 天试跑里 21/22 条记忆进入 dormant，`memory_search` 一条都找不到。
# 那是**想不起来**，不是**忘了**：记忆还在库里，只是不再被主动提起。
# 主动浮现（surface）可以只挑活跃的，但用户/模型主动来问时必须能挖到。
# candidate 也要能被检索到：它表示「还没确认」，不等于「不存在」。我们自己的实验结论
# 大多以 observed 写进来、重要度偏低，落在 candidate；把它们藏起来的结果是
# 「我自己记过的事，问的时候找不到」。答案里会带 status，让上层自己判断可信度。
VISIBLE_STATUS = ("active", "contested", "dormant", "candidate")

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
    return json.dumps(data or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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

        existing = self.db.conn.execute(
            "SELECT * FROM memory WHERE subject_id=? AND text_sha256=? AND status<>'deleted' "
            "ORDER BY created_at LIMIT 1",
            (self.subject, digest),
        ).fetchone()

        if existing is not None:
            result = self._merge_existing(existing, provenance, session, actor, idem_key, dry_run)
            if not dry_run:
                result.update(
                    self._attach_meta(existing["id"], groups, grade, grade_reason)
                )
            return result

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
        """同一条原文再次入库：不新增行，只并证据（同会话不重复计）。"""
        extra = _json_load(existing["extra_json"])
        tiers: list[str] = list(extra.get("evidence_tiers") or [existing["provenance"]])
        same_session = existing["session_id"] == session
        if not same_session:
            tiers.append(provenance)
        conf = scoring.confidence_from_tiers(existing["provenance"], tiers)
        new_evidence = 0 if same_session else 1
        extra.update({"evidence_tiers": tiers, "evidence_sessions": len(tiers)})

        if not dry_run:
            with self.db.tx() as conn:
                conn.execute(
                    "UPDATE memory SET evidence_count=evidence_count+?, confidence=?, extra_json=? WHERE id=?",
                    (new_evidence, conf, _json_dump(extra), existing["id"]),
                )
                events.append(
                    conn,
                    actor=actor,
                    op="memory.evidence",
                    target_tbl="memory",
                    target_id=existing["id"],
                    subject_id=self.subject,
                    payload={"new_evidence": new_evidence, "sessions": len(tiers)},
                    idem_key=idem_key,
                )
                if idem_key:
                    events.idem_remember(conn, idem_key, "memory.evidence", existing["id"])

        return {
            "id": existing["id"],
            "status": existing["status"],
            "importance": existing["importance"],
            "confidence": conf,
            "duplicate_of": existing["id"],
            "sanitized": False,
            "idempotent_replay": False,
        }

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

        cascaded = {
            "entity_mention": self.db.conn.execute(
                "SELECT COUNT(*) c FROM entity_mention WHERE memory_seq=?", (row["seq"],)
            ).fetchone()["c"],
            "relation_refs": self.db.conn.execute(
                "SELECT COUNT(*) c FROM relation WHERE evidence_json LIKE ?", (f"%{memory_id}%",)
            ).fetchone()["c"],
        }
        with self.db.tx() as conn:
            # 真删会级联清掉 mention，但关系证据是 JSON，必须手动摘掉悬空指针（I4）
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
            events.append(
                conn,
                actor=actor,
                op="memory.hard_delete",
                target_tbl="memory",
                target_id=memory_id,
                subject_id=self.subject,
                payload={"reason": reason, "tombstone": True},
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
        started = time.perf_counter()
        q = normalize(query or "")
        if not q:
            raise ValidationError("query 不能为空", field="query")
        k = min(k or self.cfg.get("retrieval.top_k", 8), self.cfg.get("limits.max_k", 50))
        budget = budget or self.cfg.get("retrieval.token_budget", 600)
        filters = filters or {}
        rrf_k = self.cfg.get("retrieval.rrf_k", 60)

        terms = query_mod.content_terms(q)
        # 语文范围配置要在召回阶段就用（角色词/名字当种子），所以提前算。
        scope_cfg = scope_mod.settings(self.cfg)
        scope_on = bool(scope_cfg.get("enabled", True))
        # 实体桥（M22）：默认关闭，由 retrieval.entity.enabled 打开；benchmark 用环境开关
        # 强制开/关（bench/longrun/systems/pmem.py），线上默认值由 Lead 决定。
        ent_raw = self.cfg.get("retrieval.entity")
        ent_cfg: dict[str, Any] = ent_raw if isinstance(ent_raw, dict) else {}
        entity_on = bool(ent_cfg.get("enabled", False))
        entity_bridge: dict[str, Any] = {}
        channels: dict[str, list[int]] = {}
        if mode in ("auto", "keyword"):
            # 查询改写：原句的命中永远保留，变体只往后补候选，不会挤掉原来的结果。
            content_q = " ".join(query_mod.content_terms(q))
            tok = self._chan_tokens(content_q) if content_q else []
            for seq in self._chan_tokens(q):
                if seq not in tok:
                    tok.append(seq)
            # 变体里「用户」这类自称展开注入的高 df 词会把候选池灌满同主语记忆，
            # 所以**只删这个词本身**（保留「户叫」这类真正有区分度的接缝双字）。
            # 原句自己写了「用户」就按原样查。
            alias_drop = ALIAS_TOKENS if not any(word in q for word in ALIAS_TOKENS) else frozenset()
            for variant in query_variants(q):
                for seq in self._chan_tokens(variant, drop=alias_drop):
                    if seq not in tok:
                        tok.append(seq)
            if tok:
                channels["tok"] = tok
            tri = self._chan_trigram(q)
            if tri:
                channels["tri"] = tri
            # 语文范围召回（M16）：问句里的名字（「X她/他」的 X）和长尾角色词（小姨/二舅）
            # 单独当一路 FTS 种子。词法主通道按 bigram 拼，名字+角色被切成接缝双字时
            # 会漏掉「小明爸爸…」这种候选，这一路按**整词**召回。
            if scope_on and bool(scope_cfg.get("recall", True)):
                scope_terms = scope_mod.recall_terms(q)
                # 只在「名字+代词」或者词法本身弱的时候才加这一路：
                # 角色词（儿子/爸爸/同事）df 不低，无条件加会把候选池灌大、检索变慢，
                # 而实测它的收益只出现在「X她/他」这类需要按名字召回的问句上。
                weak = len(channels.get("tok") or []) < k * 2
                named = bool(scope_mod.named_pronoun_subjects(q))
                if scope_terms and (named or weak):
                    scope_seqs = self._chan_terms(
                        scope_terms, limit=int(scope_cfg.get("recall_limit", 16) or 16)
                    )
                    if scope_seqs:
                        channels["scope"] = scope_seqs
            # 实体桥（M22）：问句里出现了库里**真实存在**的实体（名字/关系称呼/别名）时，
            # 顺着 entity_mention 把「另一条记忆里说的事」放进候选；再从词法种子做一跳
            # 共现实体扩展——多跳题的典型形态是「种子里有『王子轩不交作业』，答案在
            # 『王子轩爸妈离婚』」，两条之间没有共同稀有词，只有实体能把它们连起来。
            if entity_on:
                from ..net import entities as net_entities

                try:
                    entity_bridge = net_entities.memory_hits(
                        self.db.conn,
                        subject=self.subject,
                        query=q,
                        limit=int(ent_cfg.get("limit", 16) or 16),
                        seed_seqs=(channels.get("tok") or [])[
                            : int(ent_cfg.get("hop_seeds", 8) or 8)
                        ],
                        hop=bool(ent_cfg.get("hop", True)),
                        hop_entities=int(ent_cfg.get("hop_entities", 6) or 6),
                        hop_min_mentions=int(ent_cfg.get("hop_min_mentions", 2) or 2),
                        hop_limit=int(ent_cfg.get("hop_limit", 12) or 12),
                        min_mentions=int(ent_cfg.get("min_mentions", 2) or 2),
                    )
                except Exception as exc:  # noqa: BLE001 实体桥是增强，挂了不能带崩检索
                    entity_bridge = {"error": f"{type(exc).__name__}: {exc}"}
                if entity_bridge.get("channel"):
                    channels["entity"] = list(entity_bridge["channel"])
            # 向量通道（M30）：把语义相似度**当一路候选**参与 RRF，而不是只在末尾补两条。
            # 为什么需要它：我们自己聊天里的问句（「许可证现在怎么表述的？」）几乎没有稀有词，
            # 词法通道被高频双字带着跑，长摘要反而排前面；而语义通道能认出「讲的是同一件事」。
            # 默认关：先 A/B，确认不会把公开语料的可答率带下去再开。
            vec_channel_cfg = vector_mod.settings(self.cfg)
            if bool(vec_channel_cfg.get("channel", False)) and bool(
                vec_channel_cfg.get("enabled", False)
            ):
                try:
                    vector_mod.ensure_once(self.db, self.cfg)
                    sims = vector_mod.search(
                        self.db,
                        self.cfg,
                        q,
                        limit=max(4, int(vec_channel_cfg.get("channel_limit", 12) or 12)),
                        min_score=float(vec_channel_cfg.get("min_score", 0.55) or 0.0),
                    )
                except Exception:  # noqa: BLE001 向量挂了不能带崩词法
                    sims = {}
                if sims:
                    channels["vec"] = [
                        seq for seq, _score in sorted(sims.items(), key=lambda kv: -kv[1])
                    ]
            if not channels:
                # 兜底：只有词法与 trigram 都没命中才做全表 LIKE（慢，但少见）
                like = self._chan_like(q)
                if like:
                    channels["like"] = like

        # 每路权重：默认全 1.0；实体通道按 retrieval.entity.weight 降权（A/B 扫参用）。
        # 只把 weights 交给 rrf，**不改各通道的 rank 列表**——rank 列表同时决定候选池
        # 里谁先进 64 条，改它副作用比调权大；调权只改融合分数。
        channel_names = list(channels)
        channel_weights: list[float] | None = None
        if "entity" in channels:
            channel_weights = [1.0] * len(channel_names)
            channel_weights[channel_names.index("entity")] = max(
                0.0, float(ent_cfg.get("weight", 1.0) or 0.0)
            )
        fused = scoring.rrf(list(channels.values()), k=rrf_k, weights=channel_weights)
        # 归一化尺子固定在**词法那几路的权重和**上（不含 entity 的权重）：这样开/关实体桥、
        # 或给实体路降权时，词法候选的 rrf_norm 一分不变，A/B 才可归因；实体路自己的贡献
        # 按 weight 线性缩放（weight<=1 时归一化值不会越过词法上限，>1 会被 _clip 截住）。
        base_channels = max(1, len(channels) - (1 if "entity" in channels else 0))
        if not fused:
            rows: list[Any] = []
        else:
            pool = max(int(self.cfg.get("retrieval.candidate_pool", 64) or 64), k * 8)
            rows = self._fetch_scored(fused, filters, limit=pool)

        candidate_sets = [query_mod.bigrams(row["text_norm"]) for row in rows]
        row_sets = {row["seq"]: s for row, s in zip(rows, candidate_sets)}
        ngram_cache: dict[int, set[str]] = {}
        half_lives = self.cfg.get("retrieval.half_life_days", {})
        row_ids = [row["id"] for row in rows]
        group_map = group_mod.group_map(self.db, "memory", row_ids)
        grade_map = group_mod.grade_map(self.db, "memory", row_ids)
        cov_best = 0.0
        cov_by_seq: dict[int, float] = {}
        pending: list[tuple[Any, float]] = []  # (row, relevance)
        term_weights = self._term_weights(terms)
        for row in rows:
            rrf_norm = scoring.rrf_normalize(fused[row["seq"]], base_channels, rrf_k)
            # 覆盖率：查询里的实词，这条记忆命中了几成。RRF 的相邻名次只差百分之一，
            # 长问句下基本是平的——所以「相关性」必须由覆盖率来定，而不是名次。
            cov = query_mod.coverage_idf_sets(
                terms, candidate_sets, row_sets[row["seq"]], term_weights or None
            )
            cov_best = max(cov_best, cov)
            cov_by_seq[row["seq"]] = cov
            relevance = (0.6 * cov + 0.4 * rrf_norm) if terms else rrf_norm
            pending.append((row, relevance))
            ngram_cache[row["seq"]] = char_ngrams(row["text_norm"][:200])

        # 关联扩散（M12）：先用**覆盖率**选种子，再在稀有词共现图上做 PPR，
        # 把扩散质量当 ``graph_affinity`` 记进 final_score —— 只重排已有候选，不额外注入。
        # 这一格权重（w_graph）从 0.5 版起就留着没用过，现在终于有东西填了。
        spread_info: dict[str, Any] = {}
        affinity: dict[int, float] = {}
        mass_out: dict[int, float] = {}
        graph_cfg = self.cfg.get("retrieval.graph", {}) or {}
        if isinstance(graph_cfg, dict) and bool(graph_cfg.get("enabled", True)) and pending:
            # 只在**词法没把握**的时候动图：覆盖率高说明问句和某条记忆本来就对得上，
            # 这时重排只可能把对的挤掉（实测 p09-q25 就是这么丢的）。
            trigger = float(graph_cfg.get("trigger_coverage", 0.45) or 0.0)
            if cov_best >= trigger and len(rows) >= k:
                spread_info = {"skipped": "coverage", "coverage": round(cov_best, 3)}
            else:
                seeds = self._seed_weights(pending, cov_by_seq, int(graph_cfg.get("seeds", 5) or 5))
                mass = self._graph_mass(seeds, graph_cfg) if seeds else {}
                mass_out = mass
                if mass and bool(graph_cfg.get("rerank", False)):
                    # 默认只补候选、不重排：实测重排会为了强邻接把对的挤到第 9 名。
                    affinity = mass
                spread_info = {"seeds": len(seeds), "reached": len(mass), "coverage": round(cov_best, 3)}

        # 时效语义（M14）：两根时间轴不能混用。``created_at`` 是「这行什么时候写进库」，
        # ``valid_from`` 才是「这件事什么时候成立」——回填/日记派生出来的旧事实如果用写入
        # 时间算新旧，会显得「刚刚发生」，把真正的新说法挤下去。
        temporal_cfg = temporal_mod.settings(self.cfg)
        temporal_on = bool(temporal_cfg.get("enabled", True))
        temporal_axis = str(temporal_cfg.get("axis", "valid_from") or "valid_from")
        # 有效期边界（M15）：已失效/未生效的事实默认降权，但「问过去/问将来」的句子例外。
        expired_penalty = float(temporal_cfg.get("expired_penalty", 0.10) or 0.0)
        pending_penalty = float(temporal_cfg.get("pending_penalty", 0.06) or 0.0)
        grace_days = float(temporal_cfg.get("valid_grace_days", 0.0) or 0.0)
        past_q = temporal_on and temporal_mod.is_past_query(q)
        old_q = temporal_on and temporal_mod.is_old_query(q)
        future_q = temporal_on and temporal_mod.is_future_query(q)
        # 「问旧事」把新近度反过来用（可在配置里关掉，做消融）。
        # 但「当初…后来…」这种复合问句不算：它同时问旧事和新结果，反转会把新结果压掉
        # （实测 p09-q25「小邹当初从哪儿回来的，后来我俩怎么了」丢掉「分手」那条）。
        old_invert = (
            old_q
            and not temporal_mod.is_update_query(q)
            and bool(temporal_cfg.get("invert_recency_on_old", True))
        )
        # 语文范围（M15）：角色词、「我」vs「我的朋友」、名字+代词指代本人。
        role_bonus = float(scope_cfg.get("role_bonus", 0.035) or 0.0)
        other_owner_penalty = float(scope_cfg.get("other_owner_penalty", 0.06) or 0.0)
        owner_conflict_penalty = float(scope_cfg.get("owner_conflict_penalty", 0.08) or 0.0)
        q_self = scope_on and scope_mod.is_self_query(q)
        q_relations = scope_mod.relation_words(q) if scope_on else set()
        update_q = temporal_on and temporal_mod.is_update_query(q)
        fresh_bonus = float(temporal_cfg.get("fresh_bonus", 0.0) or 0.0) if update_q else 0.0
        stale_penalty = float(temporal_cfg.get("stale_penalty", 0.0) or 0.0) if update_q else 0.0
        fresh_hl = float(temporal_cfg.get("fresh_half_life_days", 30) or 30)
        # 时间锚点（M28）：「十月十二号」「八月」说的是**哪一次**。
        # 只认明确日期/月份，模糊词（上次/那次/以前）一律不猜——猜错窗口比不猜更糟。
        anchor_cfg = temporal_cfg.get("anchor")
        anchor_cfg = anchor_cfg if isinstance(anchor_cfg, dict) else {}
        anchor_win = (
            temporal_mod.anchor(q)
            if temporal_on and bool(anchor_cfg.get("enabled", True))
            else None
        )
        anchor_bonus = float(anchor_cfg.get("bonus", 0.06) or 0.0)
        anchor_miss = float(anchor_cfg.get("miss_factor", 1.0) or 1.0)
        anchor_tol = int(anchor_cfg.get("tolerance_days", 7) or 0)
        # 答案类型（M28）：问数字/地点/时间时，候选里没有那种东西的别抢前排。
        # person 不做字面判断（没有 NER），kind_of 给了也跳过。
        atype_cfg = self.cfg.get("retrieval.answer_type")
        atype_cfg = atype_cfg if isinstance(atype_cfg, dict) else {}
        atype = None
        if bool(atype_cfg.get("enabled", True)):
            kind = answer_type_mod.kind_of(q)
            atype = kind if kind and kind != "person" else None
        type_bonus = float(atype_cfg.get("bonus", 0.06) or 0.0)
        type_miss = float(atype_cfg.get("miss_factor", 1.0) or 1.0)
        scored: list[tuple[int, float]] = []
        stale_seqs: dict[int, str] = {}
        by_seq: dict[int, dict[str, Any]] = {}
        debug_rows: list[dict[str, Any]] | None = [] if os.environ.get("PMEM_DEBUG_SEARCH") else None
        for row, relevance in pending:
            stamp: str | None = None
            if temporal_on and temporal_axis == "valid_from":
                stamp = temporal_mod.real_axis(row)
                age = days_between(stamp) if stamp else days_between(row["created_at"])
            else:
                age = days_between(row["created_at"])
            stamp = stamp or row["created_at"]
            hl = float(half_lives.get(row["kind"], 365))
            recency_v = scoring.recency(age, hl)
            if old_invert:
                # 问「第一次/当初/最早」：新近度反过来用——越老越可能是答案。
                recency_v = 1.0 - recency_v
            score = scoring.final_score(
                rrf_norm=relevance,
                importance_v=row["importance"],
                confidence_v=row["confidence"],
                recency_v=recency_v,
                graph_affinity=min(1.0, float(affinity.get(row["seq"], 0.0))),
                w_rrf=self.cfg.get("retrieval.w_rrf", 0.55),
                w_importance=self.cfg.get("retrieval.w_importance", 0.15),
                w_confidence=self.cfg.get("retrieval.w_confidence", 0.12),
                w_recency=self.cfg.get("retrieval.w_recency", 0.10),
                w_graph=self.cfg.get("retrieval.w_graph", 0.08),
            )
            stale_kind = ""
            if (
                temporal_on
                and not past_q
                and not old_invert
                and temporal_mod.expired(row, grace_days=grace_days)
            ):
                # 有效期过了：这条事实不再成立。问「以前/当时」时不罚——那时它成立。
                stale_kind = "expired"
                score = max(0.0, score - expired_penalty)
            elif temporal_on and not future_q and temporal_mod.pending(row, grace_days=grace_days):
                # 还没生效：问「现在/最近」时它不该抢答；问「下周/计划」时反而该优先。
                stale_kind = "pending"
                score = max(0.0, score - pending_penalty)
            if stale_kind:
                stale_seqs[int(row["seq"])] = stale_kind
            # 时间锚点（M28）：问句点明了哪一天/哪个月，落在窗口里的加分，
            # 窗口外的按配置压一档（默认 1.0 = 只加分不扣分，扣分要 A/B 证明过再开）。
            anchor_state = ""
            if anchor_win is not None:
                in_win = temporal_mod.in_anchor_window(stamp, anchor_win, tolerance_days=anchor_tol)
                if in_win is True:
                    anchor_state = "in"
                    if anchor_bonus:
                        score = min(1.0, score + anchor_bonus)
                elif in_win is False:
                    anchor_state = "out"
                    if anchor_miss < 1.0:
                        score = max(0.0, score * anchor_miss)
            # 答案类型（M28）：问「多少/哪+地点/什么时候」时，候选里得有那种东西的字面证据。
            type_state = ""
            if atype:
                if answer_type_mod.has_evidence(row["text_raw"], atype):
                    type_state = "hit"
                    if type_bonus:
                        score = min(1.0, score + type_bonus)
                else:
                    type_state = "miss"
                    if type_miss < 1.0:
                        score = max(0.0, score * type_miss)
            if scope_on:
                if role_bonus:
                    score += scope_mod.role_bonus(q, row["text_raw"], bonus=role_bonus)
                name_bonus = float(scope_cfg.get("name_bonus", 0.04) or 0.0)
                if name_bonus:
                    score += scope_mod.name_focus(q, row["text_raw"], bonus=name_bonus)
                if other_owner_penalty and q_self and not q_relations and scope_mod.other_owner(
                    row["text_raw"]
                ):
                    score = max(0.0, score - other_owner_penalty)
                if owner_conflict_penalty and scope_mod.owner_conflict(row["text_raw"], q):
                    score = max(0.0, score - owner_conflict_penalty)
                if scope_on and (role_bonus or other_owner_penalty or owner_conflict_penalty):
                    score = min(1.0, score)
            if update_q and (fresh_bonus or stale_penalty):
                if fresh_bonus:
                    score = min(1.0, score + fresh_bonus * temporal_mod.freshness(row, half_life_days=fresh_hl))
                if stale_penalty and temporal_mod.looks_stale(row["text_raw"]):
                    score = max(0.0, score - stale_penalty)
            boost = group_mod.boost_for(grade_map.get(row["id"]))
            if boost:
                score = max(0.0, min(1.0, score + boost))
            scored.append((row["seq"], score))
            by_seq[row["seq"]] = {"__row": row}
            if debug_rows is not None:
                debug_rows.append(
                    {
                        "seq": row["seq"],
                        "id": row["id"],
                        "cov": round(float(cov_by_seq.get(row["seq"], 0.0)), 3),
                        "affinity": round(float(affinity.get(row["seq"], 0.0)), 3),
                        "score": round(score, 4),
                        "anchor": anchor_state,
                        "atype": type_state,
                        "text": (row["text_raw"] or "")[:120],
                    }
                )

        # 有效期边界（M15）：已失效/未生效的事实**不跟当前有效的候选抢第一**。
        # 只把它们的分数压到「最好的有效候选之下」，不直接丢弃——没有有效候选时
        # （比如整库都是历史事实）它们仍然会出现在结果里，只是排在后面。
        if temporal_on and stale_seqs:
            floor_cov = float(temporal_cfg.get("valid_cov_floor", 0.0) or 0.0)
            best_valid: float | None = None
            for seq, score in scored:
                if seq in stale_seqs:
                    continue
                if float(cov_by_seq.get(seq, 0.0) or 0.0) >= floor_cov:
                    best_valid = score if best_valid is None else max(best_valid, score)
            if best_valid is not None:
                capped = 0
                for idx, (seq, score) in enumerate(scored):
                    if seq in stale_seqs and score >= best_valid:
                        scored[idx] = (seq, max(0.0, best_valid - 0.005))
                        capped += 1
                if capped:
                    temporal_info_cap = {"stale_capped": capped, "stale": dict(stale_seqs)}
                else:
                    temporal_info_cap = {"stale": dict(stale_seqs)}
            else:
                temporal_info_cap = {"stale": dict(stale_seqs)}
        else:
            temporal_info_cap = {}

        # MMR 的相似度只算一次：不缓存的话是 O(n·k²) 次 Jaccard，10k 库上就吃掉 30 ms
        sim_cache: dict[tuple[int, int], float] = {}

        def sim(a: int, b: int) -> float:
            key = (a, b) if a < b else (b, a)
            cached = sim_cache.get(key)
            if cached is None:
                cached = jaccard(ngram_cache.get(a, set()), ngram_cache.get(b, set()))
                sim_cache[key] = cached
            return cached

        mmr_pool = int(self.cfg.get("retrieval.mmr_pool", 32) or 0)
        if mmr_pool and len(scored) > mmr_pool:
            # 全量 MMR 是 O(n·k²) 的 Jaccard，实测吃掉 40% 检索时间，
            # 收益只有偶尔一条近重复——先按相关性留前 pool 条，再做去冗。
            scored.sort(key=lambda pair: -pair[1])
            del scored[mmr_pool:]

        picked = scoring.mmr_select(
            scored,
            sim,
            limit=max(k * 3, k),
            lam=self.cfg.get("retrieval.mmr_lambda", 0.7),
        )

        # 相对覆盖率下限（M28）：比「最佳候选」覆盖率低太多的条目，多半只是同一批人的
        # 另一件事（问「钓鱼在哪个湖」，候选里混进「府河只钓到两条小鲫鱼」）。
        # 只截**词法候选**，后面图/向量/实体补进来的多跳答案不受影响。
        floor_frac = float(self.cfg.get("retrieval.min_rel_coverage", 0.0) or 0.0)
        if floor_frac > 0 and picked and cov_by_seq:
            best_cov = max(cov_by_seq.values())
            if best_cov > 0:
                kept = [pair for pair in picked if cov_by_seq.get(pair[0], 0.0) >= floor_frac * best_cov]
                picked = kept or picked[:1]

        payload: list[dict[str, Any]] = []
        payload_seq: list[int] = []
        for seq, score in picked:
            row = by_seq[seq]["__row"]
            payload_seq.append(seq)
            payload.append(
                {
                    "id": row["id"],
                    "text": row["text_raw"],
                    "score": round(score, 4),
                    "kind": row["kind"],
                    "status": row["status"],
                    "day": row["day_key"],
                    "importance": row["importance"],
                    "confidence": row["confidence"],
                    "provenance": row["provenance"],
                    "channels": [name for name, ranks in channels.items() if seq in ranks],
                    "truncated": False,
                    "groups": group_map.get(row["id"], []),
                    "grade": grade_map.get(row["id"]),
                }
            )

        payload, used = scoring.fit_budget(payload, budget)
        payload = payload[:k]
        payload_seq = payload_seq[: len(payload)]
        # 注入侧元信息（M28）：一屏里出现两条以上讲同一批人的记忆时，光看日期模型分不清
        # 「十月那次」和「八月那次」。把「涉及谁」一起给它，让它自己挑。一次 IN 查询，
        # 关掉就不查；实体表缺/坏都只是少这行备注，不影响检索。
        meta_cfg = self.cfg.get("retrieval.inject_meta")
        meta_cfg = meta_cfg if isinstance(meta_cfg, dict) else {}
        if bool(meta_cfg.get("enabled", True)) and payload_seq:
            limit_ent = max(1, int(meta_cfg.get("entities", 3) or 3))
            ent_map: dict[int, list[str]] = {}
            marks_e = ",".join("?" for _ in payload_seq)
            try:
                for ent in self.db.conn.execute(
                    f"SELECT em.memory_seq AS seq, e.name AS name FROM entity_mention em "
                    f"JOIN entity e ON e.id=em.entity_id "
                    f"WHERE em.memory_seq IN ({marks_e}) AND e.status='active' "
                    f"ORDER BY em.memory_seq, em.seq",
                    tuple(payload_seq),
                ).fetchall():
                    names = ent_map.setdefault(int(ent["seq"]), [])
                    if ent["name"] not in names and len(names) < limit_ent:
                        names.append(ent["name"])
            except Exception:  # noqa: BLE001 实体表异常不影响检索
                ent_map = {}
            for item, seq in zip(payload, payload_seq):
                names = ent_map.get(int(seq)) or []
                if names:
                    item["entities"] = names
        # 预算按**实际要注入的条目**算（fit_budget 的 used 含被 [:k] 截掉的部分）。
        spent = sum(estimate_tokens(item["text"]) + 12 for item in payload)
        # 每条已选结果现在有多「有实词依据」：补候选时只用覆盖率为 0 的尾巴换。
        payload_cov = [float(cov_by_seq.get(seq, 0.0) or 0.0) for seq in payload_seq]

        graph_items: list[dict[str, Any]] = []
        if bool(graph_cfg.get("append", False)):
            graph_items, extra = self._graph_append(mass=mass_out, picked=picked, graph_cfg=graph_cfg)
            spread_info = {**spread_info, **extra}
        if graph_items:
            payload, payload_seq, payload_cov, spent, added, evicted = self._append_items(
                graph_items,
                payload=payload,
                payload_seq=payload_seq,
                payload_cov=payload_cov,
                k=k,
                budget=int(budget),
                spent=spent,
                policy=str(self.cfg.get("retrieval.graph.evict", "zero_cover") or "zero_cover"),
            )
            spread_info = {**spread_info, "added": added, "evicted": evicted}

        # 向量通道（M13 / 0.13.0）：词法没把握时，用本地 bge 的余弦相似度补候选。
        # 三条纪律：只补不重排（前 head 名不动）、补进来按余弦和词法尾巴抢 k 个槽位、
        # 默认只在覆盖率低于阈值时跑（词法有把握时不添噪，也不烧这 4 ms）。
        vector_info: dict[str, Any] = {}
        vec_cfg = vector_mod.settings(self.cfg)
        if bool(vec_cfg.get("enabled", False)):
            # 第一次检索顺手补齐缺的向量（幂等、一次 NOT EXISTS 点查）。
            vector_mod.ensure_once(self.db, self.cfg)
            trigger = float(vec_cfg.get("trigger_coverage", 0.55) or 0.0)
            gated = str(vec_cfg.get("trigger", "coverage") or "coverage") == "coverage"
            if gated and cov_best >= trigger:
                vector_info = {"skipped": "coverage", "coverage": round(cov_best, 3)}
            else:
                vec_items, vector_info = self._vector_append(
                    q=q,
                    cov_by_seq=cov_by_seq,
                    picked=picked,
                    filters=filters,
                    vec_cfg=vec_cfg,
                )
                if vec_items:
                    payload, payload_seq, payload_cov, spent, added, evicted = self._append_items(
                        vec_items,
                        payload=payload,
                        payload_seq=payload_seq,
                        payload_cov=payload_cov,
                        k=k,
                        budget=int(budget),
                        spent=spent,
                        policy=str(vec_cfg.get("policy", "score") or "score"),
                        head=int(vec_cfg.get("head", 4) or 4),
                    )
                    vector_info = {**vector_info, "added": added, "evicted": evicted}
        # 时效语义（M14）：旧说法是路标，不是答案。词法命中的旧说法已经沿
        # ``superseded_by`` 链到新版本（旧版本状态 superseded、不进候选池），
        # 这里顺着链把它补回来——「问原来那套后来怎么样了」时这才是答案。
        temporal_info: dict[str, Any] = {}
        if temporal_on and bool(temporal_cfg.get("chain", True)):
            chain_items, temporal_info = self._temporal_append(
                q=q,
                fused=fused,
                payload_seq=payload_seq,
                filters=filters,
                temporal_cfg=temporal_cfg,
                update_q=update_q,
            )
            if chain_items:
                payload, payload_seq, payload_cov, spent, added, evicted = self._append_items(
                    chain_items,
                    payload=payload,
                    payload_seq=payload_seq,
                    payload_cov=payload_cov,
                    k=k,
                    budget=int(budget),
                    spent=spent,
                    policy="score",
                    head=int(temporal_cfg.get("chain_head", 4) or 0),
                )
                temporal_info = {**temporal_info, "added": added, "evicted": evicted}

        # 实体桥补候选（M22）：桥到了、但词法没进前 k 的记忆，用「只换实词覆盖率 0
        # 的尾巴」的保守策略补进来。多跳题的答案与问句没有共同稀有词，词法尾巴里
        # 多是零覆盖噪声，换掉它们不会伤到真正命中的结果；补不进去就是净零，不硬挤。
        entity_append_info: dict[str, Any] = {}
        # 补候选默认关闭：A/B 实测（p01..p10 × 308 题）光靠通道进 RRF 就能捞回 5 道多跳、
        # 零 hit→miss；再补候选没有额外收益，却多一份「置换词法尾巴」的风险。留着开关。
        if entity_on and bool(ent_cfg.get("append", False)):
            cand = [
                (int(seq), float(score))
                for seq, score in (entity_bridge.get("scores") or {}).items()
                if int(seq) not in payload_seq
            ]
            cand.sort(key=lambda item: -item[1])
            cand = cand[: max(0, int(ent_cfg.get("append_max", 2) or 2))]
            if cand:
                try:
                    ent_items = self._entity_append(cand, filters=filters)
                except Exception:  # noqa: BLE001 补候选失败不影响主结果
                    ent_items = []
                if ent_items:
                    payload, payload_seq, payload_cov, spent, added, evicted = self._append_items(
                        ent_items,
                        payload=payload,
                        payload_seq=payload_seq,
                        payload_cov=payload_cov,
                        k=k,
                        budget=int(budget),
                        spent=spent,
                        # 默认 score：桥到的多跳答案在词法尾巴上常常「有覆盖率」（只是撞得不对），
                        # 只换覆盖率 0 的尾巴换不动它；先保护前 head 名，再按分数竞争。
                        policy=str(ent_cfg.get("evict", "score") or "score"),
                        head=int(ent_cfg.get("head", 4) or 4),
                    )
                    entity_append_info = {"added": added, "evicted": evicted}

        # 兜底彼此**互斥**：图/向量/时效链已经补进相关记忆时，原话兜底只会把它挤掉；
        # 都没给东西（冷启动/没有边/没模型）才回原话里捞。
        raw_items = self._raw_fallback(
            q=q,
            terms=terms,
            k=k,
            cov_best=cov_best,
            got=len(payload),
            have_graph=bool(spread_info.get("added"))
            or bool(vector_info.get("added"))
            or bool(temporal_info.get("added")),
        )
        if raw_items:
            # 兜底也要守预算：主结果已经花掉一部分，剩下的才给原文
            remaining = max(0, int(budget) - int(spent))
            fitted: list[dict[str, Any]] = []
            for item in raw_items:
                cost = estimate_tokens(item["text"]) + 12
                if cost > remaining:
                    continue
                remaining -= cost
                fitted.append(item)
            if fitted:
                keep = max(1, k - len(fitted))
                payload = payload[:keep] + fitted
        # 日记通道（M18）：「上周咱们都干嘛了」问的是**那几天的经过**，不是某一条事实。
        # 日记不参与候选池重排（它和记忆不是一个量纲），只在一种情况下出现：
        # 明确的时间窗 + 问经过（或词法明显没把握）→ 放最前面，它就是答案。
        # 其余情况一律不插：问事实的句子不能被日记顶掉，没时间窗的冷启动也不该倒一篇
        # 不相干的日记出来——那不是「帮你回忆」，那是噪音。
        episode_info: dict[str, Any] = {}
        ep_cfg = self.cfg.get("retrieval.episode", {}) or {}
        if isinstance(ep_cfg, dict) and bool(ep_cfg.get("enabled", True)):
            window = temporal_mod.range_window(q) if temporal_on else None
            head = False
            if window is not None:
                head = temporal_mod.asks_for_events(q) or cov_best < float(
                    ep_cfg.get("gate_coverage", 0.55) or 0.0
                )
            if head:
                from datetime import timedelta

                journal_mod.ensure_episode_index(self.db, limit=64)
                since = None
                until = None
                if window is not None:
                    if window.get("day"):
                        # 「11月3号那天发生了什么」：窗口就是那一天，两边都卡死。
                        since = str(window["day"])
                        until = str(window["day"])
                    else:
                        since = (now() - timedelta(days=int(window["days"]))).date().isoformat()
                ep_items = journal_mod.search_episodes(
                    self.db,
                    query=q,
                    since=since,
                    until=until,
                    subject=self.subject,
                    limit=max(1, int(ep_cfg.get("max", 3) or 3)),
                )
                sparse_info: dict[str, Any] = {}
                # 稀疏自适应（M19）：写日记是会断的（忙、忘、没聊），窗口里空着不代表
                # 「那段时间不存在」。空窗时逐级放宽，并且**说清楚放宽到了哪**——
                # 答近的 + 说明缺口，比装作查不到有用，也比硬说「没发生什么」诚实。
                if not ep_items and window is not None and not window.get("day"):
                    wider_days = max(int(window["days"]) * 4, 8)
                    wider_since = (now() - timedelta(days=wider_days)).date().isoformat()
                    ep_items = journal_mod.search_episodes(
                        self.db,
                        query=q,
                        since=wider_since,
                        subject=self.subject,
                        limit=max(1, int(ep_cfg.get("max", 3) or 3)),
                    )
                    if ep_items:
                        sparse_info = {
                            "sparse": True,
                            "widened_days": wider_days,
                            "note": f"「{window.get('label')}」这段时间没有日记，放宽到最近 {wider_days} 天才找到",
                        }
                if not ep_items:
                    recent_items = journal_mod.recent(
                        self.db,
                        subject=self.subject,
                        days=365 * 3,
                        items=max(1, int(ep_cfg.get("sparse_recent", 2) or 2)),
                    )
                    if recent_items:
                        ep_items = [journal_mod.episode_item(row) for row in recent_items]
                        latest = str(ep_items[0].get("day") or "")
                        sparse_info = {
                            "sparse": True,
                            "latest": latest,
                            "note": f"这段时间没有日记，下面是最近的一篇（写在 {latest}）",
                        }
                if ep_items:
                    # 日记放在最前面，预算不够时**让尾部的普通记忆让位**——
                    # 它是这个问题要的答案，不能因为「600 token 已经被别的东西花光」就消失。
                    # （第一版只用剩余预算，结果「11月3号那天发生了什么」一条日记都插不进来。）
                    fitted_ep: list[dict[str, Any]] = []
                    for item in ep_items:
                        need = estimate_tokens(str(item.get("text") or "")) + 12
                        while payload and int(spent) + need > int(budget):
                            dropped = payload.pop()
                            if payload_seq:
                                payload_seq.pop()
                            spent = max(0, int(spent) - (estimate_tokens(str(dropped.get("text") or "")) + 12))
                        if int(spent) + need > int(budget):
                            break
                        fitted_ep.append(item)
                        spent = int(spent) + need
                    if fitted_ep:
                        payload = fitted_ep + payload[: max(0, k - len(fitted_ep))]
                        payload_seq = [0] * len(fitted_ep) + payload_seq
                        episode_info = {
                            "items": len(fitted_ep),
                            "at": "head",
                            "window": (window or {}).get("label"),
                            **sparse_info,
                        }
                        used = sum(estimate_tokens(item["text"]) + 12 for item in payload)
        # 补候选之后重算一次注入量：budget_used 是给人和 benchmark 看的，不能停在补之前。
        if (
            vector_info.get("added")
            or spread_info.get("added")
            or temporal_info.get("added")
            or entity_append_info.get("added")
        ):
            used = sum(estimate_tokens(item["text"]) + 12 for item in payload)
        # 「不确定就说出来」（M18）：把「这次召回有多有把握」算成确定性信号，交给渲染层说人话。
        # 为什么不靠自觉：长对话里最先失效的就是自觉；数字先算出来，才有东西可说。
        doubt_cfg = self.cfg.get("retrieval.doubt", {}) or {}
        uncertainty: dict[str, Any] = {}
        if isinstance(doubt_cfg, dict) and bool(doubt_cfg.get("enabled", True)):
            first = payload[0] if payload else {}
            top_cov = float(cov_by_seq.get(payload_seq[0], 0.0)) if payload_seq else 0.0
            # 分差只在**同一种东西**之间比：日记/原话的分数是另算的，混进来会永远报「咬得紧」。
            mem_scores = [
                float(item.get("score") or 0.0)
                for item in payload
                if str(item.get("source") or "") not in ("episode", "journal")
            ]
            margin = (mem_scores[0] - mem_scores[1]) if len(mem_scores) > 1 else 1.0
            reasons: list[str] = []
            drought_age = None
            if not payload:
                reasons.append("empty")
                # 记忆断档（M19 补）：一条都没召回时，分清楚「这事没记过」和
                # 「最近根本没在记」——后者更该被说出来，因为它能修。
                drought_days = int(self.cfg.get("retrieval.drought_days", 14) or 0)
                last = self.last_memory_at()
                if drought_days and last:
                    age = days_between(last)
                    if age is not None and age >= drought_days:
                        reasons.append("drought")
                        drought_age = int(age)
            else:
                # 只有「问经过 + 头一条就是日记」时覆盖率才不适用（日记没有这把尺子）；
                # 别的「头一条碰巧是日记/原话」不能免检摘要——问事实却只捞到一篇日记，
                # 恰恰是最该说「不确定」的时候。
                asks_events = temporal_mod.asks_for_events(q) if temporal_on else False
                is_overview = bool(episode_info) and asks_events and str(first.get("source") or "") == "episode"
                if not is_overview and top_cov < float(doubt_cfg.get("coverage", 0.35) or 0.0):
                    reasons.append("coverage")
                if len(mem_scores) > 1 and margin < float(doubt_cfg.get("margin", 0.02) or 0.0):
                    reasons.append("close")
            uncertainty = {
                "weak": bool(reasons),
                "reasons": reasons,
                "coverage": round(top_cov, 3),
                "margin": round(margin, 3),
                **({"drought_days": drought_age} if "drought" in reasons else {}),
            }
        # 统一带上 status：补候选那几路（图/向量/时效链/原话）各自造的条目没这个字段，
        # 而「候选 / 生效 / 休眠」正是上层判断可信度要看的。一次 IN 查询补齐。
        if payload:
            ids = [str(item.get("id")) for item in payload if item.get("id")]
            if ids:
                marks_s = ",".join("?" for _ in ids)
                status_map = {
                    row["id"]: row["status"]
                    for row in self.db.conn.execute(
                        f"SELECT id, status FROM memory WHERE id IN ({marks_s})", tuple(ids)
                    )
                }
                for item in payload:
                    if not item.get("status"):
                        item["status"] = status_map.get(str(item.get("id")))

        latency_ms = int((time.perf_counter() - started) * 1000)
        entity_info: dict[str, Any] = {}
        if entity_on:
            entity_info = {
                "query": entity_bridge.get("query_entities") or [],
                "hop": entity_bridge.get("hop_entities") or [],
                "query_seqs": len(entity_bridge.get("query_seqs") or []),
                "hop_seqs": len(entity_bridge.get("hop_seqs") or []),
                **entity_append_info,
            }
            if entity_bridge.get("error"):
                entity_info["error"] = entity_bridge["error"]

        if record:
            self._record_search(
                query=q,
                mode=mode,
                channels=channels,
                payload=payload,
                used=used,
                latency_ms=latency_ms,
                client=client,
                uncertainty=uncertainty,
            )
        return {
            "items": payload,
            "total_candidates": len(fused),
            "budget_used": used,
            "latency_ms": latency_ms,
            "channels": {name: len(ranks) for name, ranks in channels.items()},
            "spread": spread_info,
            **({"episode": episode_info} if episode_info else {}),
            **({"uncertainty": uncertainty} if uncertainty else {}),
            **({"vector": vector_info} if vector_info else {}),
            **({"entity": entity_info} if entity_info else {}),
            **({"temporal": {**temporal_info, **temporal_info_cap}} if (temporal_info or temporal_info_cap) else {}),
            **(
                {
                    "debug": {
                        "candidates": sorted(debug_rows, key=lambda r: -r["score"])[:200],
                        "pool": len(rows),
                    }
                }
                if debug_rows is not None
                else {}
            ),
        }

    def _chan_tokens(self, q: str, *, drop: frozenset[str] | set[str] | None = None) -> list[int]:
        seg = tokenize(q)
        if not seg:
            return []
        terms = [t for t in seg.split() if t.strip()]  # 长文档别把 OR 项堆到上千
        if drop:
            terms = [t for t in terms if t not in drop]
        return self._chan_terms(terms)

    def _chan_terms(self, terms: list[str], *, limit: int = 50) -> list[int]:
        """按**整词**做 FTS 召回（不再分词）：给角色词/名字这种不能拆的词用。"""
        words = [t for t in terms if t and t.strip()][:64]
        if not words:
            return []
        expr = " OR ".join(f'"{t.replace(chr(34), chr(34)*2)}"' for t in words)
        try:
            rows = self.db.conn.execute(
                "SELECT rowid AS seq FROM memory_fts_tok WHERE memory_fts_tok MATCH ? "
                "ORDER BY bm25(memory_fts_tok) LIMIT ?",
                (expr, int(limit)),
            ).fetchall()
        except Exception:
            return []
        return [r["seq"] for r in rows]

    def _chan_trigram(self, q: str) -> list[int]:
        if len(q) < 3:
            return []
        expr = '"' + q.replace('"', '""') + '"'
        try:
            rows = self.db.conn.execute(
                "SELECT rowid AS seq FROM memory_fts_tri WHERE memory_fts_tri MATCH ? "
                "ORDER BY bm25(memory_fts_tri) LIMIT 50",
                (expr,),
            ).fetchall()
        except Exception:
            return []
        return [r["seq"] for r in rows]

    def last_memory_at(self) -> str | None:
        """最近一条活着的记忆写进来的时间（记忆断档判断用）。"""
        try:
            row = self.db.conn.execute(
                "SELECT MAX(created_at) AS ts FROM memory WHERE status<>'deleted'"
            ).fetchone()
            return str(row["ts"]) if row and row["ts"] else None
        except Exception:  # noqa: BLE001 老库/只读：当作没有
            return None

    def _memory_count(self) -> int:
        if self._memory_total is None:
            row = self.db.conn.execute("SELECT COUNT(*) c FROM memory WHERE status<>'deleted'").fetchone()
            self._memory_total = int(row["c"] or 0)
        return self._memory_total

    def _term_weights(self, terms: list[str]) -> dict[str, float]:
        """全库 IDF 词权：``log(1 + N/(1+df))``，df=0 的词直接不进表。

        为什么要查全库 df：覆盖率的问题是「问句里的词，这条记忆命中了几成」。
        候选集内的 df 是反向偏置的（虚词双字正是被选中的原因），全库 df 才是真稀罕度。
        成本：每个词一次 FTS 点查（``memory_fts_tok`` 就是现成的倒排），
        12 个词约 1 ms；带进程内缓存，重复问句近零成本。
        """
        if not terms:
            return {}
        total = self._memory_count()
        if total <= 0:
            return {}
        out: dict[str, float] = {}
        for term in terms[:24]:
            df = self._df_cache.get(term)
            if df is None:
                expr = '"' + term.replace('"', '""') + '"'
                try:
                    row = self.db.conn.execute(
                        "SELECT COUNT(*) c FROM memory_fts_tok WHERE memory_fts_tok MATCH ?", (expr,)
                    ).fetchone()
                    df = int(row["c"] or 0)
                except Exception:  # noqa: BLE001 索引不可用 → 退回候选集内 df
                    return {}
                if len(self._df_cache) > 4096:
                    self._df_cache.clear()
                self._df_cache[term] = df
            if df <= 0:
                continue
            out[term] = math.log(1.0 + total / (1.0 + df))
        return out

    @staticmethod
    def _seed_weights(
        pending: list[tuple[Any, float]],
        cov_by_seq: dict[int, float],
        limit: int,
    ) -> list[tuple[int, float]]:
        """种子 = 真的命中了问句实词、且相关性靠前的几条（``cov > 0``）。

        只按分数取种子是错的：弱覆盖时榜首常是「撞到一个双字就进来」的无关记忆，
        从它出发扩散只会放大噪声（第一版实测：从「厨房水龙头」漂出一堆水龙头）。
        一条都没有命中实词时宁可不扩散——扩散是锦上添花，不是无中生有。
        """
        ranked: list[tuple[float, float, int]] = []
        for row, relevance in pending:
            seq = int(row["seq"])
            cov = float(cov_by_seq.get(seq, 0.0) or 0.0)
            if cov <= 0:
                continue
            ranked.append((cov, float(relevance), seq))
        if not ranked:
            return []
        ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
        return [(seq, max(0.05, rel)) for _cov, rel, seq in ranked[: max(0, limit)]]

    def _graph_mass(
        self,
        seeds: list[tuple[int, float]],
        graph_cfg: dict[str, Any],
    ) -> dict[int, float]:
        """种子 → 全图扩散质量（PPR）。只取两跳邻域，每层一条 ``IN (...)`` 查询。"""
        if not seeds:
            return {}
        adj = graph_mod.neighbourhood(
            self.db.conn,
            [seq for seq, _ in seeds],
            hops=int(graph_cfg.get("hops", 2) or 2),
            max_nodes=graph_mod.MAX_NODES,
        )
        if not adj:
            return {}
        return graph_mod.spread(
            adj,
            seeds,
            alpha=float(graph_cfg.get("alpha", 0.5) or 0.5),
            iterations=int(graph_cfg.get("iterations", 3) or 3),
        )

    def _graph_append(
        self,
        *,
        mass: dict[int, float],
        picked: list[tuple[int, float]],
        graph_cfg: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """（默认关闭）把扩散到、但词法没进前 k 的记忆补进来。

        为什么默认关：实测在这套 90 天语料上，答案是**已经在候选里、只是排在第 9-30 名**，
        补一条只会占掉别的槽位（净收益 0）；真正的杠杆是 ``graph_affinity`` 重排。
        留着它是因为冷启动/候选池被截断时它仍然有用，而且开关可回到旧行为。
        """
        if not mass:
            return [], {}
        g = graph_cfg or {}
        min_share = float(g.get("min_share", 0.12) or 0.0)
        max_items = max(0, int(g.get("max_items", 2) or 0))
        shown = {int(seq) for seq, _ in picked}
        chosen: list[tuple[int, float]] = []
        for seq, share in sorted(mass.items(), key=lambda kv: (-kv[1], kv[0])):
            if len(chosen) >= max_items:
                break
            if share < min_share:
                break
            if seq in shown:
                continue
            chosen.append((seq, share))
        if not chosen:
            return [], {"added": 0}
        statuses = VISIBLE_STATUS
        marks = ",".join("?" for _ in chosen)
        rows = self.db.conn.execute(
            f"SELECT * FROM memory WHERE seq IN ({marks}) AND subject_id=? "
            f"AND status IN ({','.join('?' for _ in statuses)})",
            (*[seq for seq, _ in chosen], self.subject, *statuses),
        ).fetchall()
        by_seq_rows = {int(row["seq"]): row for row in rows}
        ids = [row["id"] for row in rows]
        group_map = group_mod.group_map(self.db, "memory", ids)
        grade_map = group_mod.grade_map(self.db, "memory", ids)
        out: list[dict[str, Any]] = []
        for seq, share in chosen:
            row = by_seq_rows.get(seq)
            if row is None:  # 图里还留着、但记忆已不可见 → 跳过
                continue
            out.append(
                {
                    "id": row["id"],
                    "text": row["text_raw"],
                    "score": round(min(0.7, 0.18 + 0.5 * share), 4),
                    "kind": row["kind"],
                    "day": row["day_key"],
                    "importance": row["importance"],
                    "confidence": row["confidence"],
                    "provenance": row["provenance"],
                    "channels": ["graph"],
                    "truncated": False,
                    "groups": group_map.get(row["id"], []),
                    "grade": grade_map.get(row["id"]),
                    "source": "graph",
                    "reach": round(share, 3),
                }
            )
        return out, {"added": len(out)}

    def _temporal_append(
        self,
        *,
        q: str,
        fused: dict[int, float],
        payload_seq: list[int],
        filters: dict[str, Any],
        temporal_cfg: dict[str, Any],
        update_q: bool,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """旧说法命中 → 沿 supersede 链补替代版本。

        词法通道（FTS）里**看得到** superseded 的行（触发器照常维护索引），
        只是候选池按状态把它们滤掉了。所以这里从词法结果里挑旧说法、查它的
        ``superseded_by``，把链尾那个还在用的版本当候选补进来。

        补进来的分数按「旧说法排第几」定：旧说法越像问句，它的替代版本越可能是答案。
        """
        if not fused:
            return [], {}
        if bool(temporal_cfg.get("chain_only_update", False)) and not update_q:
            return [], {}
        ranked = sorted(fused, key=lambda seq: -fused[seq])
        rank_of = {seq: idx + 1 for idx, seq in enumerate(ranked)}
        max_old = int(temporal_cfg.get("chain_max_old", 60) or 60)
        replacements = temporal_mod.chain_replacements(
            self.db.conn,
            ranked[:max_old],
            subject=self.subject,
            hops=int(temporal_cfg.get("chain_hops", 3) or 3),
            max_old=max_old,
        )
        if not replacements:
            return [], {"chain": 0}
        shown = {int(seq) for seq in payload_seq}
        picked: list[tuple[int, float, int]] = []
        for old_seq, (new_seq, _hops) in replacements.items():
            if int(new_seq) in shown:
                continue
            base = float(temporal_cfg.get("chain_score", 0.72) or 0.72)
            if not update_q:
                # 非更新题里「旧说法→新版本」只是锦上添花：压到尾巴，别把答案顶掉。
                base = float(temporal_cfg.get("chain_score_noupdate", base) or base)
            score = temporal_mod.chain_score(
                rank_of.get(int(old_seq), 99),
                base=base,
                step=float(temporal_cfg.get("chain_step", 0.02) or 0.02),
                floor=float(temporal_cfg.get("chain_min", 0.55) or 0.0),
            )
            picked.append((int(new_seq), score, int(old_seq)))
        if not picked:
            return [], {"chain": len(replacements), "added": 0}
        picked.sort(key=lambda item: -item[1])
        picked = picked[: max(1, int(temporal_cfg.get("chain_max_items", 2) or 2))]
        rows = self._fetch_scored(
            {seq: score for seq, score, _old in picked}, filters, limit=max(2, len(picked) * 2)
        )
        by_seq = {int(row["seq"]): row for row in rows}
        ids = [row["id"] for row in rows]
        group_map = group_mod.group_map(self.db, "memory", ids)
        grade_map = group_mod.grade_map(self.db, "memory", ids)
        out: list[dict[str, Any]] = []
        for seq, score, old_seq in picked:
            row = by_seq.get(seq)
            if row is None:  # 替代版本不可见（删了/密级过滤）→ 跳过
                continue
            out.append(
                {
                    "id": row["id"],
                    "text": row["text_raw"],
                    "score": round(score, 4),
                    "kind": row["kind"],
                    "day": row["day_key"],
                    "importance": row["importance"],
                    "confidence": row["confidence"],
                    "provenance": row["provenance"],
                    "channels": ["supersede"],
                    "truncated": False,
                    "groups": group_map.get(row["id"], []),
                    "grade": grade_map.get(row["id"]),
                    "source": "supersede",
                    "from_seq": int(old_seq),
                }
            )
        return out, {"chain": len(replacements), "update_q": bool(update_q)}

    def _entity_append(
        self,
        cand: list[tuple[int, float]],
        *,
        filters: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """实体桥补候选：桥到、但词法没选中的 seq → payload 条目（只补不重排）。

        ``cand`` 是 ``[(seq, 桥强度)]``。分数压在词法头部之下、噪声尾巴之上：
        这类记忆在「词法上答不出」，能进榜的唯一理由就是实体桥，所以不跟词法争前几名；
        能不能留下由调用方 ``_append_items`` 的置换策略决定。
        """
        if not cand:
            return []
        fused = {int(seq): float(score) for seq, score in cand}
        rows = self._fetch_scored(fused, filters, limit=max(1, len(cand)))
        if not rows:
            return []
        by_seq = {int(row["seq"]): row for row in rows}
        ids = [row["id"] for row in rows]
        group_map = group_mod.group_map(self.db, "memory", ids)
        grade_map = group_mod.grade_map(self.db, "memory", ids)
        out: list[dict[str, Any]] = []
        for seq, strength in cand:
            row = by_seq.get(int(seq))
            if row is None:  # 被状态/密级/分组过滤掉了 → 不补
                continue
            out.append(
                {
                    "id": row["id"],
                    "text": row["text_raw"],
                    "score": round(min(0.62, 0.30 + 0.12 * float(strength)), 4),
                    "kind": row["kind"],
                    "day": row["day_key"],
                    "importance": row["importance"],
                    "confidence": row["confidence"],
                    "provenance": row["provenance"],
                    "channels": ["entity"],
                    "truncated": False,
                    "groups": group_map.get(row["id"], []),
                    "grade": grade_map.get(row["id"]),
                    "source": "entity",
                    "reach": round(float(strength), 3),
                }
            )
        return out

    def _vector_append(
        self,
        *,
        q: str,
        cov_by_seq: dict[int, float],
        picked: list[tuple[int, float]],
        filters: dict[str, Any],
        vec_cfg: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """向量补候选：余弦排前几条里，挑没被词法选中的记忆，按预算装进来。

        只补不重排——和关联图同一个立场：改排序会同时牵动所有题，收益无法归因。
        相似度低于 ``min_score`` 的宁可不给：no_memory 题（问的是没记住的事）
        词法覆盖率天然是 0，必然会触发这里，阈值就是防它把「像但不相关」的条目塞进去。
        """
        max_items = max(0, int(vec_cfg.get("max_items", 2) or 0))
        if max_items <= 0:
            return [], {"triggered": True, "limited": 0}
        limit = max(max_items, int(vec_cfg.get("limit", 8) or 8))
        scores = vector_mod.search(
            self.db,
            self.cfg,
            q,
            limit=limit,
            min_score=float(vec_cfg.get("min_score", 0.45) or 0.0),
        )
        if not scores:
            return [], {"triggered": True, "candidates": 0}
        # 过滤条件（subject/status/kind/分组/密级）复用词法那条 SQL，别写第二份。
        rows = self._fetch_scored(scores, filters, limit=max(limit, max_items))
        shown = {int(seq) for seq, _score in picked}
        chosen = [row for row in rows if int(row["seq"]) not in shown][:max_items]
        if not chosen:
            return [], {"triggered": True, "candidates": len(scores)}
        ids = [row["id"] for row in chosen]
        group_map = group_mod.group_map(self.db, "memory", ids)
        grade_map = group_mod.grade_map(self.db, "memory", ids)
        out: list[dict[str, Any]] = []
        for row in chosen:
            seq = int(row["seq"])
            sim = float(scores.get(seq, 0.0) or 0.0)
            out.append(
                {
                    "id": row["id"],
                    "text": row["text_raw"],
                    # 余弦相似度（0..1）和词法的 final_score 同为相关性量纲，
                    # 直接当分数参与尾巴竞争；乘 0.98 只是让同分时词法结果优先。
                    "score": round(min(0.95, 0.98 * sim), 4),
                    "kind": row["kind"],
                    "day": row["day_key"],
                    "importance": row["importance"],
                    "confidence": row["confidence"],
                    "provenance": row["provenance"],
                    "channels": ["vector"],
                    "truncated": False,
                    "groups": group_map.get(row["id"], []),
                    "grade": grade_map.get(row["id"]),
                    "source": "vector",
                    "reach": round(sim, 3),
                }
            )
        top = round(max(float(v) for v in scores.values()), 3) if scores else 0.0
        return out, {
            "triggered": True,
            "candidates": len(scores),
            "top": top,
            "coverage": round(max(cov_by_seq.values(), default=0.0), 3),
        }

    def _append_items(
        self,
        items: list[dict[str, Any]],
        *,
        payload: list[dict[str, Any]],
        payload_seq: list[int],
        payload_cov: list[float],
        k: int,
        budget: int,
        spent: int,
        policy: str = "zero_cover",
        head: int = 4,
    ) -> tuple[list[dict[str, Any]], list[int], list[float], int, int, int]:
        """把补来的条目塞进 k 个槽位：先占空位，再按策略换尾巴。

        为什么只换「覆盖率 0」的：k 个槽位是硬上限，补进来的条目必须挤掉一条。
        覆盖率 0 意味着这条记忆一个问句实词都没撞上，只是被 RRF/时效抬进来的，
        换掉它不会伤到「真的命中了问题」的结果；全员都有覆盖率时宁可不补。
        返回 ``(payload, payload_seq, payload_cov, spent, added, evicted)``。
        """
        if not items:
            return payload, payload_seq, payload_cov, spent, 0, 0
        if policy == "score":
            # 按相似度竞争：前 ``head`` 名不动，尾巴（第 head+1 名之后）和补来的条目
            # 一起按分数排序，分低的掉出去。这是唯一真正能补进漏答题的策略——
            # 漏答题的词法尾巴往往也有覆盖率，「只换覆盖率为 0 的尾巴」根本换不动。
            head_n = max(0, min(int(head), len(payload)))
            remaining = max(0, int(budget) - int(spent))
            fitted: list[dict[str, Any]] = []
            for item in items:
                cost = estimate_tokens(item["text"]) + 12
                if cost > remaining:
                    continue
                remaining -= cost
                spent += cost
                fitted.append(item)
            if not fitted:
                return payload, payload_seq, payload_cov, spent, 0, 0
            tail = payload[head_n:] + fitted
            tail_seq = payload_seq[head_n:] + [0] * len(fitted)
            tail_cov = payload_cov[head_n:] + [float(item.get("reach") or 0.0) for item in fitted]
            room = max(0, k - head_n)
            order = sorted(range(len(tail)), key=lambda i: -float(tail[i].get("score") or 0.0))
            chosen = order[:room]
            lex_tail = len(payload) - head_n
            kept_tail = sum(1 for i in chosen if i < lex_tail)
            evicted = max(0, lex_tail - kept_tail)
            return (
                payload[:head_n] + [tail[i] for i in chosen],
                payload_seq[:head_n] + [tail_seq[i] for i in chosen],
                payload_cov[:head_n] + [tail_cov[i] for i in chosen],
                spent,
                len(fitted),
                evicted,
            )
        free_slots = max(0, k - len(payload))
        if policy == "lowest_cover":
            # 先换覆盖率最低的（同覆盖率换分数最低的）：赌「撞上的实词最少的那条」最没用。
            order = sorted(
                range(len(payload_seq)), key=lambda i: (payload_cov[i], payload[i]["score"])
            )
        elif policy == "tail":
            order = list(range(len(payload_seq) - 1, -1, -1))
        else:  # zero_cover：只换一条实词都没撞上的，最保守
            order = sorted(
                (i for i in range(len(payload_seq)) if payload_cov[i] <= 0.0),
                key=lambda i: payload[i]["score"],
            )
        spare = order
        room = free_slots + len(spare)
        remaining = max(0, int(budget) - int(spent))
        fitted: list[dict[str, Any]] = []
        for item in items[:room]:
            cost = estimate_tokens(item["text"]) + 12
            if cost > remaining:
                continue
            remaining -= cost
            spent += cost
            fitted.append(item)
        if not fitted:
            return payload, payload_seq, payload_cov, spent, 0, 0
        evict = set(spare[: max(0, len(fitted) - free_slots)])
        kept = [item for idx, item in enumerate(payload) if idx not in evict]
        kept_seq = [seq for idx, seq in enumerate(payload_seq) if idx not in evict]
        kept_cov = [cov for idx, cov in enumerate(payload_cov) if idx not in evict]
        kept = kept[: max(0, k - len(fitted))]
        kept_seq = kept_seq[: len(kept)]
        kept_cov = kept_cov[: len(kept)]
        return (
            kept + fitted,
            kept_seq + [0] * len(fitted),
            kept_cov + [float(item.get("reach") or 0.0) for item in fitted],
            spent,
            len(fitted),
            len(evict),
        )

    def _raw_fallback(
        self,
        *,
        q: str,
        terms: list[str],
        k: int,
        cov_best: float,
        got: int,
        have_graph: bool = False,
    ) -> list[dict[str, Any]]:
        """派生事实答不上来时，回原话里捞一捞。

        为什么需要它：``_facts`` 把用户原话改写成「用户……」并截断 80 字，
        人一多、话一长，细节就丢了；而 journal_line 里原话还在（保留期内）。
        只在**派生结果薄弱**时查，且最多补 ``raw_fallback_max`` 条，
        避免每轮多一份注入成本。

        ``have_graph``：关联扩散已经补进相关记忆时不再做原话兜底——
        两条兜底抢同一份剩余槽位，先到的把后到的挤掉，图给的还是记忆单元，
        比未消化的原话更干净。图没东西（冷启动/没有边）才轮到原话。
        """
        if have_graph:
            return []
        if not bool(self.cfg.get("retrieval.raw_fallback", True)):
            return []
        threshold = float(self.cfg.get("retrieval.raw_fallback_coverage", 0.30) or 0.0)
        if got >= k and cov_best >= threshold:
            return []
        limit = max(1, int(self.cfg.get("retrieval.raw_fallback_max", 2) or 2))
        # 这里用**整句**的双字，而不是 content_terms：后者删虚词会在词中间接缝，
        # 造出「班骑」这种不存在的词（「上班骑什么」→ 上被当虚词删掉），原文兜底会漏。
        tokens = tokenize(q).split()
        if not tokens:
            return []
        expr = " OR ".join(f'"{token}"' for token in tokens[:64])
        try:
            rows = self.db.conn.execute(
                "SELECT jl.id, jl.text, jl.day_key, jl.role, bm25(journal_fts) AS bm25 "
                "FROM journal_fts JOIN journal_line jl ON jl.seq=journal_fts.rowid "
                "WHERE journal_fts MATCH ? "
                "AND NOT EXISTS (SELECT 1 FROM journal_retraction r "
                "WHERE r.subject_id=jl.subject_id AND r.line_id=jl.id) "
                "ORDER BY bm25(journal_fts) LIMIT ?",
                (expr, limit * 4),
            ).fetchall()
        except Exception:  # noqa: BLE001 老库没建索引：兜底不存在也不报错
            return []
        if not rows:
            return []
        query_terms = query_mod.bigrams(q)
        ranked = []
        for row in rows:
            text = str(row["text"] or "")
            if not text:
                continue
            overlap = len(query_terms & query_mod.bigrams(text))
            ranked.append((overlap, float(row["bm25"] or 0.0), row))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        out: list[dict[str, Any]] = []
        seen_text: set[str] = set()
        for overlap, _bm25, row in ranked:
            if len(out) >= limit:
                break
            text = str(row["text"])
            key = normalize(text)
            if not key or key in seen_text:
                continue
            seen_text.add(key)
            out.append(
                {
                    "id": row["id"],
                    "text": text,
                    "score": round(min(1.0, 0.35 + 0.1 * overlap), 4),
                    "kind": "journal",
                    "day": row["day_key"],
                    "importance": 0.5,
                    "confidence": 0.6,
                    "provenance": "user_explicit" if row["role"] == "user" else "observed",
                    "channels": ["journal"],
                    "truncated": False,
                    "groups": [],
                    "grade": None,
                    "source": "journal",
                }
            )
        return out

    def _chan_like(self, q: str) -> list[int]:
        if len(q) > 200:  # 文档级输入不做全表扫描
            return []
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self.db.conn.execute(
            "SELECT seq FROM memory WHERE text_norm LIKE ? ESCAPE '\\' LIMIT 50",
            (f"%{escaped}%",),
        ).fetchall()
        return [r["seq"] for r in rows]

    def _fetch_scored(self, fused: dict[int, float], filters: dict[str, Any], *, limit: int) -> list[Any]:
        seqs = sorted(fused, key=lambda s: -fused[s])[: max(limit * 2, 100)]
        if not seqs:
            return []
        marks = ",".join("?" for _ in seqs)
        sql = f"SELECT * FROM memory WHERE seq IN ({marks}) AND subject_id=?"
        params: list[Any] = [*seqs, self.subject]
        statuses = filters.get("status") or list(VISIBLE_STATUS)
        sql += " AND status IN (" + ",".join("?" for _ in statuses) + ")"
        params.extend(statuses)
        kinds = filters.get("kind")
        if kinds:
            sql += " AND kind IN (" + ",".join("?" for _ in kinds) + ")"
            params.extend(kinds)
        if filters.get("since"):
            sql += " AND day_key >= ?"
            params.append(filters["since"])
        if filters.get("pinned_only"):
            sql += " AND pinned=1"
        if filters.get("scope_key"):
            sql += " AND scope_key=?"
            params.append(filters["scope_key"])
        group_path = filters.get("group")
        if group_path:
            base = group_mod.normalize_path(str(group_path))
            sql += (
                " AND id IN (SELECT gm.target_id FROM grp_member gm "
                "JOIN grp g ON g.id=gm.grp_id WHERE gm.target_tbl='memory' "
                "AND g.status='active' AND (g.path=? OR g.path LIKE ?))"
            )
            params.extend([base, base + "/%"])
        grades = filters.get("grade")
        if grades:
            if isinstance(grades, str):
                grades = [grades]
            grades = [group_mod.validate_grade(g) for g in grades]
            sql += (
                " AND id IN (SELECT target_id FROM target_grade "
                "WHERE target_tbl='memory' AND grade IN ("
                + ",".join("?" for _ in grades)
                + "))"
            )
            params.extend(grades)
        if not self.cfg.get("policy.allow_secret", False):
            sql += " AND sensitivity <> 'secret'"
        rows = self.db.conn.execute(sql, params).fetchall()
        # 候选池截断必须按**相关性**，不能按行序：SQL 没有 ORDER BY，返回顺序是 rowid，
        # 一旦 fused 的候选超过池子，seq 靠前的会被留下、真正高分的反而被裁掉
        # （实体桥把候选池撑大后踩到过：一条排第 1 的记忆因为 seq 大被挤出池子）。
        rank = {seq: idx for idx, seq in enumerate(seqs)}
        rows = sorted(rows, key=lambda row: rank.get(int(row["seq"]), len(seqs)))
        return rows[:limit]

    def _record_search(
        self,
        *,
        query: str,
        mode: str,
        channels: dict[str, list[int]],
        payload: list[dict[str, Any]],
        used: int,
        latency_ms: int,
        client: str | None,
        uncertainty: dict[str, Any] | None = None,
    ) -> None:
        ids = [p["id"] for p in payload]
        with self.db.tx() as conn:
            recall_id = ulid()
            conn.execute(
                "INSERT INTO recall_log(id, ts, subject_id, query, mode, channels_json, returned_json, "
                "injected_tokens, latency_ms, client) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    recall_id,
                    now_iso(),
                    self.subject,
                    query,
                    mode,
                    _json_dump({k: len(v) for k, v in channels.items()}),
                    _json_dump([{"id": p["id"], "score": p["score"]} for p in payload]),
                    used,
                    latency_ms,
                    client,
                ),
            )
            for mid in ids:
                conn.execute(
                    "UPDATE memory SET use_count=use_count+1, last_used_at=? WHERE id=?", (now_iso(), mid)
                )
            # 「不确定」也要留痕（M18）：只写事件日志，不进注入——否则每轮都在道歉。
            # 有了这条流水，之后能回答「我到底在哪些问题上没把握」，而不是靠印象。
            if uncertainty and uncertainty.get("weak"):
                events.append(
                    conn,
                    actor="recall",
                    op="recall.weak",
                    target_tbl="recall_log",
                    target_id=recall_id,
                    subject_id=self.subject,
                    payload={
                        "query": query,
                        "reasons": list(uncertainty.get("reasons") or []),
                        "coverage": uncertainty.get("coverage"),
                        "margin": uncertainty.get("margin"),
                        "returned": len(payload),
                    },
                )

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
