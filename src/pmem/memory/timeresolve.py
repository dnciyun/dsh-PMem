"""相对日期换算：把记忆里的「上周五 / 下周三 / 三天前」按**这条记忆自己的日期**落成绝对日期。

为什么要有这一层（G3）：
- 靶场里时间类失败只有 14 道，其中「日期不符」5 道——模型拿到了相对说法，但换算错了或不敢算；
- 反过来，把「让模型自己换算」写进提示词（v4b）实测更差：通过题也开始写日期，被判「无证据日期」；
- 所以正确形态是**渲染层换算好**：条目照常注入，只是在后面附一句
  `〔「上周五」=2026-05-01（按记录日 2026-05-03 算）〕`，模型不需要自己算。

口径（实测校准过两条，错了会直接判「编造日期」）：
1. 基准是**这条记忆自己的日期**（``valid_from``/``day``），不是今天——记忆里写的「上周五」
   指的是它被记下那次的上周五；
2. 周起始按**周日**算：中文日历/口语里 2026-05-03（周日）说的「上周五」= 2026-05-01。
   按周一起算会算出 04-24，靶场 P01-NEW-001 就是这么被判错的。
3. 只认**明确**说法；裸的「周三」有歧义（本周还是下周）不换算——宁可少标，不能标错。

纯确定性、零依赖、不调模型。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

_WEEKDAY = re.compile(
    r"(上上周|上周|上星期|上礼拜|这周|本周|这星期|这礼拜|下周|下星期|下礼拜)([一二三四五六日天])"
)
_DAY_WORDS = {"今天": 0, "昨天": -1, "前天": -2, "明天": 1, "后天": 2}
_NUM_TOKEN = r"[0-9一二三四五六七八九十两百]+|半"
_OFFSET = re.compile(rf"({_NUM_TOKEN})\s*(天|日|周|个?月|年)\s*(以前|之前|前|以后|之后|后)")
# 裸「周五/星期三/礼拜天」（核对专用，见 loose_relative_dates）
_BARE_WEEKDAY = re.compile(r"(周|星期|礼拜)([一二三四五六日天])")
# 「上个月24号 / 下个月3日 / 这个月5号」（核对专用）
_MONTH_DAY = re.compile(rf"(上|下|这|本)?个?月\s*({_NUM_TOKEN})\s*[号日]")
# 中文数字绝对日期：二〇二五年十二月二日 / 十二月二号（腊月、廿三不猜——宁可漏报）
_CN_YEAR_TOKEN = r"[〇零一二三四五六七八九]{4}"
_CN_DATE_YMD = re.compile(rf"({_CN_YEAR_TOKEN})\s*年\s*({_NUM_TOKEN})\s*月\s*({_NUM_TOKEN})\s*[日号]")
_CN_DATE_MD = re.compile(rf"({_NUM_TOKEN})\s*月\s*({_NUM_TOKEN})\s*[日号]")
# 周日起算：周日=0 … 周六=6
_WEEK_SUN = {"日": 0, "天": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_int_text(value: int) -> str:
    """12 → 十二、2 → 二、25 → 二十五、10 → 十（给文本匹配生成中文数字用）。"""
    digits = "零一二三四五六七八九"
    value = int(value)
    if value < 10:
        return digits[value]
    if value < 20:
        return "十" + (digits[value - 10] if value > 10 else "")
    if value < 100:
        return digits[value // 10] + "十" + (digits[value % 10] if value % 10 else "")
    return str(value)


def cn_date_hits(text: str) -> list[dict[str, object]]:
    """从文本里抽中文数字绝对日期（核对用）：带年份的优先占位，再抽月日。

    只做「能确定性解析」的写法：二〇二五年十二月二日 / 十二月二号 / 12月2号之外的
    中文月日。腊月、廿三、初一这类（农历/无对应公历）**不猜**——宁可漏报。
    返回结构与 ``selfcheck._extract_dates`` 的 hit 一致：span/start/year/month/day。
    """
    hits: list[dict[str, object]] = []
    occupied: list[tuple[int, int]] = []

    def overlaps(span: tuple[int, int]) -> bool:
        return any(span[0] < end and start < span[1] for start, end in occupied)

    for match in _CN_DATE_YMD.finditer(text or ""):
        span = match.span()
        if overlaps(span):
            continue
        year_digits = "".join(str(_CN_DIGITS.get(ch, ch)) for ch in match.group(1).replace("〇", "零"))
        month = _parse_count(match.group(2))
        day = _parse_count(match.group(3))
        if not year_digits.isdigit() or month is None or day is None:
            continue
        if not (1 <= int(month) <= 12 and 1 <= int(day) <= 31):
            continue
        occupied.append(span)
        hits.append({"span": match.group(0), "start": span[0], "year": int(year_digits),
                     "month": int(month), "day": int(day)})
    for match in _CN_DATE_MD.finditer(text or ""):
        span = match.span()
        if overlaps(span):
            continue
        month = _parse_count(match.group(1))
        day = _parse_count(match.group(2))
        if month is None or day is None or int(month) != month or int(day) != day:
            continue
        if not (1 <= int(month) <= 12 and 1 <= int(day) <= 31):
            continue
        occupied.append(span)
        hits.append({"span": match.group(0), "start": span[0], "year": None,
                     "month": int(month), "day": int(day)})
    hits.sort(key=lambda item: item["start"])
    return hits


def _as_date(value: str | None) -> date | None:
    try:
        return datetime.fromisoformat(str(value or "")[:10]).date()
    except ValueError:
        return None


def _parse_count(token: str) -> float | None:
    """「3 / 三 / 十二 / 二十五 / 半」→ 数字；不懂的说法返回 None（宁缺勿错）。"""
    if token == "半":
        return 0.5
    if token.isdigit():
        return float(token)
    if "十" not in token:
        return float(_CN_DIGITS[token]) if token in _CN_DIGITS else None
    left, _, right = token.partition("十")
    tens = 1 if not left else _CN_DIGITS.get(left)
    ones = 0 if not right else _CN_DIGITS.get(right)
    if tens is None or ones is None:
        return None
    return float(tens * 10 + ones)


def _shift_months(day: date, months: int) -> date:
    year = day.year + (day.month - 1 + months) // 12
    month = (day.month - 1 + months) % 12 + 1
    for candidate in (day.day, 30, 29, 28):
        try:
            return day.replace(year=year, month=month, day=candidate)
        except ValueError:
            continue
    return day


def relative_dates(text: str, base_iso: str | None) -> list[tuple[str, str]]:
    """返回 [(说法, 绝对日期 ISO)]；换不出来的说法不出现。

    只认明确说法（裸「周三」有歧义，不在这里换算——渲染注脚要保持宁少勿错）；
    核对链路要更宽松，用 ``loose_relative_dates``。
    """
    return _resolve_relative(text, base_iso, loose=False)


def loose_relative_dates(text: str, base_iso: str | None) -> list[tuple[str, str]]:
    """核对专用（task-36）：在 ``relative_dates`` 基础上补两类可推导说法。

    - 裸「周五 / 星期三 / 礼拜天」：按「本周内还没到的那天」推（若本周已过则跳过，
      因为「周三」也可能是下周三，歧义时宁可漏报）；
    - 「上个月24号 / 下个月3日 / 这个月5号」：按基准日期做月位移。

    只给核对/取证用，不改渲染注脚（``annotate`` 仍用 ``relative_dates``）。
    """
    return _resolve_relative(text, base_iso, loose=True)


def _resolve_relative(text: str, base_iso: str | None, *, loose: bool) -> list[tuple[str, str]]:
    base = _as_date(base_iso)
    if not base or not text:
        return []
    found: dict[str, str] = {}

    def add(phrase: str, day: date) -> None:
        if phrase and phrase not in found:
            found[phrase] = day.isoformat()

    for word, delta in _DAY_WORDS.items():
        if word in text:
            add(word, base + timedelta(days=delta))
    week_start = base - timedelta(days=(base.weekday() + 1) % 7)
    for match in _WEEKDAY.finditer(text):
        prefix, char = match.group(1), match.group(2)
        if prefix == "上上周":
            start = week_start - timedelta(days=14)
        elif prefix in ("上周", "上星期", "上礼拜"):
            start = week_start - timedelta(days=7)
        elif prefix in ("下周", "下星期", "下礼拜"):
            start = week_start + timedelta(days=7)
        else:
            start = week_start
        add(match.group(0), start + timedelta(days=_WEEK_SUN[char]))
    if loose:
        # 裸周X：本周内还没到的那个周X（已过则不推——可能是下周，歧义宁漏报）。
        prefixed = [item.span() for item in _WEEKDAY.finditer(text)]
        for match in _BARE_WEEKDAY.finditer(text):
            if any(match.start() < end and start < match.end() for start, end in prefixed):
                continue
            day = week_start + timedelta(days=_WEEK_SUN[match.group(2)])
            if day >= base:
                add(match.group(0), day)
        # 上个月24号 / 下个月3日 / 这个月5号：月位移 + 日。
        for match in _MONTH_DAY.finditer(text):
            amount = {"上": -1, "下": 1}.get(match.group(1) or "", 0)
            day_number = _parse_count(match.group(2))
            if day_number is None or day_number != int(day_number):
                continue
            shifted = _shift_months(base, amount)
            try:
                add(match.group(0), shifted.replace(day=int(day_number)))
            except ValueError:
                continue
    for match in _OFFSET.finditer(text):
        count = _parse_count(match.group(1))
        if count is None:
            continue
        sign = -1 if match.group(3) in ("以前", "之前", "前") else 1
        amount = sign * count
        unit = match.group(2)
        if "天" in unit or "日" in unit:
            day = base + timedelta(days=amount)
        elif "周" in unit:
            day = base + timedelta(days=amount * 7)
        elif "月" in unit:
            day = _shift_months(base, int(amount)) if amount == int(amount) else base + timedelta(days=int(amount * 30))
        else:
            day = base.replace(year=base.year + int(amount)) if amount == int(amount) else _shift_months(base, int(amount * 12))
        add(match.group(0), day)
    return list(found.items())


def annotate(text: str, base_iso: str | None, *, limit: int = 4) -> str:
    """给一条记忆补时间换算注脚；没有可换算的说法就返回空串。"""
    pairs = relative_dates(text, base_iso)[:limit]
    if not pairs:
        return ""
    body = "；".join(f"「{phrase}」={stamp}" for phrase, stamp in pairs)
    return f"〔{body}〕"
