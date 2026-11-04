"""时效语义（M14 / 0.14.0）：让「后来怎么样/改成什么/最后定了」这类问题回答**新说法**。

**要解决的问题**：90 天语料 35 道 update 题里漏 6 道，而且好几道是「旧说法排第 1、
新说法根本不进候选」。原因有三层：

1. **时间轴用错了**。每条记忆有两根时间：``created_at``（这行什么时候写进库）和
   ``valid_from``（这件事在现实里什么时候成立）。检索打分一直用 ``created_at``——
   回填、导入、日记派生出来的旧事实全都「刚刚发生」，把真正的新说法挤下去。
   打分必须用现实时间轴。
2. **旧说法是路标，不是答案**。用户问「原来那套后来怎么样了」时，词法命中的是**旧说法**；
   而旧说法已经被 ``superseded_by`` 链到新说法（状态 ``superseded``，不进候选池）。
   顺着链走一步，才是他真正想要的。
3. **「新」本身是信息**。更新类问句（后来/最后/改成/换成/现在/最终/还…吗）里，
   同等相关时新说法该排前面；反过来，旧说法（「本来想」「原来打算」）不该出现在前排。

三条实现原则（对齐 PLAN §1）：

- **不改原文**：链是读 ``superseded_by`` / ``supersedes_id`` 现成的列，不写任何东西；
- **派生可解释**：每个补进来的条目都带 ``via="supersede"`` 和 ``from_seq``，能说清
  「因为这条旧说法命中，所以补了它的替代版本」，不做黑箱重排；
- **可关**：``retrieval.temporal.enabled = false`` 回到纯词法 + 向量。
"""

from __future__ import annotations

import math
import re
from datetime import date
from typing import Any, Iterable

from ..base.clock import days_between, now, parse_iso

# 更新类问句的标志词。选词标准：日常中文里**几乎只**在「问变化」时出现的说法，
# 不选「现在」这种到处都有的词（「我现在在写代码」不是更新题）。
UPDATE_MARKERS = (
    "后来",
    "最后",
    "改成",
    "换成",
    "换到",
    "改编",
    "改学",
    "不再",
    "还是",
    "最终",
    "到底",
    "原来",
    "本来",
    "坚持下来",
    "之后",
    "现在",
    "啥了",
    "怎么定",
    "怎么样了",
)

# 旧说法标志：候选文本里出现这些词时，它更可能是「已经被替代的那个版本」。
STALE_MARKERS = (
    "本来",
    "原来",
    "原计划",
    "打算",
    "本来想",
    "当时想",
    "一开始",
    "起初",
    "还在犹豫",
    "没想好",
)


# 「问过去」的标志词：这些句子里，过期的事实可能正是答案，不该按「已失效」降权。
PAST_MARKERS = (
    "上个月",
    "上礼拜",
    "上个星期",
    "去年",
    "当年",
    "当时",
    "那时候",
    "那会儿",
    "以前",
    "之前",
    "从前",
    "小时候",
    "那年",
    "曾经",
    "原来",
    "本来",
)

# 「问将来」的标志词：未生效的计划在这里才是答案。
FUTURE_MARKERS = (
    "下周",
    "下礼拜",
    "下个月",
    "下次",
    "明天",
    "后天",
    "将来",
    "以后",
    "未来",
    "计划",
    "打算",
    "接下来",
    "准备",
)


# 「问最早/第一次」的标志词：这些句子问的是**旧的那件事**，新近度该反过来用。
# 和 PAST_MARKERS 的区别：PAST_MARKERS 是「允许过期事实出现」，
# OLD_MARKERS 是「把老事实往前排」（第一次修车、当初的方案、最早那版）。
OLD_MARKERS = (
    "第一次",
    "头一回",
    "头一次",
    "头一遭",
    "当初",
    "一开始",
    "起初",
    "最早",
    "最初",
    "原先",
    "最开始",
)


