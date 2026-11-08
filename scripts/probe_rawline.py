"""原话通道探针（task-53）：只读本地库，量「每题补了几条原话 / 多少 token / 多旧」。

跑法：
  python scripts/probe_rawline.py --personas P01,P20 --questions 30
数据与 probe_rerank.py 同口径（.tmp/external-b/work-fix34 或 outputs/pmem）。
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
from pmem.memory.text import estimate_tokens  # noqa: E402

BASE = {"retrieval": {"time_router": {"enabled": False}, "vector": {"enabled": False, "channel": False}}}
ON = {
    "retrieval": {
        "time_router": {"enabled": False},
        "vector": {"enabled": False, "channel": False},
        "rawline": {
            "enabled": True,
            "count": 8,
            "max_age_days": 45,
            "min_score": 0.25,
            "budget_share": 0.4,
            "dedupe_overlap": 0.6,
        },
    }
}


def _data(work: str | None) -> tuple[Path, Path]:
    candidates = [Path(work)] if work else []
    candidates += [ROOT / ".tmp" / "external-b" / "work-fix34"]
    bench = Path(r"F:\Ai\chat-memory-benchmark\outputs\pmem")
    candidates.append(bench)
    for base in candidates:
        packets = base / "packets.jsonl"
        if not packets.exists() and (base / "grading_base579_flashhigh" / "packets.jsonl").exists():
            packets = base / "grading_base579_flashhigh" / "packets.jsonl"
        if packets.exists():
            return base, packets
    raise SystemExit("找不到 packets.jsonl；用 --work 指定")


def _db(base: Path, persona: str) -> Path:
    for rel in (Path("dbs") / f"{persona}.db", Path("work") / persona / "pmem.db"):
        path = base / rel
        if path.exists():
            return path
    raise SystemExit(f"找不到 {persona} 数据库")


def _is_raw(item: dict) -> bool:
    return "rawline" in (item.get("channels") or [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--personas", default="P01,P20")
    ap.add_argument("--questions", type=int, default=30)
    ap.add_argument("--work", default="")
    args = ap.parse_args()
    personas = [part.strip() for part in args.personas.split(",") if part.strip()]
    base, packets_path = _data(args.work or None)
    rows = [json.loads(line) for line in packets_path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    by_persona: dict[str, list[dict]] = {}
    for row in rows:
        by_persona.setdefault(str(row.get("id") or "").split("-", 1)[0], []).append(row)
    per = max(1, args.questions // max(1, len(personas)))
    picked = []
    for persona in personas:
        picked.extend(by_persona.get(persona, [])[:per])

    off_cfg = Config(BASE)
    on_cfg = Config(ON)
    tmp = Path(tempfile.mkdtemp(prefix="pmem-rawline-"))
    apps: list[PMem] = []
    added_counts: list[int] = []
    added_tokens: list[int] = []
    ages: list[float] = []
    base_items: list[int] = []
    on_items: list[int] = []
    base_chars: list[int] = []
    on_chars: list[int] = []
    try:
        for persona in personas:
            src = _db(base, persona)
            off_path = tmp / f"{persona}-off.db"
            on_path = tmp / f"{persona}-on.db"
            shutil.copyfile(src, off_path)
            shutil.copyfile(src, on_path)
            app_off = PMem.open(off_path, config=off_cfg)
            app_on = PMem.open(on_path, config=on_cfg)
            apps.extend([app_off, app_on])
            for row in [item for item in picked if str(item["id"]).startswith(persona + "-")]:
                clock.set_now(row.get("now"))
                q = str(row.get("question") or "")
                started = time.perf_counter()
                off = app_off.store.search(q, k=8, budget=2400, record=False)["items"]
                on = app_on.store.search(q, k=8, budget=2400, record=False)["items"]
                raw = [item for item in on if _is_raw(item)]
                added_counts.append(len(raw))
                added_tokens.append(sum(estimate_tokens(str(item.get("text") or "")) for item in raw))
                for item in raw:
                    try:
                        moment = clock.parse_iso(str(item.get("valid_from") or ""))
                        now = clock.now()
                        ages.append(max(0.0, (now - moment).total_seconds() / 86400.0))
                    except Exception:  # noqa: BLE001
                        pass
                base_items.append(len(off))
                on_items.append(len(on))
                base_chars.append(sum(len(str(item.get("text") or "")) for item in off))
                on_chars.append(sum(len(str(item.get("text") or "")) for item in on))
    finally:
        clock.reset_now()
        for app in apps:
            app.close()
        shutil.rmtree(tmp, ignore_errors=True)

    report = {
        "data": str(base),
        "personas": personas,
        "questions": len(picked),
        "avg_added_rawline": round(statistics.mean(added_counts), 2) if added_counts else 0.0,
        "max_added_rawline": max(added_counts) if added_counts else 0,
        "avg_added_tokens": round(statistics.mean(added_tokens), 1) if added_tokens else 0.0,
        "avg_age_days": round(statistics.mean(ages), 1) if ages else 0.0,
        "avg_baseline_items": round(statistics.mean(base_items), 1) if base_items else 0.0,
        "avg_on_items": round(statistics.mean(on_items), 1) if on_items else 0.0,
        "avg_baseline_chars": round(statistics.mean(base_chars), 1) if base_chars else 0.0,
        "avg_on_chars": round(statistics.mean(on_chars), 1) if on_chars else 0.0,
        "questions_with_rawline": sum(1 for count in added_counts if count) if added_counts else 0,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
