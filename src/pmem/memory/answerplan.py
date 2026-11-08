"""作答计划（AnswerPlan）：把「这题该怎么答」从散文 prompt 变成结构化字段。

为什么要有这个模块
------------------
实测（G1 试点，96 题四臂同判卷）说明：把 4 条「作答纪律」当散文塞进 system prompt，
能把「未知当事实」从 31 题降到 21、通过数 +9；但散文有两个毛病：

1. **没法 A/B**：想单独关掉一条，只能改字符串，改了就说不清是谁的功劳；
2. **没法被消费**：宿主、CLI、MCP 各自拼自己的 prompt，同一件事被解释三遍。

所以这里只做一件事：**一次算清「答什么、不许加什么、日期以哪天为准、手上有什么证据」，
产出一个 dict；渲染交给自己模块里唯一的 `render()`。**

约束（见 docs/roadmap.md §0.2）
------------------------------
- 规则是**数据**（`answer.plan.*` 配置）不是 if 链：加一类「不许补」只是往 suppress 里加一项；
- 问句怎么理解**不在这里重新判断**，复用 `memory.query.content_terms` 和
  `memory.retrieval.timecue`（一次解析，处处消费）；
- 渲染只有一个入口，宿主/MCP/CLI 都调它，不许各自拼字符串。

对外 API
--------
- ``build_plan(store, question, *, when=None, evidences=None) -> dict``
- ``render(plan) -> str``
- ``POLICY_KEYS`` / ``SUPPRESS_LABEL``：给 schema 与文档用的常量
"""

from __future__ import annotations

from typing import Any

from ..base import clock
from . import query as query_mod
from .retrieval import timecue

# 「不许补」的类别。加上一类 = 往配置 suppress 里加一个 code，不新开分支。
SUPPRESS_LABEL: dict[str, str] = {
    "date_without_basis": "没有依据的日期",
    "unsupported_name": "材料里没有的称呼/人名",
    "amount_without_basis": "没有依据的金额和数量",
    "unrelated_past": "和问题无关的旧事",
    "other_private": "别人的私事",
}

POLICY_KEYS: tuple[str, ...] = (
    "enabled",
    "max_sentences",
    "suppress",
    "assert_when_evidenced",
    "allow_creative",
    "date_basis_required",
)

DEFAULT_POLICY: dict[str, Any] = {
    "enabled": True,
    "max_sentences": 2,
    "suppress": ["date_without_basis", "unsupported_name", "unrelated_past", "other_private"],
    "assert_when_evidenced": True,
    "allow_creative": False,
    "date_basis_required": True,
}


def _policy(store: Any) -> dict[str, Any]:
    """配置 → 生效策略。缺键回落到默认值，所以老库/老配置行为不变。"""
    merged = dict(DEFAULT_POLICY)
    cfg = getattr(store, "cfg", None)
    raw = None
    if cfg is not None:
        raw = cfg.get("answer.plan") if hasattr(cfg, "get") else None
    if isinstance(raw, dict):
        for key in POLICY_KEYS:
            if key in raw:
                merged[key] = raw[key]
    # suppress 只认已知 code，未知的原样保留（前向兼容：新 code 旧库也能读到）
    merged["suppress"] = [str(code) for code in (merged.get("suppress") or [])]
    return merged


def _evidence_card(item: dict[str, Any]) -> dict[str, Any]:
    """检索条目 → 证据卡。字段固定，渲染器只读这些键，不再看原始 item。"""
    return {
        "id": str(item.get("id") or ""),
        "day": str(item.get("day") or item.get("valid_from") or "")[:10],
        "role": str(item.get("role") or ""),
        "kind": str(item.get("kind") or ""),
        "confidence": item.get("confidence"),
        "age_days": item.get("age_days"),
        "text": str(item.get("text") or ""),
    }


def rules_for(policy: dict[str, Any]) -> list[dict[str, str]]:
    """策略 → 纪律条目（code + 中文）。渲染器只遍历它，不写死任何一条。"""
    rules: list[dict[str, str]] = []
    suppress = [SUPPRESS_LABEL.get(code, code) for code in policy.get("suppress") or []]
    if policy.get("enabled", True):
        rules.append({"code": "answer_asked_only", "text": "只回答题目问到的点。"})
    if suppress:
        rules.append(
            {
                "code": "no_unsupported_details",
                "text": "不补材料里没有的事实：" + "、".join(suppress) + "。",
            }
        )
    if policy.get("date_basis_required", True):
        rules.append(
            {
                "code": "date_with_basis",
                "text": "相对时间要写成绝对日期，并说明依据哪一天算的；连依据都没有就说没有。",
            }
        )
    if policy.get("assert_when_evidenced", True):
        rules.append(
            {
                "code": "assert_when_evidenced",
                "text": "材料里写清楚的就肯定说；只有材料里没有或前后对不上时才说不知道/不确定。",
            }
        )
    if policy.get("allow_creative", False):
        rules.append(
            {
                "code": "creative_allowed",
                "text": "题目明确要求续写或创作时可以写，但要说明这是编的，不是记忆里的事。",
            }
        )
    rules.append(
        {
            "code": "keep_it_short",
            "text": f"最多 {int(policy.get('max_sentences') or 2)} 句，宁可少说不要多说。",
        }
    )
    return rules


def build_plan(
    store: Any,
    question: str,
    *,
    when: str | None = None,
    evidences: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """算一份作答计划。只读，不写库，不调模型。"""
    policy = _policy(store)
    text = str(question or "")
    terms = query_mod.content_terms(text) if text.strip() else []
    rules = rules_for(policy)
    cards = [_evidence_card(item) for item in (evidences or []) if isinstance(item, dict)]
    basis = str(when or clock.now() or "")
    return {
        "question": text,
        "terms": terms,
        "time_scoped": bool(timecue.is_time_question(text)),
        "date_basis": basis[:10],
        "policy": policy,
        "rules": rules,
        "evidence": cards,
        "evidence_count": len(cards),
        "must_not_add": list(policy.get("suppress") or []),
    }


def render(plan: dict[str, Any]) -> str:
    """唯一的渲染入口：计划 → 给作答方看的那一段。"""
    lines = ["作答纪律："]
    for index, rule in enumerate(plan.get("rules") or [], start=1):
        lines.append(f"{index}. {rule.get('text', '')}")
    basis = str((plan.get("policy") or {}).get("date_basis") or plan.get("date_basis") or "")
    if basis and (plan.get("policy") or {}).get("date_basis_required", True):
        lines.append(f"（相对日期的基准：今天是 {basis}）")
    return "\n".join(lines)


def summarize(plan: dict[str, Any]) -> str:
    """一句话摘要，给控制台/日志用。"""
    policy = plan.get("policy") or {}
    return (
        f"计划：{len(plan.get('rules') or [])} 条纪律、{plan.get('evidence_count', 0)} 条证据、"
        f"日期基准 {plan.get('date_basis') or '-'}、时间题 {'是' if plan.get('time_scoped') else '否'}、"
        f"最多 {int(policy.get('max_sentences') or 2)} 句"
    )
