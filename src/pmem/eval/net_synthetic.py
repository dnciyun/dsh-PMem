"""M3 合成评测集：关系抽取、同名消歧、别名、2 跳基准。

真值怎么来：模板生成时**同时**产出 gold 三元组，不做事后标注。这样
precision/recall 是"规则和出题人是否理解一致"，而不是"规则和自己一致"——
出题人和规则仍然是同一套中文模板，这一点在报告里必须写明，不能把 1.0
说成"理解正确"。

三类样本：

1. **关系**：人名 × (住哪 / 在哪工作 / 喜欢什么 / 不喜欢什么) → gold；
   外加问句、第三人称、未知宾语三类干扰项（gold 为空，专测假阳性）。
2. **同名消歧**：同一个 2 字名，三份不同上下文（公司 A/B/C）。正确行为是
   拆成 3 个身份；没有上下文的提及应**弃权**而不是乱认。
3. **别名**：``张伟也叫小伟`` 之后，``小伟在 X 工作`` 的 src 必须还是张伟。
"""

from __future__ import annotations

import random
from typing import Any

from ..memory.text import normalize
from ..net import lexicon
from ..net.entities import norm_name

SURNAMES = list("陈林王李张刘黄赵周吴徐孙马朱胡郭何高罗郑")
GIVEN = list("默岚舟宁然澄野川禾言知远清和静宜安可")

CITIES = sorted(lexicon.CITIES)
ORGS = sorted(lexicon.ORGS)
CONCEPTS = sorted(lexicon.CONCEPTS)
TOOLS = sorted(lexicon.TOOLS)


def _name_for(i: int) -> str:
    return SURNAMES[i % len(SURNAMES)] + GIVEN[(i // len(SURNAMES)) % len(GIVEN)]


_ALL_NAMES = [
    SURNAMES[s] + GIVEN[g]
    for s in range(len(SURNAMES))
    for g in range(len(GIVEN))
    if SURNAMES[s] + GIVEN[g] not in lexicon.TYPE_BY_TERM
]


def _names(start: int, count: int) -> list[str]:
    """名字按 400 个组合的顺序切片，保证关系集/消歧集/别名集**互不重叠**。"""
    return _ALL_NAMES[start : start + count]


def _nick_for(i: int) -> str:
    return "小" + GIVEN[i % len(GIVEN)]


HARD_CASES: list[tuple[str, tuple[str, str, str] | None]] = [
    ("我搬去了成都", ("我", "lives_in", "成都")),
    ("我老家在兰州", ("我", "lives_in", "兰州")),
    ("我在阿里干活", ("我", "works_at", "阿里")),
    ("我平时喜欢喝咖啡", ("我", "likes", "咖啡")),
    ("我喜欢在周末爬山", ("我", "likes", "爬山")),
    ("我在字节跳动上班", ("我", "works_at", "字节跳动")),
    ("我最近在用Notion", ("我", "uses", "Notion")),
]


def hard() -> dict[str, Any]:
    """口语/换说法样本：**不设门**，只用来公开 T0 模板的召回缺口。"""
    memories = [text for text, _ in HARD_CASES]
    gold = [
        [norm_name(item[0]), item[1], norm_name(item[2])]
        for _, item in HARD_CASES
        if item
    ]
    return {"memories": memories, "gold": gold}


def build(seed: int = 7, people: int = 40) -> dict[str, Any]:
    rng = random.Random(seed)
    memories: list[dict[str, Any]] = []
    gold: set[tuple[str, str, str]] = set()
    relation_names = _names(0, people)
    disambig_names = _names(people, 12)
    alias_names = _names(people + 12, 10)

    def add(text: str, *, kind: str = "semantic", provenance: str = "user_explicit") -> None:
        memories.append(
            {
                "text": text,
                "kind": kind,
                "provenance": provenance,
                "session_id": f"net-{len(memories)}",
            }
        )

    for i, name in enumerate(relation_names):
        city = CITIES[i % len(CITIES)]
        org = ORGS[i % len(ORGS)]
        thing = CONCEPTS[i % len(CONCEPTS)]
        other = CONCEPTS[(i * 7 + 3) % len(CONCEPTS)]
        add(f"{name}住在{city}")
        gold.add((norm_name(name), "lives_in", norm_name(city)))
        add(f"{name}在{org}工作")
        gold.add((norm_name(name), "works_at", norm_name(org)))
        add(f"{name}喜欢{thing}")
        gold.add((norm_name(name), "likes", norm_name(thing)))
        if i % 2 == 0:
            add(f"{name}不喜欢{other}")
            gold.add((norm_name(name), "dislikes", norm_name(other)))

        # ---- 干扰项：gold 为空 ----
        add(f"{name}喜欢{thing}吗？")  # 问句
        add(f"他住在{city}")  # 第三人称、无名字
        add(f"{name}喜欢frobnicate{i}")  # 未知宾语 → 不建边
        add("今天天气不错，随便聊聊")  # 无关系流水账

    # ---- 同名不同人（3 个身份 / 名字） ----
    disambig: list[dict[str, Any]] = []
    for i, name in enumerate(disambig_names):
        orgs = [ORGS[(i * 3 + k) % len(ORGS)] for k in range(3)]
        things = [CONCEPTS[(i * 5 + k) % len(CONCEPTS)] for k in range(3)]
        disambig.append({"name": name, "orgs": orgs, "things": things})
        for k in range(3):
            add(f"{name}在{orgs[k]}工作")
            add(f"{name}喜欢{things[k]}")
        # 无上下文提及：应弃权，不产生边
        add(f"{name}最近在考虑换工作")

    # ---- 别名 ----
    aliases: list[dict[str, Any]] = []
    for i, name in enumerate(alias_names):
        nick = _nick_for(i)
        org = ORGS[(i * 11 + 2) % len(ORGS)]
        aliases.append({"name": name, "nick": nick, "org": org})
        add(f"{name}也叫{nick}")
        add(f"{nick}在{org}工作")

    rng.shuffle(memories)
    return {
        "seed": seed,
        "memories": memories,
        "gold": sorted(gold),
        "relation_names": relation_names,
        "disambig": disambig,
        "aliases": aliases,
        "people": people,
    }
