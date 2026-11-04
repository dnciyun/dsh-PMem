"""语文压力题库 runner（stress-v1）：46 个独立小场景，各建一个小库。

为什么单独一套：90 天长跑语料是「一个用户 90 天的连续对话」，适合测长期记忆与排名；
但有些最容易翻车的语文问题在长跑里出现得太少——过期事实、未生效计划、
「我/本人/自己/我们/咱」对「用户」、「他/她/它」指代、「小明他爸」和「小明」、
两个同名的小明、房租和奖金这种数字干扰。这套题库每题一个干净的小库，
可控、可复现，也方便把「哪一类语文压力不行」直接打印出来。

用法：

    python bench/stress/run.py                       # 默认配置
    python bench/stress/run.py --show                # 打印每题 top-3
    python bench/stress/run.py --cfg '{"retrieval":{"temporal":{"expired_penalty":0}}}'
    python bench/stress/run.py --raw .tmp/stress-before   # 存 raw，供 A/B 对比

指标：命中（top-8 里有 expect.any）、名次、抗干扰（top-3 里出现 expect.none = 违规）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent.parent
sys.path.insert(0, str(REPO / "src"))

from pmem.base import clock  # noqa: E402
from pmem.base.config import Config  # noqa: E402
from pmem.gate.sdk import PMem  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CORPUS = ROOT / "corpus.json"
RESULTS = ROOT / "results"
PUNCT = re.compile(r"[\s，。！？、；：「」『』（）()…—\-~·,.!?;:\"'“”‘’\[\]【】<>《》/\\|]+")


def norm(text: str) -> str:
    return PUNCT.sub("", str(text or ""))


def contains_any(blob: str, needles) -> bool:
    return any(norm(n) and norm(n) in blob for n in needles)


def load_corpus() -> dict:
    return json.loads(CORPUS.read_text(encoding="utf-8"))


def run_case(case: dict, overrides: dict, now: str) -> dict:
    clock.set_now(now)
    cfg = Config(
        {
            "retrieval": {"vector": {"enabled": False}},
            **overrides,
        }
    )
    with tempfile.TemporaryDirectory(prefix="pmem-stress-") as tmp:
        app = PMem.open(Path(tmp) / "stress.db", config=cfg)
        app.db.init()
        try:
            for memory in case.get("memories") or []:
                app.store.add(
                    memory["text"],
                    kind=memory.get("kind", "semantic"),
                    provenance=memory.get("provenance", "user_explicit"),
                    valid_from=memory.get("valid_from"),
                    valid_to=memory.get("valid_to"),
                    created_at=memory.get("created_at") or memory.get("valid_from"),
                )
            data = app.search(case["query"], k=8, record=False)["data"]
        finally:
            app.close()
    items = data.get("items") or []
    expect = (case.get("expect") or {}).get("any") or []
    none = (case.get("expect") or {}).get("none") or []
    rank = None
    for idx, item in enumerate(items, start=1):
        if contains_any(norm(item.get("text")), expect):
            rank = idx
            break
    # 干扰题的口径是「第一条给的是不是对的那个」：这些题只有一个正确答案，
    # 干扰项出现在第 2-3 名是合理的（它是同一件事的近邻），出现在第 1 名才算答错。
    top1 = [norm(items[0].get("text"))] if items else []
    noise = bool(none) and any(contains_any(text, none) for text in top1)
    return {
        "id": case["id"],
        "category": case["category"],
        "query": case["query"],
        "rank": rank,
        "hit": rank is not None,
        "noise": noise,
        "latency_ms": data.get("latency_ms"),
        "temporal": data.get("temporal"),
        "items": [
            {"text": str(item.get("text") or "")[:90], "score": item.get("score"), "source": item.get("source")}
            for item in items[:8]
        ],
    }


def summarize(results: list[dict]) -> dict:
    by_cat: dict[str, dict] = {}
    for row in results:
        cat = by_cat.setdefault(row["category"], {"n": 0, "hit": 0, "noise": 0, "mrr": 0.0})
        cat["n"] += 1
        if row["noise"]:
            cat["noise"] += 1
        if row["hit"]:
            cat["hit"] += 1
            cat["mrr"] += 1.0 / row["rank"]
    for cat in by_cat.values():
        cat["hit_rate"] = round(100.0 * cat["hit"] / max(1, cat["n"]), 1)
        cat["mrr"] = round(cat["mrr"] / max(1, cat["n"]), 4)
    total = {
        "n": len(results),
        "hit": sum(1 for row in results if row["hit"]),
        "noise": sum(1 for row in results if row["noise"]),
    }
    total["hit_rate"] = round(100.0 * total["hit"] / max(1, total["n"]), 1)
    total["mrr"] = round(mean((1.0 / row["rank"]) if row["hit"] else 0.0 for row in results), 4)
    return {"total": total, "by_category": by_cat}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="语文压力题库（stress-v1）")
    parser.add_argument("--show", action="store_true", help="打印每题结果与 top-3")
    parser.add_argument("--cfg", default="", help="JSON 覆盖配置，如 '{\"retrieval\":{\"temporal\":{...}}}'")
    parser.add_argument("--raw", default="", help="把逐题结果写到该路径（JSON）")
    parser.add_argument("--failures", action="store_true", help="只打印没命中的题")
    parser.add_argument("--violations", action="store_true", help="只打印抗干扰违规的题")
    args = parser.parse_args(argv)

    doc = load_corpus()
    overrides = json.loads(args.cfg) if args.cfg.strip() else {}
    now = doc.get("now") or "2026-11-03T23:00:00+01:00"
    results = [run_case(case, overrides, now) for case in doc["cases"]]
    summary = summarize(results)

    print(f"\n语文压力题库 {doc.get('version')} ｜ {summary['total']['n']} 题 ｜ 命中 "
          f"{summary['total']['hit_rate']}% ｜ MRR {summary['total']['mrr']:.3f} ｜ "
          f"抗干扰违规 {summary['total']['noise']}")
    for cat, data in sorted(summary["by_category"].items()):
        print(f"  {cat:<14} n={data['n']:2d}  命中 {data['hit_rate']:5.1f}%  MRR {data['mrr']:.3f}  违规 {data['noise']}")

    if args.show or args.failures:
        for row in results:
            if args.failures and row["hit"]:
                continue
            if args.violations and not row["noise"]:
                continue
            flag = "✓" if row["hit"] else "✗"
            print(f"\n{flag} {row['id']:>10} rank={row['rank']}  {row['query']}")
            for idx, item in enumerate(row["items"][:3], start=1):
                print(f"    #{idx} [{item['score']}] {item['source'] or '-'} {item['text'][:70]}")

    if args.raw:
        out = Path(args.raw)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
