"""分组与分级（M3.5）：AI 自建的层级目录 + 重要性等级。

两个概念刻意分开：

- **分组**：`grp` 树 + `grp_member` 多对多。一个东西可以同时属于「我/姓名」
  和「工作/项目/PMem」，这是真实的：我的名字既是身份也是项目署名。
- **分级**：`target_grade` 一对一。等级是**这东西本身有多重要**，不是它在哪个组；
  同一份记忆放在两个组里，等级仍然只有一个。

为什么让 AI 自己建组而不是预置一套分类：预置分类永远不够用，而且一旦写死，
AI 就会把新东西硬塞进旧筐。代价是可能建出一堆近义组，所以有护栏：
深度 ≤4、每层同名去重、每组 ≤200、空组可回收、改名/合并都留事件。

分级不覆盖 `memory.importance`：importance 是打分算出来的，grade 是"我判断它有多重要"，
两者冲突时保留两者，检索时 grade 只做小幅加权（见 GRADE_INFO），不篡改原始分。
"""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from typing import Any, Iterable

from ..base import events
from ..base.clock import now_iso
from ..base.errors import BudgetError, ConflictError, NotFoundError, ValidationError
from ..base.ids import ulid

MAX_DEPTH = 4
MAX_SEGMENT = 24
MAX_GROUPS = 200

GRADE_INFO: dict[str, dict[str, Any]] = {
    "S": {
        "label": "核心身份",
        "boost": 0.10,
        "meaning": "名字、身份、长期设定；跨会话、跨模型都必须稳定",
    },
    "A": {
        "label": "长期重要",
        "boost": 0.06,
        "meaning": "长期偏好、关系、目标；影响判断",
    },
    "B": {
        "label": "有用",
        "boost": 0.03,
        "meaning": "项目细节、阶段性事实；需要时参考",
    },
    "C": {
        "label": "情境",
        "boost": 0.0,
        "meaning": "一次性事件、临时状态；默认只作背景",
    },
    "D": {
        "label": "琐碎",
        "boost": -0.04,
        "meaning": "噪声、重复、过期；可归档",
    },
}

TARGET_TABLES = ("memory", "entity", "trait", "experience")


def _norm_segment(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).strip()
    return t.lower()


def normalize_path(path: str) -> str:
    """``我 / 姓名`` → ``我/姓名``；拒绝空段、`.`、`..`、超深、超长。"""
    if not isinstance(path, str) or not path.strip():
        raise ValidationError("分组路径不能为空", field="path")
    raw = unicodedata.normalize("NFKC", path).replace("\\", "/")
    parts = [part.strip() for part in raw.split("/") if part.strip()]
    if not parts:
        raise ValidationError("分组路径不能为空", field="path")
    if len(parts) > MAX_DEPTH:
        raise ValidationError(f"分组最多 {MAX_DEPTH} 层：{path}", field="path")
    for part in parts:
        if part in (".", ".."):
            raise ValidationError("分组名不能是 . 或 ..", field="path")
        if len(part) > MAX_SEGMENT:
            raise ValidationError(f"分组名单段最多 {MAX_SEGMENT} 字：{part}", field="path")
    return "/".join(parts)


def validate_grade(grade: str) -> str:
    value = (grade or "").strip().upper()
    if value not in GRADE_INFO:
        raise ValidationError(
            f"未知等级 {grade!r}", hint=f"可选 {', '.join(GRADE_INFO)}", field="grade"
        )
    return value


def boost_for(grade: str | None) -> float:
    return float(GRADE_INFO.get(grade or "", {}).get("boost", 0.0))


