"""失败聚类审核队列（P3-18）：把「可复核的失败样例」聚成 JSON，交给人看。

为什么要有这一层
----------------
G0/G1/G2 的教训是「改参数前先看失败形态」：同一个数字背后可能是完全不同的问题
（weak 召回 / 空召回 / 浮现了没人用 / 冲突没裁决）。这些证据已经躺在三张表里，
但没人会去翻 SQL。这里把它们**只读**汇总、按形态聚类、落成一份待人工审核的 JSON；
**不自动改任何参数**——改参数要人看（P3 的原则）。

数据源（全部只读）：
- ``recall_log``：本次召回返回了什么（空召回 = ``returned_json`` 为空）；
- ``event_log``：``recall.weak``（低覆盖/分差小/空/断档的确定性原因码）、
  ``selfcheck*`` / ``conflict*`` 事件；
- ``surface_log``：浮现过的候选里，之后**没有被真正使用**的那批（``last_used_at``
  为空或不晚于浮现时刻）——它们是「浮现预算的浪费」候选。

输出形状：
    {
      "generated_at": "…",
      "total": 12,
      "clusters": [
        {"key": "recall.weak:coverage", "count": 4,
         "samples": [{"id": "…", "kind": "recall", "text_excerpt": "…",
                      "evidence": {"ts": "…", "coverage": 0.12, …}}]}
      ]
    }

纯只读：本模块不写库、不改参数；CLI 只在 ``--out`` 落 JSON。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ..base import clock
from ..base.config import Config, default_config_path
from ..base.db import Database

SAMPLE_CAP = 5  # 每个聚类最多留几条样例（再多也没人看）
EXCERPT_CHARS = 80
SUBJECT_DEFAULT = "self"


def _rows(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
    """查一组行；表缺/库是旧版本都当空（审核队列不能因为缺表就跑不起来）。"""
    try:
        return conn.execute(sql, params).fetchall()
    except Exception:  # noqa: BLE001
        return []


def _json(raw: Any, default: Any = None) -> Any:
    try:
        return json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _excerpt(text: Any) -> str:
    return " ".join(str(text or "").split())[:EXCERPT_CHARS]


class _Cluster:
    __slots__ = ("count", "samples")

    def __init__(self) -> None:
        self.count = 0
        self.samples: list[dict[str, Any]] = []

    def add(self, sample: dict[str, Any]) -> None:
        self.count += 1
        if len(self.samples) < SAMPLE_CAP:
            self.samples.append(sample)


def _weak_payloads(conn: Any, limit: int) -> dict[str, dict[str, Any]]:
    """recall.weak 事件：recall_id → payload（原因码/覆盖率/分差/返回条数）。"""
    payloads: dict[str, dict[str, Any]] = {}
    for row in _rows(
        conn,
        "SELECT target_id, payload_json FROM event_log "
        "WHERE op='recall.weak' ORDER BY ts DESC LIMIT ?",
        (int(limit),),
    ):
        audit = _json(row["payload_json"], {}) or {}
        payloads[str(row["target_id"])] = audit if isinstance(audit, dict) else {}
    return payloads


def _recall_clusters(conn: Any, *, limit: int, subject: str) -> dict[str, _Cluster]:
    clusters: dict[str, _Cluster] = {}
    weak = _weak_payloads(conn, limit)
    for row in _rows(
        conn,
        "SELECT id, ts, query, mode, returned_json, injected_tokens FROM recall_log "
        "WHERE subject_id=? ORDER BY ts DESC LIMIT ?",
        (subject, int(limit)),
    ):
        recall_id = str(row["id"])
        audit = weak.get(recall_id)
        returned = _json(row["returned_json"], []) or []
        if audit:
            reasons = sorted(str(item) for item in (audit.get("reasons") or []))
            if reasons == ["empty"]:
                key = "recall.empty"
            else:
                key = "recall.weak:" + ("+".join(reasons) or "unspecified")
        elif not returned:
            key = "recall.empty"
            audit = {}
        else:
            continue
        sample = {
            "id": recall_id,
            "kind": "recall",
            "text_excerpt": _excerpt(row["query"]),
            "evidence": {
                "ts": row["ts"],
                "mode": row["mode"],
                "returned": len(returned),
                "injected_tokens": int(row["injected_tokens"] or 0),
                "reasons": list(audit.get("reasons") or []),
                "coverage": audit.get("coverage"),
                "margin": audit.get("margin"),
            },
        }
        clusters.setdefault(key, _Cluster()).add(sample)
    return clusters


def _surface_clusters(conn: Any, *, limit: int, subject: str) -> dict[str, _Cluster]:
    clusters: dict[str, _Cluster] = {}
    for row in _rows(
        conn,
        "SELECT s.id, s.ts, s.session_id, s.mode, s.grade, s.tokens, s.cue, "
        "s.target_tbl, s.target_id, m.text_raw AS mem_text, m.last_used_at, "
        "e.name AS entity_name "
        "FROM surface_log s "
        "LEFT JOIN memory m ON m.id=s.target_id AND m.subject_id=? "
        "LEFT JOIN entity e ON e.id=s.target_id AND e.subject_id=? "
        "ORDER BY s.ts DESC LIMIT ?",
        (subject, subject, int(limit)),
    ):
        target_tbl = str(row["target_tbl"] or "")
        if target_tbl == "memory":
            if not row["mem_text"]:
                continue  # 记忆已删/别的 subject：不当作失败样例
            excerpt = _excerpt(row["mem_text"])
            last_used = row["last_used_at"]
        else:
            if not row["entity_name"]:
                continue
            excerpt = _excerpt(row["entity_name"])
            last_used = None
        # 「被弃用的候选」：浮现之后没有被真正使用（last_used_at 空或不晚于浮现时刻）。
        if last_used and str(last_used) > str(row["ts"]):
            continue
        key = "surface.discarded:" + (str(row["mode"] or "unknown"))
        clusters.setdefault(key, _Cluster()).add(
            {
                "id": str(row["id"]),
                "kind": "surface",
                "text_excerpt": excerpt,
                "evidence": {
                    "ts": row["ts"],
                    "session_id": row["session_id"],
                    "mode": row["mode"],
                    "grade": row["grade"],
                    "tokens": int(row["tokens"] or 0),
                    "cue": row["cue"],
                    "target": f"{target_tbl}:{row['target_id']}",
                    "last_used_at": last_used,
                },
            }
        )
    return clusters


def _event_clusters(conn: Any, *, limit: int, subject: str) -> dict[str, _Cluster]:
    clusters: dict[str, _Cluster] = {}
    for row in _rows(
        conn,
        "SELECT id, ts, actor, op, target_tbl, target_id, payload_json FROM event_log "
        "WHERE (op LIKE 'selfcheck%' OR op LIKE 'conflict%') AND subject_id=? "
        "ORDER BY ts DESC LIMIT ?",
        (subject, int(limit)),
    ):
        payload = _json(row["payload_json"], {}) or {}
        excerpt = ""
        if isinstance(payload, dict):
            for key in ("query", "note", "reason", "text", "summary"):
                if payload.get(key):
                    excerpt = _excerpt(payload[key])
                    break
        clusters.setdefault("event:" + str(row["op"] or "unknown"), _Cluster()).add(
            {
                "id": str(row["id"]),
                "kind": "event",
                "text_excerpt": excerpt,
                "evidence": {
                    "ts": row["ts"],
                    "actor": row["actor"],
                    "op": row["op"],
                    "target": f"{row['target_tbl'] or ''}:{row['target_id'] or ''}",
                    "payload": payload if isinstance(payload, dict) else {},
                },
            }
        )
    return clusters


def build(
    db: Any,
    *,
    limit: int = 200,
    subject: str = SUBJECT_DEFAULT,
    now: str | None = None,
) -> dict[str, Any]:
    """把一个 PMem 库里的失败样例聚成审核队列（只读，不写库、不改参数）。

    ``limit``：每个数据源最多往前扫多少行（默认 200）；每个聚类最多留 5 条样例。
    返回 ``{generated_at, total, clusters: [{key, count, samples}]}``，
    ``total`` 是扫到的样例总数（不只是留下的样例数）。
    """
    cap = max(1, int(limit or 1))
    conn = db.conn
    clusters: dict[str, _Cluster] = {}
    for source in (
        _recall_clusters(conn, limit=cap, subject=subject),
        _surface_clusters(conn, limit=cap, subject=subject),
        _event_clusters(conn, limit=cap, subject=subject),
    ):
        for key, cluster in source.items():
            bucket = clusters.setdefault(key, _Cluster())
            bucket.count += cluster.count
            for sample in cluster.samples:
                if len(bucket.samples) < SAMPLE_CAP:
                    bucket.samples.append(sample)
    ordered = sorted(clusters.items(), key=lambda item: (-item[1].count, item[0]))
    return {
        "generated_at": str(now or clock.now_iso()),
        "total": sum(cluster.count for _, cluster in ordered),
        "clusters": [
            {"key": key, "count": cluster.count, "samples": cluster.samples}
            for key, cluster in ordered
        ],
    }


def write(path: str | Path, data: dict[str, Any]) -> Path:
    """把审核队列落成 UTF-8 JSON（唯一写盘动作；不碰库）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return target


