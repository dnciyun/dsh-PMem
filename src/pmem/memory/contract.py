"""证据契约（P2-11 / P2-14）：把「这题要覆盖什么 / 证据够不够 / 风险多高」变成字段。

为什么要有这一层（G0/G1 的教训）
--------------------------------
靶场里 68% 的失败是「证据在上下文里但没用上」；G1 试了四次「再加一段作答纪律」，
四次都更差（-57 / -98 / 外推 -23 / -59）——因为答案一旦变长/变谨慎，判卷就把多出来
的细节按「未知当事实 / 否认证据」扣回去。所以正确形态不是再写 prompt，而是**把
「这题要什么、手上有什么」算成机器可读的字段**，让渲染层/核对层各自只读字段。

本模块只做三件事，且都是确定性的：

1. ``must_cover``：这题按问法需要覆盖哪几类东西（数字/地点/时间/人/多条）；
   判据复用 ``answer_type.kind_of`` 与 ``query.plan`` 的并列信号，**只标注不扣分**；
2. ``answerability``：证据够不够答（answerable / partial / unanswerable），
   由覆盖率 + must_cover 命中 + 调用方给的 ``uncertainty.weak`` 决定；
3. ``cards``：检索条目 → 证据卡（id/text/subject/type/span/via/covers），
   渲染层只读这些键，不再自己解释原始 item。

边界：**纯函数**——不读 DB、不读 cfg、不调模型、0 网络。开关（answer.contract.enabled /
answer.answerability.enabled）由调用方（持有 config 的 sdk/渲染层）决定是否调用本模块、
是否传入 ``answerability_enabled=False``。
"""

from __future__ import annotations

from typing import Any

from . import answer_type
from . import query as query_mod

# 槽位的中文名：``contract.must_cover`` 直接给渲染层 join（单测/文档示例都是中文），
# 机器要读原 id 时用 ``contract.must_cover_slots``（英文槽位，和 answer_type 对齐）。
SLOT_LABELS: dict[str, str] = {
    "number": "数字",
    "place": "地点",
    "time": "时间",
    "person": "人",
    "aggregate": "多条",
}

# 一张证据卡能「覆盖」哪些槽位，按这个顺序检查（与 answer_type.has_evidence 对齐）。
_CARD_TYPES: tuple[str, ...] = ("number", "place", "time", "person")
# 聚合槽位没有单条证据可对应：用「至少 N 条」当确定性代理（不做主体聚类）。
_AGGREGATE_MIN_ITEMS = 3


def _must_cover(question: str, plan: dict[str, Any]) -> list[str]:
    """问法 → 需要覆盖的槽位。只标注，不参与打分。"""
    slots: list[str] = []
    kind = answer_type.kind_of(question)
    if kind in _CARD_TYPES:
        slots.append(kind)
    aggregate = str(plan.get("kind") or "") == "aggregate" or bool(plan.get("aggregate_terms"))
    if aggregate and "aggregate" not in slots:
        slots.append("aggregate")
    return slots


def _card(item: dict[str, Any], must_cover: list[str]) -> dict[str, Any]:
    """一条检索条目 → 证据卡。字段固定：渲染器只读这里，不再看原始 item。"""
    text = str(item.get("text") or "")
    card_type: str | None = None
    covers: list[str] = []
    for kind in _CARD_TYPES:
        if answer_type.has_evidence(text, kind):
            if card_type is None:
                card_type = kind
            if kind in must_cover and kind not in covers:
                covers.append(kind)
    extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
    return {
        "id": str(item.get("id") or ""),
        "text": text,
        "subject": item.get("subject") or extra.get("subject"),
        "type": card_type,
        "span": item.get("span"),
        "via": item.get("via"),
        "covers": covers,
    }


def _coverage(question: str, cards: list[dict[str, Any]]) -> float:
    """问句实词被**最好那条**证据卡覆盖的比例（0~1）。

    复用 ``query.content_terms`` + ``query.coverage_idf``（带 IDF 的覆盖率，
    候选集内越罕见的词越值钱）；不新造相似度函数。
    """
    terms = query_mod.content_terms(question)
    texts = [str(card.get("text") or "") for card in cards if card.get("text")]
    if not terms or not texts:
        return 0.0
    best = 0.0
    for text in texts:
        best = max(best, float(query_mod.coverage_idf(terms, texts, target=text)))
    return round(best, 3)