# ------------------------------------------------------------------ 建组
def ensure(
    db: Any,
    subject: str,
    path: str,
    *,
    kind: str = "custom",
    description: str | None = None,
    assigned_by: str = "ai",
) -> Any:
    """确保路径存在，返回叶子组。已存在则复用（幂等）。"""
    full_path = normalize_path(path)
    parts = full_path.split("/")
    parent_id: str | None = None
    row: Any = None
    with db.tx() as conn:
        for index, name in enumerate(parts):
            prefix = "/".join(parts[: index + 1])
            row = conn.execute(
                "SELECT * FROM grp WHERE subject_id=? AND path=? AND status='active'",
                (subject, prefix),
            ).fetchone()
            if row is not None:
                parent_id = row["id"]
                continue
            count = conn.execute(
                "SELECT COUNT(*) c FROM grp WHERE subject_id=? AND status='active'", (subject,)
            ).fetchone()["c"]
            if count >= MAX_GROUPS:
                raise BudgetError(
                    f"分组数已达上限 {MAX_GROUPS}",
                    hint="先合并近义组或跑 `pmem group gc` 归档空组",
                )
            grp_id = ulid()
            ts = now_iso()
            node_kind = "self" if (index == 0 and name == "我") else (
                kind if index == len(parts) - 1 else "custom"
            )
            try:
                conn.execute(
                    "INSERT INTO grp(id, subject_id, parent_id, name, name_norm, path, depth, kind, "
                    "description, status, ai_created, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?, 'active', ?,?,?)",
                    (
                        grp_id,
                        subject,
                        parent_id,
                        name,
                        _norm_segment(name),
                        prefix,
                        index + 1,
                        node_kind,
                        description if index == len(parts) - 1 else None,
                        1 if assigned_by == "ai" else 0,
                        ts,
                        ts,
                    ),
                )
            except sqlite3.IntegrityError:
                # 并发/重入：另一条路径刚建出来，直接复用
                row = conn.execute(
                    "SELECT * FROM grp WHERE subject_id=? AND path=? AND status='active'",
                    (subject, prefix),
                ).fetchone()
                if row is None:
                    raise
                parent_id = row["id"]
                continue
            events.append(
                conn,
                actor=f"system:{assigned_by}",
                op="group.create",
                target_tbl="grp",
                target_id=grp_id,
                subject_id=subject,
                payload={"path": prefix, "kind": kind, "ai_created": assigned_by == "ai"},
            )
            row = conn.execute("SELECT * FROM grp WHERE id=?", (grp_id,)).fetchone()
            parent_id = row["id"]
    return row


def _group_ref(db: Any, subject: str, ref: str) -> Any:
    row = db.conn.execute(
        "SELECT * FROM grp WHERE id=? AND status='active'", (ref,)
    ).fetchone()
    if row is not None:
        return row
    try:
        path = normalize_path(ref)
    except ValidationError:
        path = ""
    if path:
        row = db.conn.execute(
            "SELECT * FROM grp WHERE subject_id=? AND path=? AND status='active'",
            (subject, path),
        ).fetchone()
        if row is not None:
            return row
    row = db.conn.execute(
        "SELECT * FROM grp WHERE subject_id=? AND name_norm=? AND status='active' LIMIT 1",
        (subject, _norm_segment(ref)),
    ).fetchone()
    if row is not None:
        return row
    raise NotFoundError(f"找不到分组 {ref!r}", field="path")


def _target_exists(db: Any, target_tbl: str, target_id: str) -> bool:
    if target_tbl == "memory":
        return db.conn.execute(
            "SELECT 1 FROM memory WHERE id=? AND status<>'deleted'", (target_id,)
        ).fetchone() is not None
    if target_tbl == "entity":
        return db.conn.execute(
            "SELECT 1 FROM entity WHERE id=? AND status='active'", (target_id,)
        ).fetchone() is not None
    if target_tbl == "trait":
        return db.conn.execute("SELECT 1 FROM trait WHERE id=?", (target_id,)).fetchone() is not None
    if target_tbl == "experience":
        return db.conn.execute(
            "SELECT 1 FROM experience WHERE id=?", (target_id,)
        ).fetchone() is not None
    return False