def enabled(config: Any) -> bool:
    return bool(config is not None and config.get("ops.review_queue.enabled", False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pmem.ops.review_queue",
        description="只读汇总失败样例，聚类成待人工审核的 JSON（不自动改参数）",
    )
    parser.add_argument("--db", required=True, help="PMem 库路径（只读打开）")
    parser.add_argument("--out", required=True, help="输出 JSON 路径")
    parser.add_argument("--config", default=None, help="TOML 配置路径（默认 $DSH_HOME/pmem/config.toml）")
    parser.add_argument("--limit", type=int, default=None, help="每个数据源最多扫多少行（默认取配置 200）")
    parser.add_argument("--subject", default=SUBJECT_DEFAULT)
    args = parser.parse_args(argv)

    try:
        config = Config.load(args.config or default_config_path())
    except Exception as exc:  # noqa: BLE001 配置文件坏了要给人话，不是 traceback
        print(f"读配置失败（{args.config or default_config_path()}）：{exc}", file=sys.stderr)
        return 2
    if not enabled(config):
        print(
            "ops.review_queue.enabled=false：审核队列默认关。"
            "先在配置里打开（ops.review_queue.enabled = true）再跑，别把它当默认任务。",
            file=sys.stderr,
        )
        return 2
    db_path = Path(str(args.db))
    if not db_path.exists():
        print(f"库不存在：{db_path}", file=sys.stderr)
        return 2
    limit = int(args.limit if args.limit is not None else (config.get("ops.review_queue.limit", 200) or 200))
    limit = max(1, limit)
    db = Database(str(db_path), read_only=True)
    try:
        data = build(db, limit=limit, subject=str(args.subject or SUBJECT_DEFAULT))
    finally:
        db.close()
    target = write(args.out, data)
    print(
        f"审核队列已写入 {target}：{data['total']} 条样例，"
        f"{len(data['clusters'])} 个聚类（只读汇总，未改任何参数）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
