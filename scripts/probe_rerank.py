"""两阶段重排探针（task-49）：排序变化 + 单题重排耗时 + 冷启动加载。

为什么单独一个脚本：重排器是可选件，产品测试不能依赖 300 MB 模型；这里用本地
已下载的模型对真实库做只读探针，输出可复现的数字，不跑 714 全量作答。

跑法：
  python scripts/probe_rerank.py --personas P01,P20 --questions 30
数据默认取 .tmp/external-b/work-fix34（有 packets.jsonl + dbs/<P>.db）；
找不到时退回 outputs/pmem 的 packets + work/<P>/pmem.db。
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pmem.base import clock  # noqa: E402
from pmem.base.config import Config  # noqa: E402
from pmem.gate.sdk import PMem  # noqa: E402
from pmem.memory.retrieval import rerank  # noqa: E402

BASE_CFG = {
    "retrieval": {
        "time_router": {"enabled": False},
        "vector": {"enabled": False, "channel": False},
    }
}
RERANK_ON = {
    "retrieval": {
        "time_router": {"enabled": False},
        "vector": {"enabled": False, "channel": False},
        "rerank": {"enabled": True, "pool": 40, "max_len": 320, "threads": 1, "blend": 1.0},
    }
}


def _resolve_data(work: str | None) -> tuple[Path, Path]:
    candidates = []
    if work:
        candidates.append(Path(work))
    candidates.append(ROOT / ".tmp" / "external-b" / "work-fix34")
    benchmark = Path(r"F:\Ai\chat-memory-benchmark\outputs\pmem")
    candidates.append(benchmark)
    for base in candidates:
        packets = base / "packets.jsonl"
        if not packets.exists() and (base / "grading_base579_flashhigh" / "packets.jsonl").exists():
            packets = base / "grading_base579_flashhigh" / "packets.jsonl"
        if packets.exists():
            return base, packets
    raise SystemExit("找不到 packets.jsonl；用 --work 指定包含 packets.jsonl 的目录")


def _db_for(base: Path, persona: str) -> Path:
    for rel in (Path("dbs") / f"{persona}.db", Path("work") / persona / "pmem.db"):
        path = base / rel
        if path.exists():
            return path
    raise SystemExit(f"找不到 {persona} 的数据库（{base}）")


def _top_ids(items: list[dict], n: int = 8) -> list[str]:
    return [str(item.get("id") or "") for item in items[:n]]


def main() -> int:
    ap = argparse.ArgumentParser(description="bge-reranker 两阶段检索探针")
    ap.add_argument("--personas", default="P01,P20", help="逗号分隔的人物，默认 P01,P20")
    ap.add_argument("--questions", type=int, default=30, help="总题数，默认 30")
    ap.add_argument("--work", default="", help="数据根目录（含 packets.jsonl + dbs/）")
    args = ap.parse_args()
    personas = [part.strip() for part in args.personas.split(",") if part.strip()]
    base, packets_path = _resolve_data(args.work or None)
    rows = [json.loads(line) for line in packets_path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    by_persona: dict[str, list[dict]] = {}
    for row in rows:
        persona = str(row.get("id") or "").split("-", 1)[0]
        by_persona.setdefault(persona, []).append(row)
    per = max(1, args.questions // max(1, len(personas)))
    picked: list[dict] = []
    for persona in personas:
        picked.extend(by_persona.get(persona, [])[:per])

    off_cfg = Config(BASE_CFG)
    on_cfg = Config(RERANK_ON)
    ok, why = rerank.ready(on_cfg)
    print(f"data={base}")
    print(f"ready={ok} ({why})")
    if not ok:
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="pmem-rerank-"))
    apps: list[PMem] = []
    order_changed = member_changed = top1_changed = 0
    timings: list[float] = []
    cold_ms = 0.0
    rank_shifts: list[int] = []
    try:
        for persona in personas:
            src = _db_for(base, persona)
            db_path = tmp / f"{persona}.db"
            shutil.copyfile(src, db_path)
            app_off = PMem.open(db_path, config=off_cfg)
            apps.append(app_off)
            copy_path = tmp / f"{persona}-on.db"
            shutil.copyfile(src, copy_path)
            app_on = PMem.open(copy_path, config=on_cfg)
            apps.append(app_on)
            for row in [item for item in picked if str(item["id"]).startswith(persona + "-")]:
                clock.set_now(row.get("now"))
                q = str(row.get("question") or "")
                off = app_off.store.search(q, k=40, budget=2400, record=False)
                docs = [str(item.get("text") or "") for item in off.get("items") or []]
                # 冷启动：第一次 score 包含 tokenizer 解析 + ONNX session 加载。
                if cold_ms == 0.0 and docs:
                    start = time.perf_counter()
                    rerank.score(q, docs[:5], on_cfg)
                    cold_ms = (time.perf_counter() - start) * 1000
                start = time.perf_counter()
                scores = rerank.score(q, docs or [q], on_cfg)
                timings.append((time.perf_counter() - start) * 1000)
                on = app_on.store.search(q, k=40, budget=2400, record=False)
                off_ids = _top_ids(off.get("items") or [])
                on_ids = _top_ids(on.get("items") or [])
                if off_ids != on_ids:
                    order_changed += 1
                if set(off_ids) != set(on_ids):
                    member_changed += 1
                if off_ids[:1] != on_ids[:1]:
                    top1_changed += 1
                shift = [on_ids.index(item) - off_ids.index(item) for item in set(off_ids) & set(on_ids)]
                rank_shifts.extend(shift)
                if len(scores) != len(docs):
                    print(f"! score/doc 数不一致 {row['id']}: {len(scores)} vs {len(docs)}")
    finally:
        clock.reset_now()
        for app in apps:
            app.close()
        shutil.rmtree(tmp, ignore_errors=True)

    timings.sort()
    def percentile(values: list[float], pct: float) -> float:
        if not values:
            return 0.0
        index = min(len(values) - 1, int((len(values) - 1) * pct))
        return values[index]

    report = {
        "personas": personas,
        "questions": len(picked),
        "ready": ok,
        "cold_load_ms": round(cold_ms, 1),
        "rerank_p50_ms": round(statistics.median(timings), 1) if timings else 0.0,
        "rerank_p95_ms": round(percentile(timings, 0.95), 1),
        "top8_order_changed": order_changed,
        "top8_membership_changed": member_changed,
        "top1_changed": top1_changed,
        "avg_rank_shift": round(sum(rank_shifts) / len(rank_shifts), 2) if rank_shifts else 0.0,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
