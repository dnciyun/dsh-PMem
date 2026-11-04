"""关联扩散通道（M12 / 0.12.0）：稀有词共现图 + 带重启随机游走。

**要解决的问题**：词法命中是种子，不是答案。

问「自行车头一回送去修，是哪个零件出的问题」，词法层撞到的是
「第三次去修车摊」，真正的答案是「后闸松了……换线管」——两条记忆共享
「修车摊 / 自行车 / 后闸」这些**稀有词**，但问句里一个都没有，所以词法层看不见。
人不是这么回忆的：先想起「我修过自行车」，再顺着「修车摊」漂到具体那一次。

**做法**（和 soul / HippoRAG 同源，但落在 PMem 自己的派生层上）：

1. 每条记忆一个节点；两条记忆共享**稀有词**就拉一条边，边权 = Σ 1/df
   （越少见的词，越能说明「说的是同一件事」）；
2. 查询时用词法结果（payload 前几条）做种子，在图上做 PPR（alpha 重启，3 轮），
   按扩散质量补候选——一跳只能覆盖第一圈，PPR 才能让两跳外的限定条件浮出来；
3. 只在**词法覆盖弱**的时候动手（``retrieval.graph.trigger_coverage``）：
   词法有把握时不加噪，也不多花注入成本。

三条约束（对齐 PLAN §1）：

- **派生可重放**：边只由 ``memory.text_seg`` 决定，``rebuild()`` 随时整表重算，
  不存原文、不调模型；增量写入是同一公式的近似（df 取当时的），重建即规范化。
- **权重要能解释**：边权就是「共享了几个稀有词、每个词多稀有」，没有隐藏参数。
- **不改变既有排序**：扩散结果只做**补候选**（新条目），不重排已有词法结果——
  改排序会同时牵动所有题，收益无法归因。

性能：邻接只按种子分层取（每层一条 ``IN (...)`` 查询），检索时最多两跳；
780 条记忆上整轮 < 1 ms，10k 边上每跳仍是毫秒级（与 ``net.graph`` 同一取法）。
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable

from ..base.clock import now_iso

RELATED = "related"
MIN_TERM_LEN = 2
# 每个点最多保留几条边：fan-out 是查询时两跳成本的唯一上限。
DEFAULT_PER_NODE = 8
# 边权下限（1/df 的和）：一条边至少要有一个不太常见的共享词才值得走。
DEFAULT_MIN_WEIGHT = 0.12
# 稀有词上限（绝对数）：df 太大的词说明是「大家都说过」，它连出来的边是噪声。
DEFAULT_MAX_DF = 24
# 单次查询最多装多少个节点进内存（防止 hub 把内存打爆）。
MAX_NODES = 400

_REBUILD_SQL_DELETE = "DELETE FROM memory_edge"
_INSERT_SQL = (
    "INSERT INTO memory_edge(a_seq, b_seq, rel, w, built_at) VALUES(?,?,?,?,?) "
    "ON CONFLICT(a_seq, b_seq, rel) DO UPDATE SET w=excluded.w, built_at=excluded.built_at"
)


# 双字里带这些字的，基本都是「了定」「了三」这种接缝词或虚词组合，
# 它们能把两条毫不相干的记忆连起来（实测：「改了三版」和「输了三百块」共享「了三」）。
# 注意**不能**把 上/下/中/里 放进来：它们常是词的一部分（早上/上班/中午/家里），
# 之前 content_terms 逐字删虚词就是把「早上」删成「早」才出的覆盖率 bug。
_FUNC_CHARS = frozenset("的了着过是在有和与或就都也还又再才只被把给对向从到为吗呢吧啊呀嘛哦嗯我你他她它这那")


def _terms(text_seg: str | None) -> list[str]:
    out: list[str] = []
    for token in (text_seg or "").split():
        if len(token) < MIN_TERM_LEN:
            continue
        if any(ch in _FUNC_CHARS for ch in token):
            continue
        out.append(token)
    return out


def effective_max_df(total: int, max_df: int = DEFAULT_MAX_DF) -> int:
    """「稀有」是相对的：一个词出现在超过 10% 的记忆里，就不是区分信号了。

    绝对上界（默认 24）只兜住大库；小库（几十条记忆）里几乎所有词都「罕见」，
    于是「用户」这种词会把不相干的记忆全连成一张网（测试里 3 条记忆连出 3 条边）。
    取 ``min(max_df, max(2, 10% * total))``：库里只有 3 条时只有 df=2 才算稀有。
    """
    if total <= 0:
        return max_df
    return max(2, min(max_df, int(0.10 * total)))


def _pair_weight(df: int) -> float:
    """一条共享稀有词的贡献：越少见越重。df=2 时 0.5，df=24 时约 0.04。"""
    return 1.0 / max(1, df)


def _canon(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def _truncate(weights: dict[int, dict[int, float]], *, per_node: int, min_weight: float) -> list[tuple[int, int, float]]:
    """每个点只留最强的 ``per_node`` 条边；边权低于 ``min_weight`` 的丢掉。

    截断必须是**对称**的：a 保留了 b，b 也必须保留 a，否则图变成有向的，
    PPR 的「关联没有方向」前提就不成立了。所以先按点各留一份，再取交集
    （任一侧觉得这条边不够强，就是不够强）。
    """
    kept: dict[int, set[int]] = {}
    for seq, links in weights.items():
        top = sorted(links.items(), key=lambda kv: (-kv[1], kv[0]))[:per_node]
        kept[seq] = {other for other, w in top if w >= min_weight}
    out: list[tuple[int, int, float]] = []
    seen: set[tuple[int, int]] = set()
    for seq, others in kept.items():
        for other in others:
            if seq not in kept.get(other, ()):  # 对侧没留 → 这条边不成
                continue
            pair = _canon(seq, other)
            if pair in seen:
                continue
            seen.add(pair)
            out.append((pair[0], pair[1], round(weights[seq][other], 6)))
    return out


def rebuild(
    db: Any,
    *,
    max_df: int = DEFAULT_MAX_DF,
    per_node: int = DEFAULT_PER_NODE,
    min_weight: float = DEFAULT_MIN_WEIGHT,
    ts: str | None = None,
) -> dict[str, Any]:
    """整表重算（派生层可重放：同样的记忆 → 同样的边）。

    一条 ``SELECT seq, text_seg`` 扫全表就够了，不需要重新分词——
    ``text_seg`` 本来就是分词结果，图只是它的另一种读法。
    """
    stamp = ts or now_iso()
    conn = db.conn
    rows = conn.execute("SELECT seq, text_seg FROM memory WHERE status<>'deleted' ORDER BY seq").fetchall()
    total = len(rows)
    postings: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        for term in sorted(set(_terms(row["text_seg"]))):
            postings[term].append(int(row["seq"]))

    cap = effective_max_df(total, max_df)
    weights: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    pairs = 0
    for term, seqs in postings.items():
        df = len(seqs)
        if df < 2 or df > cap:
            continue
        w = _pair_weight(df)
        for i, a in enumerate(seqs):
            for b in seqs[i + 1 :]:
                weights[a][b] += w
                weights[b][a] += w
                pairs += 1

    edges = _truncate(weights, per_node=per_node, min_weight=min_weight)
    with db.tx():
        conn.execute(_REBUILD_SQL_DELETE)
        conn.executemany(_INSERT_SQL, [(a, b, RELATED, w, stamp) for a, b, w in edges])
        _mark_covered(db, total)
    return {
        "memories": total,
        "terms": len(postings),
        "pairs": pairs,
        "edges": len(edges),
        "nodes": len({a for a, _, _ in edges} | {b for _, b, _ in edges}),
        "max_df": cap,
    }


def link_new(
    conn: Any,
    *,
    seq: int,
    text_seg: str | None,
    max_df: int = DEFAULT_MAX_DF,
    per_node: int = DEFAULT_PER_NODE,
    min_weight: float = DEFAULT_MIN_WEIGHT,
    ts: str | None = None,
    total: int | None = None,
) -> int:
    """新记忆入库时增量连边（在调用方的写事务里跑，不另开事务）。

    做法和 rebuild 同公式，但只算「这一条 ↔ 已有记忆」：
    用 FTS 当倒排表（``memory_fts_tok`` 本来就是 token → rowid），
    每个候选词先取 ``max_df+1`` 条 postings，超了就说明这词太常见，跳过。

    为什么不等重建：真实使用时每轮都在加记忆，如果每次加完都整表重算，
    一次就是几百毫秒；这里每条约 10–30 次索引点查，量级是毫秒。
    """
    stamp = ts or now_iso()
    terms = sorted(set(_terms(text_seg)))
    if not terms:
        return 0
    if total is None:
        total = int(conn.execute("SELECT COUNT(*) c FROM memory WHERE status<>'deleted'").fetchone()["c"] or 0)
    if total <= 1:
        _mark_covered(conn, total)
        return 0

    cap = effective_max_df(total, max_df)
    weights: dict[int, float] = defaultdict(float)
    for term in terms[:64]:  # 再长就是文档级输入，图不该按整篇连边
        expr = '"' + term.replace('"', '""') + '"'
        try:
            rows = conn.execute(
                "SELECT rowid AS r FROM memory_fts_tok WHERE memory_fts_tok MATCH ? LIMIT ?",
                (expr, cap + 1),
            ).fetchall()
        except Exception:  # noqa: BLE001 索引不可用时图退化成不可用，但不该挡住写入
            return 0
        if len(rows) > cap or len(rows) < 2:
            continue
        w = _pair_weight(len(rows))
        for row in rows:
            other = int(row["r"])
            if other != seq:
                weights[other] += w
    if not weights:
        _mark_covered(conn, total)
        return 0

    top = sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))[:per_node]
    kept = {other: w for other, w in top if w >= min_weight}
    if not kept:
        _mark_covered(conn, total)
        return 0
    conn.execute("DELETE FROM memory_edge WHERE (a_seq=? OR b_seq=?) AND rel=?", (seq, seq, RELATED))
    conn.executemany(
        _INSERT_SQL,
        [(*_canon(seq, other), RELATED, round(w, 6), stamp) for other, w in kept.items()],
    )
    _mark_covered(conn, total)
    return len(kept)


def _mark_covered(db_or_conn: Any, total: int) -> None:
    """记下「图覆盖到第几条记忆」：开库时用来判断要不要补建。"""
    conn = getattr(db_or_conn, "conn", db_or_conn)
    try:
        conn.execute(
            "INSERT INTO schema_meta(key, value, updated_at) VALUES('graph_covered', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (str(total), now_iso()),
        )
    except Exception:  # noqa: BLE001 元信息写不进去不影响图本身
        pass


def covered(db: Any) -> int:
    try:
        row = db.conn.execute("SELECT value FROM schema_meta WHERE key='graph_covered'").fetchone()
        return int(row["value"]) if row else 0
    except Exception:  # noqa: BLE001
        return 0


def ensure(db: Any, *, max_df: int = DEFAULT_MAX_DF, log: Any = None) -> dict[str, Any]:
    """开库时的自愈：老库没有图就整表重建，落了几条就按缺口增量补。

    只在 ``covered < 当前记忆数`` 时动手，正常情况下是一次 ``COUNT(*)``
    加一次元信息读，零开销。
    """
    try:
        total = int(db.conn.execute("SELECT COUNT(*) c FROM memory WHERE status<>'deleted'").fetchone()["c"] or 0)
    except Exception:  # noqa: BLE001 库还没建表
        return {"memories": 0, "rebuilt": False, "linked": 0}
    have = covered(db)
    if total == 0 or have >= total:
        return {"memories": total, "rebuilt": False, "linked": 0}
    edges = int(db.conn.execute("SELECT COUNT(*) c FROM memory_edge").fetchone()["c"] or 0)
    if edges == 0:
        info = rebuild(db, max_df=max_df)
        if log:
            log(f"graph rebuild: {info}")
        return {**info, "rebuilt": True, "linked": 0}
    # 有图但没覆盖全（老库/导入）：只补缺的那些行，不整表重算
    rows = db.conn.execute(
        "SELECT seq, text_seg FROM memory WHERE status<>'deleted' ORDER BY seq"
    ).fetchall()
    linked = 0
    for row in rows[-max(0, total - have) :]:
        link_new(
            db.conn,
            seq=int(row["seq"]),
            text_seg=row["text_seg"],
            max_df=max_df,
            total=total,
        )
        linked += 1
    if log:
        log(f"graph catch-up: {linked} 条")
    return {"memories": total, "rebuilt": False, "linked": linked}


def neighbourhood(
    conn: Any,
    seeds: Iterable[int],
    *,
    hops: int = 2,
    max_nodes: int = MAX_NODES,
) -> dict[int, list[tuple[int, float]]]:
    """按种子分层取邻接（每层一条 ``IN (...)`` 查询，不做递归 CTE）。"""
    adj: dict[int, list[tuple[int, float]]] = defaultdict(list)
    frontier = {int(s) for s in seeds}
    seen: set[int] = set(frontier)
    for _ in range(max(0, hops)):
        if not frontier:
            break
        marks = ",".join("?" for _ in frontier)
        params = (*frontier, *frontier, RELATED)
        rows = conn.execute(
            f"SELECT a_seq, b_seq, w FROM memory_edge "
            f"WHERE (a_seq IN ({marks}) OR b_seq IN ({marks})) AND rel=?",
            params,
        ).fetchall()
        nxt: set[int] = set()
        for row in rows:
            a, b, w = int(row["a_seq"]), int(row["b_seq"]), float(row["w"])
            adj[a].append((b, w))
            adj[b].append((a, w))
            for node in (a, b):
                if node not in seen:
                    seen.add(node)
                    nxt.add(node)
        if len(seen) > max_nodes:
            frontier = set(sorted(nxt)[: max(0, max_nodes - len(seen) + len(nxt))])
        else:
            frontier = nxt
    return dict(adj)


def spread(
    adj: dict[int, list[tuple[int, float]]],
    seeds: list[tuple[int, float]],
    *,
    alpha: float = 0.5,
    iterations: int = 3,
) -> dict[int, float]:
    """带重启的随机游走（PPR）：alpha 是「回到种子」的那份质量。

    ``alpha=0.5`` 三轮是**故意做浅**的：图小的时候长游走会让什么都可达，
    排序就不再有区分度。种子按词法分数分配起始质量，强的命中带更多质量。
    """
    if not adj or not seeds:
        return {}
    prefer = {int(seq): max(0.2, float(score or 1.0)) for seq, score in seeds}
    total = sum(prefer.values()) or 1.0
    prefer = {k: v / total for k, v in prefer.items()}
    mass = dict(prefer)
    for _ in range(max(0, iterations)):
        nxt: dict[int, float] = {seq: alpha * share for seq, share in prefer.items()}
        for node, cur in mass.items():
            links = adj.get(node)
            if not links:
                continue
            norm = sum(w for _, w in links) or 1.0
            for other, w in links:
                nxt[other] = nxt.get(other, 0.0) + (1.0 - alpha) * cur * (w / norm)
        mass = nxt
    peak = max(mass.values()) if mass else 0.0
    if peak <= 0:
        return {}
    return {seq: value / peak for seq, value in mass.items()}


def hop_distance(
    adj: dict[int, list[tuple[int, float]]],
    seeds: Iterable[int],
    target: int,
    *,
    max_hops: int = 3,
) -> int | None:
    """种子到目标的最短跳数（调试/可解释性用，验证「这条是怎么被捞上来的」）。"""
    seen = {int(s): 0 for s in seeds}
    frontier = list(seen)
    for hop in range(1, max_hops + 1):
        nxt: list[int] = []
        for node in frontier:
            for other, _ in adj.get(node, ()):
                if other not in seen:
                    seen[other] = hop
                    nxt.append(other)
        if target in seen:
            return seen[target]
        frontier = nxt
    return None


def stats(db: Any) -> dict[str, Any]:
    conn = db.conn
    edges = int(conn.execute("SELECT COUNT(*) c FROM memory_edge WHERE rel=?", (RELATED,)).fetchone()["c"] or 0)
    nodes = int(
        conn.execute(
            "SELECT COUNT(*) c FROM (SELECT a_seq FROM memory_edge UNION SELECT b_seq FROM memory_edge)"
        ).fetchone()["c"]
        or 0
    )
    total = int(conn.execute("SELECT COUNT(*) c FROM memory WHERE status<>'deleted'").fetchone()["c"] or 0)
    avg_w = float(conn.execute("SELECT AVG(w) v FROM memory_edge").fetchone()["v"] or 0.0)
    return {
        "edges": edges,
        "nodes": nodes,
        "memories": total,
        "covered": covered(db),
        "avg_weight": round(avg_w, 4),
        "isolated": max(0, total - nodes),
    }