def assign(
    db: Any,
    *,
    target_tbl: str,
    target_id: str,
    path: str | None = None,
    grade: str | None = None,
    rationale: str | None = None,
    evidence: Iterable[str] = (),
    assigned_by: str = "ai",
) -> dict[str, Any]:
    """把目标放进分组、打等级；两者可单独使用。"""
    if target_tbl not in TARGET_TABLES:
        raise ValidationError(f"不支持的目标类型 {target_tbl}", field="target_tbl")
    if not target_id:
        raise ValidationError("target_id 不能为空", field="target_id")
    if not _target_exists(db, target_tbl, target_id):
        raise NotFoundError(f"目标不存在：{target_tbl}:{target_id}", field="target_id")

    chosen_path: str | None = None
    if path:
        leaf = ensure(db, "self", path, assigned_by=assigned_by)
        chosen_path = leaf["path"]
        with db.tx() as conn:
            exists = conn.execute(
                "SELECT 1 FROM grp_member WHERE grp_id=? AND target_tbl=? AND target_id=?",
                (leaf["id"], target_tbl, target_id),
            ).fetchone()
            if exists is None:
                conn.execute(
                    "INSERT INTO grp_member(id, grp_id, target_tbl, target_id, assigned_by, created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (ulid(), leaf["id"], target_tbl, target_id, assigned_by, now_iso()),
                )
                events.append(
                    conn,
                    actor=f"system:{assigned_by}",
                    op="group.assign",
                    target_tbl=target_tbl,
                    target_id=target_id,
                    subject_id="self",
                    payload={"path": chosen_path, "assigned_by": assigned_by},
                )

    if grade:
        set_grade(
            db,
            target_tbl=target_tbl,
            target_id=target_id,
            grade=grade,
            rationale=rationale,
            evidence=list(evidence),
            assigned_by=assigned_by,
        )

    return {
        "target": {"tbl": target_tbl, "id": target_id},
        "groups": groups_for(db, target_tbl, target_id),
        "grade": grade_of(db, target_tbl, target_id),
        "path_added": chosen_path,
    }


def set_grade(
    db: Any,
    *,
    target_tbl: str,
    target_id: str,
    grade: str,
    rationale: str | None = None,
    evidence: Iterable[str] = (),
    assigned_by: str = "ai",
) -> dict[str, Any]:
    value = validate_grade(grade)
    if not _target_exists(db, target_tbl, target_id):
        raise NotFoundError(f"目标不存在：{target_tbl}:{target_id}", field="target_id")
    evidence_ids = [str(mid) for mid in evidence if mid]
    ts = now_iso()
    with db.tx() as conn:
        old = conn.execute(
            "SELECT grade FROM target_grade WHERE target_tbl=? AND target_id=?",
            (target_tbl, target_id),
        ).fetchone()
        conn.execute(
            "INSERT INTO target_grade(target_tbl, target_id, grade, rationale, evidence_json, "
            "assigned_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(target_tbl, target_id) DO UPDATE SET grade=excluded.grade, "
            "rationale=excluded.rationale, evidence_json=excluded.evidence_json, "
            "assigned_by=excluded.assigned_by, updated_at=excluded.updated_at",
            (
                target_tbl,
                target_id,
                value,
                rationale,
                json.dumps([{"memory_id": mid} for mid in evidence_ids], ensure_ascii=False),
                assigned_by,
                ts,
                ts,
            ),
        )
        events.append(
            conn,
            actor=f"system:{assigned_by}",
            op="group.grade",
            target_tbl=target_tbl,
            target_id=target_id,
            subject_id="self",
            payload={"grade": value, "previous": old["grade"] if old else None, "rationale": rationale},
        )
    return {"target": target_id, "grade": value, "previous": old["grade"] if old else None}


def groups_for(db: Any, target_tbl: str, target_id: str) -> list[str]:
    rows = db.conn.execute(
        "SELECT g.path FROM grp_member gm JOIN grp g ON g.id=gm.grp_id "
        "WHERE gm.target_tbl=? AND gm.target_id=? AND g.status='active' ORDER BY g.path",
        (target_tbl, target_id),
    ).fetchall()
    return [row["path"] for row in rows]


def group_map(db: Any, target_tbl: str, ids: list[str]) -> dict[str, list[str]]:
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = db.conn.execute(
        f"SELECT gm.target_id, g.path FROM grp_member gm JOIN grp g ON g.id=gm.grp_id "
        f"WHERE gm.target_tbl=? AND gm.target_id IN ({marks}) AND g.status='active' "
        f"ORDER BY g.path",
        (target_tbl, *ids),
    ).fetchall()
    out: dict[str, list[str]] = {}
    for row in rows:
        out.setdefault(row["target_id"], []).append(row["path"])
    return out


