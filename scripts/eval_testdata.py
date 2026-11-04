"""用 testdata/ 里的语料评测 PMem：可答率、MRR、长时程、时效、抗干扰、延迟、注入成本。

用法：
    python scripts/eval_testdata.py                     # 10 个人全跑
    python scripts/eval_testdata.py --personas p01,p02  # 只跑两个人
    python scripts/eval_testdata.py --k 8 --out .tmp/eval.json

口径（和 README 里那张表一致）：
- 可答率：卡片 + top-k 检索结果里出现 expect.any 的任意片段即算答对（no_memory 题不判可答）
- MRR：第一条含答案的检索结果名次的倒数
- 长时程：按答案单元距第 90 天的天数分桶，45 天以上的可答率
- 时效：update 类题，答案必须出现在检索结果里
- 抗干扰：precision / no_memory 题的 expect.none 不能出现在前 3 条
- 检索 p50：进程内检索计时（不含进程启动）
- 每题注入：背景条 + top-k 检索文本的 token 粗估（中文≈1 字 1 token）
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from pmem.base.config import Config  # noqa: E402
from pmem.gate.sdk import PMem  # noqa: E402
from pmem.memory import journal as journal_mod  # noqa: E402

DATA = REPO / "testdata"
PUNCT = re.compile(r"[\s，。！？、；：「」『』（）()…—\-~·,.!?;:\"'“”‘’\[\]【】<>《》/\\|]+")

UNIT_KIND = {
    "fact": "semantic",
    "episodic": "episodic",
    "preference": "semantic",
    "update": "semantic",
    "trivia": "episodic",
    "relation": "person",
    "plan": "goal",
}


def norm(text: str) -> str:
    return PUNCT.sub("", str(text or ""))


def contains_any(blob: str, needles: list[str]) -> bool:
    return any(norm(n) and norm(n) in blob for n in needles)


def rank_of(hit_norms: list[str], needles: list[str]) -> int | None:
    for index, text in enumerate(hit_norms, start=1):
        if contains_any(text, needles):
            return index
    return None


def tokens(text: str) -> float:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text or ""))
    return cjk + (len(text or "") - cjk) / 4


def ts_of(date: str, hhmm: str) -> str:
    return f"{date}T{hhmm}:00"


def grade_for_unit(unit: dict) -> str:
    kind = unit.get("kind")
    importance = int(unit.get("importance") or 3)
    if kind == "relation" and importance >= 4:
        return "A"
    if importance >= 5:
        return "A"
    if kind in ("fact", "preference", "update", "plan"):
        return "B"
    return "C"


class Replay:
    """把语料按天喂进一个临时库，再回第 90 天问一遍。"""

    def __init__(self, workdir: Path, label: str = "self") -> None:
        self.workdir = Path(workdir)
        self.db_path = self.workdir / "pmem.db"
        self._unit_mem: dict[str, str] = {}
        self._query_seq = 0
        self.app = PMem.open(str(self.db_path), config=Config())
        self.app.db.init()
        # 记忆主体固定用库里的默认 subject；persona 只用来给 surface 的会话起名
        self.scope = self.app.subject
        self.label = label
        self._wake_built = False
        self._wake_cache = ""

    def ingest_day(self, day: dict) -> None:
        for session in day.get("sessions") or []:
            for turn in session.get("turns") or []:
                journal_mod.append(
                    self.app.db,
                    turn["text"],
                    subject=self.scope,
                    session_id=session.get("id"),
                    role=turn["role"],
                    ts=ts_of(day["date"], session.get("time", "00:00")),
                )
        for unit in day.get("units") or []:
            saved = self.app.store.add(
                unit["text"],
                kind=UNIT_KIND.get(unit.get("kind"), "semantic"),
                grade=grade_for_unit(unit),
                grade_reason=f"聊天提取：{unit.get('kind')} imp{unit.get('importance')}",
                provenance="user_explicit",
                source_kind="unit",
                source_ref=unit.get("id"),
                idem_key=f"{self.scope}:{unit.get('id')}",
                valid_from=ts_of(day["date"], "12:00"),
                created_at=ts_of(day["date"], "12:00"),
            )
            memory_id = saved.get("id") if isinstance(saved, dict) else None
            if not memory_id:
                continue
            self._unit_mem[str(unit.get("id"))] = memory_id
            old_unit = unit.get("supersedes")
            old_memory = self._unit_mem.get(str(old_unit)) if old_unit else None
            if old_unit and old_memory:
                stamp = ts_of(day["date"], "12:00")
                self.app.db.conn.execute(
                    "UPDATE memory SET status='superseded', superseded_by=?, valid_to=? WHERE id=?",
                    (memory_id, stamp, old_memory),
                )
                self.app.db.conn.execute(
                    "UPDATE memory SET supersedes_id=? WHERE id=?", (old_memory, memory_id)
                )
        self.app.db.conn.commit()
        journal_mod.digest(
            self.app.db,
            subject=self.scope,
            day_key=day["date"],
            store=self.app.store,
            config=self.app.config,
        )

    def query(self, q: str, k: int = 8) -> dict:
        started = time.perf_counter()
        result = self.app.search(q, k=k)
        search_ms = (time.perf_counter() - started) * 1000
        items = (result.get("data") or {}).get("items") or []
        hits = [{"id": i.get("id"), "text": i.get("text") or "", "score": i.get("score")} for i in items]
        surface_text = ""
        self._query_seq += 1
        try:
            surface = self.app.surface(
                session_id=f"{self.label}-probe-{self._query_seq}", cue=q, mode="turn"
            )
            surface_text = str((surface.get("data") or {}).get("text") or "")
        except Exception:  # noqa: BLE001 浮现挂了不影响检索
            surface_text = ""
        if surface_text:
            hits.append({"id": "surface", "text": surface_text, "score": None})
        if not self._wake_built:
            try:
                data = self.app.wake().get("data") or {}
                self._wake_cache = str(data.get("live") or data.get("text") or "")
            except Exception:  # noqa: BLE001
                self._wake_cache = ""
            self._wake_built = True
        card = self._wake_cache
        return {
            "hits": hits,
            "card": card,
            "card_chars": len(card),
            "hit_chars": sum(len(h["text"]) for h in hits),
            "elapsed_ms": (time.perf_counter() - started) * 1000,
            "retrieval_ms": search_ms,
        }

    def close(self) -> None:
        self.app.close()


def bucket_of(gap: int | None) -> str:
    if gap is None:
        return "unknown"
    if gap <= 14:
        return "0-14天"
    if gap <= 44:
        return "15-44天"
    return "45天以上"


def evaluate_persona(corpus: dict, k: int) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        engine = Replay(Path(tmp), label=str(corpus.get("id") or "self"))
        try:
            unit_day = {
                unit["id"]: day["day"]
                for day in corpus.get("days") or []
                for unit in day.get("units") or []
            }
            for day in corpus.get("days") or []:
                engine.ingest_day(day)
            rows = []
            for probe in corpus.get("probes") or []:
                res = engine.query(probe["query"], k=k)
                unit_ids = probe.get("unit_ids") or []
                gaps = [90 - unit_day[u] for u in unit_ids if u in unit_day]
                gap = min(gaps) if gaps else None
                expect = (probe.get("expect") or {}).get("any") or []
                none = (probe.get("expect") or {}).get("none") or []
                hit_norms = [norm(h["text"]) for h in res["hits"]]
                card_norm = norm(res["card"])
                in_card = bool(expect) and contains_any(card_norm, expect)
                rank = rank_of(hit_norms, expect) if expect else None
                noise_bad = bool(none) and any(contains_any(t, none) for t in hit_norms[:3])
                rows.append(
                    {
                        "id": probe.get("id"),
                        "category": probe.get("category"),
                        "gap": gap,
                        "bucket": bucket_of(gap),
                        "answer_ok": in_card or (rank is not None),
                        "hit_ok": rank is not None,
                        "rank": rank,
                        "mrr": (1.0 / rank) if rank else 0.0,
                        "noise_bad": noise_bad,
                        "fresh_ok": (rank is not None) if probe.get("category") == "update" else None,
                        "retrieval_ms": res["retrieval_ms"],
                        "inject_tokens": round(tokens(res["card"]) + tokens("".join(h["text"] for h in res["hits"])), 1),
                    }
                )
            return {"rows": rows, "persona": corpus.get("id")}
        finally:
            engine.close()


def summarize(all_rows: list[dict]) -> dict:
    answerable = [r for r in all_rows if r["category"] != "no_memory"]
    mrr_rows = [r for r in answerable if r["rank"] is not None or r["mrr"] == 0]
    long_rows = [r for r in answerable if r["bucket"] == "45天以上"]
    update_rows = [r for r in all_rows if r["category"] == "update"]
    noise_rows = [r for r in all_rows if r["category"] in ("precision", "no_memory")]
    lat = [r["retrieval_ms"] for r in all_rows]
    lat.sort()
    return {
        "probes": len(all_rows),
        "answerable": len(answerable),
        "answer_rate": round(100.0 * sum(1 for r in answerable if r["answer_ok"]) / max(1, len(answerable)), 1),
        "mrr": round(statistics.mean([r["mrr"] for r in mrr_rows]) if mrr_rows else 0.0, 3),
        "long_keep": round(100.0 * sum(1 for r in long_rows if r["answer_ok"]) / max(1, len(long_rows)), 1),
        "fresh": round(100.0 * sum(1 for r in update_rows if r["fresh_ok"]) / max(1, len(update_rows)), 1),
        "noise_ok": round(100.0 * sum(1 for r in noise_rows if not r["noise_bad"]) / max(1, len(noise_rows)), 1),
        "retrieval_p50_ms": round(statistics.median(lat), 2) if lat else 0.0,
        "inject_tokens_mean": round(statistics.mean([r["inject_tokens"] for r in all_rows]), 1) if all_rows else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--personas", default="")
    parser.add_argument("--k", type=int, default=8)
    parser.add_argument("--out", default="")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args()

    corpus_dir = DATA / "corpus-90d"
    names = (
        [p.strip() for p in args.personas.split(",") if p.strip()]
        if args.personas
        else sorted(p.stem for p in corpus_dir.glob("p*.json") if not p.stem.startswith("_"))
    )
    all_rows: list[dict] = []
    for name in names:
        path = corpus_dir / f"{name}.json"
        if not path.is_file():
            print(f"跳过（没有语料）：{name}")
            continue
        corpus = json.loads(path.read_text(encoding="utf-8"))
        result = evaluate_persona(corpus, args.k)
        rows = result["rows"]
        all_rows.extend(rows)
        m = summarize(rows)
        if not args.json:
            print(
                f"{name}: 可答 {m['answer_rate']}% · MRR {m['mrr']} · 45天+ {m['long_keep']}% · "
                f"时效 {m['fresh']}% · 抗干扰 {m['noise_ok']}% · 检索 p50 {m['retrieval_p50_ms']}ms"
            )

    summary = summarize(all_rows)
    summary["personas"] = len(names)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print()
        print("== 汇总 ==")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print("已写入", args.out)


if __name__ == "__main__":
    main()
