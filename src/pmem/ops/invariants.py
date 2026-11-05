"""不变量自检（`docs/02` §5 I1–I23）。

``pmem doctor`` 调它。每条返回 ``{id, ok, detail}``；失败不抛异常 —— 体检要能一次看全。

实现方式（逐条）：

- I1  ``memory`` 原文不可变触发器存在            → 查 ``sqlite_master``（schema 层执法）
- I2  活跃记忆必须有 ``provenance``/``confidence`` → 扫 ``memory``
- I3  派生行必须带证据指针                        → 扫 relation/trait/pattern/experience 的 ``evidence_json`` 非空
- I4  证据指针不能指向已不存在（真删）的记忆      → 扫 relation/trait/pattern/experience（容错 JSON）
  + ``conflict.left_id/right_id`` + ``feedback.target_id``
- I5  ``trait`` 版本链无环                        → 扫 ``trait.prev_id``
- I6  ``event_log`` 哈希链连续                    → ``events.verify_chain``
- I8  ``supersedes_id``/``superseded_by`` 双向一致 → 扫 ``memory``
- I9  向量行不指向已消失的记忆                    → 扫 ``memory_vec_plain``
- I12 派生行必须有 ``derivation``                  → 扫 relation/trait/pattern
- I13 ``scope_key`` 形状                           → 正则扫 ``memory``
- I14 关系权重不超过谓词上限                       → JOIN ``relation_type``
- I15 ``entity_mention`` 的 span 落在正文内        → JOIN ``memory``
- I16 活跃关系无自环                               → 扫 ``relation``
- I17 分组路径/深度自洽 + 成员指向存在的宿主        → 扫描（软删是停放，不报错）
- I18 关联图边规范化 / 无悬空                       → 扫 ``memory_edge``
- I19 向量维度自洽 / 无悬空                         → 扫 ``memory_vec_plain``
- I20 日记声称派生的事实「交代过」多少              → 分组扫 ``memory.episode_id`` + ``idem`` 幂等键
  （``journal:{ep}:{i}`` 与 ``journal:{ep}:{i}:skipped`` 都算交代过）
- I21 FTS 索引行数漂移（journal/memory/episode）    → 基表行数 vs FTS 文档数（``<fts>_docsize`` 影子表）
- I22 事件链锚点（防尾部截断）                      → ``events.check_anchor``（锚点由 backup/snapshot 写入）
- I23 派生事实的 ``day_key`` 必须是发生日            → 扫 ``memory``（只查 ``source_kind='journal'``）

不在 ``check_all`` 里的三条：I7（``trait`` 键唯一）由唯一索引执法；I10（软删不进默认召回）
由召回 SQL + 契约测试守；I11（快照可读性）由 ``pmem backup drill`` 与 sleep s8 自检覆盖。
所以 ``doctor`` 的项数 = 20（I1–I6、I8、I9、I12–I23），不是 23。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..base import events
from ..base.clock import occurred_day
from ..base.db import Database

SCOPE_RE = re.compile(r"^project:[0-9a-f]{8,64}$|^project:manual$")


def _scalar(conn: Any, sql: str, params: tuple = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _table_exists(conn: Any, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _parse_evidence(raw: Any) -> tuple[list[Any], str | None]:
    """容错解析证据 JSON：坏 JSON 不抛异常，返回 ``(items, 错误说明)``。

    为什么需要：I4 是体检脚本，不能因为一条脏数据把整次 doctor 打崩；但也不能
    假装没看见 —— 坏 JSON 本身算一条要报的问题（detail 里列出来）。
    """
    if raw is None or raw == "":
        return [], None
    if isinstance(raw, (list, dict)):
        payload = raw
    else:
        try:
            payload = json.loads(str(raw))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            return [], f"{type(exc).__name__}: {exc}"
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("evidence")
    else:
        items = None
    return (items if isinstance(items, list) else []), None


def _evidence_memory_ids(raw: Any) -> tuple[list[str], str | None]:
    """证据 JSON → 其中引用的 memory id 列表（容错）。"""
    items, err = _parse_evidence(raw)
    if err:
        return [], err
    ids: list[str] = []
    for item in items:
        if isinstance(item, dict):
            memory_id = item.get("memory_id")
            if memory_id:
                ids.append(str(memory_id))
    return ids, None


def _fts_doc_count(conn: Any, fts: str) -> int | None:
    """FTS5 索引里的文档条数；拿不到（缺影子表）返回 ``None``。

    为什么不能直接 ``SELECT COUNT(*) FROM memory_fts_tok``：它是 external-content
    表（``content='memory'``），COUNT 会去扫 ``memory`` 基表 —— 索引坏没坏都返回
    基表行数。真正的索引文档数在 ``<fts>_docsize`` 里（FTS5 默认 columnsize=1）。
    """
    shadow = f"{fts}_docsize"
    if not _table_exists(conn, shadow):
        return None
    return _scalar(conn, f"SELECT COUNT(*) FROM {shadow}")


def _journal_accounted(conn: Any) -> dict[str, int]:
    """每篇 episode「交代过」的派生条数（按 index 去重）。

    两种幂等键都算交代过：

    - ``journal:{episode_id}:{index}``          事实写成了（或被合并成证据）；
    - ``journal:{episode_id}:{index}:skipped``  这行原话被跳过（噪声/已撤回，repair 记的）。
    """
    out: dict[str, set[int]] = {}
    for row in conn.execute("SELECT key FROM idem WHERE key LIKE 'journal:%'"):
        parts = str(row[0]).split(":")
        if len(parts) not in (3, 4) or parts[0] != "journal" or not parts[1]:
            continue
        if len(parts) == 4 and parts[3] != "skipped":
            continue  # 认识但格式不对的键不算交代，避免脏键把账抹平
        try:
            index = int(parts[2])
        except (TypeError, ValueError):
            continue
        out.setdefault(parts[1], set()).add(index)
    return {episode_id: len(indexes) for episode_id, indexes in out.items()}


def check_all(db: Database, *, subject: str = "self") -> list[dict[str, Any]]:
    conn = db.conn
    results: list[dict[str, Any]] = []

    def add(check_id: str, ok: bool, detail: str = "") -> None:
        results.append({"id": check_id, "ok": bool(ok), "detail": detail})

    # I1 原文不可变：触发器必须在
    trigger = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='mem_immutable'"
    ).fetchone()
    add("I1", trigger is not None, "mem_immutable 触发器存在" if trigger else "缺少不可变触发器")

    # I2 活跃记忆必须有来源与置信度
    bad = _scalar(
        conn,
        "SELECT COUNT(*) FROM memory WHERE status IN ('active','contested') "
        "AND (confidence <= 0 OR provenance IS NULL OR provenance='')",
    )
    add("I2", bad == 0, f"{bad} 条活跃记忆缺来源/置信度")

    # I3 派生行必须带证据
    empty_evidence = 0
    for table in ("relation", "trait", "pattern", "experience"):
        empty_evidence += _scalar(
            conn, f"SELECT COUNT(*) FROM {table} WHERE evidence_json IS NULL OR evidence_json IN ('','[]','{{}}')"
        )
    add("I3", empty_evidence == 0, f"{empty_evidence} 条派生行没有证据指针")

    # I4 证据指针不能指向已被真删的记忆
    #
    # 覆盖所有「按 id 引用 memory」的派生落点：relation/trait/pattern/experience 的
    # evidence_json（JSON 数组），conflict 的 left/right 指针，feedback 的 target。
    # 真删只清 relation（store.delete），其余几张表历史上会留悬空；先让体检看得见。
    # JSON 坏掉不能把 doctor 打崩，单独计数并在 detail 里点名。
    dangling_by: dict[str, int] = {}
    bad_json_by: dict[str, int] = {}
    for table in ("relation", "trait", "pattern", "experience"):
        for row in conn.execute(f"SELECT evidence_json FROM {table}"):
            memory_ids, err = _evidence_memory_ids(row[0])
            if err:
                bad_json_by[table] = bad_json_by.get(table, 0) + 1
                continue
            for memory_id in memory_ids:
                if conn.execute("SELECT 1 FROM memory WHERE id=?", (memory_id,)).fetchone() is None:
                    dangling_by[table] = dangling_by.get(table, 0) + 1
    for row in conn.execute("SELECT left_tbl, left_id, right_tbl, right_id FROM conflict"):
        for tbl, ident in ((row[0], row[1]), (row[2], row[3])):
            if tbl == "memory" and ident:
                if conn.execute("SELECT 1 FROM memory WHERE id=?", (str(ident),)).fetchone() is None:
                    dangling_by["conflict"] = dangling_by.get("conflict", 0) + 1
    for row in conn.execute("SELECT target_tbl, target_id FROM feedback"):
        if row[0] == "memory" and row[1]:
            if conn.execute("SELECT 1 FROM memory WHERE id=?", (str(row[1]),)).fetchone() is None:
                dangling_by["feedback"] = dangling_by.get("feedback", 0) + 1
    dangling = sum(dangling_by.values())
    bad_json = sum(bad_json_by.values())
    if dangling or bad_json:
        pieces = [f"{table} {count}" for table, count in sorted(dangling_by.items()) if count]
        if pieces:
            detail = f"{dangling} 个悬空证据指针（{'，'.join(pieces)}）"
        else:
            detail = "0 个悬空证据指针"
        if bad_json:
            bad_pieces = [f"{table} {count}" for table, count in sorted(bad_json_by.items())]
            detail += f"；另有 {bad_json} 条证据 JSON 解析失败（{'，'.join(bad_pieces)}）"
    else:
        detail = "0 个悬空证据指针"
    add("I4", dangling == 0 and bad_json == 0, detail)

    # I5 特质版本链不能成环
    cycles = 0
    rows = conn.execute("SELECT id, prev_id FROM trait").fetchall()
    prev = {r["id"]: r["prev_id"] for r in rows}
    for start in prev:
        seen = set()
        node = start
        while node and node in prev:
            if node in seen:
                cycles += 1
                break
            seen.add(node)
            node = prev[node]
    add("I5", cycles == 0, f"{cycles} 个特质版本链成环")

    # I6 事件哈希链
    chain_ok, chain_detail = events.verify_chain(conn)
    add("I6", chain_ok, chain_detail)

    # I8 supersede 双向一致
    inconsistent = _scalar(
        conn,
        "SELECT COUNT(*) FROM memory a JOIN memory b ON a.superseded_by=b.id "
        "WHERE b.supersedes_id IS NOT a.id",
    ) + _scalar(
        conn,
        "SELECT COUNT(*) FROM memory a JOIN memory b ON a.supersedes_id=b.id "
        "WHERE b.superseded_by IS NOT a.id",
    )
    add("I8", inconsistent == 0, f"{inconsistent} 条替代关系单向")

    # I9 向量行不能指向不存在的记忆
    orphan_vec = _scalar(
        conn,
        "SELECT COUNT(*) FROM memory_vec_plain v LEFT JOIN memory m ON v.memory_seq=m.seq WHERE m.seq IS NULL",
    )
    add("I9", orphan_vec == 0, f"{orphan_vec} 条孤儿向量")

    # I12 派生行必须有 derivation（experience 表按设计没有该列，故不查）
    missing_derivation = 0
    for table in ("relation", "trait", "pattern"):
        missing_derivation += _scalar(
            conn, f"SELECT COUNT(*) FROM {table} WHERE derivation IS NULL OR derivation=''"
        )
    add("I12", missing_derivation == 0, f"{missing_derivation} 条派生行缺 derivation")

    # I13 scope_key 形状
    rows = conn.execute("SELECT DISTINCT scope_key FROM memory").fetchall()
    bad_scope = [r["scope_key"] for r in rows if not SCOPE_RE.match(r["scope_key"] or "")]
    add("I13", not bad_scope, f"非法 scope_key：{bad_scope[:3]}")

    # I14 关系权重不得超过谓词上限（防止手工/派生写越界）
    over_weight = _scalar(
        conn,
        "SELECT COUNT(*) FROM relation r JOIN relation_type t ON t.predicate=r.predicate "
        "WHERE r.weight > t.max_weight + 1e-9",
    )
    add("I14", over_weight == 0, f"{over_weight} 条关系权重超过谓词上限")

    # I15 实体提及的 span 必须落在正文内
    bad_span = _scalar(
        conn,
        "SELECT COUNT(*) FROM entity_mention em JOIN memory m ON m.seq=em.memory_seq "
        "WHERE em.span_start < 0 OR em.span_end <= em.span_start "
        "OR em.span_end > length(m.text_norm)",
    )
    add("I15", bad_span == 0, f"{bad_span} 条提及 span 越界")

    # I16 活跃关系不允许自环
    self_loop = _scalar(
        conn, "SELECT COUNT(*) FROM relation WHERE src_id=dst_id AND status IN ('active','contested')"
    )
    add("I16", self_loop == 0, f"{self_loop} 条关系自环")

    # I17 分组路径/深度自洽；成员只能指向「还存在」的宿主
    # 软删是停放不是消失：记忆行、分组、等级都留着等恢复，成员关系必须跟着留，
    # 否则捞回来的时候它悄悄掉出原来的分组。所以这里只查宿主是不是真的没了。
    # 实体不同：合并会把成员迁到保留的那一侧，留在非活跃实体上的成员就是没迁干净的残留。
    bad_group = _scalar(
        conn,
        "SELECT COUNT(*) FROM grp g LEFT JOIN grp p ON p.id=g.parent_id "
        "WHERE g.status='active' AND ("
        "g.depth <> 1 + length(g.path) - length(replace(g.path,'/','')) "
        "OR (g.parent_id IS NULL AND g.path <> g.name) "
        "OR (g.parent_id IS NOT NULL AND (p.id IS NULL OR g.path <> p.path || '/' || g.name)))",
    )
    orphan_member = _scalar(
        conn,
        "SELECT COUNT(*) FROM grp_member gm JOIN grp g ON g.id=gm.grp_id "
        "WHERE g.status <> 'active'",
    )
    parked = _scalar(
        conn,
        "SELECT COUNT(*) FROM grp_member gm JOIN memory m ON m.id=gm.target_id "
        "WHERE gm.target_tbl='memory' AND m.status='deleted'",
    )
    orphan_target = _scalar(
        conn,
        "SELECT COUNT(*) FROM grp_member gm WHERE "
        "(gm.target_tbl='memory' AND NOT EXISTS (SELECT 1 FROM memory m WHERE m.id=gm.target_id)) "
        "OR (gm.target_tbl='entity' AND NOT EXISTS "
        "  (SELECT 1 FROM entity e WHERE e.id=gm.target_id AND e.status='active'))",
    )
    detail = (
        f"{bad_group} 个分组路径/深度不一致，{orphan_member} 条成员指向非活跃分组，"
        f"{orphan_target} 条成员指向已消失的目标"
    )
    if parked:
        detail += f"（另有 {parked} 条挂在停放记忆上，属软删保留，不报错）"
    add("I17", bad_group == 0 and orphan_member == 0 and orphan_target == 0, detail)

    # I18 关联图边规范化：a_seq < b_seq（无向边只存一份）、无自环、不指向已消失的记忆
    bad_edge = _scalar(conn, "SELECT COUNT(*) FROM memory_edge WHERE a_seq >= b_seq")
    dangling_edge = _scalar(
        conn,
        "SELECT COUNT(*) FROM memory_edge e "
        "LEFT JOIN memory a ON a.seq=e.a_seq LEFT JOIN memory b ON b.seq=e.b_seq "
        "WHERE a.seq IS NULL OR b.seq IS NULL",
    )
    add(
        "I18",
        bad_edge == 0 and dangling_edge == 0,
        f"{bad_edge} 条边未规范化，{dangling_edge} 条边指向不存在的记忆",
    )

    # I19 向量表自洽：维度>0、blob 长度 = dim×2（fp16）或 dim×4（老 fp32）、不指向已消失的记忆
    bad_vec = _scalar(
        conn,
        "SELECT COUNT(*) FROM memory_vec_plain "
        "WHERE dim <= 0 OR length(vec) NOT IN (dim*2, dim*4)",
    )
    dangling_vec = _scalar(
        conn,
        "SELECT COUNT(*) FROM memory_vec_plain v "
        "LEFT JOIN memory m ON m.seq=v.memory_seq WHERE m.seq IS NULL",
    )
    add(
        "I19",
        bad_vec == 0 and dangling_vec == 0,
        f"{bad_vec} 条向量维度不自洽，{dangling_vec} 条向量指向不存在的记忆",
    )

    # I20 日记「声称派生的事实」必须真的派生过（digest 半截失败的探测器）
    #
    # 判据不是「现在还剩几条」，而是「交代过几条」：一条日记派生的事实会在
    # ``store.add`` 的同一个事务里留下 ``idem_key = journal:{episode_id}:{index}``；
    # repair 判定某行原话是噪声/已撤回而跳过时，会留下
    # ``journal:{episode_id}:{index}:skipped``。两种都算「已交代」。
    # 所以「行还在 / 被停放 / 被真删 / 被跳过」都不会误报，只有
    # **半截失败**（进程被杀、store.add 抛错且没被 repair 处理）才既没有行也没有键。
    #
    # 两次分组扫描，不逐篇 COUNT/LIKE：``memory.episode_id`` 没有索引，
    # 逐篇查会退化成 O(篇数 × 全表)，把 doctor 拖慢。
    derived_map = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            "SELECT episode_id, COUNT(*) c FROM memory WHERE episode_id IS NOT NULL GROUP BY episode_id"
        )
    }
    accounted_map = _journal_accounted(conn)
    bad_episodes = 0
    missing_facts = 0
    for row in conn.execute("SELECT id, memory_count FROM episode WHERE memory_count>0"):
        claimed = int(row[1] or 0)
        accounted = max(derived_map.get(str(row[0]), 0), accounted_map.get(str(row[0]), 0))
        if accounted < claimed:
            bad_episodes += 1
            missing_facts += claimed - accounted
    add(
        "I20",
        bad_episodes == 0,
        f"{bad_episodes} 篇日记声称的事实没派生全（共缺 {missing_facts} 条；"
        f"`pmem journal repair` 可补）",
    )

    # I21 FTS 索引行数漂移（只查「条数对不对」，不查内容/校验和）
    #
    # 为什么只查行数：倒排索引内部的字节级损坏（malformed inverted index）由
    # ``pmem doctor`` 的 PRAGMA integrity_check 管；这里只抓最常见的「写了基表没写索引 /
    # 删了基表没删索引」—— rebuild/reindex 能修的那种。
    #
    # memory 的基线用**全表行数**（含软删/停放），不是 status<>'deleted'：
    # 软删只 UPDATE status，不触发 FTS 更新触发器（mem_au 只看 text_seg/text_norm），
    # 索引行按设计保留（恢复时不用重建）；硬删才会经 mem_ad 触发器删索引。
    # 拿 status<>'deleted' 去比，任何一条软删都会误报红。
    drift: list[str] = []
    journal_lines = _scalar(conn, "SELECT COUNT(*) FROM journal_line")
    journal_docs = _fts_doc_count(conn, "journal_fts")
    memory_rows = _scalar(conn, "SELECT COUNT(*) FROM memory")
    soft_deleted = _scalar(conn, "SELECT COUNT(*) FROM memory WHERE status='deleted'")
    tok_docs = _fts_doc_count(conn, "memory_fts_tok")
    tri_docs = _fts_doc_count(conn, "memory_fts_tri")
    episodes = _scalar(conn, "SELECT COUNT(*) FROM episode")
    episode_docs = _fts_doc_count(conn, "episode_fts")
    if journal_docs is None:
        drift.append("journal_fts 缺 docsize 影子表（索引不可读）")
    elif journal_lines != journal_docs:
        drift.append(f"journal_line {journal_lines} ≠ journal_fts {journal_docs}")
    for label, docs in (("memory_fts_tok", tok_docs), ("memory_fts_tri", tri_docs)):
        if docs is None:
            drift.append(f"{label} 缺 docsize 影子表（索引不可读）")
        elif memory_rows != docs:
            drift.append(f"memory {memory_rows} ≠ {label} {docs}")
    if episode_docs is None:
        drift.append("episode_fts 缺 docsize 影子表（索引不可读）")
    elif episodes != episode_docs:
        drift.append(f"episode {episodes} ≠ episode_fts {episode_docs}")
    if drift:
        detail = (
            "FTS 行数漂移：" + "；".join(drift)
            + "（原话索引跑 `pmem journal reindex` 重建；其余先跑 `pmem doctor` 看完整性，"
            "别手工删 FTS 行）"
        )
    else:
        detail = (
            f"索引同步：journal {journal_lines}/{journal_docs} · "
            f"memory {memory_rows}/{tok_docs}/{tri_docs}"
            + (f"（含软删 {soft_deleted}，索引按设计保留）" if soft_deleted else "")
            + f" · episode {episodes}/{episode_docs}"
        )
    add("I21", not drift, detail)

    # I22 事件链锚点：只允许追加，砍尾巴/改尾巴要立刻红
    #
    # 为什么需要：``verify_chain``（I6）只能发现「改/删中段」；删掉尾部若干行后链
    # 仍然自洽 —— 单看 I6 永远发现不了审计日志被截断。锚点是外部参照（backup/snapshot
    # 时由 events.write_anchor 写入），拿「当前条数/锚点事件 hash」对一下就知道有没有被砍。
    anchor_ok, anchor_detail = events.check_anchor(conn)
    add("I22", anchor_ok, anchor_detail)

    # I23 派生事实的 day_key 必须是「发生日」（不是整理日）
    #
    # 为什么只查 journal：其它 source_kind（cli/dsh_session/rule/pressure）的
    # ``valid_from`` 可能表示「这条事实从何时开始生效」，与 ``day_key`` 允许不同；
    # 日记派生事实的语义是死的——日期就该是原料那一行 ``ts`` 的本地日期。
    # 迁移（``Database._migrate_memory_day``）会回填老库；这里守回填之后不再漂移。
    bad_day = 0
    for row in conn.execute(
        "SELECT valid_from, day_key FROM memory WHERE source_kind='journal' "
        "AND valid_from IS NOT NULL AND status<>'deleted'"
    ):
        occurred = occurred_day(row["valid_from"])
        if occurred is not None and occurred != row["day_key"]:
            bad_day += 1
    add("I23", bad_day == 0, f"{bad_day} 条派生事实的 day_key 不是发生日")

    return results


def summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    failed = [r for r in results if not r["ok"]]
    return {
        "total": len(results),
        "passed": len(results) - len(failed),
        "failed": failed,
        "ok": not failed,
    }