def _covered_slots(cards: list[dict[str, Any]], must_cover: list[str]) -> set[str]:
    covered: set[str] = set()
    for card in cards:
        covered.update(card.get("covers") or [])
    if "aggregate" in must_cover and len(cards) >= _AGGREGATE_MIN_ITEMS:
        covered.add("aggregate")
    return covered


def _answerability(
    coverage: float,
    missing: list[str],
    must_cover: list[str],
    card_count: int,
    weak: bool,
) -> str:
    """证据够不够答：覆盖率为主（0.75），槽位命中为辅（0.25），三档输出。

    为什么槽位只占 1/4：日期/数字这类「类型命中」太容易了——生日那一条也能让
    「驾照什么时候过期」命中 time 槽。覆盖率（问句实词被证据覆盖多少）才是主信号。
    ``weak`` 只把 answerable 降成 partial，不把 partial 打成 unanswerable（弃答要保守）。
    """
    if card_count <= 0:
        return "unanswerable"
    slot_ratio = (
        (len(must_cover) - len(missing)) / len(must_cover) if must_cover else 0.0
    )
    score = 0.75 * coverage + 0.25 * slot_ratio
    if score >= 0.60:
        level = "answerable"
    elif score >= 0.20:
        level = "partial"
    else:
        level = "unanswerable"
    if weak and level == "answerable":
        level = "partial"
    return level


def _risk(
    coverage: float,
    missing: list[str],
    must_cover: list[str],
    card_count: int,
    weak: bool,
) -> tuple[float, list[str]]:
    """风险 0~1 + 原因码。只做加权和，没有 if 分支决定行为。"""
    risk = 0.0
    reasons: list[str] = []
    if coverage < 0.60:
        risk += 0.50 * (0.60 - coverage) / 0.60
        reasons.append("low_coverage")
    if must_cover:
        ratio = len(missing) / len(must_cover)
        if ratio > 0:
            risk += 0.25 * ratio
            reasons.append("missing_slots")
    if card_count < _AGGREGATE_MIN_ITEMS:
        risk += 0.10
        reasons.append("few_cards")
    if weak:
        risk += 0.15
        reasons.append("uncertain")
    return round(min(1.0, risk), 3), reasons


def build(
    question: str,
    items: list[dict[str, Any]],
    *,
    plan: dict[str, Any] | None = None,
    uncertainty: dict[str, Any] | None = None,
    answerability_enabled: bool = True,
) -> dict[str, Any]:
    """算一份证据契约：``{"contract": {...}, "cards": [...]}``。

    - ``plan``：``query.plan(question)`` 的结果；不传就现算（一次，不重复判断）；
    - ``uncertainty``：检索返回体里的 ``uncertainty``（可选，只读 ``weak``）；
    - ``answerability_enabled``：调用方按 ``answer.answerability.enabled`` 传；
      传 False 时契约里 ``answerability=None``（默认 True 是为了兼容 sdk.py 当前
      「只在开关打开时才调用 build」的接线）。
    """
    text = str(question or "")
    plan = plan if isinstance(plan, dict) else query_mod.plan(text)
    safe_items = [item for item in (items or []) if isinstance(item, dict)]
    must_cover = _must_cover(text, plan)
    cards = [_card(item, must_cover) for item in safe_items]
    if not text.strip():
        # 没有问句（宿主只给了 items）：契约无法判断「够不够答」，返回空契约而不是瞎判。
        return {
            "contract": {
                "kind": "",
                "must_cover": [],
                "must_cover_slots": [],
                "answerability": None,
                "risk": None,
                "reasons": ["no_question"],
                "coverage": 0.0,
            },
            "cards": cards,
        }
    coverage = _coverage(text, cards)
    missing = [slot for slot in must_cover if slot not in _covered_slots(cards, must_cover)]
    weak = bool((uncertainty or {}).get("weak"))
    risk, reasons = _risk(coverage, missing, must_cover, len(cards), weak)
    contract: dict[str, Any] = {
        "kind": str(plan.get("kind") or "lookup"),
        # 渲染层直接 join 这一列（中文，见 docs §4 的示例）；机器读下面那列英文槽位。
        "must_cover": [SLOT_LABELS.get(slot, slot) for slot in must_cover],
        "must_cover_slots": must_cover,
        "answerability": None,
        "risk": risk,
        "reasons": reasons,
        "coverage": coverage,
    }
    if answerability_enabled:
        contract["answerability"] = _answerability(
            coverage, missing, must_cover, len(cards), weak
        )
    return {"contract": contract, "cards": cards}


__all__ = ["build", "SLOT_LABELS"]
