"""外部独立基准 · A 层（免费层）：只看「记忆能不能把证据召回来」。

基准仓库：大肥鱼 · 聊天记忆系统评测（chat-memory-benchmark）。
本脚本**不调任何模型、不联网、不写 data/pmem.db**：
1. 每个人物一个隔离临时 PMem 库（Config 关向量，保证确定性）；
2. 按逻辑时间把历史逐轮喂进 journal 流水，整天完整后跑一次无模型 digest；
3. 按 cutoff 回放问题，取两路上下文：原生检索 `store.search(q, k)` 与「最近 6 个完整回合」原文窗口；
4. 用 `grading/evidence.jsonl` 的 fact meaning + source quotes 做字符 bigram 覆盖率匹配（确定性）。
所有指标都是**证据可召回性的代理**，不是官方成绩，也不是「答对率」。

用法：
    python bench/external/chat_memory_bench.py --benchmark-root .tmp/external-bench/chat-memory-benchmark \
        --out .tmp/external-a/report.json --md .tmp/external-a/report.md
    python bench/external/chat_memory_bench.py --benchmark-root ... --personas P01,P12 --k 10
    ... --limit 20       # 每个人物只跑前 20 题（冒烟）
    ... --keep           # 保留临时库到 .tmp/external-a/
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from pmem.base import clock  # noqa: E402
from pmem.base.clock import day_key as clock_day_key  # noqa: E402
from pmem.base.clock import parse_iso  # noqa: E402
from pmem.base.config import Config  # noqa: E402
from pmem.gate.sdk import PMem  # noqa: E402
from pmem.memory import journal  # noqa: E402

THRESHOLDS = (0.35, 0.25, 0.5)
PRIMARY_THRESHOLD = 0.35
WINDOW_TURNS = 6
DEFAULT_K = 10
PUNCT = re.compile(r"[\s，。！？、；：「」『』（）()…—\-~·,.!?;:\"'“”‘’\[\]【】<>《》/\\|]+")
DATASET_DIR = "datasets/natural-timegrid-20261101-v2"


# --------------------------------------------------------------------------- 匹配


def norm(text: Any) -> str:
    """匹配前归一：去空白/标点、英文小写。只影响代理匹配，不改原文。"""
    return PUNCT.sub("", str(text or "")).lower()


def bigrams(text: Any) -> set[str]:
    body = norm(text)
    if len(body) < 2:
        return {body} if body else set()
    return {body[index : index + 2] for index in range(len(body) - 1)}


def coverage(fact_grams: set[str], item_text: Any) -> float:
    """该 fact 的 bigram 被一条上下文条目覆盖的比例。"""
    if not fact_grams:
        return 0.0
    return len(fact_grams & bigrams(item_text)) / len(fact_grams)


def best_coverage(fact_grams: set[str], items: Iterable[str]) -> float:
    value, _text = best_item(fact_grams, items)
    return value


def best_item(fact_grams: set[str], items: Iterable[str]) -> tuple[float, str]:
    """该 fact 覆盖比例最高的一条上下文条目；返回 (覆盖率, 条目原文截断)。"""
    best = 0.0
    best_text = ""
    for item in items:
        value = coverage(fact_grams, item)
        if value > best:
            best = value
            best_text = str(item)
            if best >= 1.0:
                break
    return round(best, 4), best_text[:200]


def fact_coverage(fact: dict[str, Any], items: Iterable[str], *, mode: str = "primary") -> tuple[float, str]:
    """一个 fact 在给定上下文里的覆盖率（取它自己的各个 source 里最好的那项）。

    主口径（``primary``）：对 ``{meaning, quote1, quote2, ...}`` **逐项**单独算覆盖，
    取最大值——判分证据本来就是「一个 meaning + 若干条原话」，任何一项被召回，
    事实就可重建；拼在一起反而要求同一段上下文同时覆盖所有原话，不合理。
    保守口径（``strict``）：把 meaning + 全部 quote 拼成一个大 bigram 集合再算，
    长 quote 会稀释分母，作为下界保留。
    """
    options = [fact.get("strict") or set()] if mode == "strict" else list(fact.get("options") or [])
    best = 0.0
    best_text = ""
    for option in options:
        value, text = best_item(option, items)
        if value > best:
            best = value
            best_text = text
    return best, best_text


def question_recall(
    facts: list[dict[str, Any]],
    memory_items: list[str],
    window_items: list[str],
    wake_text: str,
    threshold: float,
    *,
    mode: str = "primary",
) -> dict[str, Any]:
    """一道题在 fact 级的召回：先算每个 fact，再按题平均。

    ``facts`` 每项：``{"fact_id", "meaning", "options", "strict"}``；
    ``mode="primary"`` 用「逐项 source 取最优」（主指标），``mode="strict"`` 用
    旧的全 quote 拼接口径（保守下界）。返回 memory/window/combined/wake 与逐 fact 明细。
    """
    per_fact: list[dict[str, Any]] = []
    for fact in facts:
        memory_best, memory_text = fact_coverage(fact, memory_items, mode=mode)
        window_best, window_text = fact_coverage(fact, window_items, mode=mode)
        wake_best, _ = fact_coverage(fact, [wake_text], mode=mode) if wake_text else (0.0, "")
        per_fact.append(
            {
                "fact_id": fact["fact_id"],
                "meaning": fact["meaning"],
                "memory_coverage": memory_best,
                "window_coverage": window_best,
                "wake_coverage": wake_best,
                "memory_best_text": memory_text,
                "window_best_text": window_text,
                "memory_recalled": memory_best >= threshold,
                "window_recalled": window_best >= threshold,
                "combined_recalled": max(memory_best, window_best) >= threshold,
                "wake_recalled": wake_best >= threshold,
            }
        )

    def mean(key: str) -> float:
        return round(sum(1.0 for item in per_fact if item[key]) / len(per_fact), 4) if per_fact else 0.0

    return {
        "facts": per_fact,
        "memory_recall": mean("memory_recalled"),
        "window_recall": mean("window_recalled"),
        "combined_recall": mean("combined_recalled"),
        "wake_recall": mean("wake_recalled"),
        "memory_best_coverage": round(max((item["memory_coverage"] for item in per_fact), default=0.0), 4),
        "window_best_coverage": round(max((item["window_coverage"] for item in per_fact), default=0.0), 4),
    }


# --------------------------------------------------------------------------- 汇总


def _row_payload(row: dict[str, Any], threshold_key: str) -> dict[str, Any]:
    """row 里 primary 与各阈值明细的取法不同，统一成 payload。"""
    if threshold_key == "primary":
        return row["primary"]
    if threshold_key == "pure":
        return row.get("pure") or row["primary"]
    if threshold_key == "strict":
        return row.get("strict") or row["primary"]
    return (row.get("thresholds") or {}).get(threshold_key) or row["primary"]


def summarize_questions(rows: list[dict[str, Any]], threshold_key: str = "primary") -> dict[str, Any]:
    """对一组题做汇总：题级平均 + fact 级命中率 + 记忆增量。"""
    if not rows:
        return {
            "questions": 0,
            "facts": 0,
            "memory_recall": 0.0,
            "window_recall": 0.0,
            "combined_recall": 0.0,
            "memory_gain_pp": 0.0,
        }
    payloads = [_row_payload(row, threshold_key) for row in rows]
    memory = sum(payload["memory_recall"] for payload in payloads) / len(payloads)
    window = sum(payload["window_recall"] for payload in payloads) / len(payloads)
    combined = sum(payload["combined_recall"] for payload in payloads) / len(payloads)
    fact_total = sum(len(payload["facts"]) for payload in payloads)
    return {
        "questions": len(rows),
        "facts": fact_total,
        "memory_recall": round(100.0 * memory, 1),
        "window_recall": round(100.0 * window, 1),
        "combined_recall": round(100.0 * combined, 1),
        "memory_gain_pp": round(100.0 * (combined - window), 1),
    }


def group_summaries(rows: list[dict[str, Any]], key_fn) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        for key in key_fn(row):
            grouped.setdefault(str(key), []).append(row)
    return {key: summarize_questions(items) for key, items in sorted(grouped.items())}


def build_spotcheck(rows: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    """人工抽查候选：按覆盖率从低到高均匀抽，既看边界也看高分，防止只看「像的」。"""
    candidates: list[dict[str, Any]] = []
    for row in rows:
        for fact in row["primary"]["facts"]:
            if not fact["memory_recalled"]:
                continue
            candidates.append(
                {
                    "id": row["id"],
                    "persona": row["persona"],
                    "question": row["question"],
                    "interval_days": row["interval_days"],
                    "content_tags": row["content_tags"],
                    "fact_meaning": fact["meaning"],
                    "memory_coverage": fact["memory_coverage"],
                    "window_coverage": fact["window_coverage"],
                    "memory_best_text": fact["memory_best_text"],
                }
            )
    if not candidates:
        return []
    candidates.sort(key=lambda item: (item["memory_coverage"], item["id"]))
    step = max(1, len(candidates) // max(1, limit))
    picked = [candidates[index] for index in range(0, len(candidates), step)][:limit]
    return picked


def build_failure_samples(rows: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    """window 与 memory 都没召回的题：带证据 meaning 与最高分检索条目，方便人看。

    同一 case 的 d03/d15/d45 是同一道题的不同时间档；先每个 case 抽一条，
    不够再补重复档，避免 10 条失败样本看着像 3 条。
    """
    failures = [row for row in rows if row["missed"]]
    # 先按「记忆最高覆盖率」升序（最惨的在前），再按题号稳定排序
    failures.sort(key=lambda row: (row["primary"]["memory_best_coverage"], row["id"]))
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    rest: list[dict[str, Any]] = []
    for row in failures:
        base = str(row["id"]).split("/d", 1)[0]
        if base in seen:
            rest.append(row)
        else:
            seen.add(base)
            unique.append(row)
    selected = unique[:limit]
    if len(selected) < limit:
        selected.extend(rest[: limit - len(selected)])
    samples: list[dict[str, Any]] = []
    for row in selected:
        samples.append(
            {
                "id": row["id"],
                "persona": row["persona"],
                "question": row["question"],
                "interval_days": row["interval_days"],
                "content_tags": row["content_tags"],
                "evidence_meanings": [fact["meaning"] for fact in row["primary"]["facts"]],
                "memory_items": [
                    {"text": item["text"][:220], "score": item["score"], "coverage": item["coverage"]}
                    for item in row["memory_items"][:3]
                ],
                "window_items": [item[:160] for item in row["window_items"][:2]],
                "memory_best_coverage": row["primary"]["memory_best_coverage"],
                "window_best_coverage": row["primary"]["window_best_coverage"],
            }
        )
    return samples


# --------------------------------------------------------------------------- 回放


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def load_benchmark(root: Path) -> dict[str, Any]:
    base = root / DATASET_DIR
    inputs = load_jsonl(base / "exam-inputs.jsonl")
    evidence = {row["id"]: row for row in load_jsonl(root / "grading" / "evidence.jsonl")}
    answer_key = {row["id"]: row for row in load_jsonl(base / "review-only" / "answer-key.jsonl")}
    missing_evidence = [row["id"] for row in inputs if row["id"] not in evidence]
    missing_key = [row["id"] for row in inputs if row["id"] not in answer_key]
    if missing_evidence or missing_key:
        raise SystemExit(
            f"基准数据不完整：缺 evidence {len(missing_evidence)} 条、缺 answer-key {len(missing_key)} 条"
        )
    manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    return {
        "root": root,
        "base": base,
        "inputs": inputs,
        "evidence": evidence,
        "answer_key": answer_key,
        "manifest": manifest,
    }


def _question_facts(bench: dict[str, Any], question_id: str) -> list[dict[str, Any]]:
    """取该题 scored_fact_ids 对应的 evidence fact（meaning + quotes → bigram 集）。"""
    evidence_row = bench["evidence"][question_id]
    facts_by_id = {str(item["fact_id"]): item for item in evidence_row.get("facts") or []}
    quotes: dict[str, list[str]] = {}
    for source in evidence_row.get("sources") or []:
        quotes.setdefault(str(source.get("fact_id")), []).append(str(source.get("quote") or ""))
    scored = [str(fid) for fid in (bench["answer_key"][question_id].get("scored_fact_ids") or [])]
    facts: list[dict[str, Any]] = []
    for fact_id in scored:
        item = facts_by_id.get(fact_id)
        if item is None:
            continue
        text = " ".join([str(item.get("meaning") or ""), *quotes.get(fact_id, [])])
        meaning = str(item.get("meaning") or "")
        # 主口径：meaning / 每条 quote 各自单独成一项，任一被召回即事实可重建。
        options = [bigrams(meaning), *[bigrams(quote) for quote in quotes.get(fact_id, []) if quote]]
        facts.append(
            {
                "fact_id": fact_id,
                "meaning": meaning,
                # 保守下界：meaning + 全部 quote 拼在一起（会被长 quote 稀释）
                "strict": bigrams(text),
                "options": options,
            }
        )
    return facts


def _history_turns(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    turns = []
    for row in history:
        stamp = str(row["逻辑时间"])
        turns.append(
            {
                "turn": int(row["轮次"]),
                "time": stamp,
                "date": clock_day_key(parse_iso(stamp)),
                "user": str(row["用户消息"]),
                "assistant": str(row["助手回复"]),
            }
        )
    return turns


def _temp_root(*, keep: bool) -> tuple[Path, Any]:
    """返回 (临时根目录, cleanup)；--keep 时放在 .tmp/external-a/ 且不清理。"""
    if keep:
        target = REPO / ".tmp" / "external-a" / f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        target.mkdir(parents=True, exist_ok=True)
        return target, None
    ctx = tempfile.TemporaryDirectory(prefix="pmem-external-a-")
    return Path(ctx.name), ctx


def _cleanup_db(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        target = Path(str(path) + suffix)
        try:
            if target.exists():
                target.unlink()
        except OSError:
            pass


def run_persona(
    bench: dict[str, Any],
    persona: str,
    *,
    k: int,
    limit: int | None,
    config: Config,
    work_root: Path,
    keep: bool,
) -> dict[str, Any]:
    """跑一个人物：隔离库 + 逐轮喂 + 按天 digest + 按 cutoff 取两路上下文。"""
    started = time.perf_counter()
    db_path = work_root / f"{persona}.db"
    _cleanup_db(db_path)
    app = PMem.open(db_path, config=config)
    app.db.init()
    # 硬约束：无模型 writer 才允许（否则 digest 会试图调模型）
    if journal.build_writer(app.config) is not None:
        app.close()
        raise RuntimeError("当前配置启用了 journal 模型 writer，违反「不调模型」要求")

    history = _history_turns(load_jsonl(bench["base"] / "histories" / f"{persona}.jsonl"))
    last_turn_of_date: dict[str, int] = {}
    for index, turn in enumerate(history):
        last_turn_of_date[turn["date"]] = index

    inputs = [row for row in bench["inputs"] if row["persona"] == persona]
    inputs.sort(key=lambda row: (row["cutoff_turn"], row["id"]))
    if limit:
        inputs = inputs[:limit]

    fed = 0
    digest_stats = {"runs": 0, "seconds": 0.0, "failures": []}
    rows: list[dict[str, Any]] = []
    search_seconds = 0.0
    wake_seconds = 0.0

    try:
        for question in inputs:
            cutoff = int(question["cutoff_turn"])
            while fed < cutoff:
                turn = history[fed]
                clock.set_now(datetime.fromisoformat(turn["time"]))
                session = f"{persona}-{turn['date']}"
                journal.append(app.db, turn["user"], role="user", ts=turn["time"], session_id=session)
                journal.append(app.db, turn["assistant"], role="assistant", ts=turn["time"], session_id=session)
                fed += 1
                after_index = fed - 1
                date = turn["date"]
                if last_turn_of_date.get(date) == after_index:
                    # digest 的 min_lines 门槛会拦「今天还没结束的日子」；回放里这一天
                    # 已经完整了，所以把时钟推到次日再消化（相当于夜间巩固发生在
                    # 第二天凌晨），否则某些完整日子会被当成 partial 拒掉。
                    clock.set_now(datetime.fromisoformat(turn["time"]) + timedelta(days=1))
                    digest_started = time.perf_counter()
                    result = journal.digest(
                        app.db,
                        subject="self",
                        day_key=date,
                        store=app.store,
                        config=app.config,
                    )
                    digest_stats["runs"] += 1
                    digest_stats["seconds"] += time.perf_counter() - digest_started
                    if not result.get("ok"):
                        digest_stats["failures"].append(
                            {"day_key": date, "reason": result.get("reason"), "lines": result.get("lines")}
                        )

            clock.set_now(datetime.fromisoformat(question["now"]))
            search_started = time.perf_counter()
            result = app.store.search(question["question"], k=k, record=False)
            search_seconds += time.perf_counter() - search_started
            items = result.get("items") or []
            memory_items = [str(item.get("text") or "") for item in items]
            memory_meta = [
                {
                    "text": str(item.get("text") or ""),
                    "source": str(
                        item.get("source")
                        or ("journal" if "journal" in (item.get("channels") or []) else "memory")
                    ),
                    "score": item.get("score"),
                }
                for item in items
            ]
            pure_memory_items = [meta["text"] for meta in memory_meta if meta["source"] != "journal"]

            window_turns = history[max(0, cutoff - WINDOW_TURNS) : cutoff]
            window_items = [
                f"用户：{turn['user']}\n助手：{turn['assistant']}" for turn in window_turns
            ]

            wake_started = time.perf_counter()
            try:
                wake_payload = app.wake()
                wake_data = wake_payload.get("data") if isinstance(wake_payload.get("data"), dict) else {}
                wake_text = str(wake_data.get("live") or wake_data.get("text") or "")
            except Exception:  # noqa: BLE001 醒来卡是加分项，挂了不影响主指标
                wake_text = ""
            wake_seconds += time.perf_counter() - wake_started

            facts = _question_facts(bench, question["id"])
            if not facts:
                raise RuntimeError(f"题目 {question['id']} 没有可匹配的 scored facts")
            answer = bench["answer_key"][question["id"]]
            thresholds_payload: dict[str, Any] = {}
            for threshold in THRESHOLDS:
                thresholds_payload[str(threshold)] = question_recall(
                    facts, memory_items, window_items, wake_text, threshold
                )
            primary = thresholds_payload[str(PRIMARY_THRESHOLD)]
            strict = question_recall(
                facts, memory_items, window_items, wake_text, PRIMARY_THRESHOLD, mode="strict"
            )
            pure = question_recall(facts, pure_memory_items, window_items, wake_text, PRIMARY_THRESHOLD)
            missed = (not any(fact["window_recalled"] for fact in primary["facts"])) and (
                not any(fact["memory_recalled"] for fact in primary["facts"])
            )
            # 供失败样本用的「最高 coverage 的 memory 条目」（主口径：逐项 source 取最优）
            memory_ranked = []
            for item in items[:3]:
                text = str(item.get("text") or "")
                memory_ranked.append(
                    {
                        "text": text,
                        "score": item.get("score"),
                        "coverage": round(
                            max(
                                (coverage(option, text) for fact in facts for option in fact["options"]),
                                default=0.0,
                            ),
                            4,
                        ),
                    }
                )
            rows.append(
                {
                    "id": question["id"],
                    "persona": persona,
                    "cutoff_turn": cutoff,
                    "now": question["now"],
                    "question": question["question"],
                    "interval_days": int(answer.get("interval_days", -1)),
                    "content_tags": list(answer.get("content_tags") or []),
                    "returned": len(items),
                    "search_ms": round((time.perf_counter() - search_started) * 1000, 2),
                    "wake_tokens": len(wake_text),
                    "vector_channel_items": int((result.get("channels") or {}).get("vec") or 0),
                    "primary": primary,
                    "pure": pure,
                    "strict": strict,
                    "thresholds": thresholds_payload,
                    "journal_items": sum(1 for meta in memory_meta if meta["source"] == "journal"),
                    "memory_items": memory_ranked,
                    "window_items": window_items,
                    "missed": missed,
                }
            )
    finally:
        app.close()
        if not keep:
            _cleanup_db(db_path)

    elapsed = round(time.perf_counter() - started, 2)
    return {
        "persona": persona,
        "questions": len(rows),
        "rows": rows,
        "elapsed_s": elapsed,
        "search_seconds": round(search_seconds, 2),
        "wake_seconds": round(wake_seconds, 2),
        "digest": {
            "runs": digest_stats["runs"],
            "seconds": round(digest_stats["seconds"], 2),
            "failures": digest_stats["failures"][:10],
        },
    }


# --------------------------------------------------------------------------- 输出


def build_report(
    persona_results: list[dict[str, Any]],
    *,
    args: argparse.Namespace,
    bench: dict[str, Any],
    vector_info: dict[str, Any] | None = None,
    comparison: dict[str, Any] | None = None,
) -> dict[str, Any]:
    all_rows = [row for result in persona_results for row in result["rows"]]
    totals = summarize_questions(all_rows)
    by_threshold = {str(threshold): summarize_questions(all_rows, threshold_key=str(threshold)) for threshold in THRESHOLDS}
    vector_enabled = bool(getattr(args, "vector", False))
    report = {
        "benchmark": {
            "name": "chat-memory-benchmark · natural-timegrid-20261101-v2",
            "root": str(bench["root"]),
            "dataset": DATASET_DIR,
            "manifest_version": bench["manifest"].get("version"),
            "pmem_version": _pmem_version(),
            "frozen_clock": os.environ.get("PMEM_NOW"),
            "run_kind": "vector-on" if vector_enabled else "vector-off",
            "k": args.k,
            "threshold": PRIMARY_THRESHOLD,
            "window_turns": WINDOW_TURNS,
            "personas": [result["persona"] for result in persona_results],
            "network_calls": 0,
            "model_calls": 0,
            "config": (
                "retrieval.vector.enabled=true（生产对照；结果可能受 embedding 版本/平台影响，不保证逐字节可复现）"
                if vector_enabled
                else "retrieval.vector.enabled=false（确定性主结果；digest 用无模型 template）"
            ),
            "vector": {
                "requested": vector_enabled,
                **(vector_info or {}),
            },
        },
        "totals": totals,
        "totals_strict_union": summarize_questions(all_rows, threshold_key="strict"),
        "totals_excluding_journal": summarize_questions(all_rows, threshold_key="pure"),
        "by_persona_strict_union": {
            result["persona"]: summarize_questions(result["rows"], threshold_key="strict")
            for result in persona_results
        },
        "diagnostics": {
            "journal_item_share": round(
                100.0
                * sum(row.get("journal_items") or 0 for row in all_rows)
                / max(1, sum(int(row.get("returned") or 0) for row in all_rows)),
                1,
            ),
            "questions_with_journal_items": sum(1 for row in all_rows if row.get("journal_items")),
            "avg_returned_items": round(
                sum(int(row.get("returned") or 0) for row in all_rows) / max(1, len(all_rows)), 2
            ),
        },
        "by_threshold": by_threshold,
        "by_persona": {result["persona"]: summarize_questions(result["rows"]) for result in persona_results},
        "by_interval": group_summaries(all_rows, lambda row: [f"{row['interval_days']}天"]),
        "by_tag": group_summaries(all_rows, lambda row: row["content_tags"]),
        "by_interval_x_tag": group_summaries(
            all_rows,
            lambda row: [f"{row['interval_days']}天·{tag}" for tag in row["content_tags"]],
        ),
        "failure_samples": build_failure_samples(all_rows, limit=10),
        "spotcheck_candidates": build_spotcheck(all_rows, limit=10),
        "vector_diagnostics": {
            "queries_with_vec_channel": sum(1 for row in all_rows if row.get("vector_channel_items")),
            "share": round(
                100.0 * sum(1 for row in all_rows if row.get("vector_channel_items")) / max(1, len(all_rows)), 1
            ),
            "avg_vec_items_per_query": round(
                sum(int(row.get("vector_channel_items") or 0) for row in all_rows) / max(1, len(all_rows)), 2
            ),
        },
        "digest": {result["persona"]: result["digest"] for result in persona_results},
        "timing": {
            "per_persona": {result["persona"]: result["elapsed_s"] for result in persona_results},
            "total_s": round(sum(result["elapsed_s"] for result in persona_results), 2),
            "search_s": round(sum(result["search_seconds"] for result in persona_results), 2),
            "wake_s": round(sum(result["wake_seconds"] for result in persona_results), 2),
        },
        "limitations": [
            "代理指标：用 evidence 的 fact meaning / 每条 quote 做字符 bigram 覆盖匹配，不是官方 rubric 判分，也不是「答对率」。",
            "主指标把 meaning 与每条 quote 分别作为独立判据、取最优（任何一项被召回即事实可重建）；"
            "`strict_union` 是「meaning + 全部 quote 拼一起」的旧口径，长 quote 会稀释分母，作为保守下界保留。",
            "覆盖阈值是启发式（默认 0.35，附 0.25/0.5 敏感性）；不同措辞/指代会漏匹配，误判两个方向都可能。",
            "窗口基线是「最近 6 个完整回合的原文」，天然包含原话，因此 interval=0 附近偏乐观。",
            "digest 走无模型模板（template_draft），不是模型日记；长期记忆的召回上限受模板派生事实质量限制。",
            (
                "本次开启了向量通道做生产对照；embedding 版本/平台可能影响结果，不保证逐字节可复现，"
                "以 benchmark.vector 里的后端/是否降级为准。"
                if vector_enabled
                else "向量通道显式关闭以保证确定性；本报告不覆盖向量通道质量（另见 report-vector）。"
            ),
            "每个 persona 独立库、按 cutoff 顺序回放；不是多次独立会话，也不模拟 DSH 的每轮浮现/handoff。",
        ],
    }
    if comparison:
        report["comparison"] = comparison
    return report


def _pmem_version() -> str:
    try:
        import pmem

        return str(getattr(pmem, "__version__", "unknown"))
    except Exception:  # noqa: BLE001
        return "unknown"


def vector_info(cfg: Config, *, enabled: bool) -> dict[str, Any]:
    """记录向量通道实际走哪条：onnx 模型 / 缺失降级 / numpy 不可用。

    ``--vector`` 只是「请求打开」；到底生效没有以 ``ready()`` 为准。降级时
    ``effective=False``，报告不会把降级结果冒充成向量成绩。
    """
    info: dict[str, Any] = {"requested": bool(enabled), "effective": False, "backend": "unknown"}
    try:
        from pmem.memory import vector as vec

        ok, reason = vec.ready(cfg)
        info["ready"] = bool(ok)
        info["reason"] = str(reason)
        info["degraded"] = not bool(ok)
        info["effective"] = bool(enabled and ok)
        if ok:
            info["model"] = str(vec.model_id(cfg))
            info["dim"] = int(vec.dim_of(cfg))
            info["onnx"] = str(vec.onnx_path(cfg))
            info["backend"] = f"onnx:{info['model']}"
        else:
            info["backend"] = "unavailable-degraded"
    except Exception as exc:  # noqa: BLE001 探测失败不能拖垮主流程
        info.update({"ready": False, "reason": f"{type(exc).__name__}: {exc}", "degraded": True, "effective": False})
        info["backend"] = "unavailable-degraded"
    return info


def build_comparison(off_report: dict[str, Any], on_report: dict[str, Any]) -> dict[str, Any]:
    """向量开 − 向量关：总表三档、按人物、按考点/时间档的 combined 差值。"""

    def totals(report: dict[str, Any]) -> dict[str, float]:
        primary = report.get("totals") or {}
        strict = report.get("totals_strict_union") or {}
        return {
            "memory_recall": float(primary.get("memory_recall", 0.0)),
            "window_recall": float(primary.get("window_recall", 0.0)),
            "combined_recall": float(primary.get("combined_recall", 0.0)),
            "strict_union_combined": float(strict.get("combined_recall", 0.0)),
        }

    off_totals = totals(off_report)
    on_totals = totals(on_report)
    deltas = {key: round(on_totals[key] - off_totals[key], 1) for key in off_totals}

    def group_delta(key: str) -> dict[str, dict[str, float]]:
        off_group = off_report.get(key) or {}
        on_group = on_report.get(key) or {}
        out: dict[str, dict[str, float]] = {}
        for name in sorted(set(off_group) & set(on_group)):
            off_item, on_item = off_group[name], on_group[name]
            if int(off_item.get("questions", 0)) < 5:
                continue
            out[name] = {
                "questions": int(on_item.get("questions", 0)),
                "off_combined": float(off_item.get("combined_recall", 0.0)),
                "on_combined": float(on_item.get("combined_recall", 0.0)),
                "delta_combined": round(
                    float(on_item.get("combined_recall", 0.0)) - float(off_item.get("combined_recall", 0.0)), 1
                ),
                "off_memory": float(off_item.get("memory_recall", 0.0)),
                "on_memory": float(on_item.get("memory_recall", 0.0)),
            }
        return out

    by_persona = group_delta("by_persona")
    by_tag = group_delta("by_tag")
    by_interval = group_delta("by_interval")
    ordered_tags = sorted(by_tag.items(), key=lambda pair: pair[1]["delta_combined"])
    return {
        "off_run": off_report.get("benchmark", {}).get("run_kind"),
        "on_run": on_report.get("benchmark", {}).get("run_kind"),
        "vector": on_report.get("benchmark", {}).get("vector"),
        "totals": {"off": off_totals, "on": on_totals, "delta_on_minus_off": deltas},
        "by_persona": by_persona,
        "by_interval": by_interval,
        "by_tag_bottom": dict(ordered_tags[:8]),
        "by_tag_top": dict(ordered_tags[-8:]),
    }


def render_markdown(report: dict[str, Any]) -> str:
    totals = report["totals"]
    lines = [
        "# 外部基准 A 层报告：证据召回（代理）",
        "",
        f"- 基准：{report['benchmark']['name']}",
        f"- PMem {report['benchmark']['pmem_version']}，k={report['benchmark']['k']}，"
        f"匹配阈值={report['benchmark']['threshold']}，窗口={report['benchmark']['window_turns']} 回合",
        f"- 人物：{', '.join(report['benchmark']['personas'])}；题目 {totals['questions']}，fact {totals['facts']}",
        f"- 运行档：**{report['benchmark'].get('run_kind', 'vector-off')}**；配置：{report['benchmark'].get('config')}",
        "- **这是代理指标**：只证明证据能不能被召回，不代表官方判分/答对率；digest 用的是无模型模板。",
        "",
        "## 总表（主指标：每个 fact 的 meaning / 每条 quote 逐项取最优）",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| memory_recall（检索） | {totals['memory_recall']}% |",
        f"| window_recall（最近 6 回合） | {totals['window_recall']}% |",
        f"| combined_recall（并集） | {totals['combined_recall']}% |",
        f"| 记忆增量 combined − window | {totals['memory_gain_pp']} pp |",
        f"| memory_recall（排除 journal 原话兜底） | {report['totals_excluding_journal']['memory_recall']}% |",
        f"| **strict_union_recall（旧口径：meaning+全部 quote 拼接，保守下界）** | "
        f"{report['totals_strict_union']['combined_recall']}%（memory {report['totals_strict_union']['memory_recall']}%） |",
        "",
        f"> 诊断：检索条目里 journal 原话兜底占 {report['diagnostics']['journal_item_share']}%，"
        f"{report['diagnostics']['questions_with_journal_items']} 道题命中了至少一条原话兜底条目"
        f"（平均返回 {report['diagnostics']['avg_returned_items']} 条 / k）。"
        "原话兜底是 PMem 的真实能力（长期原话检索），但它和「日记派生事实」不是一层；"
        "排除后的那一行更接近「巩固后的记忆」，看差异就知道分数里有多少是原话兜底挣的。",
        "",
        "### 向量通道实际状态",
        "",
    ]
    vector = report["benchmark"].get("vector") or {}
    lines += [
        f"- 请求开启：{vector.get('requested', False)}；实际生效：{vector.get('effective', False)}"
        f"（degraded={vector.get('degraded', True)}）",
        f"- 后端：{vector.get('backend', 'unknown')}；模型：{vector.get('model', '—')}；"
        f"原因：{vector.get('reason', '—')}",
        f"- 检索里真正走到向量通道的题：{report.get('vector_diagnostics', {}).get('queries_with_vec_channel', 0)} / "
        f"{totals['questions']} "
        f"（{report.get('vector_diagnostics', {}).get('share', 0)}%，平均每 query "
        f"{report.get('vector_diagnostics', {}).get('avg_vec_items_per_query', 0)} 条 vec 结果）",
    ]
    if vector.get("requested") and not vector.get("effective"):
        lines.append("- **警告：--vector 实际没生效（向量不可用/降级），本报告不能当作向量成绩。**")
    lines += [
        "",
        "### 阈值敏感性",
        "",
        "| 阈值 | memory | window | combined | 增量 |",
        "|---|---|---|---|---|",
    ]
    for threshold in sorted(report["by_threshold"], key=float):
        item = report["by_threshold"][threshold]
        lines.append(
            f"| {threshold} | {item['memory_recall']}% | {item['window_recall']}% | "
            f"{item['combined_recall']}% | {item['memory_gain_pp']} pp |"
        )
    lines += [
        "",
        "## 按人物",
        "",
        "| persona | 题数 | fact | memory | window | combined | 增量 | 耗时(s) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for persona, item in report["by_persona"].items():
        lines.append(
            f"| {persona} | {item['questions']} | {item['facts']} | {item['memory_recall']}% | "
            f"{item['window_recall']}% | {item['combined_recall']}% | {item['memory_gain_pp']} pp | "
            f"{report['timing']['per_persona'].get(persona)} |"
        )
    comparison = report.get("comparison") or {}
    if comparison:
        lines += [
            "",
            "## 向量开 / 关对照（on − off，新主指标）",
            "",
            "| 指标 | 关 | 开 | 差(pp) |",
            "|---|---|---|---|",
        ]
        totals_cmp = comparison.get("totals") or {}
        for key, label in (
            ("memory_recall", "memory_recall"),
            ("combined_recall", "combined_recall"),
            ("strict_union_combined", "strict_union combined"),
        ):
            off_value = (totals_cmp.get("off") or {}).get(key, 0.0)
            on_value = (totals_cmp.get("on") or {}).get(key, 0.0)
            delta = (totals_cmp.get("delta_on_minus_off") or {}).get(key, 0.0)
            lines.append(f"| {label} | {off_value} | {on_value} | {delta:+.1f} |")
        lines += [
            "",
            "| persona | 题数 | 关 combined | 开 combined | 差(pp) |",
            "|---|---|---|---|---|",
        ]
        for persona, item in (comparison.get("by_persona") or {}).items():
            lines.append(
                f"| {persona} | {item['questions']} | {item['off_combined']} | "
                f"{item['on_combined']} | {item['delta_combined']:+.1f} |"
            )
        lines += ["", "| 考点（差值最差/最好各 8） | 题数 | 关 combined | 开 combined | 差(pp) |", "|---|---|---|---|---|"]
        tag_items = list((comparison.get("by_tag_bottom") or {}).items())
        tag_items += list((comparison.get("by_tag_top") or {}).items())
        seen_tags: set[str] = set()
        for tag, item in tag_items:
            if tag in seen_tags:
                continue
            seen_tags.add(tag)
            lines.append(
                f"| {tag} | {item['questions']} | {item['off_combined']} | "
                f"{item['on_combined']} | {item['delta_combined']:+.1f} |"
            )
    lines += [
        "",
        "## 按时间档（interval_days）",
        "",
        "| 档位 | 题数 | memory | window | combined | 增量 |",
        "|---|---|---|---|---|---|",
    ]
    for key, item in report["by_interval"].items():
        lines.append(
            f"| {key} | {item['questions']} | {item['memory_recall']}% | {item['window_recall']}% | "
            f"{item['combined_recall']}% | {item['memory_gain_pp']} pp |"
        )
    tag_items = sorted(
        ((key, item) for key, item in report["by_tag"].items() if item["questions"] >= 5),
        key=lambda pair: pair[1]["combined_recall"],
    )
    lines += ["", "## 按考点（题数 ≥5，只列最差/最好各 8 个）", "", "| 考点 | 题数 | memory | window | combined |", "|---|---|---|---|---|"]
    for key, item in (tag_items[:8] + tag_items[-8:]) if len(tag_items) > 8 else tag_items:
        lines.append(
            f"| {key} | {item['questions']} | {item['memory_recall']}% | {item['window_recall']}% | "
            f"{item['combined_recall']}% |"
        )
    lines += ["", "## 失败样本（window 与 memory 都没召回，最多 10 条）", ""]
    for sample in report["failure_samples"]:
        lines.append(f"### {sample['id']} · {sample['persona']} · {sample['interval_days']} 天")
        lines.append(f"- 问题：{sample['question']}")
        lines.append(f"- 证据：" + "；".join(sample["evidence_meanings"]))
        if sample["memory_items"]:
            lines.append(
                f"- 最高分检索条目（coverage {sample['memory_items'][0]['coverage']}）："
                f"{sample['memory_items'][0]['text'][:160]}"
            )
        lines.append("")
    lines += [
        "",
        "## 人工抽查候选（匹配是否误判，需人看）",
        "",
        "按覆盖率从低到高均匀抽；低分是边界样本，高分是看起来最铁的匹配。",
        "",
        "| 题 | 覆盖率 | 证据 fact | 最高分检索条目 |",
        "|---|---|---|---|",
    ]
    for candidate in report.get("spotcheck_candidates") or []:
        lines.append(
            f"| {candidate['id']} | {candidate['memory_coverage']:.2f} | "
            f"{candidate['fact_meaning'][:60]} | {candidate['memory_best_text'][:120]} |"
        )
    lines += ["", "## 局限", ""]
    lines += [f"- {item}" for item in report["limitations"]]
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="外部基准 A 层（证据召回代理指标，不调模型）")
    parser.add_argument("--benchmark-root", required=True, help="chat-memory-benchmark clone 路径")
    parser.add_argument("--personas", default="", help="逗号分隔，默认全部 6 个人物")
    parser.add_argument("--k", type=int, default=DEFAULT_K)
    parser.add_argument("--out", default="", help="默认 .tmp/external-a/report.json；--vector 时为 report-vector.json")
    parser.add_argument("--md", default="")
    parser.add_argument("--limit", type=int, default=0, help="每个人物只跑前 N 题（冒烟）")
    parser.add_argument("--keep", action="store_true", help="保留临时库到 .tmp/external-a/")
    parser.add_argument(
        "--vector",
        action="store_true",
        help="打开向量通道（生产对照；默认关，保持确定性主结果）",
    )
    parser.add_argument("--compare", default="", help="与一份向量关闭的 JSON 对照，生成差值")
    args = parser.parse_args(argv)

    root = Path(args.benchmark_root).resolve()
    if not root.is_dir():
        raise SystemExit(f"benchmark root 不存在：{root}")
    bench = load_benchmark(root)
    personas = [item.strip() for item in args.personas.split(",") if item.strip()] or sorted(
        {row["persona"] for row in bench["inputs"]}
    )
    unknown = [persona for persona in personas if persona not in {row["persona"] for row in bench["inputs"]}]
    if unknown:
        raise SystemExit(f"未知 persona：{unknown}")
    missing_history = [
        persona for persona in personas if not (bench["base"] / "histories" / f"{persona}.jsonl").is_file()
    ]
    if missing_history:
        raise SystemExit(f"缺历史文件：{missing_history}")

    config = Config() if args.vector else Config({"retrieval": {"vector": {"enabled": False}}})
    vec_info = vector_info(config, enabled=args.vector)
    if args.vector and not vec_info.get("effective"):
        print(
            "警告：--vector 请求打开但向量通道实际没生效（降级）：" + str(vec_info.get("reason")),
            file=sys.stderr,
        )
    work_root, cleanup = _temp_root(keep=args.keep)
    started = time.perf_counter()
    results: list[dict[str, Any]] = []
    try:
        for persona in personas:
            print(f"[external-a] 跑 {persona} ...", flush=True)
            result = run_persona(
                bench,
                persona,
                k=args.k,
                limit=args.limit or None,
                config=config,
                work_root=work_root / "dbs",
                keep=args.keep,
            )
            results.append(result)
            print(
                f"  {persona}: {result['questions']} 题 / {result['elapsed_s']}s "
                f"(search {result['search_seconds']}s, digest {result['digest']['seconds']}s, "
                f"digest 失败 {len(result['digest']['failures'])})",
                flush=True,
            )
    finally:
        if cleanup is not None:
            cleanup.cleanup()

    comparison = None
    if args.compare:
        compare_path = Path(args.compare)
        if not compare_path.is_file():
            raise SystemExit(f"--compare 文件不存在：{compare_path}")
    report = build_report(results, args=args, bench=bench, vector_info=vec_info)
    if args.compare:
        report["comparison"] = build_comparison(
            json.loads(Path(args.compare).read_text(encoding="utf-8")), report
        )
    report["timing"]["wall_total_s"] = round(time.perf_counter() - started, 2)
    out_path = Path(args.out) if args.out else (
        REPO / ".tmp" / "external-a" / ("report-vector.json" if args.vector else "report.json")
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path = Path(args.md) if args.md else out_path.with_suffix(".md")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_markdown(report) + "\n", encoding="utf-8")

    totals = report["totals"]
    print("")
    print("=== 外部基准 A 层（证据召回·代理） ===")
    print(
        f"题目 {totals['questions']} / fact {totals['facts']}；"
        f"memory {totals['memory_recall']}% · window {totals['window_recall']}% · "
        f"combined {totals['combined_recall']}% · 增量 {totals['memory_gain_pp']} pp"
    )
    print(
        f"  排除 journal 原话兜底后 memory {report['totals_excluding_journal']['memory_recall']}%"
        f"（兜底条目占返回 {report['diagnostics']['journal_item_share']}%）"
    )
    print(
        f"  strict_union（旧口径保守下界）memory {report['totals_strict_union']['memory_recall']}% / "
        f"combined {report['totals_strict_union']['combined_recall']}%"
    )
    print(
        f"  向量：requested={vec_info.get('requested')} effective={vec_info.get('effective')} "
        f"backend={vec_info.get('backend')} reason={vec_info.get('reason')}"
    )
    vec_diag = report.get("vector_diagnostics") or {}
    print(
        f"  走向量通道的题：{vec_diag.get('queries_with_vec_channel', 0)} / {totals['questions']}"
        f"（{vec_diag.get('share', 0)}%，平均 {vec_diag.get('avg_vec_items_per_query', 0)} 条/query）"
    )
    if report.get("comparison"):
        delta = report["comparison"]["totals"]["delta_on_minus_off"]
        print(
            f"  向量差(开−关)：memory {delta.get('memory_recall', 0):+.1f}pp · "
            f"combined {delta.get('combined_recall', 0):+.1f}pp · "
            f"strict_union combined {delta.get('strict_union_combined', 0):+.1f}pp"
        )
    for threshold in sorted(report["by_threshold"], key=float):
        item = report["by_threshold"][threshold]
        print(
            f"  阈值 {threshold}: memory {item['memory_recall']}% / window {item['window_recall']}% / "
            f"combined {item['combined_recall']}%"
        )
    for persona, item in report["by_persona"].items():
        print(
            f"  {persona}: {item['questions']} 题 · memory {item['memory_recall']}% · "
            f"window {item['window_recall']}% · combined {item['combined_recall']}% · "
            f"{report['timing']['per_persona'][persona]}s"
        )
    print(f"报告：{out_path} ；{md_path}")
    if report["digest"] and any(result["digest"]["failures"] for result in results):
        print("警告：有 digest 失败，详见报告 digest 字段", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
