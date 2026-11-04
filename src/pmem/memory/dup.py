"""近重复检测与合并（M2）。

三条判定线（`docs/03` §6）：

- **同指纹**：`text_sha256` 相同 → 入库时就合并证据，不会走到这里；
- **同断言**：`claim_sig` 相同且不是 `txt:` 兜底（"我住在杭州" vs "我目前住在杭州"）；
- **同内容**：字符 3-gram 与 token 的混合相似度 ≥ `dedup.near`。

性能：用**分桶阻塞**（同签名桶 + 最稀 token 桶）把 O(n²) 降到近线性；
310 条记忆全量比较约 48k 对，阻塞后只剩几百对。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from ..base import events
from ..base.clock import now_iso
from ..base.db import Database
from ..memory.text import char_ngrams, jaccard, normalize, tokenize

DEFAULT_NEAR = 0.92


def _tokens(text: str) -> set[str]:
    seg = tokenize(text) or normalize(text)
    return {t for t in seg.split() if t.strip()}


def mixed_similarity(a_grams: set[str], a_tokens: set[str], b_grams: set[str], b_tokens: set[str]) -> float:
    j3 = jaccard(a_grams, b_grams)
    jt = jaccard(a_tokens, b_tokens)
    return round(0.5 * j3 + 0.5 * jt, 4)


def similarity(a: str, b: str) -> float:
    """混合相似度：字符 3-gram（抗错字/顺序）与 token（抗长文稀释）各半。"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return mixed_similarity(char_ngrams(a), _tokens(a), char_ngrams(b), _tokens(b))


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        self.parent.setdefault(item, item)
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def find_duplicate_groups(
    db: Database,
    *,
    subject: str = "self",
    near: float = DEFAULT_NEAR,
    max_items: int = 5000,
) -> list[dict[str, Any]]:
    """找重复簇。返回 ``[{reason, members:[ids], representative, size}]``。"""
    rows = db.conn.execute(
        "SELECT id, text_norm, text_sha256, claim_sig, kind, created_at FROM memory "
        "WHERE subject_id=? AND status NOT IN ('deleted') AND canonical_id IS NULL "
        "ORDER BY created_at LIMIT ?",
        (subject, max_items),
    ).fetchall()
    if len(rows) < 2:
        return []

    texts = [r["text_norm"] for r in rows]
    grams = [char_ngrams(t) for t in texts]
    toks = [_tokens(t) for t in texts]
    sigs = [r["claim_sig"] or "" for r in rows]
    kinds = [r["kind"] for r in rows]

    # 分桶：同 kind + 同断言；txt 兜底的按"最稀 token"聚
    df: Counter[str] = Counter()
    for token_set in toks:
        df.update(token_set)

    def bucket_key(i: int) -> list[str]:
        keys: list[str] = []
        sig = sigs[i]
        if sig and not sig.startswith("txt:"):
            keys.append(f"sig::{kinds[i]}::{sig}")
        tokens = toks[i]
        if tokens:
            rarest = min(tokens, key=lambda t: (df[t], -len(t)))
            keys.append(f"tok::{kinds[i]}::{rarest}")
        if not keys:
            keys.append(f"sha::{texts[i][:16]}")
        return keys

    buckets: dict[str, list[int]] = defaultdict(list)
    for i in range(len(rows)):
        for key in bucket_key(i):
            buckets[key].append(i)

    uf = _UnionFind()
    reasons: dict[tuple[str, str], str] = {}
    seen_pairs: set[tuple[int, int]] = set()

    for indices in buckets.values():
        if len(indices) < 2:
            continue
        for a_pos in range(len(indices)):
            i = indices[a_pos]
            for b_pos in range(a_pos + 1, len(indices)):
                j = indices[b_pos]
                if (i, j) in seen_pairs:
                    continue
                seen_pairs.add((i, j))
                if kinds[i] != kinds[j]:
                    continue
                if sigs[i] == sigs[j] and sigs[i] and not sigs[i].startswith("txt:"):
                    reason = "sig"
                elif rows[i]["text_sha256"] == rows[j]["text_sha256"]:
                    reason = "sha"
                elif mixed_similarity(grams[i], toks[i], grams[j], toks[j]) >= near:
                    reason = "sim"
                else:
                    continue
                uf.union(rows[i]["id"], rows[j]["id"])
                reasons[(rows[i]["id"], rows[j]["id"])] = reason

    groups: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        groups[uf.find(row["id"])].append(row["id"])

    order = {row["id"]: pos for pos, row in enumerate(rows)}
    out: list[dict[str, Any]] = []
    for representative, members in groups.items():
        if len(members) < 2:
            continue
        best_reason = "sim"
        member_set = set(members)
        for (a, b), why in reasons.items():
            if a in member_set and b in member_set:
                best_reason = why
                break
        members.sort(key=lambda m: order[m])
        out.append(
            {
                "representative": members[0],
                "members": members,
                "reason": best_reason,
                "size": len(members),
            }
        )
    out.sort(key=lambda g: -g["size"])
    return out


def merge_groups(
    db: Database,
    groups: list[dict[str, Any]],
    *,
    actor: str = "system:consolidate",
    dry_run: bool = False,
) -> dict[str, Any]:
    """把重复簇合并到代表条目：其余条目 ``canonical_id`` 指向代表，状态降级为 archived。"""
    if dry_run:
        return {"merged": 0, "skipped": len(groups), "dry_run": True, "groups": len(groups)}

    merged = 0
    skipped = 0
    absorbed_total: dict[str, list[str]] = {}
    with db.tx() as conn:
        for group in groups:
            rep = group["representative"]
            rep_row = conn.execute("SELECT id, status FROM memory WHERE id=?", (rep,)).fetchone()
            if rep_row is None:
                skipped += 1
                continue
            members = [m for m in group["members"] if m != rep]
            for member in members:
                conn.execute(
                    "UPDATE memory SET canonical_id=?, status='archived' WHERE id=? AND status <> 'deleted'",
                    (rep, member),
                )
                merged += 1
            if members:
                conn.execute(
                    "UPDATE memory SET evidence_count=evidence_count+?, importance=MAX(importance, 0.5) "
                    "WHERE id=?",
                    (len(members), rep),
                )
                absorbed_total[rep] = members
                events.append(
                    conn,
                    actor=actor,
                    op="memory.merge",
                    target_tbl="memory",
                    target_id=rep,
                    payload={"reason": group["reason"], "absorbed": members},
                )
    return {"merged": merged, "skipped": skipped, "groups": len(groups), "dry_run": False}


def consolidate(
    db: Database,
    *,
    subject: str = "self",
    near: float = DEFAULT_NEAR,
    dry_run: bool = False,
    actor: str = "system:consolidate",
) -> dict[str, Any]:
    """一次完整的去重巩固：找簇 → 合并 → 写事件。"""
    groups = find_duplicate_groups(db, subject=subject, near=near)
    stats = merge_groups(db, groups, actor=actor, dry_run=dry_run)
    stats["candidates"] = sum(g["size"] - 1 for g in groups)
    stats["at"] = now_iso()
    return stats
