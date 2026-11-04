"""合成评测集（M2）：带真值标签的场景生成器。

为什么要自己造题：真实会话没有标注，而 `1.md` 第六章要求"进化效果必须通过测试衡量"。
先有一套可复现、带 ground truth 的合成题把算法调到及格线，再用真实数据看漂移。

规模：**50 个事实组 × 4 种说法 = 200 条记忆 / 300 对重复**，外加冲突、时间替代、
否定、干扰项，以及两类查询（词法可命中 / 只靠语义）。

五类样本：

1. **近重复**：同一事实 4 种说法（判定为重复）；每组独立持有人，避免与冲突组串味。
2. **槽位冲突**：功能性谓词（住哪/在哪工作）给出不同值，时间窗重叠；**同桶内两两都标**。
3. **否定冲突**：同一对象"喜欢 / 不喜欢"。
4. **时间替代**：不同值但时间窗不重叠（**不是**冲突，专测误报）。
5. **干扰项**：多值谓词（喜欢什么/用什么）与流水账，专测误召回。
"""

from __future__ import annotations

import itertools
import random
from typing import Any

CITIES = [
    "苏州", "武汉", "西安", "重庆", "南京", "合肥", "厦门", "泉州", "长沙", "郑州",
    "青岛", "大连", "昆明", "贵阳", "兰州", "银川", "海口", "三亚", "洛阳", "开封",
]
COMPANIES = [
    "字节", "小米", "美团", "京东", "网易", "携程", "蔚来", "大疆", "商汤", "旷视",
    "小红书", "知乎", "豆瓣", "迅雷", "同程",
]
THINGS = [
    "咖啡", "拉面", "徒步", "露营", "爬山", "骑行", "摄影", "围棋", "游泳", "滑雪",
    "吉他", "书法", "养鱼", "种花", "烘焙",
]
TOOLS = [
    "Python", "Rust", "Figma", "Notion", "Obsidian", "Vim", "Emacs", "Docker",
    "Kubernetes", "Postgres", "Redis", "Neovim", "Zed", "Raycast", "Alfred",
]


def _lives_variants(name: str, city: str) -> list[str]:
    return [
        f"{name}住在{city}",
        f"{name}现在住在{city}",
        f"{name}一直住在{city}",
        f"{name}住在{city}，已经很多年了",
    ]


def _works_variants(name: str, company: str) -> list[str]:
    return [
        f"{name}在{company}工作",
        f"{name}现在在{company}工作",
        f"{name}在{company}上班",
        f"{name}在{company}工作，挺久了",
    ]


def _likes_variants(name: str, thing: str) -> list[str]:
    return [
        f"{name}喜欢{thing}",
        f"{name}挺喜欢{thing}的",
        f"{name}特别喜欢{thing}",
        f"{name}喜欢{thing}，喜欢很多年了",
    ]


def _uses_variants(name: str, tool: str) -> list[str]:
    return [
        f"{name}用{tool}",
        f"{name}平时用{tool}",
        f"{name}这个月一直在用{tool}",
        f"{name}用{tool}，用得很熟",
    ]


