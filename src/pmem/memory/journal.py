"""日记（M10）：把当天的原话消化成一篇日记。

三层分工，正好对上「重要 / 基本 / 不重要」：

- ``journal_line`` 原始流水 —— 原样存、不做判定，只在库里活很短一段；
- ``episode``      日记正文 —— 派生结论，一天一篇，带证据指针；
- ``memory``       日记里抽出的事实 —— 带 ``episode_id``，于是会进星图。

为什么不让模型「随口写」：写日记是**批量**动作，一次调用处理一整天，
比每轮多塞几十 token 便宜，也比每轮让模型自己决定「这值不值得记」稳定。
这也正是人睡觉时干的事：白天攒流水，晚上才整理。

两条硬约束：

1. **原文只追加**：流水只 INSERT，正文从不改；日记记的是派生结论。
2. **结论必有证据**：日记里出现的每个《专名》必须在当天原文里能找到，
   每行日记至少要和原文共有一句四字片段，否则整篇判为编造、回落到模板版。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..base import events
from ..base.clock import day_key as today_key
from ..base.clock import now, now_iso, to_iso
from ..base.errors import NotFoundError
from ..base.ids import ulid
from .text import tokenize

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
CHANNEL = "journal"

# 模型写日记时给的指令。要点：只许概括，不许新增；专名照抄；人称不能错。
# 人称是最容易错的地方：原话里「我」指用户，日记里必须变成「你」，
# 而日记自己的「我」才是写日记的人。第一版就栽在这里（「我妈说明天过来，让你……」）。
JOURNAL_SYSTEM = (
    "你在写今天的日记，写给自己看。\n"
    "人称规则（最容易错，先看这条）：\n"
    "- 原文里「你：」是对方说的话，里面的「我」指对方；写进日记时一律改成「你」。\n"
    "- 原文里「我：」是你自己说的话，写进日记仍然用「我」。\n"
    "- 于是日记里「你」永远指对方，「我」永远指你自己，不要混用。\n"
    "例：原文「你：我妈说明天过来，让我收拾屋子」→ 日记「你妈说明天过来，让你收拾屋子」。\n"
    "其它硬规则：\n"
    "1. 只能使用原文里出现过的信息，一个都不许新增；\n"
    "2. 专名照抄原文（动画名、人名、地名），不要改写成别的说法；\n"
    "3. 每行不超过 30 个字，写 3 到 6 行，按时间顺序；\n"
    "4. 标题写今天最值得记的一件事，不超过 20 字，**不要写日期**；\n"
    "5. 不写评价、不写推测、不写「可能」「似乎」。\n"
    '只输出 JSON：{"title": "...", "lines": ["...", "..."]}'
)

_TITLE_RE = re.compile(r"[\r\n]+")
_BOOK_RE = re.compile(r"《([^》]{1,40})》")


@dataclass
class DayDraft:
    """一篇日记的草稿。``lines`` 用第二人称，事实由它派生。"""

    title: str
    lines: list[str] = field(default_factory=list)
    mode: str = "template"
    tokens: int = 0
    notes: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


# -- 流水 ------------------------------------------------------------------

# DSH 每轮附带的运行时上下文。它**有时排在用户真消息前面**，而且行长得很像正文，
# 所以不能指望调用方（插件）永远剥干净：网关这一层要自己再兜一次。
# 实测漏网的是 ``Current DeepSeek Harness implementation checkout: …`` 这一行——
# 只挡 ``Current DSH`` / ``Current runtime context`` 是不够的，直接挡 ``Current ` 前缀。
# 另外两类也真实漏过（写完日记才发现当天流水里有一半是脚手架的台词）：
# ``You are the Team Lead named "lead"…`` / ``Use list_agents({})…`` 这类**宿主人格提示**，
# 以及 ``Background subagent … finished`` 这类**子代理通知**——它们都不是用户说的话，
# 一旦进了流水就会派生出「用户You are the Team Lead…」这种记忆，比漏记更糟。
_RUNTIME_LINE = re.compile(
    r"^(Current |Approval prompts|Browser time zone|Time sampled|"
    r"\[骨架\]|\[索引\]|\[提示\]|<<<PMEM_WAKE|"
    r"You are the Team Lead|You are an AI agent powered|Use list_agents\(|The Team Lead named|"
    r"Agent Teams is available|Background subagent |- )"
)


def _strip_runtime(text: str) -> str:
    """剥掉开头的运行时行，只留真消息。全被剥光就返回空串（那这行本来就不该记）。"""
    lines = str(text or "").splitlines()
    start = 0
    while start < len(lines):
        line = lines[start].strip()
        if line == "" or _RUNTIME_LINE.match(line):
            start += 1
            continue
        break
    return chr(10).join(lines[start:]).strip()



def append(
    db: Any,
    text: str,
    *,
    subject: str = "self",
    session_id: str | None = None,
    role: str = ROLE_USER,
    ts: str | None = None,
) -> dict[str, Any]:
    """追加一行流水。原文原样存，不做任何改写。"""
    body = _strip_runtime(text)
    if not body:
        return {"ok": False, "reason": "empty"}
    if role not in (ROLE_USER, ROLE_ASSISTANT):
        raise ValueError(f"role 只能是 user/assistant：{role}")
    stamp = ts or now_iso()
    line_id = ulid()
    day = today_key() if ts is None else _day_of(stamp)
    cur = db.conn.execute(
        "INSERT INTO journal_line(id, subject_id, session_id, day_key, ts, role, text, created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (line_id, subject, session_id, day, stamp, role, body, now_iso()),
    )
    # 原文兜底索引：派生事实会截断/改人称，原话原样进 FTS，检索薄弱时回来捞。
    with_index = True
    try:
        db.conn.execute(
            "INSERT INTO journal_fts(rowid, text_seg) VALUES(?,?)",
            (int(cur.lastrowid), tokenize(body)),
        )
    except Exception:  # noqa: BLE001 索引写失败不能挡住原文入库（旧库/只读场景）
        with_index = False
    return {"ok": True, "id": line_id, "day_key": day, "role": role, "indexed": with_index}


# -- 撤回（M27 / 1.10.0） --------------------------------------------------
#
# 为什么「撤回」不是「删除」：
# - 删除是**清账**：这条不该存在，删掉（memory_delete --hard 不可逆）；
# - 撤回是**表态**：这句我说过，但它不算数了。原文和事件链一个字都不改，
#   只多一条「撤回」事实；读的时候默认不看它，想查的时候照样查得到。
# 谁可以撤：用户（"这句别记了"）和 AI（"我刚才说错了"）都可以；原话只追加，
# 所以撤回只加墓碑，不删行。


def retracted_ids(
    db: Any, *, subject: str = "self", line_ids: Iterable[str] | None = None
) -> set[str]:
    """一批原话 id 里，哪些已经被撤回过。``line_ids=None`` 表示整库（统计用）。"""
    if line_ids is not None:
        ids = list(line_ids)[:900]
        if not ids:
            return set()
        marks = ",".join("?" for _ in ids)
        rows = db.conn.execute(
            f"SELECT line_id FROM journal_retraction WHERE subject_id=? AND line_id IN ({marks})",
            (subject, *ids),
        ).fetchall()
    else:
        rows = db.conn.execute(
            "SELECT line_id FROM journal_retraction WHERE subject_id=?", (subject,)
        ).fetchall()
    return {str(row["line_id"]) for row in rows}


def retract(
    db: Any,
    line_id: str,
    *,
    subject: str = "self",
    reason: str = "",
    actor: str = "self",
    excerpt_chars: int = 120,
) -> dict[str, Any]:
    """撤回一条原话：追加墓碑，原文不动。重复撤回返回 ``already=True``。"""
    row = db.conn.execute(
        "SELECT id, session_id, role, text FROM journal_line WHERE id=? AND subject_id=?",
        (line_id, subject),
    ).fetchone()
    if row is None:
        raise NotFoundError(
            f"找不到这条原话 {line_id}",
            hint="先用 memory_trace / pmem trace 查到原话 id，再撤；认不出是哪句就不撤。",
            field="target",
        )
    existing = db.conn.execute(
        "SELECT id, created_at FROM journal_retraction WHERE subject_id=? AND line_id=?",
        (subject, line_id),
    ).fetchone()
    if existing is not None:
        return {
            "ok": True,
            "already": True,
            "id": existing["id"],
            "line_id": line_id,
            "created_at": existing["created_at"],
        }
    body = str(row["text"] or "")
    excerpt = body if len(body) <= excerpt_chars else body[: max(1, excerpt_chars - 1)] + "…"
    rid = ulid()
    stamp = now_iso()
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO journal_retraction(id, subject_id, line_id, session_id, role, reason, "
            "excerpt, actor, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                rid,
                subject,
                line_id,
                row["session_id"],
                row["role"],
                str(reason or ""),
                excerpt,
                actor,
                stamp,
            ),
        )
        events.append(
            conn,
            actor=actor,
            op="journal.retract",
            target_tbl="journal_line",
            target_id=line_id,
            subject_id=subject,
            payload={
                "reason": str(reason or ""),
                "excerpt": excerpt,
                "session_id": row["session_id"],
                "role": row["role"],
            },
        )
    return {
        "ok": True,
        "already": False,
        "id": rid,
        "line_id": line_id,
        "session_id": row["session_id"],
        "role": row["role"],
        "excerpt": excerpt,
        "created_at": stamp,
    }


def _day_of(iso: str) -> str:
    """ISO 时间 → 本地日期键。解析失败就退回今天，不抛。"""
    try:
        from datetime import datetime

        return datetime.fromisoformat(iso).astimezone().date().isoformat()
    except (TypeError, ValueError):
        return today_key()


def pending(
    db: Any,
    *,
    subject: str = "self",
    day_key: str | None = None,
    limit: int = 200,
) -> list[Any]:
    """还没消化的流水，按时间正序。

    撤回过的原话不再进待消化队列：它已经是「不算数」的表态，不该再变成日记。
    """
    if day_key is None:
        sql = (
            "SELECT * FROM journal_line jl WHERE jl.subject_id=? AND jl.episode_id IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM journal_retraction r "
            "WHERE r.subject_id=jl.subject_id AND r.line_id=jl.id) "
            "ORDER BY jl.seq LIMIT ?"
        )
        params: tuple[Any, ...] = (subject, int(limit))
    else:
        sql = (
            "SELECT * FROM journal_line jl WHERE jl.subject_id=? AND jl.episode_id IS NULL "
            "AND jl.day_key=? AND NOT EXISTS (SELECT 1 FROM journal_retraction r "
            "WHERE r.subject_id=jl.subject_id AND r.line_id=jl.id) "
            "ORDER BY jl.seq LIMIT ?"
        )
        params = (subject, day_key, int(limit))
    return list(db.conn.execute(sql, params).fetchall())


def pending_days(db: Any, *, subject: str = "self") -> list[str]:
    rows = db.conn.execute(
        "SELECT jl.day_key, COUNT(*) c FROM journal_line jl WHERE jl.subject_id=? "
        "AND jl.episode_id IS NULL AND NOT EXISTS (SELECT 1 FROM journal_retraction r "
        "WHERE r.subject_id=jl.subject_id AND r.line_id=jl.id) "
        "GROUP BY jl.day_key ORDER BY jl.day_key",
        (subject,),
    ).fetchall()
    return [row["day_key"] for row in rows]


# -- 写日记 ----------------------------------------------------------------


def _squash(text: str) -> str:
    return " ".join((text or "").split())


def template_draft(rows: Iterable[Any], *, max_lines: int = 6) -> DayDraft:
    """没有模型时的兜底：原话挑几句，按时间串起来。

    不做概括也就不可能编；代价是读起来像流水账而不是日记。
    """
    picked: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if row["role"] != ROLE_USER:
            continue
        line = _squash(row["text"])
        if not line or line in seen:
            continue
        seen.add(line)
        # 用户原话里每个「我」都指用户，进日记一律变「你」（不只是句首那个）。
        # 「我们」先挖掉再放回来，免得被替换成「你们」。
        picked.append(line.replace("我们", "\x00").replace("我", "你").replace("\x00", "我们"))
        if len(picked) >= max_lines:
            break
    if not picked:
        # 全是助手的话：退而求其次，用它们的开头
        for row in rows:
            line = _squash(row["text"])
            if line:
                picked.append(("我对你说：" + line)[:34])
            if len(picked) >= 3:
                break
    title = (picked[0][:20] if picked else "没有留下什么")
    return DayDraft(title=title, lines=picked, mode="template")


def _parse_json(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def model_draft(
    model: Any,
    rows: Iterable[Any],
    *,
    day_key: str,
    max_chars: int = 3000,
) -> DayDraft:
    """让模型把一天写成一页日记。返回草稿，校验交给 ``validate``。"""
    chunks: list[str] = []
    used = 0
    for row in rows:
        who = "你" if row["role"] == ROLE_USER else "我"
        line = f"{who}：{_squash(row['text'])}"
        if used + len(line) > max_chars:
            break
        chunks.append(line)
        used += len(line)
    source = "\n".join(chunks)
    messages = [
        {"role": "system", "content": JOURNAL_SYSTEM},
        {"role": "user", "content": f"日期：{day_key}\n原文：\n{source}"},
    ]
    reply = model.complete(messages, None, hint={"write": False, "round": 0})
    tokens = int(getattr(getattr(reply, "usage", None), "total", 0) or 0)
    data = _parse_json(getattr(reply, "content", "") or "")
    if data is None:
        draft = template_draft(rows)
        draft.problems = ["模型没有给出合法 JSON"]
        draft.notes["raw"] = (getattr(reply, "content", "") or "")[:300]
        return draft
    title = _squash(str(data.get("title") or ""))[:40]
    lines = [_squash(str(x))[:60] for x in (data.get("lines") or []) if _squash(str(x))]
    draft = DayDraft(title=title or "今天", lines=lines[:8], mode="model", tokens=tokens)
    draft.notes["source"] = source
    return draft


# -- 校验：防止日记慢慢开始编 ------------------------------------------------


def _shingles(text: str, size: int = 3) -> set[str]:
    body = re.sub(r"\s+", "", text or "")
    return {body[i : i + size] for i in range(max(0, len(body) - size + 1))}


def validate(draft: DayDraft, source_rows: Iterable[Any]) -> list[str]:
    """返回问题清单；空清单表示可以落库。"""
    rows = list(source_rows)
    source = "\n".join(row["text"] for row in rows)
    problems: list[str] = []
    if not draft.lines:
        problems.append("empty")
    for name in _BOOK_RE.findall("\n".join(draft.lines + [draft.title])):
        if name not in source:
            problems.append(f"unknown_name:{name}")
    src = _shingles(source)
    for line in draft.lines:
        if not _shingles(line) & src:
            problems.append(f"no_overlap:{line[:20]}")
    return problems


# -- 消化 ------------------------------------------------------------------


def build_writer(config: Any, *, model: str | None = None) -> Callable[[list[Any]], DayDraft] | None:
    """按配置造一个「用模型写日记」的 writer；没有模型或没有凭证就返回 ``None``。

    失败一律返回 ``None`` 而不是抛：日记必须在**没有任何外部依赖**时也能写出来，
    只是写成流水账。模型是加分项，不是前置条件。
    """
    name = model or str(_cfg(config).get("model") or "").strip() or os.environ.get("PMEM_JOURNAL_MODEL", "").strip()
    if not name:
        return None
    try:
        from ..bench.model import make_model

        client = make_model("openai", model=None if name in ("openai", "default") else name)
    except Exception:  # 没凭证 / 没网络配置：静默退回模板
        return None

    def _writer(rows: list[Any]) -> DayDraft:
        return model_draft(client, rows, day_key=rows[0]["day_key"] if rows else today_key())

    setattr(_writer, "model_name", getattr(client, "name", name))
    return _writer


def digest(
    db: Any,
    *,
    subject: str = "self",
    day_key: str | None = None,
    writer: Callable[[list[Any]], DayDraft] | None = None,
    store: Any = None,
    config: Any = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """把某天（默认「最近一个还有流水的日子」）消化成一篇日记。"""
    cfg = _cfg(config)
    if writer is None:
        writer = build_writer(config)
    day = day_key or (pending_days(db, subject=subject)[-1:] or [today_key()])[0]
    rows = pending(db, subject=subject, day_key=day, limit=int(cfg.get("max_lines", 200)))
    if not rows:
        return {"ok": False, "reason": "no_lines", "day_key": day}
    # 门槛只拦「今天还没结束」：今天的流水可能正聊到一半，凑成一两句先别写。
    # 已经过去的日子必须结账，否则这一天永远消化不掉，流水也永远清不掉。
    min_lines = int(cfg.get("min_lines", 3)) if day == today_key() else 1
    if len(rows) < min_lines:
        return {"ok": False, "reason": "too_few_lines", "day_key": day, "lines": len(rows)}

    draft = (writer or template_draft)(rows)
    return _finish(db, subject=subject, day=day, rows=rows, draft=draft, store=store, dry_run=dry_run)


def write_episode(
    db: Any,
    *,
    subject: str = "self",
    day_key: str | None = None,
    title: str = "",
    lines: list[str] | None = None,
    store: Any = None,
    config: Any = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """「由我写」的那条路：模型在对话里给出 title + lines，这里只校验和落库。

    为什么不调模型自己写：写日记这件事，最合适的作者本来就是**当时在场的那个模型**——
    它刚聊完这一天，不需要凭证，也不需要把原文再发一遍给另一个端点。
    代价是它得在对话里想起来，所以醒来卡会在该睡的时候提醒一句。
    加固靠的不是模型自觉，而是 ``validate()``：编出来的专名和自造句子都会被拦下，
    拦下就整篇回落到模板版，问题清单写进 ``extra_json``。
    """
    cfg = _cfg(config)
    day = day_key or (pending_days(db, subject=subject)[-1:] or [today_key()])[0]
    rows = pending(db, subject=subject, day_key=day, limit=int(cfg.get("max_lines", 200)))
    if not rows:
        return {"ok": False, "reason": "no_lines", "day_key": day}
    body = [_squash(x)[:60] for x in (lines or []) if _squash(x)]
    draft = DayDraft(
        title=_squash(title)[:40] or (body[0][:20] if body else "今天"),
        lines=body[:8],
        mode="model",
    )
    if not draft.lines:
        return {"ok": False, "reason": "empty", "day_key": day}
    return _finish(db, subject=subject, day=day, rows=rows, draft=draft, store=store, dry_run=dry_run)


def _finish(
    db: Any,
    *,
    subject: str,
    day: str,
    rows: list[Any],
    draft: DayDraft,
    store: Any,
    dry_run: bool,
) -> dict[str, Any]:
    """校验 → 回落 → 落 episode → 原文打上 episode_id → 事实进记忆库。"""
    problems = validate(draft, rows)
    if problems:
        # 编造就整篇回落：宁可流水账，也不要假日记。
        fallback = template_draft(rows)
        fallback.problems = problems
        fallback.tokens = draft.tokens
        draft = fallback
    summary = chr(10).join(draft.lines)
    episode_id = ulid()
    facts = _facts(draft, rows=rows)
    derivation = {
        "lines": [row["id"] for row in rows],
        "line_count": len(rows),
        "mode": draft.mode,
        "problems": draft.problems,
    }
    result: dict[str, Any] = {
        "ok": True,
        "episode_id": episode_id,
        "day_key": day,
        "title": draft.title,
        "summary": summary,
        "lines": len(draft.lines),
        "mode": draft.mode,
        "problems": draft.problems,
        "tokens": draft.tokens,
        "facts": facts,
        "memory_ids": [],
    }
    if dry_run:
        return result

    with db.tx():
        db.conn.execute(
            "INSERT INTO episode(id, subject_id, channel, started_at, ended_at, day_key, title, "
            "summary, memory_count, derivation, extra_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                episode_id,
                subject,
                CHANNEL,
                rows[0]["ts"],
                rows[-1]["ts"],
                day,
                draft.title,
                summary,
                # 只有真的往记忆库派生时才把事实数写进去：传 store=None 表示
                # 「只写日记、不派生事实」（比如这篇写的是我自己的事），不能报个假数字。
                len(facts) if store is not None else 0,
                json.dumps(derivation, ensure_ascii=False, sort_keys=True),
                json.dumps(
                    {"mode": draft.mode, "tokens": draft.tokens, "problems": draft.problems},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        )
        for row in rows:
            db.conn.execute("UPDATE journal_line SET episode_id=? WHERE id=?", (episode_id, row["id"]))
        # 日记自己的检索通道（M18）：写完立刻进索引，否则「昨天咱们聊了啥」
        # 要等下次 ensure_episode_index 才找得到。
        index_episode(db, episode_id)
        events.append(
            db.conn,
            actor="journal",
            op="journal.digest",
            target_tbl="episode",
            target_id=episode_id,
            subject_id=subject,
            payload={"day_key": day, "lines": len(rows), "mode": draft.mode, "facts": len(facts)},
        )

    if store is not None and facts:
        for index, text in enumerate(facts):
            try:
                saved = store.add(
                    text,
                    kind="episodic",
                    grade="C",
                    grade_reason="日记派生：日常流水，按时间留着，不抢常驻位",
                    provenance=("user_explicit" if draft.mode == "template" else "observed"),
                    source_kind="journal",
                    source_ref=episode_id,
                    episode_id=episode_id,
                    session_id=None,
                    idem_key=f"journal:{episode_id}:{index}",
                    valid_from=rows[0]["ts"],
                )
            except Exception as exc:  # 一条事实写不进去，不能带崩整篇日记
                result.setdefault("errors", []).append({"text": text, "error": str(exc)})
                continue
            if isinstance(saved, dict) and saved.get("id"):
                result["memory_ids"].append(saved["id"])
    return result


_FAMILY = {
    "妈": "妈妈",
    "爸": "爸爸",
    "哥": "哥哥",
    "姐": "姐姐",
    "弟": "弟弟",
    "妹": "妹妹",
    "爷": "爷爷",
    "奶": "奶奶",
}


def _third_person(text: str) -> str:
    """用户原话 → 记忆事实的句式（统一成「用户……」）。确定性，不调模型。"""
    body = _squash(text).replace("我们", "用户").replace("我", "用户")
    for short, full in _FAMILY.items():
        body = body.replace("用户" + short, "用户" + full)
    body = body.replace("用户用户", "用户")
    if body and "用户" not in body:
        body = "用户" + body  # 原话里没有「我」也要能独立成句
    return body


def _facts(draft: DayDraft, *, rows: list[Any]) -> list[str]:
    """日记 → 记忆事实。

    **从用户原话派生，不从日记派生**。日记是给人读的（人称、措辞随模型走），
    事实是要被检索的（必须稳定、可追证）。两者分开之后，
    模型写得好不好都不会污染证据层——第一版拿日记改写，结果得到
    「用户我妈说明天过来」，是人称串了的直接后果。
    """
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if row["role"] != ROLE_USER:
            continue
        body = _third_person(row["text"])[:80]
        if len(body) < 4 or body in seen:
            continue
        seen.add(body)
        out.append(body)
    if out:
        return out
    # 一天全是助手的话（少见）：退回日记行，至少留个痕迹
    for line in draft.lines:
        body = _third_person(line)
        if len(body) < 4 or body in seen:
            continue
        seen.add(body)
        out.append(body[:80])
    return out


def _cfg(config: Any) -> dict[str, Any]:
    if config is None:
        return {"max_lines": 200, "min_lines": 3, "keep_days": 30}
    try:
        return dict(config.get("journal", {}) or {})
    except Exception:  # 配置对象形状不定时别炸
        return {"max_lines": 200, "min_lines": 3, "keep_days": 30}


# -- 什么时候该睡 ----------------------------------------------------------


def due(
    db: Any,
    *,
    subject: str = "self",
    now_ts: float | None = None,
    idle_minutes: int = 45,
    min_lines: int = 3,
    stale_days: int = 2,
) -> dict[str, Any]:
    """判断现在要不要写日记。返回 ``{"due": bool, "day_key": ..., "reason": ...}``。

    两种触发：

    - ``rollover``：有更早的日子还留着没消化的流水（跨天了，必须补）；
    - ``idle``：今天攒够了行数，而且安静了 ``idle_minutes`` 分钟。
    """
    days = pending_days(db, subject=subject)
    if not days:
        return {"due": False, "reason": "no_lines"}
    today = today_key()
    older = [day for day in days if day < today]
    if older:
        # 跨天不立刻抢着写：醒来卡会提醒「今天还有 N 行没整理」，
        # 让在场的模型自己写一篇（它刚聊完这一天，比模板版好得多）。
        # 但拖太久也不行——超过 stale_days 就由 daemon 用模板结账，防止流水烂在库里。
        stale = [day for day in older if _days_between(day, today) >= max(1, int(stale_days))]
        if stale:
            return {"due": True, "day_key": stale[0], "reason": "rollover"}
        return {"due": False, "reason": "awaiting_model", "day_key": older[-1], "days": len(older)}
    pending_today = pending(db, subject=subject, day_key=today)
    if len(pending_today) < min_lines:
        return {"due": False, "reason": "too_few_lines", "day_key": today, "lines": len(pending_today)}
    last = pending_today[-1]["ts"]
    silence_s = _elapsed_s(last, now_ts)
    if silence_s is None:
        return {"due": False, "reason": "bad_timestamp", "day_key": today}
    if silence_s >= idle_minutes * 60:
        return {"due": True, "day_key": today, "reason": "idle", "idle_minutes": round(silence_s / 60, 1)}
    return {"due": False, "reason": "recent", "day_key": today, "idle_minutes": round(silence_s / 60, 1)}


def _days_between(day: str, today: str) -> int:
    from datetime import date

    try:
        return (date.fromisoformat(today) - date.fromisoformat(day)).days
    except ValueError:
        return 0


def _elapsed_s(iso: str, now_ts: float | None) -> float | None:
    try:
        from datetime import datetime

        then = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    reference = now() if now_ts is None else datetime.fromtimestamp(now_ts, tz=then.tzinfo)
    return (reference - then).total_seconds()


# -- 日记检索通道（M18） ----------------------------------------------------


def _episode_text(title: str | None, summary: str | None) -> str:
    parts = [str(title or "").strip(), str(summary or "").strip()]
    return "\n".join(part for part in parts if part)


def index_episode(db: Any, episode_id: str) -> bool:
    """把一篇日记写进 ``episode_fts``（幂等）。

    为什么索引跟正文分开放：日记正文一落库就不改（派生结论，可重放），
    索引坏了随时能整表重算，所以它只是缓存，不是真相。
    """
    row = db.conn.execute("SELECT rowid, title, summary FROM episode WHERE id=?", (episode_id,)).fetchone()
    if row is None:
        return False
    text = _episode_text(row["title"], row["summary"])
    if not text:
        return False
    try:
        db.conn.execute("DELETE FROM episode_fts WHERE rowid=?", (int(row["rowid"]),))
        db.conn.execute(
            "INSERT INTO episode_fts(rowid, text_seg) VALUES(?,?)",
            (int(row["rowid"]), tokenize(text)),
        )
    except Exception:  # noqa: BLE001 老库/只读：索引写不进去不该带崩写日记
        return False
    return True


def ensure_episode_index(db: Any, *, limit: int | None = None) -> int:
    """补齐日记索引（1.9.0 之前建的库没有 episode_fts）。幂等。"""
    try:
        missing = db.conn.execute(
            "SELECT COUNT(*) AS c FROM episode e "
            "WHERE NOT EXISTS (SELECT 1 FROM episode_fts f WHERE f.rowid=e.rowid)"
        ).fetchone()["c"]
    except Exception:  # noqa: BLE001 没有表（极旧库/只读）就跳过
        return 0
    if not missing:
        return 0
    sql = (
        "SELECT e.rowid, e.title, e.summary FROM episode e "
        "WHERE NOT EXISTS (SELECT 1 FROM episode_fts f WHERE f.rowid=e.rowid) ORDER BY e.day_key"
    )
    params: tuple[Any, ...] = ()
    if limit:
        sql += " LIMIT ?"
        params = (int(limit),)
    rows = db.conn.execute(sql, params).fetchall()
    done = 0
    for row in rows:
        text = _episode_text(row["title"], row["summary"])
        if not text:
            continue
        try:
            db.conn.execute(
                "INSERT INTO episode_fts(rowid, text_seg) VALUES(?,?)",
                (int(row["rowid"]), tokenize(text)),
            )
            done += 1
        except Exception:  # noqa: BLE001 单行失败不挡整批
            continue
    return done


def episode_item(row: Any, *, score: float = 0.72, limit: int = 260) -> dict[str, Any]:
    """一篇日记变成检索结果里的一条（形状和记忆条目一致，前端不用特判）。"""
    day = str(row["day_key"] or "")
    title = str(row["title"] or "")
    summary = str(row["summary"] or "")
    head = f"【日记 {day}】{title}" if title else f"【日记 {day}】"
    body = f"{head}｜{summary}" if summary else head
    return {
        "id": row["id"],
        "text": body[:limit],
        "score": round(float(score), 4),
        "kind": "episode",
        "day": day,
        "importance": 0.5,
        "confidence": 0.7,
        "provenance": "observed",
        "channels": ["episode"],
        "truncated": len(body) > limit,
        "groups": [],
        "grade": None,
        "source": "episode",
        "memory_count": int(row["memory_count"] or 0) if "memory_count" in row.keys() else 0,
    }


def search_episodes(
    db: Any,
    *,
    query: str,
    since: str | None = None,
    until: str | None = None,
    subject: str = "self",
    limit: int = 3,
) -> list[dict[str, Any]]:
    """在日记里找「那几天」。

    两段式，和记忆检索同一个思路：先用词找（bm25），再按时间窗收窄。
    词一个都没命中时**按窗口退化成最近几篇**——「上周都干嘛了」问的是经过，
    就算一个专名都没提到，那几天的日记也是答案。
    """
    limit = max(1, int(limit))
    tokens = [t for t in tokenize(str(query or "")).split() if t.strip()][:48]
    rows: list[Any] = []
    if tokens:
        expr = " OR ".join(f'"{t.replace(chr(34), chr(34) * 2)}"' for t in tokens)
        sql = (
            "SELECT e.id, e.day_key, e.title, e.summary, e.memory_count, bm25(episode_fts) AS bm25 "
            "FROM episode_fts JOIN episode e ON e.rowid=episode_fts.rowid "
            "WHERE episode_fts MATCH ? AND e.subject_id=?"
        )
        params: list[Any] = [expr, subject]
        if since:
            sql += " AND e.day_key>=?"
            params.append(since)
        if until:
            sql += " AND e.day_key<=?"
            params.append(until)
        sql += " ORDER BY bm25(episode_fts) LIMIT ?"
        params.append(limit * 4)
        try:
            rows = db.conn.execute(sql, tuple(params)).fetchall()
        except Exception:  # noqa: BLE001 没索引就当没有这条通道
            rows = []
    if not rows:
        # 退化：窗口内最近几篇（只在给了窗口时做，否则会把无关日记塞进普通检索）
        if not since:
            return []
        try:
            if until and until == since:
                # 写明了是哪一天：连词都没命中也要那一天的那一篇。
                rows = db.conn.execute(
                    "SELECT id, day_key, title, summary, memory_count FROM episode "
                    "WHERE subject_id=? AND day_key=? ORDER BY ended_at DESC LIMIT ?",
                    (subject, since, limit),
                ).fetchall()
            else:
                rows = db.conn.execute(
                    "SELECT id, day_key, title, summary, memory_count FROM episode "
                    "WHERE subject_id=? AND day_key>=? ORDER BY day_key DESC LIMIT ?",
                    (subject, since, limit),
                ).fetchall()
        except Exception:  # noqa: BLE001
            return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if len(out) >= limit:
            break
        day = str(row["day_key"] or "")
        if day in seen:
            continue
        seen.add(day)
        # 打分只用来决定日记之间的先后：命中靠前的几篇稍微高一点，
        # 和记忆的分数不在同一个量纲上（日记不该压掉真正对得上的事实，见 store 的插入位置）。
        score = max(0.55, 0.80 - 0.05 * index)
        out.append(episode_item(row, score=score))
    return out


# -- 读 --------------------------------------------------------------------


def recent(
    db: Any,
    *,
    subject: str = "self",
    days: int = 2,
    items: int = 2,
) -> list[dict[str, Any]]:
    """最近几天的日记，新的在前。醒来卡的「最近」就取这个。"""
    from datetime import timedelta

    cutoff = (now() - timedelta(days=max(1, int(days)))).date().isoformat()
    rows = db.conn.execute(
        "SELECT id, day_key, title, summary, memory_count FROM episode WHERE subject_id=? "
        "AND day_key>=? ORDER BY day_key DESC, ended_at DESC LIMIT ?",
        (subject, cutoff, max(1, int(items))),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "id": row["id"],
                "day_key": row["day_key"],
                "title": row["title"],
                "summary": row["summary"],
                "memory_count": row["memory_count"],
            }
        )
    return out


def prune(db: Any, *, subject: str = "self", keep_days: int = 30) -> int:
    """清掉消化过、且超过保留期的原文流水。返回删除行数。"""
    from datetime import timedelta

    cutoff = (now() - timedelta(days=max(1, int(keep_days)))).date().isoformat()
    rows = db.conn.execute(
        "SELECT seq FROM journal_line WHERE subject_id=? AND episode_id IS NOT NULL AND day_key<?",
        (subject, cutoff),
    ).fetchall()
    seqs = [int(row["seq"]) for row in rows]
    if not seqs:
        return 0
    marks = ",".join("?" for _ in seqs)
    try:
        db.conn.execute(f"DELETE FROM journal_fts WHERE rowid IN ({marks})", seqs)
    except Exception:  # noqa: BLE001
        pass
    cur = db.conn.execute(
        f"DELETE FROM journal_line WHERE seq IN ({marks})",
        seqs,
    )
    return int(cur.rowcount or 0)


def ensure_index(db: Any, *, limit: int | None = None) -> int:
    """把还没进 journal_fts 的历史流水补进去。

    1.7.0 之前建的库没有 journal_fts；升级后在第一次 ``PMem()`` 打开时补一次。
    幂等：已经齐了就只花一条 COUNT 查询。
    """
    try:
        missing = db.conn.execute(
            "SELECT COUNT(*) AS c FROM journal_line jl "
            "WHERE NOT EXISTS (SELECT 1 FROM journal_fts f WHERE f.rowid=jl.seq)"
        ).fetchone()["c"]
    except Exception:  # noqa: BLE001 没有表（极旧库/只读）就跳过
        return 0
    if not missing:
        return 0
    sql = (
        "SELECT jl.seq, jl.text FROM journal_line jl "
        "WHERE NOT EXISTS (SELECT 1 FROM journal_fts f WHERE f.rowid=jl.seq) ORDER BY jl.seq"
    )
    params: tuple[Any, ...] = ()
    if limit:
        sql += " LIMIT ?"
        params = (int(limit),)
    rows = db.conn.execute(sql, params).fetchall()
    done = 0
    for row in rows:
        try:
            db.conn.execute(
                "INSERT INTO journal_fts(rowid, text_seg) VALUES(?,?)",
                (int(row["seq"]), tokenize(row["text"])),
            )
            done += 1
        except Exception:  # noqa: BLE001 单行失败不挡整批
            continue
    return done


def stats(db: Any, *, subject: str = "self") -> dict[str, Any]:
    def _one(sql: str) -> int:
        row = db.conn.execute(sql, (subject,)).fetchone()
        return int(row[0] or 0) if row else 0

    return {
        "episodes": _one("SELECT COUNT(*) FROM episode WHERE subject_id=?"),
        "lines": _one("SELECT COUNT(*) FROM journal_line WHERE subject_id=?"),
        "pending": _one(
            "SELECT COUNT(*) FROM journal_line jl WHERE jl.subject_id=? AND jl.episode_id IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM journal_retraction r "
            "WHERE r.subject_id=jl.subject_id AND r.line_id=jl.id)"
        ),
        "retracted": _one("SELECT COUNT(*) FROM journal_retraction WHERE subject_id=?"),
        "pending_days": pending_days(db, subject=subject),
        "last": latest(db, subject=subject),
    }


def latest(db: Any, *, subject: str = "self") -> dict[str, Any] | None:
    """最后一篇日记，不看时间窗（status 用）。"""
    row = db.conn.execute(
        "SELECT id, day_key, title, summary, memory_count FROM episode WHERE subject_id=? "
        "ORDER BY day_key DESC, ended_at DESC LIMIT 1",
        (subject,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "day_key": row["day_key"],
        "title": row["title"],
        "summary": row["summary"],
        "memory_count": row["memory_count"],
    }


__all__ = [
    "DayDraft",
    "append",
    "build_writer",
    "digest",
    "due",
    "ensure_episode_index",
    "episode_item",
    "index_episode",
    "latest",
    "model_draft",
    "pending",
    "pending_days",
    "prune",
    "recent",
    "retract",
    "retracted_ids",
    "search_episodes",
    "stats",
    "template_draft",
    "validate",
]

def trace(
    db: Any,
    query: str,
    *,
    subject: str = "self",
    limit: int = 20,
    max_chars: int = 400,
    include_retracted: bool = False,
) -> dict[str, Any]:
    """按字查原话：**精确子串**匹配（一字不差），返回会话、行号、角色、时间。

    为什么不用 FTS：FTS 是分词后的相关性，不是「这句话在不在这里」。要认归属、
    认承诺，就得按字查。行号是「同一会话内第几行」（按 journal_line.seq 排），
    不是全局行号。

    默认**不返回撤回过的原话**（撤回 = 这句不算数了）；``include_retracted=True``
    时照样能查到，并标明 ``retracted``——想认账的时候查得到，日常回忆时看不见。

    三条纪律：
    1. 少于 3 个字不扫，如实说「太短、没查」——**那不算没有**；
    2. 查不到就说库里没有，别拿相似记忆顶；
    3. 范围只承诺「这条线的原话流水」，不承诺全世界。
    """
    q = str(query or "").strip()
    if len(q) < 3:
        return {
            "query": q,
            "count": 0,
            "items": [],
            "too_short": True,
            "scope": "PMem 原话流水（journal_line）",
            "note": "太短，扫不了（至少 3 个字）；这不算「没有」。",
        }
    live_filter = ""
    if not include_retracted:
        live_filter = (
            "AND NOT EXISTS (SELECT 1 FROM journal_retraction r "
            "WHERE r.subject_id=journal_line.subject_id AND r.line_id=journal_line.id) "
        )
    rows = db.conn.execute(
        f"""
        SELECT id, session_id, ts, role, line_no, text, seq FROM (
            SELECT id, session_id, ts, role, text, seq,
                   ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY seq) AS line_no
            FROM journal_line WHERE subject_id=? {live_filter}
        ) WHERE instr(text, ?) > 0
        ORDER BY seq DESC LIMIT ?
        """,
        (subject, q, max(1, int(limit))),
    ).fetchall()
    gone = retracted_ids(db, subject=subject, line_ids=[row["id"] for row in rows])
    items: list[dict[str, Any]] = []
    for row in rows:
        body = str(row["text"] or "")
        clipped = body if len(body) <= max_chars else body[: max_chars - 1] + "…"
        items.append(
            {
                "session_id": row["session_id"],
                "line_no": int(row["line_no"]),
                "role": row["role"],
                "ts": row["ts"],
                "text": clipped,
                "id": row["id"],
                "retracted": row["id"] in gone,
            }
        )
    scope = "PMem 原话流水（journal_line，全库所有会话）"
    if include_retracted:
        scope += "，含已撤回"
    return {
        "query": q,
        "count": len(items),
        "items": items,
        "too_short": False,
        "scope": scope,
        "note": "" if items else "这条线的库里没有这句话（不代表没发生过：可能没进流水，或已被撤回）。",
    }
