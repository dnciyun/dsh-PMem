"""claim 抽取：host 模式任务书 + 校验入库 + 确定性规则降级。

和 ``memory_whale_prep / memory_whale_finish`` 同一条路：PMem 只准备材料与任务书，
宿主模型（会话模型）读材料、出 JSON；PMem 校验后才入库。没有宿主/超时 → ``rule_extract``
（只看三类：人名+关系、日期、带否定词的偏好），宁可少抽，不猜。

五条硬约束在这里落地：
1. evidence_quote 必须能在当天原话里找到（子串，或双字覆盖 ≥0.8），否则整条丢弃；
2. event_date 必须来自 quote，或由 quote 里的相对说法按 mentioned_at 推得，否则 status=unknown；
3. polarity 显式：quote 里有否定（没/不是/别/不要/不吃/不喝…）时落 0；
4. 幂等：去重键交给 ``claim.create``（source_line+predicate+object+polarity）；
5. 可降级：``rule_extract`` 确定性产出，不依赖宿主。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from ..base import clock
from ..base.errors import ValidationError
from . import claim as claim_mod
from . import timeresolve

# 三个规则只做保守识别；宁少抽，不猜。
_RELATION_WORDS = (
    "哥哥",
    "姐姐",
    "弟弟",
    "妹妹",
    "爸爸",
    "妈妈",
    "爸",
    "妈",
    "老公",
    "老婆",
    "对象",
    "男朋友",
    "女朋友",
    "同事",
    "朋友",
    "儿子",
    "女儿",
    "孩子",
)
_REL_NAME = re.compile(
    rf"(?:我的|我|用户)?(?P<rel>{'|'.join(_RELATION_WORDS)})(?:叫|是)(?P<name>[\u4e00-\u9fff]{{2,4}})"
)
_REL_IS = re.compile(
    rf"(?P<name>[\u4e00-\u9fff]{{2,4}})(?:是我|是我的)(?P<rel>{'|'.join(_RELATION_WORDS)})"
)
_NEGATED_PREF = re.compile(r"(?P<neg>不吃|不喝|不去|不要|不喜欢|不能|没去|没吃|没喝)(?P<obj>[\u4e00-\u9fff]{1,8})")
_DONT = re.compile(r"(?P<neg>别|不要)(?P<obj>[\u4e00-\u9fff]{1,8})")
_NEG_MARKERS = ("没", "不是", "别", "不要", "不吃", "不喝", "不去", "不喜欢", "不能", "没有")
# 规则层「不许当谓词/客体」的垃圾信号：单个副词、句读、以及把整句当客体。
_RULE_OBJECT_BAD = "得着了过又也就还都太才再要能会想说问看听来去走让给被把在和跟与及或但而因所是不没我你他她它这那个"
_RULE_MAX_PER_KIND = 8
# host 抽取层的谓词白名单（关系词/单个动作词）：超过 4 字基本是「趴在桌上睡觉」这类短语。
_MAX_EXTRACT_PREDICATE = 4
_BAD_PREDICATES = {"又到", "当前时间", "现在", "时间", "几点", "时刻", "点钟"}
_CLOCK_OBJECT = re.compile(
    r"^\s*(?:\d{1,2}[:：]\d{2}|\d{1,2}\s*点(?:半|多)?|"
    r"[一二三四五六七八九十两]{1,3}\s*点(?:半|多)?)\s*$"
)
_UNCERTAIN_MARKERS = ("可能", "好像", "也许", "大概", "说不定", "记不清", "不确定", "不清楚", "似乎")


def _rule_predicate_ok(predicate: str) -> bool:
    text = str(predicate or "").strip()
    if not text or len(text) > _MAX_EXTRACT_PREDICATE:
        return False
    if text in claim_mod._PREDICATE_BAD:
        return False
    return not any(char in claim_mod._SENTENCE_PUNCT for char in text)


def _rule_object_ok(object_: str) -> bool:
    text = str(object_ or "").strip()
    if not (1 <= len(text) <= 30):
        return False
    if any(char in claim_mod._SENTENCE_PUNCT for char in text):
        return False
    # 规则层额外收紧：客体不能是「又困得眼睛睁不开 / 一上来太熟」这种半句。
    return not any(char in _RULE_OBJECT_BAD for char in text)


def _rule_ok(item: dict[str, Any]) -> bool:
    return _rule_predicate_ok(str(item.get("predicate") or "")) and _rule_object_ok(
        str(item.get("object") or "")
    )


_RELATIVE_DATE_OBJECT = re.compile(
    r"^(今天|明天|后天|昨天|前天|大后天|大前天|"
    r"上(?:上)?(?:周|星期|礼拜)[一二三四五六日天]|"
    r"下(?:下)?(?:周|星期|礼拜)[一二三四五六日天]|这(?:周|星期|礼拜)[一二三四五六日天]|"
    r"(?:\d+|[一二三四五六七八九十百]+)\s*(?:天|日|周|个?月|年)(?:以前|之前|前|以后|之后|后))$"
)
_DATE_WORD = re.compile(
    r"(今天|明天|后天|昨天|前天|上周[一二三四五六日天]|下周[一二三四五六日天]|"
    r"这周[一二三四五六日天]|周[一二三四五六日天]|\d{1,2}\s*月\s*\d{1,2}\s*[日号]|"
    r"\d{1,2}\s*[日号])"
)


def _get(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    try:
        return config.get(key, default)
    except Exception:  # noqa: BLE001 普通 dict/坏 config 都退化成点号遍历
        current: Any = config
        for part in str(key).split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current


def _enabled(config: Any) -> bool:
    return bool(_get(config, "claim.enabled", False))


def load_material(
    db: Any, *, subject: str = "self", day_key: str, max_source_chars: int = 0
) -> dict[str, Any]:
    """当天原话（journal_line）；``max_source_chars>0`` 时只取最近 N 字符。"""
    rows = db.conn.execute(
        "SELECT id, role, text, ts FROM journal_line WHERE subject_id=? AND day_key=? "
        "ORDER BY seq",
        (subject, day_key),
    ).fetchall()
    lines = [
        {"id": str(row["id"]), "role": str(row["role"]), "text": str(row["text"]), "ts": str(row["ts"])}
        for row in rows
    ]
    total = sum(len(line["text"]) for line in lines)
    truncated = False
    cap = int(max_source_chars or 0)
    if cap > 0 and total > cap:
        kept: list[dict[str, Any]] = []
        used = 0
        for line in reversed(lines):
            size = len(line["text"])
            if used + size > cap:
                truncated = True
                break
            kept.append(line)
            used += size
        lines = list(reversed(kept))
        total = used
    return {"lines": lines, "source_chars": total, "truncated": truncated}


def _bigram_coverage(quote: str, text: str) -> float:
    grams = [quote[index : index + 2] for index in range(max(0, len(quote) - 1))]
    grams = [gram for gram in grams if gram.strip()]
    if not grams:
        return 0.0
    hits = sum(1 for gram in grams if gram in text)
    return hits / len(grams)


def _find_source(quote: str, lines: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    """quote 是否来自某条原话：先子串，再双字覆盖 ≥0.8。"""
    text = str(quote or "").strip()
    if not text:
        return None, "empty_quote"
    for line in lines:
        if text in line["text"]:
            return line, "substring"
    for line in lines:
        if _bigram_coverage(text, line["text"]) >= 0.8:
            return line, "bigram"
    return None, "quote_not_found"


def _date_matches(text: str, mentioned_at: str) -> set[str]:
    """从一段文字里收集「它指向的绝对日期」：显式数字/中文日期 + 相对说法。"""
    dates: set[str] = set()
    base = str(mentioned_at or "")[:10]
    try:
        from . import selfcheck

        today = clock.parse_day(base) if base else None
    except Exception:  # noqa: BLE001
        today = None
    if today is not None:
        try:
            for hit in selfcheck._extract_dates(text, today=today, limit=20):
                resolved = selfcheck._resolve_date(hit, today)
                if resolved is not None:
                    dates.add(resolved.isoformat())
        except Exception:  # noqa: BLE001
            pass
    for _phrase, iso in timeresolve.loose_relative_dates(text, mentioned_at):
        dates.add(str(iso)[:10])
    return dates


def _date_supported(quote: str, event_date: str | None, mentioned_at: str) -> tuple[bool, str]:
    """日期自洽：event_date 必须被 quote 的文字/相对说法支撑，否则 unknown。"""
    if not event_date:
        return True, "no_date"
    target = str(event_date)[:10]
    if target in _date_matches(quote, mentioned_at):
        return True, "quoted"
    # 同形/同月日的文字写法（2026年3月8日 / 3月8日 / 三月八号）也算来自 quote。
    try:
        year, month, day = (int(part) for part in target.split("-"))
    except (ValueError, TypeError):
        return False, "bad_event_date"
    forms = {
        target,
        f"{year}年{month}月{day}日",
        f"{year}年{month}月{day}号",
        f"{month}月{day}日",
        f"{month}月{day}号",
    }
    if any(form in quote for form in forms):
        return True, "literal"
    # 「昨天/上周五」这类相对说法能推到这个日期 → 支撑；推不到 → unknown。
    derived = _date_matches(quote, mentioned_at)
    if derived:
        return False, "date_mismatch"
    return False, "date_not_in_quote"


def _polarity(quote: str, predicate: str, obj: str, given: Any) -> tuple[int, bool]:
    """否定显式：quote 里有否定词时 polarity 落 0；返回 (polarity, corrected)。"""
    has_neg = any(marker in quote for marker in _NEG_MARKERS)
    if given is None:
        return (0 if has_neg else 1), False
    try:
        polarity = int(given)
    except (TypeError, ValueError):
        polarity = 1 if not has_neg else 0
        return polarity, True
    if polarity not in (0, 1):
        polarity = 0 if has_neg else 1
        return polarity, True
    if has_neg and polarity == 1:
        return 0, True
    return polarity, False


def prep(
    db: Any,
    *,
    subject: str = "self",
    day_key: str,
    config: Any = None,
    max_source_chars: int | None = None,
) -> dict[str, Any]:
    """准备「宿主可执行」的 claim 抽取材料 + 中文任务书。"""
    if not _enabled(config):
        return {
            "enabled": False,
            "day_key": day_key,
            "subject": subject,
            "material": [],
            "task": "",
            "example": None,
            "note": "claim.enabled=false，跳过抽取（不碰任何表）",
        }
    cap = (
        int(max_source_chars)
        if max_source_chars is not None
        else int(_get(config, "claim.extract.max_source_chars", 0) or 0)
    )
    material = load_material(db, subject=subject, day_key=day_key, max_source_chars=cap)
    kinds = list(_get(config, "claim.extract.kinds", list(claim_mod.CLAIM_KINDS)) or [])
    min_conf = float(_get(config, "claim.extract.min_confidence", 0.6) or 0)
    cand_below = float(_get(config, "claim.extract.candidate_below", 0.8) or 0)
    example = [
        {
            "kind": "third_party",
            "subject": "冻梨",
            "predicate": "睡在",
            "object": "桌上",
            "polarity": 1,
            "status": "happened",
            "event_date": None,
            "mentioned_at": "2026-11-08T10:00:00+08:00",
            "event_end": None,
            "scope": None,
            "confidence": 0.9,
            "source_line": "<当天原话 id>",
            "evidence_quote": "冻梨倒好，趴桌上睡得四仰八叉",
        },
        {
            "kind": "third_party",
            "subject": "林潇",
            "predicate": "告知",
            "object": "书脊是米糊补的",
            "polarity": 1,
            "status": "happened",
            "event_date": None,
            "mentioned_at": "2026-11-08T10:00:00+08:00",
            "event_end": None,
            "scope": None,
            "confidence": 0.88,
            "source_line": "<当天原话 id>",
            "evidence_quote": "林潇被当面告知书脊是米糊补的",
        },
        {
            "kind": "preference",
            "subject": "self",
            "predicate": "不吃",
            "object": "香菜",
            "polarity": 0,
            "status": "happened",
            "event_date": None,
            "mentioned_at": "2026-11-08T10:00:00+08:00",
            "event_end": None,
            "scope": None,
            "confidence": 0.92,
            "source_line": "<当天原话 id>",
            "evidence_quote": "我不吃香菜",
        },
    ]
    task = "\n".join(
        [
            "你是 PMem 的断言抽取器。读下面「当天原话」，把用户的事实类陈述抽成 claim JSON。",
            "只抽事实：人 / 偏好 / 时间 / 第三方 / 边界；计划、情绪、叙事不抽。",
            f"kind 只能是：{' / '.join(kinds)}；status 只能是 happened / planned / hypothetical / rumored / unknown / candidate。",
            "输出必须是纯 JSON（不要 markdown）：{\"claims\": [ ... ]}，每个对象字段为：",
            "subject, kind, predicate, object, polarity(0 否定/1 肯定), status, event_date(ISO 或 null),",
            "mentioned_at(ISO), event_end(ISO 或 null), scope(仅 boundary：public/private/in_front_of:<实体>),",
            "confidence(0~1), source_line(原话 id), evidence_quote(必须逐字来自该原话)。",
            "subject 规则：",
            "- 关于「我/用户」的事实写 subject=\"self\"；",
            "- 关于第三人称（人/宠物，如冻梨、林潇）的事实，**实体名放 subject**，predicate 是关系或动作，object 是客体；",
            "  不要把人/宠物名字塞进 object，也不要把整个动作短语当 predicate。",
            "hard rules：",
            "1) evidence_quote 必须能在你标注的 source_line 原文里找到；找不到就整条别输出。",
            "2) event_date 必须能从 evidence_quote 直接读出，或由「昨天/上周五」按 mentioned_at 推得；",
            "   **推不出来就填 null，不要编日期**（不要因为日期推不出就把 status 改成 unknown）。",
            "3) 陈述本身不确定（可能/好像/也许）才写 status=unknown；明确发生的事实写 happened。",
            "4) 否定必须显式：原话有「没/不是/别/不要/不吃/不喝」时 polarity=0。",
            "5) **不抽时钟读数/当前时间**（如「八点半了」「昨晚两点多」）；相对时间（昨晚/昨天/今天/10月11号）",
            "   必须换算成绝对 event_date，算不出就整条别输出。",
            "6) predicate 只写关系词或**单个动作词**（住/叫/生日/不吃/不喝/别提/归还到/写在/告知/睡在），",
            "   不超过 4 个字；动词短语不要把地点/宾语吞进 predicate——例如「冻梨睡在桌上」应写",
            "   subject=冻梨, predicate=睡在, object=桌上。",
            f"7) confidence < {min_conf:g} 的条不要输出；< {cand_below:g} 的会落 candidate 等用户确认。",
            "只输出 JSON；没有可抽的就输出 {\"claims\": []}。",
            "示例（宠物、第三人称人物、本人）：",
            json.dumps({"claims": example}, ensure_ascii=False, indent=2),
        ]
    )
    return {
        "enabled": True,
        "day_key": day_key,
        "subject": subject,
        "material": material["lines"],
        "materials": material["lines"],
        "source_lines": material["lines"],
        "source_chars": material["source_chars"],
        "truncated": material["truncated"],
        "task": task,
        "task_text": task,
        "example": example,
        "fields": [
            "subject",
            "kind",
            "predicate",
            "object",
            "polarity",
            "status",
            "event_date",
            "mentioned_at",
            "event_end",
            "scope",
            "confidence",
            "source_line",
            "evidence_quote",
        ],
    }


def _normalise_payload(payload: Any) -> list[dict[str, Any]]:
    data = payload
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError as exc:
            raise ValidationError(f"payload 不是合法 JSON：{exc}") from exc
    if isinstance(data, dict):
        data = data.get("claims") or data.get("items") or []
    if not isinstance(data, list):
        raise ValidationError("payload 必须是 claims 数组或 {\"claims\": [...]}")
    return [item for item in data if isinstance(item, dict)]


def apply(
    db: Any,
    *,
    subject: str = "self",
    day_key: str,
    payload: Any,
    config: Any = None,
    material: list[dict[str, Any]] | None = None,
    extractor: str = "llm:host",
    actor: str = "system:claim",
) -> dict[str, Any]:
    """校验 host 输出并入库；不合法/无据的整条丢弃，返回统计与原因。"""
    if not _enabled(config):
        return {
            "enabled": False,
            "inserted": 0,
            "merged": 0,
            "updated": 0,
            "dropped": 0,
            "claims": [],
            "note": "claim.enabled=false，不写库",
        }
    lines = material if material is not None else load_material(
        db, subject=subject, day_key=day_key
    )["lines"]
    kinds = set(_get(config, "claim.extract.kinds", list(claim_mod.CLAIM_KINDS)) or [])
    min_conf = float(_get(config, "claim.extract.min_confidence", 0.6) or 0)
    cand_below = float(_get(config, "claim.extract.candidate_below", 0.8) or 0)
    version = bool(_get(config, "claim.version.enabled", False))
    stats: Counter[str] = Counter()
    decisions: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []

    for raw in _normalise_payload(payload):
        try:
            kind = str(raw.get("kind") or "").strip()
            predicate = str(raw.get("predicate") or "").strip()
            object_text = str(raw.get("object") or "").strip()
            quote = str(raw.get("evidence_quote") or "").strip()
            if kind not in kinds:
                stats["dropped_kind"] += 1
                continue
            if not predicate or not object_text or not quote:
                stats["dropped_missing"] += 1
                continue
            # subject → subject_id：host 直接写实体名（self / 冻梨 / 林潇），不再只认 subject_id。
            subject_value = str(raw.get("subject") or raw.get("subject_id") or subject).strip() or "self"
            if subject_value in {"self", "我", "用户", "user"}:
                subject_id = "self"
            else:
                subject_id = subject_value
            if kind == "date" and predicate in {"event_on", "date"}:
                predicate = "日期"
            if predicate in _BAD_PREDICATES:
                stats["dropped_predicate"] += 1
                continue
            if len(predicate) > _MAX_EXTRACT_PREDICATE or any(
                char in claim_mod._SENTENCE_PUNCT for char in predicate
            ):
                stats["dropped_predicate"] += 1
                continue
            # 不抽时钟读数/当前时间：date 类 object 是 20:30 / 两点多 这类直接丢。
            if kind == "date" and _CLOCK_OBJECT.match(object_text):
                stats["dropped_clock"] += 1
                continue
            confidence = claim_mod._clamp(raw.get("confidence", 0.8))
            if confidence < min_conf:
                stats["dropped_confidence"] += 1
                continue
            line, matched_by = _find_source(quote, lines)
            if line is None:
                stats["dropped_evidence"] += 1
                continue
            mentioned = str(raw.get("mentioned_at") or line.get("ts") or "")
            if not mentioned:
                mentioned = f"{day_key}T00:00:00+00:00"
            event_date = raw.get("event_date")
            if kind == "date" and _RELATIVE_DATE_OBJECT.match(object_text):
                if not event_date:
                    # 只能从 object 自己的相对说法推（不能拿 quote 里别的日期顶）。
                    derived = timeresolve.loose_relative_dates(object_text, mentioned)
                    event_date = derived[0][1] if derived else None
                if not event_date or object_text not in quote:
                    stats["dropped_date_unresolved"] += 1
                    continue
                object_text = str(event_date)[:10]
            date_ok, date_why = _date_supported(quote, event_date, mentioned)
            status = str(raw.get("status") or "happened").strip()
            if status not in claim_mod.CLAIM_STATUSES:
                status = "unknown"
            if not date_ok:
                # 日期推不出来只把 event_date 置 null；事实本身不因此变 unknown。
                event_date = None
                stats["date_null"] += 1
            if any(marker in quote for marker in _UNCERTAIN_MARKERS) and status in (
                "happened",
                "planned",
            ):
                status = "unknown"
                stats["uncertain"] += 1
            polarity, corrected = _polarity(quote, predicate, object_text, raw.get("polarity"))
            if corrected:
                stats["polarity_corrected"] += 1
            if confidence < cand_below and status in ("happened", "planned"):
                status = "candidate"
                stats["candidate"] += 1
            kept.append(
                {
                    "subject_id": subject_id,
                    "kind": kind,
                    "predicate": predicate,
                    "object": object_text,
                    "polarity": polarity,
                    "status": status,
                    "event_date": str(event_date)[:10] if event_date else None,
                    "mentioned_at": mentioned,
                    "event_end": raw.get("event_end"),
                    "scope": raw.get("scope"),
                    "confidence": confidence,
                    "source_line": str(line["id"]),
                    "source_turn": raw.get("source_turn"),
                    "evidence_quote": quote,
                    "extractor": str(raw.get("extractor") or extractor),
                    "version": version,
                    "actor": actor,
                }
            )
            decisions.append(
                {"action": "candidate", "matched_by": matched_by, "date_check": date_why}
            )
        except (ValidationError, ValueError, TypeError) as exc:
            stats["dropped_invalid"] += 1
            decisions.append({"action": "dropped", "error": str(exc)})

    counts: Counter[str] = Counter()
    saved: list[dict[str, Any]] = []
    for item in kept:
        try:
            result = claim_mod.create(db, **item)
        except ValidationError:
            stats["dropped_invalid"] += 1
            continue
        action = str(result.get("action") or "")
        counts[action] += 1
        saved.append({**item, "id": result.get("claim_id"), "action": action})
    return {
        "enabled": True,
        "scanned": len(kept) + sum(value for key, value in stats.items() if key.startswith("dropped_")),
        "kept": len(kept),
        "inserted": counts["inserted"],
        "merged": counts["merged"],
        "updated": counts["updated"],
        "versioned": counts["versioned"],
        "dropped": sum(value for key, value in stats.items() if key.startswith("dropped_")),
        "reasons": dict(stats),
        "claims": saved,
    }


def _rule_candidates(material: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """确定性规则候选（三类）：人名+关系、日期、带否定词的偏好。"""
    out: list[dict[str, Any]] = []
    for line in material:
        if line.get("role") != "user":
            continue
        text = str(line.get("text") or "")
        ts = str(line.get("ts") or "")
        for sentence in re.split(r"(?<=[。！？；])", text):
            sentence = sentence.strip()
            if not sentence:
                continue
            for pattern, kind, builder in (
                (_REL_NAME, "third_party", _rule_relation_name),
                (_REL_IS, "third_party", _rule_relation_is),
            ):
                match = pattern.search(sentence)
                if not match:
                    continue
                built = builder(match, sentence, line)
                if built:
                    out.append(built)
                break
            if _DATE_WORD.search(sentence):
                built = _rule_date(sentence, line)
                if built:
                    out.append(built)
            for pattern in (_NEGATED_PREF, _DONT):
                match = pattern.search(sentence)
                if not match:
                    continue
                neg = match.group("neg")
                obj = match.group("obj").strip()
                if not obj:
                    continue
                out.append(
                    {
                        "kind": "preference",
                        "subject": "self",
                        "predicate": neg,
                        "object": obj,
                        "polarity": 0,
                        "status": "happened",
                        "mentioned_at": ts,
                        "evidence_quote": sentence,
                        "confidence": 0.75,
                        "source_line": line["id"],
                    }
                )
                break
    # 同句重复规则命中只留一条（claim.create 还会按幂等键兜底）。
    unique: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    for item in out:
        if not _rule_ok(item):
            continue
        key = (
            str(item.get("source_line") or ""),
            str(item.get("predicate") or ""),
            str(item.get("object") or ""),
            int(item.get("polarity") or 0),
        )
        unique.setdefault(key, item)
    # 每种 kind 每天最多 N 条（按 confidence 取）：长对话日不刷几百条噪音。
    capped: list[dict[str, Any]] = []
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for item in unique.values():
        by_kind.setdefault(str(item.get("kind") or ""), []).append(item)
    for items in by_kind.values():
        items.sort(key=lambda entry: -float(entry.get("confidence") or 0.0))
        capped.extend(items[:_RULE_MAX_PER_KIND])
    return capped


def _rule_relation_name(match: re.Match[str], sentence: str, line: dict[str, Any]) -> dict[str, Any]:
    rel, name = match.group("rel"), match.group("name")
    return {
        "kind": "third_party",
        "subject": name,
        "predicate": "是",
        "object": rel,
        "polarity": 1,
        "status": "happened",
        "mentioned_at": line.get("ts"),
        "evidence_quote": sentence,
        "confidence": 0.8,
        "source_line": line["id"],
    }


def _rule_relation_is(match: re.Match[str], sentence: str, line: dict[str, Any]) -> dict[str, Any]:
    name, rel = match.group("name"), match.group("rel")
    return {
        "kind": "third_party",
        "subject": name,
        "predicate": "是",
        "object": rel,
        "polarity": 1,
        "status": "happened",
        "mentioned_at": line.get("ts"),
        "evidence_quote": sentence,
        "confidence": 0.75,
        "source_line": line["id"],
    }


def _rule_date(sentence: str, line: dict[str, Any]) -> dict[str, Any] | None:
    match = _DATE_WORD.search(sentence)
    if not match:
        return None
    phrase = match.group(0)
    mentioned = str(line.get("ts") or "")
    rel = timeresolve.loose_relative_dates(phrase, mentioned)
    event_date = rel[0][1] if rel else None
    if event_date is None:
        try:
            from . import selfcheck

            base = clock.parse_day(str(mentioned)[:10]) if mentioned else None
            if base is not None:
                for hit in selfcheck._extract_dates(phrase, today=base, limit=5):
                    resolved = selfcheck._resolve_date(hit, base)
                    if resolved is not None:
                        event_date = resolved.isoformat()
                        break
        except Exception:  # noqa: BLE001
            event_date = None
    if not event_date:
        return None  # 规则层不猜日期：算不出绝对日期就不产出（不许把「今天」当 object）
    return {
        "kind": "date",
        "subject": "self",
        "predicate": "日期",
        "object": str(event_date)[:10],
        "polarity": 1,
        "status": "happened",
        "event_date": str(event_date)[:10],
        "mentioned_at": mentioned,
        "evidence_quote": sentence,
        "confidence": 0.75,
        "source_line": line["id"],
    }


def rule_candidates(material: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """规则候选（公开包装，便于测试/回填前预检）；已做谓词/客体过滤与 per-kind 上限。"""
    return _rule_candidates(material)


def rule_extract(
    db: Any,
    *,
    subject: str = "self",
    day_key: str,
    config: Any = None,
    material: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """无宿主/超时时的确定性降级：只认三类，宁少抽。"""
    if not _enabled(config):
        return {
            "enabled": False,
            "claims": [],
            "inserted": 0,
            "merged": 0,
            "updated": 0,
            "dropped": 0,
            "note": "claim.enabled=false，规则抽取也不写库",
        }
    lines = material if material is not None else load_material(
        db, subject=subject, day_key=day_key
    )["lines"]
    candidates = _rule_candidates(lines)
    result = apply(
        db,
        subject=subject,
        day_key=day_key,
        payload={"claims": candidates},
        config=config,
        material=lines,
        extractor="rule:v1",
        actor="system:claim",
    )
    result["rule_candidates"] = candidates
    return result
