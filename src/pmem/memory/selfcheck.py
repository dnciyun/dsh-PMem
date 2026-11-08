"""作答自检：回答写完之后，回库里核对它写的**日期**和**专名**有没有来源。

要解决的问题：回答里最容易出错、又最难被读者发现的，是具体的日期和人名——
「上周三」说成「上周四」、「李静」说成「李婷」，整句话读起来都通顺，只有回到库里
才能看出它没有来源。所以自检不评判观点、不判断相关性，只做两件确定性的事：

1. **日期**：答案里写了 ``2026-11-05`` / ``2026年11月5日`` / ``11月5日`` / ``11月5号``，
   就回 ``memory``（``day_key`` / ``valid_from``）和 ``journal_line``（``ts``）里找
   同一天的证据；找不到 → ``kind="date"``。没写年份的按 ``base.clock`` 的「今天」
   推到**最近的一次**（和 ``temporal.anchor`` 同一口径），这样当天的、未来的日期
   也能被判。日期本身不存在（``2月30日``）直接算问题。
2. **专名**：本主体 ``entity`` 表里的名字（含别名）在答案里出现就算有据；答案里另外
   出现了「像人名」的字串（姓氏表 + 2–4 字 + 不是虚词/亲属词结尾），而实体表和
   全部原文（``memory`` / ``journal_line``）里都找不到它 → ``kind="name"``。
   **宁可少报**：出现在问句里的（那是用户说的）、库里任何一行原文里出现过的、
   命中常见词表的，一律不报。

三条纪律（对齐 PLAN §1）：

- **只读**：只发 ``SELECT``，不写库、不写事件，也不改答案；
- **零模型**：不调用 LLM，同一份输入永远得到同一份结论；
- **不改答案**：只产出 issue 清单，要不要改、怎么改由调用方决定。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from ..base import clock
from . import timeresolve

# 下面是「配置缺失/调用方没传」时的兜底默认值：真正生效的阈值优先读
# ``selfcheck.*``（和 config.py 里的默认值同口径），显式传参再覆盖配置。
# 答案进自检前先截断：4000 字已经远超一次回答的常见长度，再长多半是粘了一整篇文档。
MAX_ANSWER_CHARS = 4000
# 单条 issue 最多带几条证据摘要（再多读者也不会看）。
MAX_EVIDENCE = 3
MAX_DATES = 40
MAX_NAMES = 20


def _cfg_int(cfg: Any, key: str, default: int) -> int:
    """读一个正整数阈值；缺失/坏值/<=0 都退回默认（配置写坏了不能把自检弄挂）。"""
    raw = cfg.get(key, default) if cfg is not None else default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return int(default)
    return value if value > 0 else int(default)

# 2026-11-05 / 2026/11/5 / 2026.11.5
_DATE_YMD_DASH = re.compile(r"(?<!\d)(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})(?!\d)")
# 2026年11月5日 / 2026年11月5号
_DATE_YMD_CN = re.compile(r"(?<!\d)(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
# 11月5日 / 11月5号（没写年份）
_DATE_MD_CN = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")

# 人名候选只在连续汉字里扫：数字、标点、英文都是天然的分隔符。
_CN_RUN = re.compile(r"[\u4e00-\u9fff]+")
# 复姓：匹配时按两个字处理，候选总长仍然是 3–4 字。
_COMPOUND_SURNAMES = frozenset(
    (
        "欧阳", "司马", "诸葛", "上官", "东方", "皇甫", "尉迟", "公孙",
        "慕容", "司徒", "令狐", "宇文", "长孙", "独孤", "南宫", "西门", "夏侯",
    )
)
# 亲属词结尾：那是「王子轩爸妈」里的称呼，不是名字的一部分。
_KIN_TAIL = set("爸妈爷奶哥姐弟妹儿女媳婆叔姨舅姑嫂侄甥夫")
# 名字（姓氏之后那部分）里不会出现的字：虚词、代词、动词、时间词。
# 「周和李婷」这种在「周」后面紧跟「和」的，不该被当成一个 4 字名字；
# 代价是「李和平」这类带实词的名字会漏——宁可少报。
_GIVEN_STOP = set(
    "的了着过吗呢吧啊哦呀嘛是在有和与跟把被让给对为于从向到说问想要会能个之其们多都也"
    "才就但可又还去来住觉得认识做写看听笑哭骂答我你他她它这那哪每各某上下里中前后时"
    "年月日号点次回条只件位岁天昨今早晚午地得打做干玩当"
)
# 常见词表：这些词以姓氏字开头、但日常不是在叫人。宁可漏报，不要每次回答都报「高兴」。
# 真正兜底的是 ``_name_in_library``（原文里出现过的名字一律不报），这张表只拦最常见的。
_COMMON_WORDS = frozenset(
    (
        "马上", "马车", "马虎", "白天", "白色", "白酒", "白发", "高兴", "高度", "高级",
        "高速", "高手", "高价", "高档", "高层", "高峰", "高温", "方向", "方面", "方式",
        "方案", "方法", "方便", "方才", "方圆", "方块", "于是", "余下", "余额", "余年",
        "任何", "任务", "任职", "任性", "何时", "何况", "何处", "程度", "程序", "华语",
        "华丽", "华人", "陈述", "陈列", "毛病", "孔子", "孔雀", "孙子", "王国", "王子",
        "家具", "康复", "严重", "严格", "武力", "武器", "武侠", "段落", "雷同", "雷达",
        "龙头", "史上", "史前", "陶瓷", "顾客", "顾虑", "顾问", "钱包", "万一", "万分",
        "向上", "向下", "向来", "汤圆", "黄金", "黄色", "黄昏", "周末", "周围", "周边",
        "周年", "周期", "周报", "周到", "江湖", "江苏", "江西", "江山", "石头", "石油",
        "叶子", "叶片", "夏天", "夏季", "夏日", "李子", "张开", "张望", "迟迟", "许久",
        "许可", "许多", "床边", "房中", "内部", "向外", "林间", "林中", "钟头", "钟表",
        "陆续", "金融", "金钱", "金属", "金色", "谢谢", "曾经", "田野", "田间", "田地",
        "杜绝", "苏醒", "苏州", "丁香", "沈阳", "汪汪", "范围", "范例", "付出", "付费",
        "熊猫", "黎明", "郑重", "胡子", "胡同", "罗列", "莫非", "莫大",
    )
)


# ----------------------------------------------------------------- 日期


def _overlaps(span: tuple[int, int], occupied: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(start < other_end and other_start < end for other_start, other_end in occupied)


def _extract_dates(text: str, *, today: date, limit: int) -> list[dict[str, Any]]:
    """抽答案里的日期写法，按出现顺序返回（带年份的优先占位，避免重复抽到月日）。"""
    hits: list[dict[str, Any]] = []
    occupied: list[tuple[int, int]] = []
    for pattern in (_DATE_YMD_DASH, _DATE_YMD_CN):
        for match in pattern.finditer(text):
            span = match.span()
            if _overlaps(span, occupied):
                continue
            occupied.append(span)
            hits.append(
                {
                    "span": match.group(0),
                    "start": span[0],
                    "year": int(match.group(1)),
                    "month": int(match.group(2)),
                    "day": int(match.group(3)),
                }
            )
    for match in _DATE_MD_CN.finditer(text):
        span = match.span()
        if _overlaps(span, occupied):
            continue
        occupied.append(span)
        hits.append(
            {
                "span": match.group(0),
                "start": span[0],
                "year": None,
                "month": int(match.group(1)),
                "day": int(match.group(2)),
            }
        )
    # 中文数字日期（task-36）：十二月二号 / 二〇二五年十二月二日；复用 timeresolve.cn_date_hits。
    for hit in timeresolve.cn_date_hits(text):
        span = (int(hit["start"]), int(hit["start"]) + len(str(hit["span"])))
        if _overlaps(span, occupied):
            continue
        occupied.append(span)
        hits.append(dict(hit))
    hits.sort(key=lambda item: item["start"])
    return hits[: max(1, int(limit or 1))]


def _resolve_date(hit: dict[str, Any], today: date) -> date | None:
    """写法 → 具体日期；不存在（2月30日）返回 ``None``。

    没写年份的按「最近的一次」推：现在 11 月、写「12月3日」就按去年算——
    人回忆「那天」几乎总是在往回看（和 ``temporal._explicit_day`` 同一口径）。
    """
    month = int(hit["month"])
    day = int(hit["day"])
    try:
        if hit["year"] is None:
            candidate = date(today.year, month, day)
            if candidate > today:
                candidate = date(today.year - 1, month, day)
            return candidate
        return date(int(hit["year"]), month, day)
    except ValueError:
        return None


def _memory_row(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "text": str(row["text"] or "")[:120],
        "day": str(row["stamp"] or "")[:10],
    }



# ------------------------------------------------- 日期证据的「宽容取证」（task-36）
# 目标：宁可漏报不可误报——记到过就不要再报「没依据」。三条补充证据全部只加支持、不加指控：
#   1) 记录正文里用中文/数字写的同月日（十二月二号 ↔ 12月2号）；
#   2) 相对说法按该行记录日推导出的绝对日期（周五 按 2026-06-30 推得 07-03）；
#   3) 上面两条的来源是 memory.text_raw 与 journal_line.text，不限 structured day。
# 缓存按 (id(conn), subject) 存一次推导结果，避免每条日期重复扫文本。
_DERIVED_DAYS_CACHE: dict[tuple[int, str], set[str]] = {}


def _text_date_patterns(target: date) -> list[str]:
    month, day = target.month, target.day
    cn_month, cn_day = timeresolve._cn_int_text(month), timeresolve._cn_int_text(day)
    values = {
        f"%{month}月{day}号%", f"%{month}月{day}日%",
        f"%{cn_month}月{cn_day}号%", f"%{cn_month}月{cn_day}日%",
    }
    return sorted(values)


def _text_date_evidence(
    conn: Any, subject: str, target: date, *, limit: int
) -> list[dict[str, Any]]:
    """记录正文里有没有「同月日」的写法（中文/数字）；有就返回摘要（仍只是**支持**证据）。"""
    cap = max(1, int(limit or 1))
    patterns = _text_date_patterns(target)
    like = " OR ".join("text_raw LIKE ?" for _ in patterns)
    mem_sql = (
        "SELECT id, text_raw AS text, COALESCE(valid_from, day_key) AS stamp FROM memory "
        f"WHERE subject_id=? AND status<>'deleted' AND ({like}) ORDER BY seq DESC LIMIT ?"
    )
    rows = conn.execute(mem_sql, (subject, *patterns, cap)).fetchall()
    evidence = [_memory_row(row) for row in rows]
    if len(evidence) >= cap:
        return evidence[:cap]
    line_like = " OR ".join("text LIKE ?" for _ in patterns)
    line_sql = (
        "SELECT id, text, ts AS stamp FROM journal_line "
        f"WHERE subject_id=? AND ({line_like}) ORDER BY seq DESC LIMIT ?"
    )
    line_rows = conn.execute(line_sql, (subject, *patterns, cap - len(evidence))).fetchall()
    evidence.extend(
        {"id": str(row["id"]), "text": str(row["text"] or "")[:120], "day": str(row["stamp"] or "")[:10]}
        for row in line_rows
    )
    return evidence[:cap]


def _derived_days(conn: Any, subject: str) -> set[str]:
    """把记录正文里的相对说法/中文日期按各自记录日推导成绝对日期集合（缓存）。"""
    # 缓存键必须带「这张库当前的记录规模」：id(conn) 在测试里会被复用，
    # 只按 id 缓存会把上一张库的推导结果串过来（宁可重算，不能串库）。
    try:
        signature = int(
            conn.execute("SELECT COUNT(*) FROM memory WHERE subject_id=?", (subject,)).fetchone()[0]
        ) + int(
            conn.execute("SELECT COUNT(*) FROM journal_line WHERE subject_id=?", (subject,)).fetchone()[0]
        )
    except Exception:  # noqa: BLE001 坏表就不缓存
        signature = -1
    key = (id(conn), subject, signature)
    cached = _DERIVED_DAYS_CACHE.get(key)
    if cached is not None:
        return cached
    days: set[str] = set()
    for sql in (
        "SELECT text_raw AS text, COALESCE(valid_from, day_key, '') AS stamp FROM memory "
        "WHERE subject_id=? AND status<>'deleted' ORDER BY seq DESC LIMIT 300",
        "SELECT text, COALESCE(day_key, ts, '') AS stamp FROM journal_line "
        "WHERE subject_id=? ORDER BY seq DESC LIMIT 300",
    ):
        try:
            rows = conn.execute(sql, (subject,)).fetchall()
        except Exception:  # noqa: BLE001 老库/坏表：取证退化，不指控
            continue
        for row in rows:
            body, stamp = str(row["text"] or ""), str(row["stamp"] or "")[:10]
            if not body or not stamp:
                continue
            for _phrase, iso in timeresolve.loose_relative_dates(body, stamp):
                days.add(iso)
            try:
                base_day = date.fromisoformat(stamp)
            except ValueError:
                continue
            for hit in timeresolve.cn_date_hits(body):
                resolved = _resolve_date(hit, base_day)  # type: ignore[arg-type]
                if resolved is not None:
                    days.add(resolved.isoformat())
    if len(_DERIVED_DAYS_CACHE) > 64:
        _DERIVED_DAYS_CACHE.clear()
    _DERIVED_DAYS_CACHE[key] = days
    return days


def _date_evidence(
    conn: Any, subject: str, target: date, *, year_known: bool, limit: int = MAX_EVIDENCE
) -> list[dict[str, Any]]:
    """memory / journal_line 里有没有这一天的记录。有就返回 1–3 条摘要。"""
    cap = max(1, int(limit or 1))
    evidence: list[dict[str, Any]] = []
    if year_known:
        stamp = target.isoformat()
        mem_rows = conn.execute(
            "SELECT id, text_raw AS text, COALESCE(valid_from, day_key) AS stamp FROM memory "
            "WHERE subject_id=? AND status<>'deleted' "
            "AND (substr(day_key,1,10)=? OR substr(valid_from,1,10)=?) "
            "ORDER BY seq DESC LIMIT ?",
            (subject, stamp, stamp, cap),
        ).fetchall()
    else:
        md = target.strftime("%m-%d")
        mem_rows = conn.execute(
            "SELECT id, text_raw AS text, COALESCE(valid_from, day_key) AS stamp FROM memory "
            "WHERE subject_id=? AND status<>'deleted' "
            "AND (substr(day_key,6,5)=? OR substr(valid_from,6,5)=?) "
            "ORDER BY seq DESC LIMIT ?",
            (subject, md, md, cap),
        ).fetchall()
    evidence.extend(_memory_row(row) for row in mem_rows)
    if len(evidence) >= cap:
        return evidence[:cap]
    # 结构化字段没有，再看记录正文的写法（中文数字/月日）——宁可漏报不可误报。
    evidence.extend(_text_date_evidence(conn, subject, target, limit=cap - len(evidence)))
    if len(evidence) >= cap:
        return evidence[:cap]
    if year_known:
        stamp = target.isoformat()
        line_rows = conn.execute(
            "SELECT id, text, ts AS stamp FROM journal_line "
            "WHERE subject_id=? AND (substr(ts,1,10)=? OR substr(day_key,1,10)=?) "
            "ORDER BY seq DESC LIMIT ?",
            (subject, stamp, stamp, cap - len(evidence)),
        ).fetchall()
    else:
        md = target.strftime("%m-%d")
        line_rows = conn.execute(
            "SELECT id, text, ts AS stamp FROM journal_line "
            "WHERE subject_id=? AND (substr(ts,6,5)=? OR substr(day_key,6,5)=?) "
            "ORDER BY seq DESC LIMIT ?",
            (subject, md, md, cap - len(evidence)),
        ).fetchall()
    evidence.extend(
        {
            "id": str(row["id"]),
            "text": str(row["text"] or "")[:120],
            "day": str(row["stamp"] or "")[:10],
        }
        for row in line_rows
    )
    if len(evidence) >= cap:
        return evidence[:cap]
    # 最后一道：相对说法按记录日推导出的绝对日期（周五/上个月24号/明天…）。
    derived = _derived_days(conn, subject)
    if year_known:
        matched = target.isoformat() in derived
    else:
        md = target.strftime("%m-%d")
        matched = any(day[5:] == md for day in derived)
    if matched:
        evidence.append(
            {
                "id": "derived-relative",
                "text": f"记录里的相对日期按记录日可推导到 {target.isoformat()}",
                "day": target.isoformat(),
            }
        )
    return evidence[:cap]


def _month_evidence(
    conn: Any, subject: str, target: date, *, year_known: bool, limit: int = MAX_EVIDENCE
) -> list[dict[str, Any]]:
    """没找到那一天时，给 1–3 条**同月**的记载当参照，让读者知道库里记到的是哪天。"""
    cap = max(1, int(limit or 1))
    if year_known:
        prefix = target.strftime("%Y-%m")
        sql = (
            "SELECT id, text_raw AS text, COALESCE(valid_from, day_key) AS stamp FROM memory "
            "WHERE subject_id=? AND status<>'deleted' "
            "AND substr(COALESCE(valid_from, day_key),1,7)=? ORDER BY seq DESC LIMIT ?"
        )
        params: tuple[Any, ...] = (subject, prefix, cap)
    else:
        month = target.strftime("%m")
        sql = (
            "SELECT id, text_raw AS text, COALESCE(valid_from, day_key) AS stamp FROM memory "
            "WHERE subject_id=? AND status<>'deleted' "
            "AND substr(COALESCE(valid_from, day_key),6,2)=? ORDER BY seq DESC LIMIT ?"
        )
        params = (subject, month, cap)
    return [_memory_row(row) for row in conn.execute(sql, params).fetchall()]


def _check_dates(
    conn: Any,
    subject: str,
    text: str,
    *,
    today: date,
    limit: int,
    evidence_limit: int = MAX_EVIDENCE,
) -> tuple[list[dict[str, Any]], int, bool]:
    """核对答案里的日期；返回 (issues, 核了几个, 是否因上限被截断)。"""
    hits = _extract_dates(text, today=today, limit=limit)
    truncated = len(hits) >= max(1, int(limit or 1))
    issues: list[dict[str, Any]] = []
    for hit in hits:
        year_known = hit["year"] is not None
        target = _resolve_date(hit, today)
        if target is None:
            issues.append(
                {
                    "kind": "date",
                    "span": str(hit["span"]),
                    "reason": f"{hit['month']}月{hit['day']}日这个日期不存在，答案里却写了它",
                    "evidence": [],
                }
            )
            continue
        evidence = _date_evidence(
            conn, subject, target, year_known=year_known, limit=evidence_limit
        )
        if evidence:
            continue
        month_refs = _month_evidence(
            conn, subject, target, year_known=year_known, limit=evidence_limit
        )
        if month_refs:
            days = "、".join(str(item.get("day") or "") for item in month_refs[:3] if item.get("day"))
            reason = f"库里没有 {target.isoformat()} 这一天的记录；同月记到的是 {days}"
        else:
            reason = (
                f"库里没有 {target.isoformat()} 这一天的记录"
                "（memory 的 day/valid_from 和日记 ts 里都没有）"
            )
        if not year_known:
            reason = f"「{hit['span']}」按最近一次算作 {target.isoformat()}；" + reason
        issues.append(
            {
                "kind": "date",
                "span": str(hit["span"]),
                "reason": reason,
                "evidence": month_refs,
            }
        )
    return issues, len(hits), truncated


# ----------------------------------------------------------------- 专名


def _surname_helpers() -> tuple[frozenset[str], set[str]]:
    """复用实体抽取那套姓氏表/虚词尾表（同一份口径，避免两份表漂移）。"""
    from ..net import entities as net_entities

    return net_entities._SURNAMES, net_entities._BRIDGE_BAD_TAIL


def _looks_like_person_name(
    candidate: str, *, surnames: frozenset[str], bad_tail: frozenset[str]
) -> bool:
    if not 2 <= len(candidate) <= 4:
        return False
    if any(ch.isdigit() for ch in candidate):
        return False
    if candidate[:2] in _COMPOUND_SURNAMES:
        given = candidate[2:]
        if not given:
            return False
    else:
        if candidate[0] not in surnames:
            return False
        given = candidate[1:]
    if candidate in _COMMON_WORDS:
        return False
    if any(ch in _GIVEN_STOP for ch in given):
        return False
    tail = candidate[-1]
    if tail in bad_tail or tail in _KIN_TAIL:
        return False
    return True


def _known_names(conn: Any, subject: str) -> list[str]:
    """本主体库里出现过的名字（实体名 + 别名），长的排前面，便于先遮蔽长的。"""
    names: list[str] = []
    for row in conn.execute(
        "SELECT name FROM entity WHERE subject_id=? AND status='active'", (subject,)
    ).fetchall():
        name = str(row["name"] or "").strip()
        if name:
            names.append(name)
    for row in conn.execute(
        "SELECT a.alias AS name FROM entity_alias a JOIN entity e ON e.id=a.entity_id "
        "WHERE e.subject_id=? AND e.status='active'",
        (subject,),
    ).fetchall():
        name = str(row["name"] or "").strip()
        if name and name not in names:
            names.append(name)
    names.sort(key=len, reverse=True)
    return names


def _mark_known_spans(
    text_lower: str, names: list[str]
) -> tuple[list[tuple[int, int]], int]:
    """把答案里出现的已知名字遮蔽掉（返回 span 列表和命中次数）。"""
    spans: list[tuple[int, int]] = []
    hits = 0
    for name in names:
        needle = name.lower()
        if not needle:
            continue
        start = 0
        while True:
            pos = text_lower.find(needle, start)
            if pos < 0:
                break
            span = (pos, pos + len(needle))
            if not any(pos < end and begin < span[1] for begin, end in spans):
                spans.append(span)
                hits += 1
            start = pos + max(1, len(needle))
    return spans, hits


def _trim_trailing(candidate: str) -> str:
    """名字右边界裁剪：把尾巴上的虚词/时间词去掉（「李静当时」→「李静」）。

    中文没有空格，「李静当时也在」里「当」紧跟着名字，3 字候选会把「当」并进来。
    裁剪只从右端去掉、不往中间动，所以「李国正」这种以实词结尾的名字不受影响。
    """
    while len(candidate) > 2 and (candidate[-1] in _GIVEN_STOP or candidate[-1] in _KIN_TAIL):
        candidate = candidate[:-1]
    return candidate


# ------------------------------------------------- 专名候选的边界/词性证据打分（task-34）
# 为什么改成打分：旧实现「姓氏 + 2–4 字 + 不是虚词尾」会把普通词切成人名
# （行李箱→李箱、周三米米→周三米、一张旧地图→张旧、那张报价单→张报价），H1 在
# 592 通过答案上误伤 6.8%/8.8%。这里不往黑名单里加词，而是给候选算**证据分**：
#   左边界（句首/标点=强，功能词=中，实词连续=负）、右边界（句末/标点/谓词/代词/语气=强，
#   时间续接=负，实词连续=负）、词性类别（数词+量词组合、昵称前缀）、实体抽取模板共现。
# 阈值可配：`NAME_BOUNDARY_MIN_SCORE`（默认 2.0）或调用方传 ``min_score``。
# 这套打分只影响「候选像不像人名」，不改 `check_answer` 的判定语义（有据/无据仍照旧）。
NAME_BOUNDARY_MIN_SCORE = 2.0

_NAME_LEFT_STRONG = set("和跟与及对给让叫请把被将是说问答看听找等带同随由从向往到于为帮陪替")
_NAME_RIGHT_STRONG = set(
    "说问道笑哭喊骂答听说看找去来在也都还又就才总不没把被给让是的了他她它们和跟与及"
    "下上中里外家改做写买拿放留帮陪当有会要想能一"
)
_NAME_DEMO_NUMERAL = set("一二三四五六七八九十几两这那哪每各第单半")
_NAME_MEASURE = set("张条只把件位个名份次棵座间本支辆台套句段篇页块周岁天年月日号点分秒斤元")
_NAME_NICK_PREFIX = set("小老阿大")
_NAME_TIME_RIGHT = set("年月日号时点分秒周礼拜")


def _name_pattern_bonus(run: str, index: int, candidate: str) -> float:
    """实体抽取模板共现证据（复用 net.entities 的人名模板；不新造规则）。

    候选正好落在「X 说 / X 告诉 / X 也 / X 爸妈 / X 家」这类高精度人名模板的 name 位时
    加 3 分——这是「共现证据」，比任何词表都可靠。
    """
    try:
        from ..net import entities as net_entities
    except Exception:  # noqa: BLE001 net 不可用时少一类证据，不影响主流程
        return 0.0
    for pattern in (
        net_entities._PERSON3,
        net_entities._PERSON2,
        net_entities._PERSON_ACT,
        net_entities._PERSON_KIN,
    ):
        try:
            matches = pattern.finditer(run, max(0, index - 3))
        except Exception:  # noqa: BLE001 坏正则/坏数据都只少一类证据
            continue
        for match in matches:
            start = match.start("name")
            if start > index:
                break
            if start == index and match.group("name") == candidate:
                return 3.0
    return 0.0


def _name_boundary_score(
    *, body: str, base: int, chunk: str, index: int, candidate: str, surnames: frozenset[str]
) -> float:
    """候选的证据分：左边界 + 右边界 + 词性类别 + 实体模板共现。"""
    left = body[base + index - 1] if base + index > 0 else ""
    end = base + index + len(candidate)
    right = body[end] if end < len(body) else ""
    score = 0.0
    if not left or not ("\u4e00" <= left <= "\u9fff"):
        score += 2.0  # 句首/标点/空白/被遮蔽的已知名之后
    elif left in _NAME_LEFT_STRONG:
        score += 1.5
    elif left in _NAME_DEMO_NUMERAL or left in _NAME_NICK_PREFIX:
        score -= 2.0  # 「一张旧」「小唐再定」：量词/昵称前面的那截不是名字开头
    else:
        score -= 2.5  # 实词连续：候选大概率是词中间（「进行李箱」的「李箱」）
    if not right or not ("\u4e00" <= right <= "\u9fff"):
        score += 2.0  # 句末/标点/空白
    elif right in _NAME_RIGHT_STRONG:
        score += 1.5  # 名字后面紧跟谓词/代词/语气词
    elif right in _NAME_TIME_RIGHT:
        score -= 2.0
    else:
        score -= 1.5  # 词继续往下走（行李箱→李箱、周三米米→周三米）
    if candidate[0] in _NAME_MEASURE and left in _NAME_DEMO_NUMERAL:
        score -= 2.5  # 「一张旧地图」的「张旧」：张是量词不是姓
    if candidate[:2] in _COMPOUND_SURNAMES or candidate[0] in surnames:
        score += 0.5
    score += _name_pattern_bonus(chunk, index, candidate)
    score += 1.0 if len(candidate) == 2 else (0.5 if len(candidate) == 3 else -1.0)
    return score


def _name_candidates(
    text: str,
    *,
    spans: list[tuple[int, int]],
    limit: int,
    min_score: float | None = None,
) -> list[str]:
    """从答案的连续汉字里挑「像人名」的候选；已知名字所在位置先遮蔽。

    候选先按 3→2→4 字生成（`_looks_like_person_name` 做硬过滤），再用
    ``_name_boundary_score`` 的证据分过阈值（默认 ``NAME_BOUNDARY_MIN_SCORE``）。
    目标：把「行李箱→李箱 / 周三米米→周三米 / 一张旧地图→张旧」这类词中间切片刷掉，
    同时保留真名（真名通常有句首/标点/谓词/模板共现的边界证据）。
    """
    threshold = NAME_BOUNDARY_MIN_SCORE if min_score is None else float(min_score)
    masked = list(text)
    for begin, end in spans:
        for index in range(max(0, begin), min(len(masked), end)):
            masked[index] = "\u0001"
    body = "".join(masked)
    surnames, bad_tail = _surname_helpers()
    found: list[str] = []
    for run in _CN_RUN.finditer(body):
        chunk = run.group(0)
        base = run.start()
        index = 0
        while index < len(chunk):
            if len(found) >= max(1, int(limit or 1)):
                return found
            head = chunk[index]
            compound = chunk[index : index + 2] in _COMPOUND_SURNAMES
            if head not in surnames and not compound:
                index += 1
                continue
            # 前两个字就是常见词（高兴/方案/马上…）时不要往后多吃一个字把它凑成名字。
            if not compound and chunk[index : index + 2] in _COMMON_WORDS:
                index += 1
                continue
            picked = ""
            # 中文人名以 2–3 字为主，4 字多是复姓或少数民族名：先试 3 再试 2，
            # 都不成立才试 4（避免「赵铁柱打」这种把后一个动词也吃进来）。
            sizes = (4, 3) if compound else (3, 2, 4)
            for size in sizes:
                if index + size > len(chunk):
                    continue
                # 先裁右边界再校验：「李静当时也在」的 3 字候选要落回「李静」。
                candidate = _trim_trailing(chunk[index : index + size])
                if not _looks_like_person_name(candidate, surnames=surnames, bad_tail=bad_tail):
                    continue
                # 时间词不是人名（周三/今天/周一）：复用 timecue 的现成判据，算词性证据。
                try:
                    from .retrieval import timecue

                    if timecue._WEEKDAY.match(candidate):
                        continue
                except Exception:  # noqa: BLE001 timecue 不可用就跳过这条证据
                    pass
                if _name_boundary_score(
                    body=body, base=base, chunk=chunk, index=index, candidate=candidate, surnames=surnames
                ) < threshold:
                    continue
                picked = candidate
                break
            if picked:
                found.append(picked)
                index += len(picked)
            else:
                index += 1
    return found


def _name_in_library(conn: Any, subject: str, name: str) -> bool:
    """这个字串在库里出现过吗（实体名之外的原文也算：实体抽取宁可漏，不能因此冤枉它）。"""
    row = conn.execute(
        "SELECT 1 FROM memory WHERE subject_id=? AND status<>'deleted' AND text_raw LIKE ? LIMIT 1",
        (subject, f"%{name}%"),
    ).fetchone()
    if row is not None:
        return True
    row = conn.execute(
        "SELECT 1 FROM journal_line WHERE subject_id=? AND text LIKE ? LIMIT 1",
        (subject, f"%{name}%"),
    ).fetchone()
    return row is not None


def _check_names(
    conn: Any,
    subject: str,
    text: str,
    *,
    max_names: int,
    min_score: float | None = None,
) -> tuple[list[dict[str, Any]], int, bool]:
    """核对答案里的专名；返回 (issues, 核了几个名字, 是否因上限被截断)。

    ``min_score``：候选证据分阈值（默认 ``NAME_BOUNDARY_MIN_SCORE``）；verifier 可以按
    自己的配置传更低/更高的阈值，selfcheck 既有调用方不传、行为由默认值决定。
    """
    text_lower = text.lower()
    names = _known_names(conn, subject)
    known_spans, recognized = _mark_known_spans(text_lower, names)
    candidates = _name_candidates(text, spans=known_spans, limit=max_names, min_score=min_score)
    truncated = len(candidates) >= max(1, int(max_names or 1))
    issues: list[dict[str, Any]] = []
    checked = recognized
    for candidate in candidates:
        checked += 1
        # 实体表没抽到、但原文里出现过的名字也是有据的（实体抽取本来就宁可漏）。
        if _name_in_library(conn, subject, candidate):
            continue
        issues.append(
            {
                "kind": "name",
                "span": candidate,
                "reason": "这个名字在本主体的实体表和原文里都没有出现过，像是编出来的",
                "evidence": [],
            }
        )
    return issues, checked, truncated


# ----------------------------------------------------------------- 出口


def check_answer(
    store: Any,
    answer: str,
    *,
    question: str | None = None,
    max_dates: int = 40,
    max_names: int = 20,
    model: Any = None,
) -> dict[str, Any]:
    """核对一条回答里的日期与专名；可选再加一层模型复核。

    参数：
        store      ``MemoryStore``（用 ``store.db.conn`` / ``store.subject``，只读）
        answer     待核对的回答正文
        question   原问句（可选）。按 gate schema 的约定**只做记录、不参与判定**：
                   答案有没有编日期/人名，只看库里的证据本身。
        max_dates  最多核对多少个日期
        max_names  最多核对多少个名字候选
        model      可选。传了才做「模型复核」（``complete(messages) -> reply`` 的对象，
                   例如 ``pmem.bench.model.OpenAICompatModel``）：让模型对照检索到的
                   记忆判断草稿里有没有「记忆里没有却说得像真的」的陈述。默认 None
                   = 只做确定性核对（0 模型调用、结果可复现）。

    截断长度与单条证据条数取 ``selfcheck.max_answer_chars`` / ``selfcheck.evidence_per_issue``
    （配置缺失时用模块默认值），和 config.py 里的默认口径一致。

    返回：``{"clean", "issues", "checked": {"dates", "names"}, "note", "model"}``。
    ``clean`` 是「这一遍没发现对不上的东西」——**不要叫它 ok**：gate 的 ``_ok()``
    已经把整条结果包成 ``{"ok": true, "data": ...}``，两处 ok 语义打架会让调用方
    看到 ``{"ok": true, "data": {"ok": false}}`` 这种自相矛盾的东西。
    单条 issue 形如 ``{"kind": "date"|"name"|"claim", "span", "reason", "evidence"}``，
    evidence 里是 1–3 条 ``{"id", "text", "day"}`` 摘要（没有可参照的记载时为空）。
    """
    cfg = getattr(store, "cfg", None)
    answer_cap = _cfg_int(cfg, "selfcheck.max_answer_chars", MAX_ANSWER_CHARS)
    evidence_cap = _cfg_int(cfg, "selfcheck.evidence_per_issue", MAX_EVIDENCE)
    notes: list[str] = []
    text = str(answer or "")
    if len(text) > answer_cap:
        text = text[:answer_cap]
        notes.append(f"答案超过 {answer_cap} 字符，已截断到前 {answer_cap} 字再核对")
    conn = store.db.conn
    subject = str(store.subject)
    today = date.fromisoformat(clock.day_key())
    date_limit = max(1, int(max_dates or 1))
    name_limit = max(1, int(max_names or 1))
    date_issues, checked_dates, dates_cut = _check_dates(
        conn,
        subject,
        text,
        today=today,
        limit=date_limit,
        evidence_limit=evidence_cap,
    )
    name_issues, checked_names, names_cut = _check_names(
        conn,
        subject,
        text,
        max_names=name_limit,
    )
    if dates_cut:
        notes.append(f"日期超过 {date_limit} 个，只核了前 {date_limit} 个")
    if names_cut:
        notes.append(f"名字超过 {name_limit} 个，只核了前 {name_limit} 个")
    issues = date_issues + name_issues
    # 可选模型复核：确定性那一遍抓「库里没有的日期/人名」，这一遍抓「记忆里没有却说得
    # 像真的陈述」。模型挂了/没配都不能影响确定性结论，所以整段包在 try 里，只往 note 写一句。
    model_info: dict[str, Any] = {
        "skipped": True,
        "note": "未配置模型复核（只做了确定性核对）",
        "issues": [],
    }
    if model is not None:
        try:
            model_info = model_check(
                store,
                text,
                question=question,
                model=model,
                evidence_limit=evidence_cap,
            )
        except Exception as exc:  # noqa: BLE001 模型是增强，挂了不能带崩自检
            model_info = {
                "skipped": True,
                "note": f"模型复核失败：{type(exc).__name__}: {exc}",
                "issues": [],
            }
        issues = issues + list(model_info.get("issues") or [])
        if model_info.get("note"):
            notes.append(str(model_info["note"]))
    return {
        "clean": not issues,
        "issues": issues,
        "checked": {"dates": checked_dates, "names": checked_names},
        "note": "；".join(notes),
        "model": model_info,
    }


SELFCHECK_SYSTEM = """你是「答后校对员」。给你一道问题、一段草稿回答、以及从长期记忆里检索到的证据。
只做一件事：找出草稿里「说得很确定、但证据里找不到出处」的陈述。