# 「问一段时间」的标志词（M18）。这类问句问的不是某条事实，而是**那几天发生了什么**，
# 答案的粒度是日记（一天一篇的叙述），不是记忆单元——所以它触发的是日记通道。
# 表里是（词形, 往回看几天, 标签），顺序即优先级：越具体越先匹配
# （「上周末」不能被「周末」抢先，「上个月」也不能落进「上周」）。
RANGE_MARKERS: tuple[tuple[tuple[str, ...], int, str], ...] = (
    (("今天", "今晚", "今早"), 1, "今天"),
    (("昨天", "昨晚", "昨儿"), 2, "昨天"),
    (("前天",), 3, "前天"),
    (("上周末",), 10, "上周末"),
    (("周末",), 8, "周末"),
    (("前几天", "前两天", "前些天"), 6, "前几天"),
    (("这几天", "这两天", "这两天"), 6, "这几天"),
    (("最近几天", "最近一阵", "最近"), 10, "最近"),
    (("这段时间", "这段"), 10, "这段时间"),
    (("上周", "上星期", "上个星期", "上礼拜"), 14, "上周"),
    (("上个月", "上月"), 32, "上个月"),
)

# 只有带上这类「问经过」的词，时间范围问句才真的在问那几天发生了什么。
# 反例：「上个月体检结果怎么样」问的是一条事实，不该把日记顶到前面去。
EVENT_MARKERS = (
    "干嘛",
    "干什么",
    "做什么",
    "做了啥",
    "做了什么",
    "干了啥",
    "干了什么",
    "发生了",
    "发生什么",
    "聊了",
    "聊啥",
    "聊什么",
    "忙什么",
    "忙啥",
    "过得",
    "都在",
    "都干",
    "都有啥",
    "经历了",
)


_DATE_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[号日]")


def _explicit_day(text: str) -> tuple[str, str] | None:
    """「11月3号」这种写明日期的问法 → (ISO 日期, 给人看的标签)。

    年份按「最近的一次」推：说的是将来的月份（现在 11 月、问 12 月）就算去年的，
    因为人回忆「那天」几乎总是在往回看。日期非法（2 月 30 号）就当没写。
    """
    match = _DATE_RE.search(text)
    if not match:
        return None
    month, day = int(match.group(1)), int(match.group(2))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    today = now().date()
    try:
        candidate = date(today.year, month, day)
    except ValueError:
        return None
    if candidate > today:
        try:
            candidate = date(today.year - 1, month, day)
        except ValueError:
            return None
    return candidate.isoformat(), f"{month}月{day}号"


def range_window(query: str) -> dict[str, Any] | None:
    """问句里有没有一段时间。「上周咱们都干嘛了」返回 ``{"days":14,"label":"上周"}``。

    两种形状：写明日期的（``{"day":"2026-11-03", ...}``）和相对时段的（``{"days":14}``）。
    只做词面判断：这些时段词在中文日常里几乎不会用来问别的事，
    误判的代价也只是日记多占一两条候选，不改变前面的排序（见 store 的用法）。
    """
    text = str(query or "")
    if not text:
        return None
    explicit = _explicit_day(text)
    if explicit is not None:
        day, label = explicit
        return {"days": 0, "day": day, "label": label}
    for words, days, label in RANGE_MARKERS:
        if any(word in text for word in words):
            return {"days": int(days), "label": label}
    return None


def asks_for_events(query: str) -> bool:
    """问句是不是在问「那几天干了什么」（而不是问某条事实的值）。"""
    text = str(query or "")
    return any(marker in text for marker in EVENT_MARKERS)


def is_old_query(query: str) -> bool:
    """问句是不是在问「最早/第一次」——这时越老的事实越可能是答案。"""
    text = str(query or "")
    return any(marker in text for marker in OLD_MARKERS)


def is_past_query(query: str) -> bool:
    text = str(query or "")
    return any(marker in text for marker in PAST_MARKERS)


def is_future_query(query: str) -> bool:
    text = str(query or "")
    return any(marker in text for marker in FUTURE_MARKERS)


def expired(row: Any, *, grace_days: float = 0.0) -> bool:
    """这条事实**已经失效**：``valid_to`` 早于现在（可留宽限期）。"""
    try:
        stamp = row["valid_to"]
    except (KeyError, IndexError, TypeError):
        return False
    if not stamp:
        return False
    return days_between(str(stamp)) > float(grace_days)


