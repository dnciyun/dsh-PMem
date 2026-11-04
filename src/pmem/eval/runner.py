"""M2 评测执行器：跑合成集，算指标，判 Gate，出报告。

用法：``python -m pmem.gate.cli eval --seed 7``
"""

from __future__ import annotations

import json
import math
import statistics
import time
from pathlib import Path
from typing import Any

from ..base.config import Config
from ..base.db import Database
from ..gate.sdk import PMem
from ..memory import conflict as conflict_mod
from ..memory import dup as dup_mod
from ..memory import feedback as feedback_mod
from ..ops import invariants
from . import synthetic

GATES = {
    "dedup_precision": 0.90,
    "conflict_f1": 0.75,
    "recall_at_k": 0.80,
    "poison_escapes": 0,
}


def _pairs_of_groups(groups: list[dict[str, Any]]) -> set[frozenset[str]]:
    out: set[frozenset[str]] = set()
    for group in groups:
        members = group["members"]
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                out.add(frozenset((members[i], members[j])))
    return out


def _prf(pred: set[Any], gold: set[Any]) -> dict[str, float]:
    if not pred and not gold:
        return {"precision": 1.0, "recall": 1.0, "f1": 1.0, "predicted": 0, "gold": 0}
    tp = len(pred & gold)
    precision = tp / len(pred) if pred else 0.0
    recall = tp / len(gold) if gold else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
        "predicted": len(pred),
        "gold": len(gold),
        "tp": tp,
    }


def _poison_check(workdir: Path) -> dict[str, Any]:
    """错误强化测试：同一句话在同会话复读，不能刷高可信度。"""
    db_path = workdir / "poison.db"
    app = PMem.open(db_path)
    escapes: list[str] = []
    try:
        store = app.store
        first = store.add("我住在火星", session_id="s1")
        base_conf = store.get(first["id"])["confidence"]
        base_evidence = store.get(first["id"])["evidence_count"]
        for _ in range(5):
            store.add("我住在火星", session_id="s1")
        item = store.get(first["id"])
        if item["evidence_count"] != base_evidence:
            escapes.append(f"same-session evidence_count {base_evidence}->{item['evidence_count']}")
        if item["confidence"] > base_conf + 1e-9:
            escapes.append(f"same-session confidence {base_conf}->{item['confidence']}")

        weak = store.add("他可能喜欢猫", provenance="inferred", session_id="s2")
        for i in range(6):
            store.add("他可能喜欢猫", provenance="inferred", session_id=f"s2-{i}")
        weak_item = store.get(weak["id"])
        if weak_item["confidence"] > 0.6 + 1e-9:
            escapes.append(f"inferred cap breached: {weak_item['confidence']}")
        if store.stats()["memory_total"] > 3:
            escapes.append(f"duplicate rows created: {store.stats()['memory_total']}")
    finally:
        app.close()
    return {"escapes": escapes, "count": len(escapes)}