def build(seed: int = 7, max_groups: int | None = None) -> dict[str, Any]:
    """``max_groups`` 用于测试里的小规模跑（默认全量 65 组 / 300+ 对）。"""
    rng = random.Random(seed)
    budget = max_groups

    def take(items: list[str]) -> list[str]:
        nonlocal budget
        if budget is None:
            return items
        chosen = items[: max(0, budget)]
        budget -= len(chosen)
        return chosen

    cities = take(CITIES)
    companies = take(COMPANIES)
    things = take(THINGS)
    tools = take(TOOLS)

    memories: list[dict[str, Any]] = []
    dup_pairs: list[tuple[str, str]] = []
    dup_pairs_hard: list[tuple[str, str]] = []
    conflict_pairs: list[tuple[str, str, str]] = []
    temporal_pairs: list[tuple[str, str]] = []
    queries: list[dict[str, Any]] = []

    def add(
        text: str,
        *,
        kind: str = "semantic",
        group: str,
        session: str,
        valid_from: str | None = None,
        valid_to: str | None = None,
        provenance: str = "user_explicit",
    ) -> str:
        key = f"m{len(memories)}"
        memories.append(
            {
                "key": key,
                "text": text,
                "kind": kind,
                "group": group,
                "session_id": session,
                "valid_from": valid_from,
                "valid_to": valid_to,
                "provenance": provenance,
            }
        )
        return key

    def add_group(variants: list[str], group: str) -> list[str]:
        keys = [add(text, group=group, session=group) for text in variants]
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                dup_pairs.append((keys[i], keys[j]))
        return keys

    # 1) 近重复：50 组 × 4 种说法 = 200 条 / 300 对
    group_index = 0
    for city in cities:
        name = f"成员{group_index}"
        keys = add_group(_lives_variants(name, city), f"dup-lives-{city}")
        queries.append({"query": city, "gold": keys, "kind": "lexical", "note": f"lives-{city}"})
        group_index += 1
    for company in companies:
        name = f"成员{group_index}"
        keys = add_group(_works_variants(name, company), f"dup-works-{company}")
        queries.append({"query": company, "gold": keys, "kind": "lexical", "note": f"works-{company}"})
        group_index += 1
    for thing in things:
        name = f"成员{group_index}"
        keys = add_group(_likes_variants(name, thing), f"dup-likes-{thing}")
        queries.append({"query": thing, "gold": keys, "kind": "lexical", "note": f"likes-{thing}"})
        group_index += 1
    for tool in tools:
        name = f"成员{group_index}"
        keys = add_group(_uses_variants(name, tool), f"dup-uses-{tool}")
        queries.append({"query": tool, "gold": keys, "kind": "lexical", "note": f"uses-{tool}"})
        group_index += 1

    # 2) 困难重复：换说法且模板不命中（规则法必然漏，用来量化"向量缺口"）
    hard_groups = [
        ("小印住在厦门", "小印家在厦门"),
        ("小印喜欢徒步", "小印爱好徒步"),
        ("小印在阿里工作", "小印就职于阿里"),
        ("小印用 Figma", "小印画图用 Figma"),
        ("小印讨厌香菜", "小印不吃香菜"),
        ("小印养了一只猫", "小印家有只猫"),
    ]
    for index, (left_text, right_text) in enumerate(hard_groups):
        group = f"hard-{index}"
        left = add(left_text, group=group, session=group)
        right = add(right_text, group=group, session=group)
        dup_pairs_hard.append((left, right))

    # 3) 槽位冲突：同桶内两两标注（漏标会让 recall 失真）。
    # 注意：detect 按 (holder, predicate) 分桶，所以同一个持有人 + 同一个功能性谓词的
    # 所有记忆必须在同一个桶里一次性把标签标全，否则会被算成误报。
    buckets: list[list[str]] = [
        ["我住在杭州", "我住在上海", "我住在北京", "我住在成都", "我住在深圳", "我住在广州", "我住在佛山"],
        ["我在腾讯工作", "我在阿里工作"],
        ["小林住在南京", "小林住在合肥"],
        ["阿哲在字节工作", "阿哲在美团工作"],
    ]
    for index, texts in enumerate(buckets):
        group = f"conflict-{index}"
        keys = [add(text, group=group, session=group) for text in texts]
        for left, right in itertools.combinations(keys, 2):
            conflict_pairs.append((left, right, "slot"))

    # 4) 否定冲突
    for thing in ("喝酒", "熬夜", "跑步", "打游戏"):
        group = f"neg-{thing}"
        left = add(f"我喜欢{thing}", group=group, session=group)
        right = add(f"我不喜欢{thing}", group=group, session=group)
        conflict_pairs.append((left, right, "negation"))

    # 5) 时间替代（负样本：不许判成冲突）
    group = "temporal-laozhou"
    old = add(
        "老周住在南京",
        group=group,
        session=group,
        valid_from="2020-01-01T00:00:00.000Z",
        valid_to="2025-12-31T00:00:00.000Z",
    )
    new = add(
        "老周住在广州",
        group=group,
        session=group,
        valid_from="2026-01-01T00:00:00.000Z",
    )
    temporal_pairs.append((old, new))

    # 6) 干扰项：多值谓词与流水账（同时喜欢火锅和烧烤不是冲突）
    distractors = [
        "我用 Excel 记一点账",
        "我喜欢火锅",
        "我喜欢烧烤",
        "我平时用 Notion 写笔记",
        "今天开了一下午会",
        "昨天把数据库迁移做完了",
        "最近在读一本讲记忆的书",
        "周末去了趟城郊",
        "我把日志轮转配置改了",
        "这个月的目标是把测试覆盖率提上去",
        "我和阿哲昨天聊到很晚",
        "小林推荐了一家面馆",
        "我在整理旧照片",
        "我把 curl 换成了 httpie",
        "我打算明年学一点 R 语言",
    ]
    for text in distractors:
        add(text, kind="episodic", group="distractor", session="distractor")

    # 7) 纯语义查询：一个原词都不共享
    semantic_queries = [
        ("家安在哪", [f"dup-lives-{city}" for city in cities]),
        ("公司是哪家", [f"dup-works-{company}" for company in companies]),
        ("平时喝什么", ["dup-likes-咖啡"]),
    ]
    for query, groups in semantic_queries:
        gold = [m["key"] for m in memories if m["group"] in groups]
        queries.append({"query": query, "gold": gold, "kind": "paraphrase", "note": ",".join(groups)})

    rng.shuffle(memories)  # 打乱注入顺序，避免"正好按顺序"的假象
    return {
        "seed": seed,
        "memories": memories,
        "dup_pairs": dup_pairs,
        "dup_pairs_hard": dup_pairs_hard,
        "conflict_pairs": conflict_pairs,
        "temporal_pairs": temporal_pairs,
        "queries": queries,
    }