def pending(row: Any, *, grace_days: float = 0.0) -> bool:
    """这条事实**还没生效**：``valid_from`` 在未来（可留宽限期）。

    注意不能用 ``days_between``：它把负天数截成 0（recency 的需要），
    在这里会把「未来 10 天」和「刚刚」算成一样。
    """
    try:
        stamp = row["valid_from"]
    except (KeyError, IndexError, TypeError):
        return False
    if not stamp:
        return False
    remaining = (parse_iso(str(stamp)) - now()).total_seconds() / 86400.0
    return remaining > float(grace_days)


def settings(cfg: Any) -> dict[str, Any]:
    """取 ``retrieval.temporal`` 那一格；配置缺失时按默认开（和内置默认一致）。"""
    value = cfg.get("retrieval.temporal", {}) if cfg is not None else {}
    return value if isinstance(value, dict) else {}


def is_update_query(query: str) -> bool:
    """问句是不是在问「变化/最新状态」。"""
    text = str(query or "")
    return any(marker in text for marker in UPDATE_MARKERS)


def looks_stale(text: str) -> bool:
    """这条候选读起来像不像「旧说法」。"""
    body = str(text or "")
    return any(marker in body for marker in STALE_MARKERS)


def freshness(
    row: Any,
    *,
    half_life_days: float = 30.0,
    fallback_key: str = "created_at",
) -> float:
    """现实时间轴上的「有多新」（0..1）。

    ``valid_from`` 缺失时退回 ``created_at``；未来生效（计划类）按「最新」算。
    半衰期默认 30 天：更新类问句关心的通常是「最近变没变」，不是「今年有没有」。
    """
    if half_life_days <= 0:
        return 1.0
    try:
        stamp = row["valid_from"] if row is not None else None
    except (KeyError, IndexError, TypeError):
        stamp = None
    if not stamp:
        try:
            stamp = row[fallback_key]
        except (KeyError, IndexError, TypeError):
            stamp = None
    if not stamp:
        return 1.0
    return math.exp(-math.log(2) * max(0.0, days_between(stamp)) / half_life_days)


def real_axis(row: Any, *, fallback_key: str = "created_at") -> str:
    """打分用的时间戳：``valid_from`` 优先，没有就退回写入时间（两根轴不混用）。"""
    try:
        stamp = row["valid_from"]
    except (KeyError, IndexError, TypeError):
        stamp = None
    if stamp:
        return str(stamp)
    try:
        return str(row[fallback_key] or "")
    except (KeyError, IndexError, TypeError):
        return ""


def chain_replacements(
    conn: Any,
    seqs: Iterable[int],
    *,
    subject: str = "self",
    hops: int = 3,
    max_old: int = 80,
) -> dict[int, tuple[int, int]]:
    """旧说法 seq → (替代版本 seq, 走了几跳)。

    只认**已经连好的链**（``superseded_by``）。链可能不止一跳（参数第一版→第二版→第三版），
    所以沿链最多走 ``hops`` 步，返回链尾那个还在用的版本。
    """
    old_seqs = [int(seq) for seq in seqs][: max(1, int(max_old))]
    if not old_seqs:
        return {}
    marks = ",".join("?" for _ in old_seqs)
    rows = conn.execute(
        f"SELECT seq, id, superseded_by FROM memory "
        f"WHERE seq IN ({marks}) AND subject_id=? AND superseded_by IS NOT NULL",
        (*old_seqs, subject),
    ).fetchall()
    if not rows:
        return {}
    out: dict[int, tuple[int, int]] = {}
    for row in rows:
        old_seq = int(row["seq"])
        target = str(row["superseded_by"] or "")
        used = 0
        while target and used < max(1, int(hops)):
            nxt = conn.execute(
                "SELECT seq, superseded_by, status FROM memory WHERE id=?",
                (target,),
            ).fetchone()
            used += 1
            if nxt is None or nxt["status"] == "deleted":
                target = ""
                break
            if not nxt["superseded_by"]:
                out[old_seq] = (int(nxt["seq"]), used)
                break
            target = str(nxt["superseded_by"])
    return out


def chain_score(rank: int, *, base: float = 0.72, step: float = 0.02, floor: float = 0.55) -> float:
    """旧说法在词法里越靠前，它的替代版本越可能就是答案。

    第 1 名 → 0.70，第 5 名 → 0.62，第 10 名 → 0.54（压到 floor）。
    """
    return max(float(floor), float(base) - float(step) * max(0, int(rank) - 1))
