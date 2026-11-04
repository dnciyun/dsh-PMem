"""生成硬负例（M29）：同一批人的另一件事，不该挤进前 3。

**为什么需要它**：原来的干扰题只有 46 道（46 道里每道约占 2.2pp），
调一次排序参数就可能被 7 道样本带偏。这里用**现成的语义标注**机械地扩样本，
不写死任何「模型觉得像」的判断：

1. 拿一道**已有**的答对题（query 是人写的，不动它）；
2. 在同一个人的记忆里找一条**共享关键词**的兄弟记忆（不同天、不同单元）；
3. 从兄弟记忆里挑 target 和标准答案里都没有的关键词当诱饵（`expect.none`）；
4. 按「兄弟记忆命中问句几个双字」分档：命中越多，越可能是真正会来抢位置的干扰。

这样造出来的题**仍然在问原来的问题**，只是明确写了「另一条相关记忆不该进前 3」。
诱饵全部来自语料里真实存在的词，不编造。

用法：

    python scripts/gen_hard_negatives.py           # 写 testdata/hard-negatives.json
    python scripts/gen_hard_negatives.py --dry-run # 只打印统计

产物是确定性的（按语料顺序生成，不含时间戳），diff 稳定。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "testdata" / "corpus-90d"
OUT = ROOT / "testdata" / "hard-negatives.json"

PUNCT = re.compile(r"[\s，。！？、,.!?;；:：\"'“”‘’（）()【】\[\]…·—-]+")
PER_PERSONA = 34  # 每人补多少条


def norm(text: object) -> str:
    return PUNCT.sub("", str(text or ""))


def bigrams(text: object) -> set[str]:
    body = norm(text)
    return {body[i : i + 2] for i in range(len(body) - 1)}


def _answer_texts(probe: dict) -> list[str]:
    return [norm(x) for x in (probe.get("expect") or {}).get("any") or [] if norm(x)]


def _pick_siblings(
    target: dict,
    units: list[dict],
    *,
    query_grams: set[str],
    answer_texts: list[str],
    target_norm: str | None = None,
    limit: int = 4,
) -> list[tuple[dict, list[str], int]]:
    """找几条「同主题、不同那一次」的记忆，各挑一组独有的诱饵词。

    同一道题最多取 3 条兄弟：**问题还是那一个，干扰换着来**。这样样本量上去了，
    但要注意它们共享同一个 query（有效独立样本数没那么多），所以报告里会分开报
    「题数」和「不重复 query 数」。
    """
    target_keywords = {k for k in target.get("keywords") or [] if len(str(k)) >= 2}
    target_norm = target_norm if target_norm is not None else norm(target.get("text"))
    if not target_keywords:
        return []
    ranked: list[tuple[int, dict, list[str], int]] = []
    for other in units:
        if other.get("id") == target.get("id"):
            continue
        other_keywords = sorted({k for k in other.get("keywords") or [] if len(str(k)) >= 2})
        if not (target_keywords & set(other_keywords)):
            continue
        other_norm = norm(other.get("text"))
        hit = sum(1 for gram in query_grams if gram in other_norm)
        # 诱饵必须是**兄弟记忆正文里真有的词**：keywords 里有一类是抽象标签（「相亲」），
        # 正文没写着，系统就永远检索不到，那种诱饵造出来的题永远不会失败。
        decoys = [
            str(k)
            for k in other_keywords
            if norm(k) in other_norm
            and norm(k) not in target_norm
            and not any(norm(k) in a for a in answer_texts)
        ]
        decoys.sort()
        if not decoys or hit < 1:
            continue
        score = hit * 10 + min(len(decoys), 3)
        ranked.append((score, other, decoys[:2], hit))
    ranked.sort(key=lambda item: (-item[0], str(item[1].get("id"))))
    out: list[tuple[dict, list[str], int]] = []
    seen_decoys: set[tuple[str, ...]] = set()
    for _score, other, decoys, hit in ranked:
        key = tuple(sorted(decoys))
        if key in seen_decoys:
            continue
        seen_decoys.add(key)
        out.append((other, decoys, hit))
        if len(out) >= limit:
            break
    return out


def _tier(hit: int) -> int:
    if hit >= 3:
        return 1  # 真和问句争位置：兄弟记忆命中 3 个以上双字
    if hit >= 2:
        return 2
    return 3


def build_persona(corpus: dict) -> list[dict]:
    units = [u for day in corpus.get("days") or [] for u in day.get("units") or []]
    by_id = {u.get("id"): u for u in units}
    made: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for probe in corpus.get("probes") or []:
        if probe.get("category") in ("precision", "no_memory"):
            continue
        query = str(probe.get("query") or "")
        if not query:
            continue
        query_grams = bigrams(query)
        answers = _answer_texts(probe)
        # 一道题可能引用多条 unit：诱饵不能出现在**这题全部目标**里
        probe_target_norm = "".join(
            norm(by_id[uid].get("text")) for uid in probe.get("unit_ids") or [] if uid in by_id
        )
        for unit_id in probe.get("unit_ids") or []:
            target = by_id.get(unit_id)
            if target is None:
                continue
            for sibling, decoys, hit in _pick_siblings(
                target,
                units,
                query_grams=query_grams,
                answer_texts=answers,
                target_norm=probe_target_norm,
            ):
                key = (str(probe.get("id")), str(sibling.get("id")))
                if key in seen:
                    continue
                seen.add(key)
                made.append(
                    {
                        "id": f"neg-{probe.get('id')}-{len(made) + 1:02d}",
                        "category": "precision",
                        "origin": "synthetic",
                        "hardness": _tier(hit),
                        "ask_day": probe.get("ask_day", 90),
                        "query": query,
                        "expect": {
                            "any": list((probe.get("expect") or {}).get("any") or []),
                            "none": decoys,
                        },
                        "unit_ids": list(probe.get("unit_ids") or []),
                        "decoy_unit": sibling.get("id"),
                        "note": (
                            f"同题硬负例：诱饵来自 {sibling.get('id')}（与目标共享关键词），"
                            f"命中问句 {hit} 个双字"
                        ),
                    }
                )
    # 先保真的（tier 1/2），再用 tier 3 凑到每人 PER_PERSONA 条
    made.sort(key=lambda p: (p["hardness"], p["id"]))
    keep = [p for p in made if p["hardness"] <= 2][:PER_PERSONA]
    if len(keep) < PER_PERSONA:
        rest = [p for p in made if p["hardness"] == 3][: PER_PERSONA - len(keep)]
        keep.extend(rest)
    keep.sort(key=lambda p: (p["hardness"], p["id"]))
    return keep


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只打印统计，不写文件")
    args = parser.parse_args()

    personas: dict[str, list[dict]] = {}
    tiers: dict[int, int] = {1: 0, 2: 0, 3: 0}
    for path in sorted(DATA.glob("p*.json")):
        corpus = json.loads(path.read_text(encoding="utf-8"))
        probes = build_persona(corpus)
        if not probes:
            continue
        personas[str(corpus.get("id") or path.stem)] = probes
        for probe in probes:
            tiers[probe["hardness"]] += 1
        print(f"{path.stem}: {len(probes)} 条（tier1 {sum(1 for p in probes if p['hardness']==1)}）")

    total = sum(len(v) for v in personas.values())
    distinct = len({(pid, p["query"]) for pid, items in personas.items() for p in items})
    payload = {
        "version": 1,
        "generator": "scripts/gen_hard_negatives.py",
        "note": (
            "合成的硬负例：问句沿用原题（人写的），expect.none 是同一个人的另一条真实记忆里"
            "独有的关键词。hardness 按「兄弟记忆命中问句几个双字」分档，1 最硬。"
            "同一道问句可能配多组诱饵，所以「题数」和「不重复问句数」要分开看。"
        ),
        "counts": {
            "probes": total,
            "distinct_queries": distinct,
            "tiers": {str(k): v for k, v in tiers.items()},
        },
        "personas": personas,
    }
    print(f"合计 {total} 条（不重复问句 {distinct} 条）；分档 {tiers}")
    if args.dry_run:
        return
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"已写入 {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
