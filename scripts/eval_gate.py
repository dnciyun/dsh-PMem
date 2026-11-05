"""检索质量回归门（0.30.0）。

用 ``testdata/`` 里真实存在的三套资产跑一遍检索评测：

- ``corpus-90d/p01..p10.json``：10 个人 × 90 天，共 **308 题**（可答率 / MRR / 长时程 / 时效 / 抗干扰 / 忠实度）；
- ``hard-negatives.json``：**277** 条合成硬负例（同一个人、另一件事；看 top3 纯净度）；
- ``holdout.json``：从 308 里切出的 **100** 题留出集（防止在调参集上自欺）。

结果和 ``testdata/eval-baseline.json`` 比，任一指标低于「基线 − 容差」就非零退出。
``--update-baseline`` 把当前实测值写成新基线（人工确认过再提交）。

**跨环境可复现**：固定 ``PMEM_NOW``，并显式关闭向量通道（CI 没装 numpy/onnx，
本机装了——不关的话同一份代码在两个环境跑出的排序不同，门会永远 flaky）。
门守的是词法/图/实体/时效这层检索逻辑；向量通道要单独做 A/B 基线。

用法：
    python scripts/eval_gate.py                    # 跑门，失败退出码 1
    python scripts/eval_gate.py --update-baseline  # 写 testdata/eval-baseline.json
    python scripts/eval_gate.py --out .tmp/eval-gate.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "testdata"
CORPUS = DATA / "corpus-90d"
BASELINE_PATH = DATA / "eval-baseline.json"
FROZEN_NOW = "2026-11-05T00:00:00Z"

sys.path.insert(0, str(REPO / "src"))


def _freeze_clock() -> str | None:
    """把 ``PMEM_NOW`` 固定住并返回旧值。

    只在**真要跑评测时**调用：模块导入时设环境变量会污染同进程的其它测试
    （pytest 只 import 这个模块，就会把时钟冻住，后面所有用例都跟着变）。
    """
    previous = os.environ.get("PMEM_NOW")
    os.environ.setdefault("PMEM_NOW", FROZEN_NOW)
    return previous


def _restore_clock(previous: str | None) -> None:
    if previous is None:
        os.environ.pop("PMEM_NOW", None)
    else:
        os.environ["PMEM_NOW"] = previous


def _frozen_now() -> str:
    """本次评测用的“现在”；跑完 env 已恢复时退回默认常量（元数据仍如实）。"""
    return os.environ.get("PMEM_NOW") or FROZEN_NOW

METRIC_KEYS = (
    "answer_rate",
    "mrr",
    "long_horizon",
    "temporal",
    "noise_resistance",
    "faithfulness",
    "hardneg_top3_purity",
    "holdout_answer_rate",
    "holdout_mrr",
)

# 允许的回落（百分比点 / MRR 绝对值）。固定环境 + 冻结时钟下，正常应几乎为 0；
# 留一点余量给 SQLite 版本差异与平台差异。
DEFAULT_TOLERANCE: dict[str, float] = {
    "answer_rate": 0.5,
    "mrr": 0.005,
    "long_horizon": 0.5,
    "temporal": 0.5,
    "noise_resistance": 0.5,
    "faithfulness": 0.5,
    "hardneg_top3_purity": 0.5,
    "holdout_answer_rate": 0.5,
    "holdout_mrr": 0.005,
}


def _load_eval_module():
    """加载 scripts/eval_testdata.py，并把它的 Config 换成「关向量」的确定性版本。"""
    path = REPO / "scripts" / "eval_testdata.py"
    spec = importlib.util.spec_from_file_location("eval_testdata_for_gate", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    from pmem.base.config import Config

    def deterministic_config(*args: Any, **kwargs: Any) -> Config:
        return Config({"retrieval": {"vector": {"enabled": False}}})

    module.Config = deterministic_config  # type: ignore[attr-defined]
    return module


def _compute_metrics_frozen(*, k: int = 8, personas: list[str] | None = None) -> dict[str, Any]:
    """跑 308 题 + 硬负例 + 留出集，返回 {metrics, counts, extras, elapsed_s}。"""
    started = time.perf_counter()
    mod = _load_eval_module()
    names = (
        personas
        if personas
        else sorted(path.stem for path in CORPUS.glob("p*.json") if not path.stem.startswith("_"))
    )
    negatives = mod.load_negatives()
    all_rows: list[dict[str, Any]] = []
    for name in names:
        path = CORPUS / f"{name}.json"
        if not path.is_file():
            continue
        corpus = json.loads(path.read_text(encoding="utf-8"))
        extras = negatives.get(name) or []
        if extras:
            corpus = {**corpus, "probes": [*(corpus.get("probes") or []), *extras]}
        result = mod.evaluate_persona(corpus, k)
        all_rows.extend(result["rows"])

    original = [row for row in all_rows if row.get("origin") == "original"]
    hardneg_rows = [row for row in all_rows if row.get("origin") != "original"]
    summary = mod.summarize(original)
    hardneg = mod.summarize_negatives(hardneg_rows) if hardneg_rows else {"probes": 0, "purity": 0.0}

    holdout_doc = json.loads((DATA / "holdout.json").read_text(encoding="utf-8"))
    holdout_ids = {str(probe.get("id")) for probe in holdout_doc.get("probes") or []}
    holdout_rows = [row for row in original if str(row.get("id")) in holdout_ids]
    holdout = mod.summarize(holdout_rows) if holdout_rows else {}

    metrics = {
        "answer_rate": summary["answer_rate"],
        "mrr": summary["mrr"],
        "long_horizon": summary["long_keep"],
        "temporal": summary["fresh"],
        "noise_resistance": summary["noise_ok"],
        "faithfulness": summary["faithfulness"],
        "hardneg_top3_purity": hardneg.get("purity", 0.0),
        "holdout_answer_rate": holdout.get("answer_rate", 0.0),
        "holdout_mrr": holdout.get("mrr", 0.0),
    }
    counts = {
        "personas": len(names),
        "probes": len(original),
        "hardneg_probes": int(hardneg.get("probes", 0)),
        "hardneg_distinct_queries": int(hardneg.get("distinct_queries", 0)),
        "holdout_probes": len(holdout_rows),
    }
    extras = {
        "retrieval_p50_ms": summary.get("retrieval_p50_ms"),
        "inject_tokens_mean": summary.get("inject_tokens_mean"),
        "hardneg": hardneg,
        "holdout": holdout,
        "k": k,
        "frozen_now": _frozen_now(),
        "vector": "disabled-for-determinism",
    }
    return {
        "metrics": metrics,
        "counts": counts,
        "extras": extras,
        "elapsed_s": round(time.perf_counter() - started, 2),
    }


def compute_metrics(*, k: int = 8, personas: list[str] | None = None) -> dict[str, Any]:
    """冻结时钟跑一遍评测；结束后把 ``PMEM_NOW`` 恢复原值（不污染调用方）。"""
    previous = _freeze_clock()
    try:
        return _compute_metrics_frozen(k=k, personas=personas)
    finally:
        _restore_clock(previous)


def compare_metrics(
    metrics: dict[str, Any],
    baseline: dict[str, Any],
    *,
    tolerance: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """逐指标比较：低于「基线 − 容差」即 regression。返回每行明细。"""
    base_metrics = baseline.get("metrics") or {}
    tol = {**DEFAULT_TOLERANCE, **(baseline.get("tolerance") or {}), **(tolerance or {})}
    rows: list[dict[str, Any]] = []
    for key in METRIC_KEYS:
        if key not in base_metrics:
            continue
        base = float(base_metrics[key])
        current = float(metrics.get(key, float("-inf")))
        allowed = float(tol.get(key, 0.0))
        minimum = base - allowed
        rows.append(
            {
                "metric": key,
                "value": current,
                "baseline": base,
                "tolerance": allowed,
                "min_allowed": round(minimum, 6),
                "delta": round(current - base, 6),
                "ok": current >= minimum - 1e-9,
            }
        )
    return rows


def compare_counts(counts: dict[str, Any], baseline: dict[str, Any]) -> list[dict[str, Any]]:
    """资产规模也必须一致：题目被删/少读几篇同样是「质量门悄悄失真」。"""
    base_counts = baseline.get("counts") or {}
    rows: list[dict[str, Any]] = []
    for key in ("personas", "probes", "hardneg_probes", "holdout_probes"):
        if key not in base_counts:
            continue
        expected = int(base_counts[key])
        actual = int(counts.get(key, -1))
        rows.append({"check": f"count.{key}", "value": actual, "baseline": expected, "ok": actual == expected})
    return rows


def build_baseline(result: dict[str, Any], *, k: int) -> dict[str, Any]:
    return {
        "version": 1,
        "generated_at": datetime.now().astimezone().isoformat(),
        "frozen_now": _frozen_now(),
        "k": k,
        "vector": "disabled-for-determinism",
        "tolerance": dict(DEFAULT_TOLERANCE),
        # 复制一份：调用方改 baseline 不能顺手把“当前实测值”也改掉。
        "metrics": dict(result["metrics"]),
        "counts": dict(result["counts"]),
        "extras": {
            "retrieval_p50_ms": result["extras"].get("retrieval_p50_ms"),
            "inject_tokens_mean": result["extras"].get("inject_tokens_mean"),
            "hardneg_by_tier": (result["extras"].get("hardneg") or {}).get("by_tier"),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检索质量回归门（308 题 + 硬负例 + 留出集）")
    parser.add_argument("--baseline", default=str(BASELINE_PATH), help="基线 JSON 路径")
    parser.add_argument("--update-baseline", action="store_true", help="用当前实测值刷新基线")
    parser.add_argument("--out", default="", help="把本次结果写到这个 JSON（可选）")
    parser.add_argument("--k", type=int, default=8)
    parser.add_argument("--personas", default="", help="只跑指定 persona（逗号分隔，调试用）")
    parser.add_argument("--json", action="store_true", help="只输出 JSON 结果")
    args = parser.parse_args(argv)

    personas = [item.strip() for item in args.personas.split(",") if item.strip()] or None
    result = compute_metrics(k=args.k, personas=personas)
    metrics = result["metrics"]

    if args.update_baseline:
        document = build_baseline(result, k=args.k)
        path = Path(args.baseline)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"已更新基线 → {path}")
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
        if args.out:
            Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0

    baseline_path = Path(args.baseline)
    if not baseline_path.is_file():
        print(f"缺少基线文件：{baseline_path}（先跑 --update-baseline 并人工确认）", file=sys.stderr)
        return 2
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    metric_rows = compare_metrics(metrics, baseline)
    count_rows = compare_counts(result["counts"], baseline)
    failed = [row for row in metric_rows if not row["ok"]] + [row for row in count_rows if not row["ok"]]

    payload = {
        "ok": not failed,
        "generated_at": datetime.now().astimezone().isoformat(),
        "frozen_now": _frozen_now(),
        "k": args.k,
        "elapsed_s": result["elapsed_s"],
        "metrics": metrics,
        "counts": result["counts"],
        "checks": metric_rows + count_rows,
        "failures": failed,
        "extras": result["extras"],
    }
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"检索质量门（k={args.k}，{result['elapsed_s']}s，冻结 {_frozen_now()}）")
        for row in metric_rows:
            mark = "✓" if row["ok"] else "✗"
            print(
                f"  {mark} {row['metric']:<22} {row['value']:>8.3f}  "
                f"（基线 {row['baseline']:.3f}，允许 ≥{row['min_allowed']:.3f}）"
            )
        for row in count_rows:
            mark = "✓" if row["ok"] else "✗"
            print(f"  {mark} {row['check']:<22} {row['value']:>8d}  （基线 {row['baseline']}）")
        if failed:
            print("\n回归：")
            for row in failed:
                print("  -", json.dumps(row, ensure_ascii=False))
        else:
            print("\n全部指标 ≥ 基线 − 容差：通过。")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
