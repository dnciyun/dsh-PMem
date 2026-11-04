"""重建派生索引：改分词规则之后，老库里的 ``text_seg`` 是旧规则算的。

分词规则一改，只改代码是不够的——库里的 ``text_seg`` 还是旧的，
新规则对那些行不生效，于是「修好了」只对以后新写的记忆成立。
这个模块做的事就是把派生列按当前规则重算一遍，并让 FTS 跟着更新。

只碰派生列（``text_seg``），原文和一切结论都不动。
"""

from __future__ import annotations

from typing import Any

from ..memory.text import tokenize


def tokens(db: Any, *, subject: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    """按当前 ``tokenize`` 规则重算所有记忆的 ``text_seg``。"""
    sql = "SELECT seq, text_norm, text_seg FROM memory WHERE status<>'deleted'"
    params: tuple[Any, ...] = ()
    if subject:
        sql += " AND subject_id=?"
        params = (subject,)
    rows = db.conn.execute(sql + " ORDER BY seq", params).fetchall()

    changed = 0
    samples: list[dict[str, Any]] = []
    for row in rows:
        fresh = tokenize(row["text_norm"])
        if fresh == (row["text_seg"] or ""):
            continue
        changed += 1
        if len(samples) < 5:
            samples.append(
                {"seq": row["seq"], "before": (row["text_seg"] or "")[:60], "after": fresh[:60]}
            )
        if dry_run:
            continue
        with db.tx():
            # 只改派生列；mem_au 触发器会把 FTS 同步过去
            db.conn.execute("UPDATE memory SET text_seg=? WHERE seq=?", (fresh, row["seq"]))
    if changed and not dry_run:
        # 关联图也是从 text_seg 派生的：分词规则变了，边也跟着重算，
        # 否则「重建索引」只修好了词法，图还停在旧规则上。
        from ..memory import graph as graph_mod

        graph_mod.rebuild(db)
    return {"memories": len(rows), "changed": changed, "dry_run": dry_run, "samples": samples}


__all__ = ["tokens"]
