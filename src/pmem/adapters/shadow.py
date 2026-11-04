"""回填与影子模式（M1.5）。

影子模式的承诺是**零风险**：只读别人的数据，只写自己的库，从不注入上下文。
它回答三个问题：

1. AWB 能不能召回到东西？（命中率）
2. 和现役的 dsh-soul 比，召回的是不是同一批内容？（重合率）
3. 注入成本多少？（tokens/turn，目标 ≤600）
"""

from __future__ import annotations

import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..base.clock import to_iso
from ..base.ids import ulid
from ..base import events as event_log
from ..gate.sdk import PMem
from ..memory.text import char_ngrams, jaccard
from . import dsh
from .soul import SoulReader

OVERLAP_THRESHOLD = 0.5  # 3-gram Jaccard 到这个值才算"同一段内容"


# ---------------------------------------------------------------- 回填

def _split_text(text: str, limit: int = 4000) -> list[str]:
    """把超长消息按段落切成 ≤limit 的块；切不动就硬切。

    原文不可变，所以不能截断：宁可拆成多条，也不丢内容（`docs/04` 的 segment 约定）。
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf = ""
    for para in text.split("\n"):
        piece = para + "\n"
        if len(buf) + len(piece) <= limit:
            buf += piece
            continue
        if buf:
            chunks.append(buf.strip())
        while len(piece) > limit:  # 单段就超长：硬切
            chunks.append(piece[:limit])
            piece = piece[limit:]
        buf = piece
    if buf.strip():
        chunks.append(buf.strip())
    return [c for c in chunks if c]


def backfill(
    app: PMem,
    files: list[Path],
    *,
    scope_key: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    node: str | None = None,
    min_chars: int = 6,
    reset: bool = False,
) -> dict[str, Any]:
    """把会话里的用户消息写成 episodic 记忆（幂等，可反复跑）。

    ``reset=True`` 只清掉"回填产生的记忆"（``source_kind='dsh_session'``）与对应幂等键，
    不动用户自己写进来的记忆。改作用域规则后重跑时要用它。
    """
    stats = {"sessions": 0, "skipped_sessions": 0, "messages": 0, "added": 0, "replay": 0, "duplicates": 0}
    if reset and not dry_run:
        with app.db.tx() as conn:
            removed = conn.execute(
                "SELECT COUNT(*) c FROM memory WHERE source_kind='dsh_session'"
            ).fetchone()["c"]
            conn.execute("DELETE FROM memory WHERE source_kind='dsh_session'")
            conn.execute("DELETE FROM idem WHERE key LIKE 'dsh:%'")
            # event_log 是追加-only，不能删行；但 idem_key 有唯一约束，重跑会撞。
            # 置空是安全的：哈希链的摘要是 hash(prev‖ts‖actor‖op‖target‖payload)，不含 idem_key。
            conn.execute("UPDATE event_log SET idem_key=NULL WHERE idem_key LIKE 'dsh:%'")
            event_log.append(
                conn,
                actor="system:backfill",
                op="backfill.reset",
                payload={"deleted": removed},
            )
            stats["reset_deleted"] = removed
    added = 0
    for path in files:
        if limit is not None and stats["messages"] >= limit:
            break
        try:
            session_events = dsh.load_events(path, node=node)
        except Exception:
            stats["skipped_sessions"] += 1
            continue
        meta = dsh.session_meta(session_events)
        if meta.get("origin") == "subagent" or not meta.get("cwd"):
            stats["skipped_sessions"] += 1
            continue
        scope = scope_key or dsh.project_scope(meta["cwd"])
        messages = dsh.user_messages(session_events, min_chars=min_chars)
        stats["sessions"] += 1
        for message in messages:
            if limit is not None and stats["messages"] >= limit:
                break
            stats["messages"] += 1
            created = _ms_to_iso(message.get("time"))
            chunks = _split_text(message["text"])
            for index, chunk in enumerate(chunks):
                ref = f"dsh:{meta['id']}:{message['seq']}"
                if len(chunks) > 1:
                    ref = f"{ref}#{index}"
                result = app.store.add(
                    chunk,
                    kind="episodic",
                    provenance="user_explicit",
                    source_kind="dsh_session",
                    source_ref=ref,
                    session_id=meta["id"],
                    scope_key=scope,
                    created_at=created,
                    extra={
                        "dsh_seq": message["seq"],
                        "origin": "backfill",
                        "segment": index if len(chunks) > 1 else None,
                        "segments": len(chunks),
                    },
                    idem_key=f"dsh:{meta['id']}:{message['seq']}" + (f"#{index}" if len(chunks) > 1 else ""),
                    actor="system:backfill",
                    dry_run=dry_run,
                )
                stats["added"] += 0 if dry_run else 1
                if result.get("idempotent_replay"):
                    stats["replay"] += 1
                if result.get("duplicate_of"):
                    stats["duplicates"] += 1
        added += 1
    stats["files_scanned"] = len(files)
    return stats


def _ms_to_iso(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return to_iso(datetime.fromtimestamp(value / 1000, UTC))


# ---------------------------------------------------------------- 影子召回

def run_shadow(
    app: PMem,
    files: list[Path],
    *,
    scope: str | None,
    out_path: Path,
    soul: SoulReader | None = None,
    limit: int | None = None,
    k: int = 8,
    node: str | None = None,
    min_chars: int = 6,
) -> dict[str, Any]:
    """逐条用户消息跑一次召回，记录 AWB / dsh-soul 各自的结果（不注入）。

    ``scope=None`` 表示"每个会话用它自己的作用域"——这样一次就能覆盖所有项目，
    而不是只测当前目录。传具体值时只跑那一个项目。
    """
    soul = soul or SoulReader()
    run_id = ulid()
    records: list[dict[str, Any]] = []
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as fh:
        for path in files:
            if limit is not None and len(records) >= limit:
                break
            try:
                events = dsh.load_events(path, node=node)
            except Exception:
                continue
            meta = dsh.session_meta(events)
            if meta.get("origin") == "subagent" or not meta.get("cwd"):
                continue
            own_scope = dsh.project_scope(meta["cwd"])
            if scope is not None and own_scope != scope:
                continue
            session_scope = scope or own_scope
            for message in dsh.user_messages(events, min_chars=min_chars):
                if limit is not None and len(records) >= limit:
                    break
                record = _one_query(app, soul, session_scope, meta["id"], message, k)
                record["run_id"] = run_id
                record["scope"] = session_scope
                records.append(record)
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
    summary = summarize(records)
    summary["run_id"] = run_id
    summary["out_path"] = str(out_path)
    return summary


def _one_query(
    app: PMem,
    soul: SoulReader,
    scope: str,
    session_id: str,
    message: dict[str, Any],
    k: int,
) -> dict[str, Any]:
    query = message["text"]
    started = time.perf_counter()
    try:
        result = app.store.search(
            query,
            k=k,
            filters={"scope_key": scope, "status": ["active", "contested"]},
            client="shadow",
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        items = result["items"]
        tokens = result["budget_used"]
    except Exception as exc:  # 影子模式不能因为一条查询炸掉整轮
        return {
            "ts": to_iso(datetime.now(UTC)),
            "session": session_id,
            "seq": message.get("seq"),
            "query": query,
            "error": repr(exc),
        }

    soul_fts = soul.search(query, scope, k=3, mode="fts")
    soul_like = soul.search(query, scope, k=3, mode="like")

    return {
        "ts": to_iso(datetime.now(UTC)),
        "session": session_id,
        "seq": message.get("seq"),
        "query": query,
        "awb": {
            "n": len(items),
            "top": [
                {"id": i["id"], "score": i["score"], "head": i["text"][:80], "ref": _ref_for(app, i["id"])}
                for i in items
            ],
            "latency_ms": latency_ms,
            "tokens": tokens,
        },
        "soul_fts": {"n": len(soul_fts), "top": [{"seq": s["seq"], "head": s["text"][:80]} for s in soul_fts]},
        "soul_like": {"n": len(soul_like), "top": [{"seq": s["seq"], "head": s["text"][:80]} for s in soul_like]},
        "overlap_fts": _overlap(items, soul_fts),
        "overlap_like": _overlap(items, soul_like),
    }


def _ref_for(app: PMem, memory_id: str) -> str | None:
    row = app.db.conn.execute("SELECT source_ref FROM memory WHERE id=?", (memory_id,)).fetchone()
    return row["source_ref"] if row else None


def _overlap(items: list[dict[str, Any]], soul_hits: list[dict[str, Any]]) -> float:
    """AWB 返回的条目里，有多少能在 dsh-soul 的命中里找到同一段内容。

    用**包含度**而不是 Jaccard：AWB 存的是完整用户消息，dsh-soul 存的是会话切片，
    长度天然不同（"杭州"那条 vs 整段 span）。Jaccard 会因为长度差把真命中判成不重合，
    包含度（AWB 文本的 3-gram 有多少出现在 soul 文本里）才对得上这个场景。
    """
    if not items or not soul_hits:
        return 0.0
    soul_grams = [char_ngrams(h["text"]) for h in soul_hits]
    hit = 0
    for item in items:
        grams = char_ngrams(item["text"])
        if not grams:
            continue
        best = max(
            (len(grams & sg) / len(grams) for sg in soul_grams),
            default=0.0,
        )
        if best >= OVERLAP_THRESHOLD:
            hit += 1
    return round(hit / len(items), 4)


# ---------------------------------------------------------------- 汇总 / 报告

def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in records if "error" not in r]
    latencies = [r["awb"]["latency_ms"] for r in ok]
    tokens = [r["awb"]["tokens"] for r in ok]
    awb_hit = [r for r in ok if r["awb"]["n"] > 0]
    soul_hit = [r for r in ok if r["soul_fts"]["n"] > 0]
    like_hit = [r for r in ok if r["soul_like"]["n"] > 0]
    both = [r for r in ok if r["awb"]["n"] > 0 and r["soul_fts"]["n"] > 0]

    def pct(values: list[int], p: float) -> int:
        if not values:
            return 0
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1))))]

    return {
        "queries": len(records),
        "errors": len(records) - len(ok),
        "awb_hit_rate": round(len(awb_hit) / len(ok), 3) if ok else 0.0,
        "soul_fts_hit_rate": round(len(soul_hit) / len(ok), 3) if ok else 0.0,
        "soul_like_hit_rate": round(len(like_hit) / len(ok), 3) if ok else 0.0,
        "overlap_fts_mean": round(statistics.fmean(r["overlap_fts"] for r in both), 3) if both else 0.0,
        "overlap_like_mean": round(statistics.fmean(r["overlap_like"] for r in ok), 3) if ok else 0.0,
        "tokens_mean": round(statistics.fmean(tokens), 1) if tokens else 0.0,
        "tokens_p95": pct(tokens, 95),
        "latency_p50_ms": pct(latencies, 50),
        "latency_p95_ms": pct(latencies, 95),
        "awb_empty_queries": len(ok) - len(awb_hit),
    }


def write_report(summary: dict[str, Any], records: list[dict[str, Any]], path: Path) -> None:
    run_ids = {r.get("run_id") for r in records if r.get("run_id")}
    run_label = "、".join(sorted(run_ids)) if run_ids else "—"
    lines = [
        "# 影子模式报告（M1.5）",
        "",
        f"> 生成：{to_iso(datetime.now(UTC))} ｜ run_id：`{run_label}` ｜ "
        f"查询数：{summary['queries']}",
        "",
        "## 总览",
        "",
        "| 指标 | 值 | 说明 |",
        "|---|---|---|",
        f"| AWB 命中率 | {summary['awb_hit_rate']:.0%} | 至少召回 1 条的查询占比 |",
        f"| dsh-soul（原生 FTS）命中率 | {summary['soul_fts_hit_rate']:.0%} | 中文无分词，基本为 0 |",
        f"| dsh-soul（等价词法 LIKE）命中率 | {summary['soul_like_hit_rate']:.0%} | 把词法换成本机可用的形式 |",
        f"| 重合率（对比原生 FTS） | {summary['overlap_fts_mean']:.0%} | 仅统计双方都有命中的查询 |",
        f"| 重合率（对比等价词法） | {summary['overlap_like_mean']:.0%} | 全体查询平均 |",
        f"| 注入 tokens 均值 | {summary['tokens_mean']} | 目标 ≤600 |",
        f"| 注入 tokens p95 | {summary['tokens_p95']} | |",
        f"| 召回延迟 p50 / p95 | {summary['latency_p50_ms']} / {summary['latency_p95_ms']} ms | |",
        "",
        "## 逐条明细",
        "",
        "| # | 查询（截断） | AWB | soul(FTS) | soul(LIKE) | 重合 |",
        "|---|---|---|---|---|---|",
    ]
    for idx, record in enumerate(records, start=1):
        if "error" in record:
            lines.append(f"| {idx} | {record['query'][:24]} | 错误 | — | — | — |")
            continue
        query = record["query"].replace("|", "\\|").replace("\n", " ")[:24]
        lines.append(
            f"| {idx} | {query} | {record['awb']['n']} | {record['soul_fts']['n']} | "
            f"{record['soul_like']['n']} | {record['overlap_like']:.0%} |"
        )
    lines += [
        "",
        "## 读法（别误读）",
        "",
        "- **命中率 100% 不代表质量好**：M1.5 的查询就是被回填的那条消息本身，",
        "  属于「自召回」；它证明管道通了、成本可控，不证明语义召回好。真正的质量要等 M2 的标注集。",
        "- **重合率低不一定是 AWB 差**：AWB 存的是「用户说过的话」，dsh-soul 存的是会话切片，",
        "  两者粒度不同；重合率衡量的是「是否找到同一段内容」，不是召回质量。",
        "- **soul 的中文命中率偏低是能力问题**：它的 `span_fts` 用 unicode61，中文整段成一个 token，",
        "  `MATCH '记忆'` 命中 0 条，而 `LIKE '%记忆%'` 有 97 条。所以另给了一列等价词法基线。",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def load_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out