规则：
1. 只依据证据判断，不用你自己的常识补；
2. 回答里明确说「不知道 / 不确定 / 没印象」不算问题；
3. 证据不足以下结论时，宁可不说（宁少报不多报）；
4. 最多列 5 条，每条给原文片段（span，尽量短）和一句原因；
5. 只输出 JSON，不要解释：
{"unsupported": [{"span": "...", "reason": "..."}], "should_say_unknown": false}"""


def _evidence_lines(store: Any, query: str, *, limit: int = 6) -> list[str]:
    """检索几条记忆当复核证据；只读（record=False，不写 recall_log）。"""
    try:
        result = store.search(str(query or "")[:120], k=limit, record=False)
    except Exception:  # noqa: BLE001 检索失败就当没有证据
        return []
    out: list[str] = []
    for item in (result or {}).get("items") or []:
        day = str(item.get("day") or "")
        text = str(item.get("text") or "").replace("\n", " ")[:120]
        if text:
            out.append(f"- [{day}] {text}")
    return out


def model_check(
    store: Any,
    answer: str,
    *,
    question: str | None = None,
    model: Any,
    evidence_limit: int = 6,
) -> dict[str, Any]:
    """模型复核：对照检索证据，列出「没有出处却说得像真的」的陈述（可选、按次计费）。

    返回 ``{"skipped": False, "issues": [...], "note": ..., "tokens": int}``；
    issue 的 ``kind`` 是 ``"claim"``（和确定性那遍的 date/name 区分开）。
    """
    query = str(question or "").strip() or str(answer or "")[:120]
    evidence = _evidence_lines(store, query, limit=evidence_limit)
    body = "\n".join(evidence) if evidence else "（这次没检索到相关记忆）"
    user = (
        f"问题：{question or '（未提供）'}\n\n"
        f"证据：\n{body}\n\n"
        f"草稿回答：\n{answer}\n"
    )
    messages = [
        {"role": "system", "content": SELFCHECK_SYSTEM},
        {"role": "user", "content": user},
    ]
    reply = model.complete(messages, None, hint={"temperature": 0})
    content = str(getattr(reply, "content", "") or "")
    parsed = _parse_model_json(content)
    tokens = int(getattr(getattr(reply, "usage", None), "total", 0) or 0)
    if parsed is None:
        return {
            "skipped": False,
            "issues": [],
            "note": "模型复核没给出合法 JSON（只采用确定性结论）",
            "tokens": tokens,
        }
    raw = parsed.get("unsupported") if isinstance(parsed, dict) else None
    issues: list[dict[str, Any]] = []
    for item in (raw or [])[:5]:
        if not isinstance(item, dict):
            continue
        span = str(item.get("span") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if not span and not reason:
            continue
        issues.append(
            {
                "kind": "claim",
                "span": span[:80],
                "reason": reason[:160] or "证据里找不到出处",
                "evidence": [],
            }
        )
    should_unknown = bool(parsed.get("should_say_unknown")) if isinstance(parsed, dict) else False
    note = "模型复核完成"
    if should_unknown:
        note = "模型复核完成：这题更该说「不知道」"
    return {"skipped": False, "issues": issues, "note": note, "tokens": tokens}


def _parse_model_json(text: str) -> Any:
    """从模型回复里抠 JSON（容忍 ```json 代码围栏和前后废话）。"""
    import json

    body = str(text or "").strip()
    if body.startswith("```"):
        body = body.strip("`")
        if body.lower().startswith("json"):
            body = body[4:]
    try:
        return json.loads(body.strip())
    except ValueError:
        pass
    start = body.find("{")
    end = body.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(body[start : end + 1])
        except ValueError:
            return None
    return None


def format_report(result: dict[str, Any]) -> str:
    """把 ``check_answer`` 的结果渲染成中文多行，给 MCP / CLI / 控制台直接用。"""
    issues = result.get("issues") or []
    if not issues:
        return "日期/专名都对得上。"
    lines = [f"自检发现 {len(issues)} 处对不上："]
    kind_labels = {"date": "日期", "name": "专名", "claim": "陈述"}
    for issue in issues:
        kind = kind_labels.get(str(issue.get("kind")), "对不上")
        line = f"- [{kind}] {issue.get('span')}：{issue.get('reason')}"
        evidence = issue.get("evidence") or []
        if evidence:
            first = evidence[0]
            day = str(first.get("day") or "")
            snippet = str(first.get("text") or "")[:40]
            line += f"（库里最接近的：{day} {snippet}）"
        lines.append(line)
    checked = result.get("checked") or {}
    lines.append(f"核对：{checked.get('dates', 0)} 个日期 / {checked.get('names', 0)} 个名字。")
    note = str(result.get("note") or "")
    if note:
        lines.append(note)
    return "\n".join(lines)