def grade_of(db: Any, target_tbl: str, target_id: str) -> str | None:
    row = db.conn.execute(
        "SELECT grade FROM target_grade WHERE target_tbl=? AND target_id=?",
        (target_tbl, target_id),
    ).fetchone()
    return row["grade"] if row else None


def grade_map(db: Any, target_tbl: str, ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = db.conn.execute(
        f"SELECT target_id, grade FROM target_grade WHERE target_tbl=? AND target_id IN ({marks})",
        (target_tbl, *ids),
    ).fetchall()
    return {row["target_id"]: row["grade"] for row in rows}


def grade_detail(db: Any, target_tbl: str, target_id: str) -> dict[str, Any] | None:
    row = db.conn.execute(
        "SELECT * FROM target_grade WHERE target_tbl=? AND target_id=?",
        (target_tbl, target_id),
    ).fetchone()
    if row is None:
        return None
    info = GRADE_INFO.get(row["grade"], {})
    return {
        "grade": row["grade"],
        "label": info.get("label", ""),
        "meaning": info.get("meaning", ""),
        "rationale": row["rationale"],
        "assigned_by": row["assigned_by"],
        "updated_at": row["updated_at"],
    }


# ------------------------------------------------------------------ 树操作
# 「还在场」的成员。软删只是停放：行和分组都留着等恢复，但任何列表都不该再显示它
# （I10 的口径）。所以显示侧统一挂这个条件；`groups_for` 不挂 —— 它是「这条记忆
# 属于哪」的直答，你主动要一条停放记忆时，答案应该包括它原来的分组。
LIVE_MEMBER = (
    "NOT (gm.target_tbl='memory' AND EXISTS "
    "(SELECT 1 FROM memory m WHERE m.id=gm.target_id AND m.status='deleted')) "
    "AND NOT (gm.target_tbl='entity' AND EXISTS "
    "(SELECT 1 FROM entity e WHERE e.id=gm.target_id AND e.status<>'active'))"
)


def tree(db: Any, subject: str = "self", *, root: str | None = None) -> list[dict[str, Any]]:
    path = normalize_path(root) if root else None
    if path:
        rows = db.conn.execute(
            "SELECT * FROM grp WHERE subject_id=? AND status='active' AND (path=? OR path LIKE ?) "
            "ORDER BY path",
            (subject, path, path + "/%"),
        ).fetchall()
    else:
        rows = db.conn.execute(
            "SELECT * FROM grp WHERE subject_id=? AND status='active' ORDER BY path", (subject,)
        ).fetchall()
    out = []
    for row in rows:
        count = db.conn.execute(
            f"SELECT COUNT(*) c FROM grp_member gm WHERE gm.grp_id=? AND {LIVE_MEMBER}",
            (row["id"],),
        ).fetchone()["c"]
        children = db.conn.execute(
            "SELECT COUNT(*) c FROM grp WHERE parent_id=? AND status='active'", (row["id"],)
        ).fetchone()["c"]
        out.append(
            {
                "id": row["id"],
                "path": row["path"],
                "name": row["name"],
                "depth": row["depth"],
                "kind": row["kind"],
                "description": row["description"],
                "ai_created": bool(row["ai_created"]),
                "members": count,
                "children": children,
            }
        )
    return out


def targets(
    db: Any,
    subject: str = "self",
    path: str | None = None,
    *,
    include_descendants: bool = True,
    target_tbl: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    sql = (
        "SELECT gm.target_tbl, gm.target_id, g.path, tg.grade FROM grp_member gm "
        "JOIN grp g ON g.id=gm.grp_id LEFT JOIN target_grade tg "
        "ON tg.target_tbl=gm.target_tbl AND tg.target_id=gm.target_id "
        f"WHERE g.subject_id=? AND g.status='active' AND {LIVE_MEMBER}"
    )
    params: list[Any] = [subject]
    if path:
        base = normalize_path(path)
        if include_descendants:
            sql += " AND (g.path=? OR g.path LIKE ?)"
            params.extend([base, base + "/%"])
        else:
            sql += " AND g.path=?"
            params.append(base)
    if target_tbl:
        sql += " AND gm.target_tbl=?"
        params.append(target_tbl)
    sql += " ORDER BY g.path, gm.created_at LIMIT ?"
    params.append(limit)
    rows = db.conn.execute(sql, params).fetchall()
    out = []
    for row in rows:
        item: dict[str, Any] = {
            "tbl": row["target_tbl"],
            "id": row["target_id"],
            "path": row["path"],
            "grade": row["grade"],
        }
        if row["target_tbl"] == "memory":
            mem = db.conn.execute("SELECT text_raw FROM memory WHERE id=?", (row["target_id"],)).fetchone()
            item["text"] = mem["text_raw"][:120] if mem else None
        elif row["target_tbl"] == "entity":
            ent = db.conn.execute("SELECT name, type FROM entity WHERE id=?", (row["target_id"],)).fetchone()
            if ent:
                item["name"] = ent["name"]
                item["type"] = ent["type"]
        out.append(item)
    return out


def rename(db: Any, ref: str, new_name: str, *, subject: str = "self") -> dict[str, Any]:
    row = _group_ref(db, subject, ref)
    name = new_name.strip()
    if not name or "/" in name or len(name) > MAX_SEGMENT:
        raise ValidationError("新分组名非法", field="name")
    parent_path = row["path"].rsplit("/", 1)[0] if row["depth"] > 1 else ""
    new_path = f"{parent_path}/{name}" if parent_path else name
    if new_path == row["path"]:
        return {"id": row["id"], "path": new_path, "renamed": 0}
    with db.tx() as conn:
        conflict = conn.execute(
            "SELECT 1 FROM grp WHERE subject_id=? AND status='active' AND id<>? "
            "AND (path=? OR path LIKE ?)",
            (subject, row["id"], new_path, new_path + "/%"),
        ).fetchone()
        if conflict:
            raise ConflictError(f"路径已存在：{new_path}", field="name")
        old_path = row["path"]
        conn.execute(
            "UPDATE grp SET path=? || substr(path, ?), depth=1+length(path)-length(replace(path,'/','')), "
            "updated_at=? WHERE subject_id=? AND status='active' AND (path=? OR path LIKE ?)",
            (new_path, len(old_path) + 1, now_iso(), subject, old_path, old_path + "/%"),
        )
        conn.execute(
            "UPDATE grp SET name=?, name_norm=?, updated_at=? WHERE id=?",
            (name, _norm_segment(name), now_iso(), row["id"]),
        )
        events.append(
            conn,
            actor="system:group",
            op="group.rename",
            target_tbl="grp",
            target_id=row["id"],
            subject_id=subject,
            payload={"from": old_path, "to": new_path},
        )
    return {"id": row["id"], "path": new_path, "renamed": 1}


def merge(db: Any, source: str, target: str, *, subject: str = "self") -> dict[str, Any]:
    src = _group_ref(db, subject, source)
    dst = _group_ref(db, subject, target)
    if src["id"] == dst["id"]:
        raise ValidationError("不能合并到自身", field="target")
    moved_members = 0
    moved_children = 0
    with db.tx() as conn:
        for member in conn.execute(
            "SELECT * FROM grp_member WHERE grp_id=?", (src["id"],)
        ).fetchall():
            exists = conn.execute(
                "SELECT 1 FROM grp_member WHERE grp_id=? AND target_tbl=? AND target_id=?",
                (dst["id"], member["target_tbl"], member["target_id"]),
            ).fetchone()
            if exists is None:
                conn.execute(
                    "INSERT INTO grp_member(id, grp_id, target_tbl, target_id, assigned_by, created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (ulid(), dst["id"], member["target_tbl"], member["target_id"],
                     member["assigned_by"], member["created_at"]),
                )
                moved_members += 1
        children = conn.execute(
            "SELECT * FROM grp WHERE parent_id=? AND status='active'", (src["id"],)
        ).fetchall()
        for child in children:
            new_path = f"{dst['path']}/{child['name']}"
            conflict = conn.execute(
                "SELECT 1 FROM grp WHERE subject_id=? AND status='active' AND id<>? "
                "AND (path=? OR path LIKE ?)",
                (subject, child["id"], new_path, new_path + "/%"),
            ).fetchone()
            if conflict:
                raise ConflictError(
                    f"合并会产生重名分组：{new_path}", hint="先改名或先合并同名子组", field="target"
                )
            old_path = child["path"]
            conn.execute(
                "UPDATE grp SET parent_id=?, path=? || substr(path, ?), "
                "depth=1+length(path)-length(replace(path,'/','')), updated_at=? "
                "WHERE subject_id=? AND status='active' AND (path=? OR path LIKE ?)",
                (
                    dst["id"],
                    new_path,
                    len(old_path) + 1,
                    now_iso(),
                    subject,
                    old_path,
                    old_path + "/%",
                ),
            )
            moved_children += 1
        conn.execute("DELETE FROM grp_member WHERE grp_id=?", (src["id"],))
        conn.execute(
            "UPDATE grp SET status='merged', merged_into=?, updated_at=? WHERE id=?",
            (dst["id"], now_iso(), src["id"]),
        )
        events.append(
            conn,
            actor="system:group",
            op="group.merge",
            target_tbl="grp",
            target_id=dst["id"],
            subject_id=subject,
            payload={
                "source": src["path"],
                "target": dst["path"],
                "members": moved_members,
                "children": moved_children,
            },
        )
    return {
        "source": src["path"],
        "target": dst["path"],
        "members": moved_members,
        "children": moved_children,
    }


def gc(db: Any, *, subject: str = "self") -> dict[str, Any]:
    """归档没有成员、也没有活跃子组的空组（根组保留）。"""
    archived = 0
    with db.tx() as conn:
        while True:
            rows = conn.execute(
                "SELECT g.id, g.path FROM grp g WHERE g.subject_id=? AND g.status='active' "
                "AND NOT (g.depth=1 AND g.kind='self') "
                "AND NOT EXISTS (SELECT 1 FROM grp_member m WHERE m.grp_id=g.id) "
                "AND NOT EXISTS (SELECT 1 FROM grp c WHERE c.parent_id=g.id AND c.status='active') "
                "ORDER BY g.depth DESC LIMIT 50",
                (subject,),
            ).fetchall()
            if not rows:
                break
            for row in rows:
                conn.execute(
                    "UPDATE grp SET status='archived', updated_at=? WHERE id=?", (now_iso(), row["id"])
                )
                archived += 1
        if archived:
            events.append(
                conn,
                actor="system:group",
                op="group.gc",
                target_tbl="grp",
                target_id=None,
                subject_id=subject,
                payload={"archived": archived},
            )
    return {"archived": archived}


def stats(db: Any, *, subject: str = "self") -> dict[str, Any]:
    by_depth = {
        row["depth"]: row["c"]
        for row in db.conn.execute(
            "SELECT depth, COUNT(*) c FROM grp WHERE subject_id=? AND status='active' GROUP BY depth",
            (subject,),
        ).fetchall()
    }
    by_kind = {
        row["kind"]: row["c"]
        for row in db.conn.execute(
            "SELECT kind, COUNT(*) c FROM grp WHERE subject_id=? AND status='active' GROUP BY kind",
            (subject,),
        ).fetchall()
    }
    grades = {
        row["grade"]: row["c"]
        for row in db.conn.execute(
            "SELECT grade, COUNT(*) c FROM target_grade GROUP BY grade"
        ).fetchall()
    }
    members = db.conn.execute("SELECT COUNT(*) c FROM grp_member").fetchone()["c"]
    grouped_memories = db.conn.execute(
        "SELECT COUNT(DISTINCT target_id) c FROM grp_member WHERE target_tbl='memory'"
    ).fetchone()["c"]
    total_memories = db.conn.execute(
        "SELECT COUNT(*) c FROM memory WHERE subject_id=? AND status<>'deleted'", (subject,)
    ).fetchone()["c"]
    return {
        "groups": sum(by_depth.values()),
        "groups_by_depth": by_depth,
        "groups_by_kind": by_kind,
        "memberships": members,
        "grades": grades,
        "graded": sum(grades.values()),
        "grouped_memories": grouped_memories,
        "total_memories": total_memories,
        "ungrouped_memories": max(0, total_memories - grouped_memories),
    }