def run(
    *,
    seed: int = 7,
    out_dir: Path | None = None,
    k: int = 5,
    max_groups: int | None = None,
    config_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out_dir = out_dir or Path(".tmp/eval")
    out_dir.mkdir(parents=True, exist_ok=True)
    data = synthetic.build(seed, max_groups=max_groups)
    db_path = out_dir / f"eval-{seed}.db"
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()

    app = PMem.open(
        db_path,
        config=Config(config_overrides) if config_overrides else None,
        apply_tuned=config_overrides is None,
    )
    key_to_id: dict[str, str] = {}
    id_to_key: dict[str, str] = {}
    try:
        for memory in data["memories"]:
            result = app.store.add(
                memory["text"],
                kind=memory["kind"],
                provenance=memory["provenance"],
                session_id=memory["session_id"],
                valid_from=memory["valid_from"],
                valid_to=memory["valid_to"],
                scope_key="project:manual",
            )
            key_to_id[memory["key"]] = result["id"]
            id_to_key[result["id"]] = memory["key"]

        # --- 去重 ---
        groups = dup_mod.find_duplicate_groups(app.db)
        pred_pairs = _pairs_of_groups(groups)
        hard_pairs = {frozenset((key_to_id[a], key_to_id[b])) for a, b in data["dup_pairs_hard"]}
        gold_pairs = {frozenset((key_to_id[a], key_to_id[b])) for a, b in data["dup_pairs"]} | hard_pairs
        dedup_metrics = _prf(pred_pairs, gold_pairs)
        dedup_metrics["groups"] = len(groups)
        dedup_metrics["hard_total"] = len(hard_pairs)
        dedup_metrics["hard_recall"] = round(len(pred_pairs & hard_pairs) / len(hard_pairs), 3) if hard_pairs else 0.0

        # --- 冲突 ---
        found = conflict_mod.detect(app.db)
        # 冲突对是**无序**的：检测端按 (小 id, 大 id) 归一化，比较时也要归一化
        pred_conf = {(tuple(sorted((c["left_id"], c["right_id"]))), c["kind"]) for c in found}
        gold_conf = {
            (tuple(sorted((key_to_id[a], key_to_id[b]))), kind) for a, b, kind in data["conflict_pairs"]
        }
        conflict_metrics = _prf(pred_conf, gold_conf)
        temporal_false_positive = sum(
            1
            for a, b in data["temporal_pairs"]
            for item in found
            if {item["left_id"], item["right_id"]} == {key_to_id[a], key_to_id[b]}
        )
        conflict_metrics["temporal_false_positive"] = temporal_false_positive

        # --- 召回（合并之前测，避免 archived 影响） ---
        lexical: list[float] = []
        semantic: list[float] = []
        lexical_mrr: list[float] = []
        ndcg: list[float] = []
        latencies: list[float] = []
        for query in data["queries"]:
            started = time.perf_counter()
            result = app.store.search(query["query"], k=k)
            latencies.append((time.perf_counter() - started) * 1000)
            hits = [id_to_key.get(item["id"]) for item in result["items"]]
            gold = {key_to_id[key] for key in query["gold"]}
            hit_gold = {key_to_id[key] for key in hits if key in key_to_id}
            recall = len(hit_gold & gold) / len(gold) if gold else 0.0
            rels = [
                1.0 if (key and key_to_id.get(key) in gold) else 0.0 for key in hits[:k]
            ]
            dcg = sum(rel / math.log2(index + 2) for index, rel in enumerate(rels))
            ideal = sum(
                1.0 / math.log2(index + 2) for index in range(min(len(gold), k))
            )
            ndcg.append(dcg / ideal if ideal else 0.0)
            if query["kind"] == "lexical":
                lexical.append(recall)
                rank = next((i for i, key in enumerate(hits, start=1) if key and key_to_id.get(key) in gold), 0)
                lexical_mrr.append(1.0 / rank if rank else 0.0)
            else:
                semantic.append(recall)

        recall_metrics = {
            "lexical_n": len(lexical),
            "lexical_recall_at_k": round(statistics.fmean(lexical), 3) if lexical else 0.0,
            "lexical_mrr": round(statistics.fmean(lexical_mrr), 3) if lexical_mrr else 0.0,
            "paraphrase_n": len(semantic),
            "paraphrase_recall_at_k": round(statistics.fmean(semantic), 3) if semantic else 0.0,
            "ndcg_at_k": round(statistics.fmean(ndcg), 4) if ndcg else 0.0,
            "latency_p50_ms": round(statistics.median(latencies), 1) if latencies else 0.0,
        }

        # --- 合并效果 ---
        merge_stats = dup_mod.consolidate(app.db)
        archived = app.db.conn.execute(
            "SELECT COUNT(*) c FROM memory WHERE canonical_id IS NOT NULL"
        ).fetchone()["c"]
        active = app.db.conn.execute(
            "SELECT COUNT(*) c FROM memory WHERE status IN ('active','contested')"
        ).fetchone()["c"]

        # --- 反馈闭环 ---
        sample_id = next(iter(key_to_id.values()))
        before = app.store.get(sample_id)["confidence"]
        feedback_mod.apply(app.db, sample_id, "wrong", note="eval")
        after = app.store.get(sample_id)["confidence"]
        feedback_ok = after < before

        # --- 不变量 ---
        inv = invariants.summary(invariants.check_all(app.db))

        # --- 中毒 ---
        poison = _poison_check(out_dir)
    finally:
        app.close()

    metrics = {
        "seed": seed,
        "memories": len(data["memories"]),
        "dedup": dedup_metrics,
        "conflict": conflict_metrics,
        "recall": recall_metrics,
        "merge": {"merged": merge_stats["merged"], "archived": archived, "active_after": active},
        "feedback": {"wrong_demotes": feedback_ok, "before": round(before, 3), "after": round(after, 3)},
        "invariants": {"passed": inv["passed"], "total": inv["total"], "failed": inv["failed"]},
        "poison": poison,
    }
    metrics["gates"] = _judge(metrics)
    metrics["ok"] = all(g["pass"] for g in metrics["gates"])
    _write_report(metrics, data, out_dir, k)
    (out_dir / f"eval-{seed}.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return metrics


def _judge(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    gates = [
        {
            "name": "去重 precision",
            "target": f"≥ {GATES['dedup_precision']}",
            "actual": metrics["dedup"]["precision"],
            "pass": metrics["dedup"]["precision"] >= GATES["dedup_precision"],
        },
        {
            "name": "冲突 F1",
            "target": f"≥ {GATES['conflict_f1']}",
            "actual": metrics["conflict"]["f1"],
            "pass": metrics["conflict"]["f1"] >= GATES["conflict_f1"],
        },
        {
            "name": f"recall@{5}",
            "target": f"≥ {GATES['recall_at_k']}",
            "actual": metrics["recall"]["lexical_recall_at_k"],
            "pass": metrics["recall"]["lexical_recall_at_k"] >= GATES["recall_at_k"],
        },
        {
            "name": "错误强化逃逸",
            "target": "= 0",
            "actual": metrics["poison"]["count"],
            "pass": metrics["poison"]["count"] == GATES["poison_escapes"],
        },
        {
            "name": "不变量",
            "target": "全过",
            "actual": f"{metrics['invariants']['passed']}/{metrics['invariants']['total']}",
            "pass": not metrics["invariants"]["failed"],
        },
    ]
    return gates


def _write_report(metrics: dict[str, Any], data: dict[str, Any], out_dir: Path, k: int) -> None:
    lines = [
        "# M2 评测报告（自动生成）",
        "",
        f"> seed={metrics['seed']} ｜ 记忆 {metrics['memories']} 条 ｜ recall@{k}",
        "",
        "## Gate",
        "",
        "| 项 | 目标 | 实测 | 结论 |",
        "|---|---|---|---|",
    ]
    for gate in metrics["gates"]:
        lines.append(f"| {gate['name']} | {gate['target']} | {gate['actual']} | {'PASS' if gate['pass'] else 'FAIL'} |")
    lines += [
        "",
        "## 明细",
        "",
        f"- 去重：预测 {metrics['dedup']['predicted']} 对 / 真值 {metrics['dedup']['gold']} 对，"
        f"P={metrics['dedup']['precision']} R={metrics['dedup']['recall']} F1={metrics['dedup']['f1']}"
        f"（困难重复 {metrics['dedup']['hard_total']} 对，命中 {metrics['dedup']['hard_recall']}）",
        f"- 冲突：预测 {metrics['conflict']['predicted']} 对 / 真值 {metrics['conflict']['gold']} 对，"
        f"P={metrics['conflict']['precision']} R={metrics['conflict']['recall']} F1={metrics['conflict']['f1']}；"
        f"时间替代误报 {metrics['conflict']['temporal_false_positive']}",
        f"- 召回：词法 {metrics['recall']['lexical_n']} 条 → recall@{k}="
        f"{metrics['recall']['lexical_recall_at_k']}，MRR={metrics['recall']['lexical_mrr']}；"
        f"仅语义 {metrics['recall']['paraphrase_n']} 条 → {metrics['recall']['paraphrase_recall_at_k']}",
        f"- 合并：吸收 {metrics['merge']['merged']} 条，归档 {metrics['merge']['archived']} 条，"
        f"活跃剩 {metrics['merge']['active_after']} 条",
        f"- 反馈：wrong 使可信度 {metrics['feedback']['before']} → {metrics['feedback']['after']}"
        f"（{'生效' if metrics['feedback']['wrong_demotes'] else '未生效'}）",
        f"- 不变量：{metrics['invariants']['passed']}/{metrics['invariants']['total']} 通过",
        f"- 错误强化逃逸：{metrics['poison']['count']}",
        "",
    ]
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------- M3 Net
NET_GATES = {
    "relation_precision": 0.85,
    "disambig_accuracy": 0.85,
    "two_hop_p95_ms": 150.0,
}


def _prf_net(predicted: set[Any], gold: set[Any]) -> dict[str, Any]:
    tp = len(predicted & gold)
    precision = tp / len(predicted) if predicted else 0.0
    recall = tp / len(gold) if gold else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
        "predicted": len(predicted),
        "gold": len(gold),
        "tp": tp,
    }


def _predicted_triples(db: Any) -> set[tuple[str, str, str]]:
    rows = db.conn.execute(
        "SELECT s.name_norm src, r.predicate p, d.name_norm dst "
        "FROM relation r JOIN entity s ON s.id=r.src_id JOIN entity d ON d.id=r.dst_id "
        "WHERE r.derivation='extracted:T0' AND r.status IN ('active','contested')"
    ).fetchall()
    return {(row["src"], row["p"], row["dst"]) for row in rows}


def _disambig_accuracy(db: Any, spec: list[dict[str, Any]]) -> dict[str, Any]:
    from ..net import entities

    total = 0
    correct = 0
    splits_ok = 0
    for item in spec:
        base = entities.norm_name(item["name"])
        edges = db.conn.execute(
            "SELECT s.id, s.name_norm, s.extra_json, d.name_norm dst "
            "FROM relation r JOIN entity s ON s.id=r.src_id JOIN entity d ON d.id=r.dst_id "
            "WHERE r.predicate='works_at' AND (s.name_norm=? OR s.name_norm LIKE ?)",
            (base, base + "#%"),
        ).fetchall()
        by_org: dict[str, set[str]] = {}
        ctx: dict[str, set[str]] = {}
        for row in edges:
            by_org.setdefault(row["dst"], set()).add(row["id"])
            try:
                stored = json.loads(row["extra_json"]).get("disambig") or []
            except (json.JSONDecodeError, TypeError):
                stored = []
            ctx[row["id"]] = set(stored)
        expected = [entities.norm_name(org) for org in item["orgs"]]
        distinct = {next(iter(by_org.get(org, set()))) for org in expected if len(by_org.get(org, set())) == 1}
        splits_ok += 1 if len(distinct) == len(expected) else 0
        for org in expected:
            total += 1
            srcs = by_org.get(org, set())
            if len(srcs) != 1:
                continue
            src = next(iter(srcs))
            if f"works_at:{org}" in ctx.get(src, set()):
                correct += 1
    return {
        "accuracy": round(correct / total, 3) if total else 0.0,
        "correct": correct,
        "total": total,
        "names": len(spec),
        "full_split_names": splits_ok,
    }


def _alias_accuracy(db: Any, spec: list[dict[str, Any]]) -> dict[str, Any]:
    from ..net import entities

    correct = 0
    total = 0
    for item in spec:
        total += 1
        name = entities.norm_name(item["name"])
        nick = entities.norm_name(item["nick"])
        org = entities.norm_name(item["org"])
        name_rows = [r for r in entities.find(db, subject="self", name=name) if r["status"] == "active"]
        nick_rows = [r for r in entities.find(db, subject="self", name=nick) if r["status"] == "active"]
        if len(name_rows) != 1 or len(nick_rows) != 1:
            continue
        if name_rows[0]["id"] != nick_rows[0]["id"]:
            continue
        edge = db.conn.execute(
            "SELECT 1 FROM relation WHERE src_id=? AND predicate='works_at' AND dst_id IN "
            "(SELECT id FROM entity WHERE name_norm=?)",
            (name_rows[0]["id"], org),
        ).fetchone()
        if edge:
            correct += 1
    return {"accuracy": round(correct / total, 3) if total else 0.0, "correct": correct, "total": total}


def _bench_two_hop(out_dir: Path, seed: int, edge_count: int) -> dict[str, Any]:
    """100k 边、2 跳查询基准。单独建库，避免污染关系评测的内存库。"""
    import random

    from ..net import graph

    db_path = out_dir / f"net-graph-{seed}.db"
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()
    app = PMem.open(db_path)
    db = app.db
    rng = random.Random(seed)
    n_entities = max(2000, edge_count // 5)
    ids = [f"E{i:06d}" for i in range(n_entities)]
    try:
        with db.tx() as conn:
            conn.executemany(
                "INSERT INTO entity(id, subject_id, type, name, name_norm, status, confidence, "
                "provenance, first_seen, last_seen, mention_count) "
                "VALUES(?, 'self', 'concept', ?, ?, 'active', 0.6, 'inferred', ?, ?, 1)",
                [
                    (ids[i], f"节点{i}", f"节点{i}", "2026-01-01T00:00:00.000Z", "2026-01-01T00:00:00.000Z")
                    for i in range(n_entities)
                ],
            )
            created = 0
            attempts = 0
            while created < edge_count and attempts < edge_count * 20:
                batch: list[tuple[Any, ...]] = []
                for _ in range(4000):
                    a = min(n_entities - 1, int(rng.paretovariate(1.3)) - 1)
                    b = rng.randrange(n_entities)
                    if a == b:
                        continue
                    weight = round(rng.uniform(0.1, 0.4), 3)
                    batch.append(
                        (
                            f"R{attempts:08d}{len(batch):04d}",
                            ids[a],
                            ids[b],
                            weight,
                            "[]",
                            "2026-01-01T00:00:00.000Z",
                        )
                    )
                attempts += len(batch)
                conn.executemany(
                    "INSERT OR IGNORE INTO relation(id, src_id, dst_id, predicate, weight, confidence, "
                    "provenance, evidence_json, created_at, updated_at, last_support_at, status, derivation) "
                    "VALUES(?,?,?, 'related_to', ?, 0.5, 'inferred', ?, ?, ?, ?, 'active', 'bench')",
                    [
                        (rid, a, b, weight, ev, ts, ts, ts)
                        for rid, a, b, weight, ev, ts in batch
                    ],
                )
                created = conn.execute("SELECT COUNT(*) c FROM relation WHERE derivation='bench'").fetchone()["c"]
        # 查询节点：50 个随机 + 5 个最高出度（测最坏情况）
        nodes = rng.sample(ids, 50)
        hubs = [
            row["src_id"]
            for row in db.conn.execute(
                "SELECT src_id, COUNT(*) c FROM relation GROUP BY src_id ORDER BY c DESC LIMIT 5"
            ).fetchall()
        ]
        times: list[float] = []
        scanned: list[int] = []
        for entity_id in nodes + hubs:
            started = time.perf_counter()
            result = graph.query(db, subject="self", src=entity_id, depth=2, limit=20)
            times.append((time.perf_counter() - started) * 1000)
            scanned.append(result["scanned"])
        edges = db.conn.execute("SELECT COUNT(*) c FROM relation").fetchone()["c"]
    finally:
        app.close()
    times.sort()
    p95 = times[int(round(0.95 * (len(times) - 1)))] if times else 0.0
    return {
        "edges": edges,
        "entities": n_entities,
        "queries": len(times),
        "p50_ms": round(statistics.median(times), 2) if times else 0.0,
        "p95_ms": round(p95, 2),
        "max_ms": round(max(times), 2) if times else 0.0,
        "max_scanned": max(scanned) if scanned else 0,
    }


def _hard_recall(seed: int, out_dir: Path) -> dict[str, Any]:
    """口语/换说法召回：不设门，只暴露 T0 模板的真实边界。"""
    from . import net_synthetic

    data = net_synthetic.hard()
    db_path = out_dir / f"net-hard-{seed}.db"
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()
    app = PMem.open(db_path)
    try:
        for text in data["memories"]:
            app.store.add(text, provenance="user_explicit", session_id="hard")
        predicted = _predicted_triples(app.db)
    finally:
        app.close()
    gold = {tuple(item) for item in data["gold"]}
    hit = predicted & gold
    return {
        "recall": round(len(hit) / len(gold), 3) if gold else 0.0,
        "matched": len(hit),
        "total": len(gold),
        "missing": sorted(gold - predicted),
        "extra": sorted(predicted - gold),
    }


def run_net(
    *,
    seed: int = 7,
    out_dir: Path | None = None,
    edge_count: int = 100000,
) -> dict[str, Any]:
    from . import net_synthetic

    out_dir = out_dir or Path(".tmp/eval")
    out_dir.mkdir(parents=True, exist_ok=True)
    data = net_synthetic.build(seed)
    from ..net.entities import norm_name

    relation_bases = {norm_name(name) for name in data["relation_names"]}
    db_path = out_dir / f"net-{seed}.db"
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()

    app = PMem.open(db_path)
    try:
        for memory in data["memories"]:
            app.store.add(
                memory["text"],
                kind=memory["kind"],
                provenance=memory["provenance"],
                session_id=memory["session_id"],
                scope_key="project:manual",
            )
        predicted = {
            triple
            for triple in _predicted_triples(app.db)
            if triple[0].split("#")[0] in relation_bases
        }
        gold = {tuple(item) for item in data["gold"]}
        relation_metrics = _prf_net(predicted, gold)
        relation_metrics["false_positives"] = sorted(predicted - gold)[:10]
        disambig = _disambig_accuracy(app.db, data["disambig"])
        alias = _alias_accuracy(app.db, data["aliases"])
        inv = invariants.summary(invariants.check_all(app.db))
        net_stats = {
            "entities": app.db.conn.execute("SELECT COUNT(*) c FROM entity WHERE status='active'").fetchone()["c"],
            "relations": app.db.conn.execute("SELECT COUNT(*) c FROM relation").fetchone()["c"],
            "mentions": app.db.conn.execute("SELECT COUNT(*) c FROM entity_mention").fetchone()["c"],
        }
    finally:
        app.close()

    two_hop = _bench_two_hop(out_dir, seed, edge_count)
    hard = _hard_recall(seed, out_dir)
    gates = [
        {
            "name": "关系抽取 precision",
            "target": f"≥ {NET_GATES['relation_precision']}",
            "actual": relation_metrics["precision"],
            "pass": relation_metrics["precision"] >= NET_GATES["relation_precision"],
        },
        {
            "name": "同名消歧准确率",
            "target": f"≥ {NET_GATES['disambig_accuracy']}",
            "actual": disambig["accuracy"],
            "pass": disambig["accuracy"] >= NET_GATES["disambig_accuracy"],
        },
        {
            "name": f"{two_hop['edges']} 边 2 跳 p95",
            "target": f"< {int(NET_GATES['two_hop_p95_ms'])} ms",
            "actual": f"{two_hop['p95_ms']} ms",
            "pass": two_hop["p95_ms"] < NET_GATES["two_hop_p95_ms"],
        },
    ]
    metrics = {
        "seed": seed,
        "memories": len(data["memories"]),
        "relation": relation_metrics,
        "disambiguation": disambig,
        "alias": alias,
        "relation_hard": hard,
        "two_hop": two_hop,
        "net": net_stats,
        "invariants": {"passed": inv["passed"], "total": inv["total"], "failed": inv["failed"]},
        "gates": gates,
        "ok": all(g["pass"] for g in gates),
    }
    _write_net_report(metrics, out_dir, seed)
    (out_dir / f"net-{seed}.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return metrics


def _write_net_report(metrics: dict[str, Any], out_dir: Path, seed: int) -> None:
    rel = metrics["relation"]
    dis = metrics["disambiguation"]
    hop = metrics["two_hop"]
    lines = [
        "# M3 关系网络评测报告（自动生成）",
        "",
        f"> seed={seed} ｜ 记忆 {metrics['memories']} 条 ｜ "
        f"实体 {metrics['net']['entities']} / 关系 {metrics['net']['relations']} / 提及 {metrics['net']['mentions']}",
        "",
        "## Gate",
        "",
        "| 项 | 目标 | 实测 | 结论 |",
        "|---|---|---|---|",
    ]
    for gate in metrics["gates"]:
        lines.append(
            f"| {gate['name']} | {gate['target']} | {gate['actual']} | {'PASS' if gate['pass'] else 'FAIL'} |"
        )
    lines += [
        "",
        "## 明细",
        "",
        f"- 关系：预测 {rel['predicted']} / 真值 {rel['gold']}，P={rel['precision']} R={rel['recall']} F1={rel['f1']}",
        f"- 同名消歧：{dis['correct']}/{dis['total']}（{dis['names']} 个名字，完整拆开 {dis['full_split_names']} 个）",
        f"- 别名：{metrics['alias']['correct']}/{metrics['alias']['total']}（{metrics['alias']['accuracy']}）",
        f"- 口语/换说法召回（**不设门**）：{metrics['relation_hard']['matched']}/{metrics['relation_hard']['total']}"
        f"（{metrics['relation_hard']['recall']}）",
        f"- 2 跳：{hop['edges']} 边 / {hop['entities']} 节点 / {hop['queries']} 次查询，"
        f"p50 {hop['p50_ms']} ms，p95 {hop['p95_ms']} ms，max {hop['max_ms']} ms（单次扫描边数上限 {hop['max_scanned']}）",
        f"- 不变量：{metrics['invariants']['passed']}/{metrics['invariants']['total']} 通过",
        "",
        "## 假阳性样本（最多 10 条）",
        "",
    ]
    for item in rel.get("false_positives") or []:
        lines.append(f"- `{item[0]}` --{item[1]}--> `{item[2]}`")
    if not rel.get("false_positives"):
        lines.append("- 无")
    lines.append("")
    (out_dir / f"net-{seed}.md").write_text("\n".join(lines), encoding="utf-8")
