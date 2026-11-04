"""质量审计（M7，docs/03 §13.1）：只读，不写事实层。

六个维度：

```
quality = 100 · ( 0.25·traceability
                + 0.20·(1 - conflict_open)
                + 0.20·coverage
                + 0.15·freshness
                + 0.10·recall_health
                + 0.10·(1 - redundancy) )
```

没有样本的维度记 ``None`` 并按剩余权重归一化——空库不会被算成 100 分，
单日库也不会被"没有特质"直接扣分。缺陷与建议都是可核对的具体行，不是措辞。

**硬约束（G7）**：本模块只做 SELECT，不 import 事实层的写接口。
持久化由 ``reflect.report`` / ``reflect.service`` 负责，且只写
``reflection`` 与 ``metrics``。
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from ..base.clock import now, to_iso
from ..base.db import Database

ACTIVE = "('active','contested')"

WEIGHTS: dict[str, float] = {
    "traceability": 0.25,
    "conflict_open": 0.20,
    "coverage": 0.20,
    "freshness": 0.15,
    "recall_health": 0.10,
    "redundancy": 0.10,
}

LABELS = {
    "traceability": "派生行有证据的比例",
    "conflict_open": "未决冲突的反向",
    "coverage": "活跃记忆有实体/关系的比例",
    "freshness": "90 天内更新过的特质比例",
    "recall_health": "最近 30 天被想起的比例（0.15 最优）",
    "redundancy": "活跃记忆的重复率反向",
}

# 缺陷 → 建议。``command`` 是只读钻取，``fix`` 是真正的修复动作（不自动执行）。
_SUGGEST: dict[str, dict[str, str]] = {
    "no_entity": {
        "action": "link_entities",
        "effort": "medium",
        "fix": "pmem net extract",
        "why": "这些记忆没有被任何实体锚定，2 跳图查询永远找不到它们",
    },
    "never_recalled": {
        "action": "surface_backlog",
        "effort": "low",
        "fix": "pmem surface --session review",
        "why": "写入后从未被想起，要么不重要，要么检索路径没覆盖",
    },
    "duplicate_text": {
        "action": "merge_duplicates",
        "effort": "medium",
        "fix": "pmem consolidate",
        "why": "同一句话占了多行，会稀释召回并让证据数虚高",
    },
    "fts_missing": {
        "action": "rebuild_index",
        "effort": "high",
        "fix": "pmem doctor",
        "why": "词法索引缺行，关键词召回会静默漏掉这些记忆",
    },
    "stale_high_grade": {
        "action": "refresh_due",
        "effort": "low",
        "fix": "pmem lifecycle tick --dry-run",
        "why": "A/B 级记忆长期没被使用；先 dry-run 看会休眠哪些，再决定",
    },
    "open_conflict_high": {
        "action": "review_conflicts",
        "effort": "low",
        "fix": "pmem conflicts list",
        "why": "高严重度冲突未裁决，继续检索会同时返回互相矛盾的版本",
    },
    "trait_under_evidenced": {
        "action": "recheck_traits",
        "effort": "medium",
        "fix": "pmem trait metrics",
        "why": "活跃特质不足 3 条证据，违反了 M6 的硬门槛",
    },
    "isolated_cluster": {
        "action": "bridge_cluster",
        "effort": "medium",
        "fix": "pmem net extract",
        "why": "同一主题的簇没有任何实体连接，属于图上的孤岛",
    },
    "low_confidence_pinned": {
        "action": "review_pinned",
        "effort": "low",
        "fix": "pmem search --pinned",
        "why": "置顶却低可信，置顶会放大错误",
    },
}

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


def _scalar(db: Database, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = db.conn.execute(sql, params).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _scope(scope_key: str | None) -> tuple[str, tuple[Any, ...]]:
    if not scope_key:
        return "", ()
    return " AND m.scope_key=?", (scope_key,)


def _samples(
    db: Database,
    sql: str,
    params: tuple[Any, ...],
    *,
    limit: int = 3,
    text_col: str = "text_raw",
    chars: int = 48,
) -> list[dict[str, Any]]:
    """取几条样本；样本只用于人读，不参与质量分。"""
    out: list[dict[str, Any]] = []
    for row in db.conn.execute(sql + " LIMIT ?", (*params, limit)).fetchall():
        text = row[text_col] if text_col in row.keys() else ""
        text = (text or "").replace("\n", " ").strip()
        out.append({"id": row["id"], "text": text[:chars]})
    return out


def _defect(
    code: str,
    severity: str,
    count: int,
    why: str,
    *,
    samples: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "code": code,
        "severity": severity,
        "count": count,
        "why": why,
        "samples": samples or [],
    }
    if extra:
        item.update(extra)
    return item


def _q_traceability(db: Database, subject: str) -> tuple[int, int]:
    total = 0
    ok = 0
    # experience 没有 derivation 列；它的可追溯性是证据链本身。
    for table, status_sql, derived_sql in (
        (
            "trait",
            "status IN ('hypothesis','established')",
            "COALESCE(derivation,'') <> ''",
        ),
        (
            "pattern",
            "status IN ('candidate','confirmed')",
            "COALESCE(derivation,'') <> ''",
        ),
        ("experience", "status='active'", "1=1"),
    ):
        total += _scalar(
            db, f"SELECT COUNT(*) FROM {table} WHERE subject_id=? AND {status_sql}", (subject,)
        )
        ok += _scalar(
            db,
            f"SELECT COUNT(*) FROM {table} WHERE subject_id=? AND {status_sql} "
            f"AND {derived_sql} "
            "AND json_array_length(COALESCE(evidence_json,'[]')) > 0",
            (subject,),
        )
    return ok, total


def _q_coverage(db: Database, subject: str, scope_key: str | None) -> tuple[int, int]:
    sc_sql, sc_params = _scope(scope_key)
    total = _scalar(
        db,
        f"SELECT COUNT(*) FROM memory m WHERE m.subject_id=? AND m.status IN {ACTIVE}{sc_sql}",
        (subject, *sc_params),
    )
    covered = _scalar(
        db,
        f"SELECT COUNT(DISTINCT m.id) FROM memory m "
        f"JOIN entity_mention em ON em.memory_seq=m.seq "
        f"WHERE m.subject_id=? AND m.status IN {ACTIVE}{sc_sql}",
        (subject, *sc_params),
    )
    return covered, total


def _q_recall(db: Database, subject: str, scope_key: str | None, days: int) -> tuple[int, int]:
    sc_sql, sc_params = _scope(scope_key)
    cutoff = to_iso(now() - timedelta(days=days))
    total = _scalar(
        db,
        f"SELECT COUNT(*) FROM memory m WHERE m.subject_id=? AND m.status IN {ACTIVE}{sc_sql}",
        (subject, *sc_params),
    )
    recalled = _scalar(
        db,
        f"SELECT COUNT(*) FROM memory m WHERE m.subject_id=? AND m.status IN {ACTIVE}{sc_sql} "
        "AND m.use_count > 0 AND m.last_used_at IS NOT NULL AND m.last_used_at >= ?",
        (subject, *sc_params, cutoff),
    )
    return recalled, total


def _q_redundancy(db: Database, subject: str, scope_key: str | None) -> tuple[int, int]:
    sc_sql, sc_params = _scope(scope_key)
    row = db.conn.execute(
        f"SELECT COUNT(*) total, COUNT(DISTINCT m.text_norm) distinct_n "
        f"FROM memory m WHERE m.subject_id=? AND m.status IN {ACTIVE}{sc_sql}",
        (subject, *sc_params),
    ).fetchone()
    return int(row["distinct_n"] or 0), int(row["total"] or 0)


def _components(
    db: Database, *, subject: str, scope_key: str | None, freshness_days: int, recall_days: int
) -> dict[str, dict[str, Any]]:
    comps: dict[str, dict[str, Any]] = {}

    def add(name: str, good: int, total: int, detail: str = "") -> None:
        value = None if total <= 0 else max(0.0, min(1.0, good / total))
        comps[name] = {
            "value": None if value is None else round(value, 4),
            "weight": WEIGHTS[name],
            "good": good,
            "total": total,
            "label": LABELS[name],
            "detail": detail,
        }

    ok, total = _q_traceability(db, subject)
    add("traceability", ok, total, "trait/pattern/experience 中有 derivation 与证据")

    open_n = _scalar(
        db, "SELECT COUNT(*) FROM conflict WHERE subject_id=? AND status='open'", (subject,)
    )
    resolved_n = _scalar(
        db, "SELECT COUNT(*) FROM conflict WHERE subject_id=? AND status='resolved'", (subject,)
    )
    conflict_total = open_n + resolved_n
    if conflict_total <= 0:
        comps["conflict_open"] = {
            "value": 1.0,
            "weight": WEIGHTS["conflict_open"],
            "good": 0,
            "total": 0,
            "label": LABELS["conflict_open"],
            "detail": "没有冲突记录",
        }
    else:
        add(
            "conflict_open",
            conflict_total - open_n,
            conflict_total,
            f"未决 {open_n} / 共 {conflict_total}",
        )

    covered, active = _q_coverage(db, subject, scope_key)
    add("coverage", covered, active, "活跃记忆有 entity_mention 的比例")

    fresh = _scalar(
        db,
        "SELECT COUNT(*) FROM trait WHERE subject_id=? "
        "AND status IN ('hypothesis','established') AND updated_at >= ?",
        (subject, to_iso(now() - timedelta(days=freshness_days))),
    )
    traits = _scalar(
        db,
        "SELECT COUNT(*) FROM trait WHERE subject_id=? AND status IN ('hypothesis','established')",
        (subject,),
    )
    add("freshness", fresh, traits, f"{freshness_days} 天内更新")

    recalled, recall_total = _q_recall(db, subject, scope_key, recall_days)
    if recall_total <= 0:
        comps["recall_health"] = {
            "value": None,
            "weight": WEIGHTS["recall_health"],
            "good": 0,
            "total": 0,
            "label": LABELS["recall_health"],
            "detail": "没有活跃记忆",
        }
    else:
        rate = recalled / recall_total
        # 0.15 最优：太低说明没被用过，太高说明只有少数几条在循环
        value = max(0.0, 1.0 - abs(rate - 0.15) / 0.85)
        comps["recall_health"] = {
            "value": round(value, 4),
            "weight": WEIGHTS["recall_health"],
            "good": recalled,
            "total": recall_total,
            "label": LABELS["recall_health"],
            "detail": f"{recall_days} 天内被想起 {recalled}/{recall_total}（{rate:.1%}，最优 15%）",
        }

    distinct_n, total_n = _q_redundancy(db, subject, scope_key)
    add("redundancy", distinct_n, total_n, "按 text_norm 去重")
    return comps


def _quality(comps: dict[str, dict[str, Any]]) -> tuple[float | None, float]:
    used = [(c["weight"], c["value"]) for c in comps.values() if c["value"] is not None]
    if not used:
        return None, 0.0
    weight_sum = sum(w for w, _ in used)
    score = 100.0 * sum(w * v for w, v in used) / weight_sum
    return round(score, 1), round(weight_sum, 4)


def _defects(
    db: Database,
    *,
    subject: str,
    scope_key: str | None,
    due_days: int,
    vector_enabled: bool,
) -> list[dict[str, Any]]:
    sc_sql, sc_params = _scope(scope_key)
    active_where = f"m.subject_id=? AND m.status IN {ACTIVE}{sc_sql}"
    base_params = (subject, *sc_params)
    out: list[dict[str, Any]] = []

    no_entity_sql = (
        f"SELECT m.id, m.text_raw FROM memory m WHERE {active_where} "
        "AND NOT EXISTS (SELECT 1 FROM entity_mention em WHERE em.memory_seq=m.seq) "
        "ORDER BY m.importance DESC, m.seq DESC"
    )
    count = _scalar(
        db,
        f"SELECT COUNT(*) FROM memory m WHERE {active_where} "
        "AND NOT EXISTS (SELECT 1 FROM entity_mention em WHERE em.memory_seq=m.seq)",
        base_params,
    )
    if count:
        out.append(
            _defect(
                "no_entity",
                "medium",
                count,
                "活跃记忆没有实体锚点，图查询与关系桥都找不到",
                samples=_samples(db, no_entity_sql, base_params),
            )
        )

    never_sql = (
        f"SELECT m.id, m.text_raw FROM memory m WHERE {active_where} AND m.use_count=0 "
        "ORDER BY m.importance DESC, m.seq DESC"
    )
    count = _scalar(
        db, f"SELECT COUNT(*) FROM memory m WHERE {active_where} AND m.use_count=0", base_params
    )
    if count:
        out.append(
            _defect(
                "never_recalled",
                "medium",
                count,
                "写入后一次都没有被检索命中",
                samples=_samples(db, never_sql, base_params),
            )
        )

    dup_sql = (
        f"SELECT m.text_norm, COUNT(*) c, MIN(m.id) id FROM memory m WHERE {active_where} "
        "GROUP BY m.text_norm HAVING c > 1 ORDER BY c DESC"
    )
    dup_rows = db.conn.execute(dup_sql, base_params).fetchall()
    if dup_rows:
        out.append(
            _defect(
                "duplicate_text",
                "medium",
                sum(int(row["c"]) - 1 for row in dup_rows),
                "同一 text_norm 有多行活跃记忆",
                samples=[
                    {"id": row["id"], "text": f"重复 {row['c']} 次：{row['text_norm'][:40]}"}
                    for row in dup_rows[:3]
                ],
                extra={"groups": len(dup_rows)},
            )
        )

    # FTS5 是 external content：直接 SELECT 索引表会回读 content 表，看不出缺行；
    # `_docsize` 是真正的索引影子表，用它判断某条记忆是否被索引。
    fts_where = (
        f"{active_where} AND ("
        "NOT EXISTS (SELECT 1 FROM memory_fts_tok_docsize f WHERE f.id=m.seq) "
        "OR NOT EXISTS (SELECT 1 FROM memory_fts_tri_docsize f WHERE f.id=m.seq))"
    )
    fts_sql = (
        f"SELECT m.id, m.text_raw FROM memory m WHERE {fts_where} "
        "ORDER BY m.importance DESC"
    )
    fts_count = _scalar(db, f"SELECT COUNT(*) FROM memory m WHERE {fts_where}", base_params)
    if fts_count:
        out.append(
            _defect(
                "fts_missing",
                "high",
                fts_count,
                "活跃记忆在 FTS 索引里没有对应行，关键词召回会漏",
                samples=_samples(db, fts_sql, base_params),
            )
        )

    if vector_enabled:
        vec_count = _scalar(db, "SELECT COUNT(*) FROM memory_vec_plain")
        active = _scalar(
            db,
            f"SELECT COUNT(*) FROM memory m WHERE {active_where}",
            base_params,
        )
        if vec_count == 0 and active > 0:
            out.append(
                _defect(
                    "vector_gap",
                    "info",
                    active,
                    "向量通道未启用（M2 降级），语义召回为 0；这是已知缺口，不是退化",
                )
            )

    due_cutoff = to_iso(now() - timedelta(days=due_days))
    stale_sql = (
        f"SELECT m.id, m.text_raw FROM memory m "
        f"JOIN target_grade g ON g.target_tbl='memory' AND g.target_id=m.id "
        f"WHERE {active_where} AND g.grade IN ('A','B') AND m.created_at < ? "
        "AND (m.last_used_at IS NULL OR m.last_used_at < ?) "
        "ORDER BY m.importance DESC, m.seq DESC"
    )
    stale_count = _scalar(
        db,
        f"SELECT COUNT(*) FROM memory m "
        f"JOIN target_grade g ON g.target_tbl='memory' AND g.target_id=m.id "
        f"WHERE {active_where} AND g.grade IN ('A','B') AND m.created_at < ? "
        "AND (m.last_used_at IS NULL OR m.last_used_at < ?)",
        (*base_params, due_cutoff, due_cutoff),
    )
    if stale_count:
        out.append(
            _defect(
                "stale_high_grade",
                "low",
                stale_count,
                f"A/B 级记忆超过 {due_days} 天没被想起（S 级与「我们」不参与淡忘）",
                samples=_samples(db, stale_sql, (*base_params, due_cutoff, due_cutoff)),
            )
        )

    conflict_sql = (
        "SELECT id, kind, slot, severity FROM conflict WHERE subject_id=? "
        "AND status='open' AND severity >= 0.7 ORDER BY severity DESC, created_at"
    )
    conflict_rows = db.conn.execute(conflict_sql, (subject,)).fetchall()
    if conflict_rows:
        out.append(
            _defect(
                "open_conflict_high",
                "high",
                len(conflict_rows),
                "severity ≥0.7 的冲突未裁决",
                samples=[
                    {"id": row["id"], "text": f"{row['kind']} · {row['slot']} · {row['severity']}"}
                    for row in conflict_rows[:3]
                ],
            )
        )

    trait_rows = db.conn.execute(
        "SELECT id, key, evidence_json FROM trait WHERE subject_id=? "
        "AND status IN ('hypothesis','established')",
        (subject,),
    ).fetchall()
    weak = []
    for row in trait_rows:
        try:
            evidence = json.loads(row["evidence_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            evidence = []
        if len(evidence) < 3:
            weak.append(row)
    if weak:
        out.append(
            _defect(
                "trait_under_evidenced",
                "high",
                len(weak),
                "活跃特质不足 3 条证据，违反 M6 硬门槛",
                samples=[{"id": row["id"], "text": row["key"]} for row in weak[:3]],
            )
        )

    cluster_sql = (
        f"SELECT m.cluster_id, COUNT(*) c FROM memory m WHERE {active_where} "
        "AND m.cluster_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM entity_mention em JOIN memory mm "
        "ON mm.seq=em.memory_seq WHERE mm.cluster_id=m.cluster_id) "
        "GROUP BY m.cluster_id HAVING c >= 2 ORDER BY c DESC"
    )
    cluster_rows = db.conn.execute(cluster_sql, base_params).fetchall()
    if cluster_rows:
        out.append(
            _defect(
                "isolated_cluster",
                "low",
                len(cluster_rows),
                "同一主题簇内没有任何实体提及，是图上的孤岛",
                samples=[
                    {"id": row["cluster_id"], "text": f"{row['c']} 条记忆"} for row in cluster_rows[:3]
                ],
            )
        )

    pinned_sql = (
        f"SELECT m.id, m.text_raw FROM memory m WHERE {active_where} "
        "AND m.pinned=1 AND m.confidence < 0.5 ORDER BY m.importance DESC"
    )
    pinned_count = _scalar(
        db,
        f"SELECT COUNT(*) FROM memory m WHERE {active_where} "
        "AND m.pinned=1 AND m.confidence < 0.5",
        base_params,
    )
    if pinned_count:
        out.append(
            _defect(
                "low_confidence_pinned",
                "medium",
                pinned_count,
                "置顶记忆可信度 <0.5，置顶会放大错误",
                samples=_samples(db, pinned_sql, base_params),
            )
        )

    out.sort(key=lambda d: (_SEVERITY_ORDER.get(d["severity"], 9), -d["count"]))
    return out


def _suggestions(defects: list[dict[str, Any]], *, limit: int = 10) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for defect in defects:
        code = defect["code"]
        spec = _SUGGEST.get(code)
        if spec is None or defect["severity"] == "info":
            continue
        out.append(
            {
                "kind": "defect",
                "action": spec["action"],
                "target": code,
                "count": defect["count"],
                "why": spec["why"],
                "effort": spec["effort"],
                "command": f"pmem reflect --only {code} --limit 20",
                "fix": spec["fix"],
                "severity": defect["severity"],
            }
        )
        if len(out) >= limit:
            break
    return out


def _actionable_rate(suggestions: list[dict[str, Any]]) -> float | None:
    if not suggestions:
        return None
    ok = sum(
        1
        for item in suggestions
        if item.get("command", "").startswith("pmem ")
        and item.get("count", 0) > 0
        and item.get("effort") in ("low", "medium", "high")
        and bool(item.get("why"))
        and bool(item.get("fix", "").startswith("pmem "))
    )
    return round(ok / len(suggestions), 3)


def audit(
    db: Database,
    *,
    subject: str = "self",
    scope_key: str | None = None,
    freshness_days: int = 90,
    recall_days: int = 30,
    due_days: int = 30,
    vector_enabled: bool = True,
) -> dict[str, Any]:
    """计算质量分、六维明细、缺陷清单与建议。纯只读。"""
    comps = _components(
        db,
        subject=subject,
        scope_key=scope_key,
        freshness_days=freshness_days,
        recall_days=recall_days,
    )
    quality, weight_sum = _quality(comps)
    active = _scalar(
        db,
        f"SELECT COUNT(*) FROM memory m WHERE m.subject_id=? AND m.status IN {ACTIVE}"
        + (_scope(scope_key)[0]),
        (subject, *_scope(scope_key)[1]),
    )
    derived = sum(c["total"] for name, c in comps.items() if name == "traceability")
    if active == 0 and derived == 0:
        # 空库只有一个"无冲突"维度，不能因此拿满分
        quality, weight_sum = None, 0.0
    defects = _defects(
        db,
        subject=subject,
        scope_key=scope_key,
        due_days=due_days,
        vector_enabled=vector_enabled,
    )
    suggestions = _suggestions(defects)
    return {
        "subject": subject,
        "scope_key": scope_key,
        "generated_at": to_iso(now()),
        "quality": quality,
        "weight_used": weight_sum,
        "insufficient_data": quality is None,
        "components": comps,
        "defects": defects,
        "suggestions": suggestions,
        "actionable_rate": _actionable_rate(suggestions),
        "metrics": {
            "memories_active": active,
            "memories_total": _scalar(
                db, "SELECT COUNT(*) FROM memory WHERE subject_id=? AND status<>'deleted'", (subject,)
            ),
            "conflicts_open": _scalar(
                db, "SELECT COUNT(*) FROM conflict WHERE subject_id=? AND status='open'", (subject,)
            ),
            "traits_active": _scalar(
                db,
                "SELECT COUNT(*) FROM trait WHERE subject_id=? "
                "AND status IN ('hypothesis','established')",
                (subject,),
            ),
            "entities": _scalar(
                db, "SELECT COUNT(*) FROM entity WHERE subject_id=? AND status='active'", (subject,)
            ),
            "relations": _scalar(
                db, "SELECT COUNT(*) FROM relation WHERE status IN ('active','dormant')"
            ),
            "derived_rows": derived,
        },
    }
