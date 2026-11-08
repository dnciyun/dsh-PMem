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
from ..base.clock import now, now_iso, occurred_day, to_iso
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
    r"^(Current |Approval prompts|Approval policy|Browser time zone|Time sampled|"
    r"\[骨架\]|\[索引\]|\[提示\]|<<<PMEM_WAKE|"
    r"You are the Team Lead|You are an AI agent powered|Use list_agents\(|The Team Lead named|"
    r"Agent Teams is available|Background subagent |- )"
)
# 另一类漏网：**后台任务完成通知**（大小写都有，还带 [status: …]）。
# 实测它们会被当成「用户说的话」写进日记，派生出「用户background job … finished」这种记忆。
_RUNTIME_JOB = re.compile(
    r"^(?:background|Background) (?:job|task) "
    r"|finished \[status: |Read its output with job_output|Read the output with job_output"
)


# QQ/B站桥接的**唤醒包装**：它们不是用户说的话，是桥接每轮塞给 agent 的说明
# （时间/角色/身份/唤醒原因 + 人设卡）。一条消息里出现两个以上这类标记，整条都不记——
# 实测它们会以 160 字截断的形式进流水，消化后派生出「用户【当前时间】…」这种记忆。
_ENVELOPE_MARKERS = (
    "【当前时间】",
    "【当前角色】",
    "【你的身份】",
    "【当前唤醒】",
    "【引导唤醒】",
    "【回复检查】",
    "【主动机会】",
    "【上线补历史】",
    "【来源判断】",
    "［统一人设卡",
)


def _strip_runtime(text: str) -> str:
    """剥掉开头的运行时行，只留真消息。全被剥光就返回空串（那这行本来就不该记）。"""
    body = str(text or "")
    if sum(1 for marker in _ENVELOPE_MARKERS if marker in body) >= 2:
        return ""
    lines = str(text or "").splitlines()
    start = 0
    while start < len(lines):
        line = lines[start].strip()
        if line == "" or _RUNTIME_LINE.match(line) or _RUNTIME_JOB.match(line):
            start += 1
            continue
        break
    body = chr(10).join(lines[start:]).strip()
    # 整条都是后台任务通知（正文里含 finished [status:…]）也算噪声，直接丢。
    if body and _RUNTIME_JOB.search(body) and len(body) < 400:
        return ""
    return body



def append(
    db: Any,
    text: str,
    *,
    subject: str = "self",
    session_id: str | None = None,
    role: str = ROLE_USER,
    ts: str | None = None,
) -> dict[str, Any]:
    """追加一行流水。原文原样存，不做任何改写。

    原文行与 FTS 索引在**同一个事务**里写：锁冲突交给 ``db.tx()`` 的重试语义
    （等，不丢）；FTS 写失败只影响索引，不影响原文——原文照样提交，失败写事件
    留痕（``journal.index_failed``），``stats()["unindexed"]`` 也能看到。
    """
    body = _strip_runtime(text)
    if not body:
        return {"ok": False, "reason": "empty"}
    if role not in (ROLE_USER, ROLE_ASSISTANT):
        raise ValueError(f"role 只能是 user/assistant：{role}")
    stamp = ts or now_iso()
    line_id = ulid()
    day = today_key() if ts is None else _day_of(stamp)
    indexed = True
    index_error: str | None = None
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO journal_line(id, subject_id, session_id, day_key, ts, role, text, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (line_id, subject, session_id, day, stamp, role, body, now_iso()),
        )
        # 原文兜底索引：派生事实会截断/改人称，原话原样进 FTS，检索薄弱时回来捞。
        try:
            conn.execute(
                "INSERT INTO journal_fts(rowid, text_seg) VALUES(?,?)",
                (int(cur.lastrowid), tokenize(body)),
            )
        except Exception as exc:  # noqa: BLE001 索引写失败不能挡住原文入库，但必须留痕
            indexed = False
            index_error = f"{type(exc).__name__}: {exc}"
            events.append(
                conn,
                actor="journal",
                op="journal.index_failed",
                target_tbl="journal_line",
                target_id=line_id,
                subject_id=subject,
                payload={"error": index_error[:300]},
            )
    result: dict[str, Any] = {"ok": True, "id": line_id, "day_key": day, "role": role, "indexed": indexed}
    if index_error:
        result["index_error"] = index_error
    return result


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
    """ISO 时间 → 本地日期键。解析失败就退回今天，不抛。

    规则收在 ``clock.occurred_day`` 一处：写入侧的 ``journal_line.day_key``、派生事实的
    ``memory.day_key``、迁移回填、I23 不变量必须用**同一个**折算，否则「同一个时刻」
    在不同层能落到不同日期上。
    """
    return occurred_day(iso, fallback=today_key()) or today_key()


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
    return _finish(
        db, subject=subject, day=day, rows=rows, draft=draft, store=store, dry_run=dry_run, config=config
    )


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
    return _finish(
        db, subject=subject, day=day, rows=rows, draft=draft, store=store, dry_run=dry_run, config=config
    )


def _finish(
    db: Any,
    *,
    subject: str,
    day: str,
    rows: list[Any],
    draft: DayDraft,
    store: Any,
    dry_run: bool,
    config: Any = None,
) -> dict[str, Any]:
    """校验 → 回落 → 落 episode → 原文打上 episode_id → 事实进记忆库。

    事实写入是**事务外逐条**的（``store.add`` 自带事务，套大事务会锁死整库），
    所以这里额外做两件事，保证「半截失败」不静默：

    1. 自己这篇写完立刻对账：还缺的用同一个 ``idem_key`` 重放补齐；
    2. 顺手扫最近 ``REPAIR_WINDOW`` 篇（上次进程被杀留下的缺口，原文还在）。
    """
    problems = validate(draft, rows)
    if problems:
        # 编造就整篇回落：宁可流水账，也不要假日记。
        fallback = template_draft(rows)
        fallback.problems = problems
        fallback.tokens = draft.tokens
        draft = fallback
    summary = chr(10).join(draft.lines)
    episode_id = ulid()
    planned = _facts_with_sources(draft, rows=rows)
    facts = [text for text, _source in planned]
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

    # 主体归属/时间归一都只在真的写记忆库、且开关打开时才算（默认态 0 额外 SQL/计算）。
    links_enabled = store is not None and _subject_links_enabled(config)
    time_enabled = store is not None and _time_normalize_enabled(config)
    entities = _known_entities(db, subject) if links_enabled else []

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
        for index, (text, source) in enumerate(planned):
            # 每条事实的时间取**它自己那一行**的 ts，不是当天第一条：
            # 一天里上午和晚上说的两件事，日期相同、时刻不同；用第一条代表全天的
            # 话，「几点说的」就再也分不出来了（而这是记忆系统仅剩的时间粒度）。
            source_ts = str(source["ts"]) if source is not None else rows[0]["ts"]
            kwargs = _fact_kwargs(episode_id, index, mode=draft.mode, valid_from=source_ts)
            links = _match_subject_links(text, entities) if links_enabled else None
            time_info = (
                _normalize_time(str(source["text"]) if source is not None else text, source_ts)
                if time_enabled
                else None
            )
            extra_payload: dict[str, Any] = {}
            if links:
                extra_payload.update(links)
            if time_info:
                extra_payload.update(time_info)
            if extra_payload:
                kwargs["extra"] = extra_payload
            try:
                saved = store.add(text, **kwargs)
            except Exception as exc:  # 一条事实写不进去，不能带崩整篇日记
                result.setdefault("errors", []).append({"text": text, "error": str(exc)})
                continue
            if links:
                result.setdefault("subject_links", []).append({"index": index, **links})
            if time_info:
                result.setdefault("time_normalized", []).append({"index": index, **time_info})
            if isinstance(saved, dict) and saved.get("id"):
                result["memory_ids"].append(saved["id"])

        # 对账（1.11.0）：上面任何一条没写进去，立刻按幂等键补一次。补不齐就把缺口
        # 摆到明面上——``incomplete`` 会进返回值、I20 会一直红着，不静默。
        if _fact_gap(db, episode_id, len(facts)) > 0:
            fixed = repair(db, subject=subject, episode_id=episode_id, store=store, config=config)
            result["repair"] = fixed
            for item in fixed.get("items") or []:
                memory_id = item.get("memory_id")
                if memory_id and memory_id not in result["memory_ids"]:
                    result["memory_ids"].append(memory_id)
            gap = _fact_gap(db, episode_id, len(facts))
            if gap > 0:
                result["incomplete"] = gap
                result.setdefault("errors", []).append(
                    {
                        "episode_id": episode_id,
                        "missing": gap,
                        "hint": "原文还在的话跑 `pmem journal repair` 可补齐；跑完仍红就是原料没了",
                    }
                )

        # 顺手扫最近几篇：上一次 digest 可能在本行之前就被杀掉（episode 已提交、
        # 事实没写完），那批流水已经消费掉，只靠当天重跑永远补不到。
        window = int(_cfg(config).get("repair_window", REPAIR_WINDOW) or REPAIR_WINDOW)
        swept = repair(db, subject=subject, limit=max(1, window), store=store, config=config)
        if swept.get("added") or swept.get("failed") or swept.get("unrepairable"):
            result["repaired_previous"] = swept
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


def _facts_with_sources(draft: DayDraft, *, rows: list[Any]) -> list[tuple[str, Any | None]]:
    """日记 → (事实正文, 来源原话行)；``None`` 表示这条来自日记行兜底。

    为什么要带来源：repair 重放前要判断某条事实的原料是不是**噪声/已撤回**，
    只有拿到对应 ``journal_line`` 才能判定；只返回字符串就判不了。
    """
    out: list[tuple[str, Any | None]] = []
    seen: set[str] = set()
    for row in rows:
        if row["role"] != ROLE_USER:
            continue
        body = _third_person(row["text"])[:80]
        if len(body) < 4 or body in seen:
            continue
        seen.add(body)
        out.append((body, row))
    if out:
        return out
    # 一天全是助手的话（少见）：退回日记行，至少留个痕迹
    for line in draft.lines:
        body = _third_person(line)
        if len(body) < 4 or body in seen:
            continue
        seen.add(body)
        out.append((body[:80], None))
    return out


def _facts(draft: DayDraft, *, rows: list[Any]) -> list[str]:
    """日记 → 记忆事实。

    **从用户原话派生，不从日记派生**。日记是给人读的（人称、措辞随模型走），
    事实是要被检索的（必须稳定、可追证）。两者分开之后，
    模型写得好不好都不会污染证据层——第一版拿日记改写，结果得到
    「用户我妈说明天过来」，是人称串了的直接后果。
    """
    return [text for text, _source in _facts_with_sources(draft, rows=rows)]


# -- 写入侧主体归属（P1-8，默认关）----------------------------------------
#
# 目的：让「各/分别/哪几」这类聚合题能按主体取全同一主体的多条事实。
# 做法刻意最小：只把**库里已经抽取过的实体**按名字/别名在 fact 文本里做一次
# 最长匹配，挂到 fact 的 extra 上；不重新抽实体、不加新规则表、不改写原文。
# 为什么不是直接查 entity_mention：mention 挂在 memory(seq) 上，而 fact 是这一轮
# **新建**的行，写之前还不存在；journal_line 也没有 mention。所以等价地复用
# 「同一套已抽取实体」（entity / entity_alias），挂出来的 id 与 mention 一致。
# 关时（默认）不查、不挂，fact 结构与 0.38.0 逐字相同。

def _subject_links_enabled(config: Any) -> bool:
    links = _cfg(config).get("subject_links")
    return bool(isinstance(links, dict) and links.get("enabled", False))


def _known_entities(db: Any, subject: str) -> list[tuple[str, str, str, float]]:
    """(surface, entity_id, type, confidence)，长名优先。一次查询，只读。"""
    rows: list[tuple[str, str, str, float]] = []
    try:
        for row in db.conn.execute(
            "SELECT id, name, type, confidence FROM entity "
            "WHERE subject_id=? AND status='active'",
            (subject,),
        ).fetchall():
            name = str(row["name"] or "").strip()
            if name:
                rows.append(
                    (name, str(row["id"]), str(row["type"] or ""), float(row["confidence"] or 0.0))
                )
        for row in db.conn.execute(
            "SELECT a.alias AS name, a.entity_id AS id, e.type AS type, e.confidence AS confidence "
            "FROM entity_alias a JOIN entity e ON e.id=a.entity_id "
            "WHERE e.subject_id=? AND e.status='active'",
            (subject,),
        ).fetchall():
            name = str(row["name"] or "").strip()
            if name:
                rows.append(
                    (name, str(row["id"]), str(row["type"] or ""), float(row["confidence"] or 0.0))
                )
    except Exception:  # noqa: BLE001 实体表缺/坏只是少挂几个 id，不能挡住写日记
        return []
    # 长名先匹配（「沈知夏」优先于「知夏」）；同长按置信度、id 稳定排序。
    rows.sort(key=lambda item: (-len(item[0]), -item[3], item[1]))
    return rows


def _match_subject_links(
    text: str, entities: list[tuple[str, str, str, float]]
) -> dict[str, Any] | None:
    """在 fact 文本里找**已抽取**的实体；命中返回 ``{subject_entity_ids, subject}``。"""
    body = str(text or "")
    if not body or not entities:
        return None
    matched: list[tuple[str, str, str, float]] = []
    for name, entity_id, type_, confidence in entities:
        if len(name) < 2:  # 单字实体（「家」「钱」）噪音太大，不挂
            continue
        if name not in body:
            continue
        if any(name in longer[0] for longer in matched):  # 已被更长的名字覆盖
            continue
        matched.append((name, entity_id, type_, confidence))
        if len(matched) >= 3:
            break
    if not matched:
        return None
    # ``subject`` 取置信度最高的人/概念；没有就退回置信度最高的那个。
    preferred = sorted(
        [item for item in matched if item[2] in ("person", "concept")],
        key=lambda item: (-item[3], item[1]),
    )
    fallback = sorted(matched, key=lambda item: (-item[3], item[1]))
    subject_name = (preferred or fallback)[0][0]
    ids = list(dict.fromkeys(item[1] for item in matched))
    return {"subject_entity_ids": ids, "subject": subject_name}


# -- 写时时间归一（P2-16，默认关）------------------------------------------
#
# 目的：把「上周五 / 下周一 / 三天前 / 半个月前 / 3月27日」在**写入时**按这条记忆
# 自己的逻辑时间（行 ts）落成绝对日期，存进 fact 的 extra（time_expr / time_abs）。
# 为什么不在渲染时算：渲染时算已经证明≈0（G3 A 实验 94→94），而且每条都要重算；
# 写时只算一次，下游（检索过滤 / 自检 / 契约）都读同一个字段。
#
# 纪律（对齐 docs/roadmap §5.1 与 config 注释）：
# 1. 只认**明确**说法；「上次/那次/以前/后来/当时」这类模糊词**不猜**——猜错窗口比不猜更糟；
# 2. 原文与 facts 文本一个字都不动，只在 extra 上加两个字段；
# 3. 换不出来（如 2 月 30 日）就只留 ``time_expr``，不写 ``time_abs``；
# 4. 关时（默认）不算、不查、不挂，结构与 0.38.0 逐字相同。

_DATE_YMD_RE = re.compile(
    r"(?<!\d)(?P<y>\d{4})\s*[-/.年]\s*(?P<m>\d{1,2})\s*[-/.月]\s*(?P<d>\d{1,2})\s*[日号]?"
)
_DATE_MD_RE = re.compile(r"(?<!\d)(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*[日号]")
# 模糊说法：即使匹配到了也不换算（防御性，当前 timeresolve 的显式规则本来就不会匹配它们）。
_VAGUE_TIME_WORDS = ("上次", "上回", "那次", "以前", "之前", "后来", "当时", "那阵子", "前段时间")


def _time_normalize_enabled(config: Any) -> bool:
    value = _cfg(config).get("time_normalize")
    return bool(isinstance(value, dict) and value.get("enabled", False))


def _as_date(value: str | None) -> Any:
    """ISO 字符串 → date；解析不了返回 None（不猜）。"""
    from datetime import date

    raw = str(value or "")
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _resolve_ymd(year: int | None, month: int, day: int, base: Any) -> Any:
    """年月日 → date；没写年份按「最近一次」（和 selfcheck/temporal 同一口径）。"""
    from datetime import date

    if base is None:
        return None
    try:
        if year is None:
            candidate = date(base.year, month, day)
            if candidate > base:
                candidate = date(base.year - 1, month, day)
            return candidate
        return date(year, month, day)
    except ValueError:  # 2 月 30 日这种不存在的日期
        return None


def _normalize_time(text: str, base_iso: str | None) -> dict[str, str] | None:
    """一句话 + 这行的逻辑时间 → ``{time_expr, time_abs?}``；没有明确说法返回 None。

    返回的是「原话片段 + 绝对日期」两个字段，**不改原文**。多说法时选先出现的
    明确日期，其次才是 timeresolve 认出的第一个相对说法（确定性，不按概率挑）。
    """
    body = str(text or "")
    base = _as_date(base_iso)
    if not body or base is None:
        return None
    # 1) 明确日期：2026-03-27 / 2026年3月27日 / 3月27日（无年份按最近一次推）
    match = _DATE_YMD_RE.search(body)
    if match:
        expr = match.group(0).strip()
        resolved = _resolve_ymd(
            int(match.group("y")), int(match.group("m")), int(match.group("d")), base
        )
        return {"time_expr": expr, "time_abs": resolved.isoformat()} if resolved else {
            "time_expr": expr
        }
    match = _DATE_MD_RE.search(body)
    if match:
        expr = match.group(0).strip()
        resolved = _resolve_ymd(None, int(match.group("m")), int(match.group("d")), base)
        return {"time_expr": expr, "time_abs": resolved.isoformat()} if resolved else {
            "time_expr": expr
        }
    # 2) 明确相对说法：复用 timeresolve（周日起算、按这条记忆自己的日期算）
    from .timeresolve import relative_dates

    for phrase, stamp in relative_dates(body, base_iso):
        if not phrase or any(word in phrase for word in _VAGUE_TIME_WORDS):
            continue
        return {"time_expr": phrase, "time_abs": stamp}
    return None


# -- 派生事实的写入与补写（digest / repair 共用）-----------------------------
#
# 为什么需要补写：``_finish`` 是**先提交日记**（一个 tx），再在事务外逐条
# ``store.add`` 派生事实。单条失败只记进 ``result["errors"]`` 就 continue，
# 而流水此时已经打上 ``episode_id``（= 已消费），``digest`` 不会再碰这一天——
# 于是那天的事实会永久缺几条，而且 I1–I19 全都看不出来（1.10.0 的真实漏洞）。
#
# 修法不是把 digest 包成一个大事务：``store.add`` 自带事务与外键/图/关系写入，
# 外面再套一个大事务会长时间占着写锁。正解是**幂等重放**：每条事实的
# ``idem_key`` 是 ``journal:{episode_id}:{index}``（写进 store 的同一个事务），
# 重放已经写成的那几条只会拿到 ``idempotent_replay``，不会重复。
# 所以「补齐」是安全的、可重入的；补不齐就让它一直红着（I20），不允许静默。

REPAIR_WINDOW = 5  # digest 收尾时顺手检查最近几篇日记


def fact_idem_key(episode_id: str, index: int) -> str:
    """第 ``index`` 条派生事实的幂等键。digest 与 repair 必须用同一个。"""
    return f"journal:{episode_id}:{index}"


def fact_skip_idem_key(episode_id: str, index: int) -> str:
    """第 ``index`` 条事实被**跳过**（噪声/已撤回）时的幂等键。

    格式与 ``fact_idem_key`` 同前缀，W2 的 I20 会认这个后缀：
    「这条声称派生过，但原料是噪声/撤回过，已明确跳过」= 不算缺。
    """
    return f"journal:{episode_id}:{index}:skipped"


_SKIP_REASON_NOISE = "noise"
_SKIP_REASON_RETRACTED = "retracted"


def _fact_kwargs(
    episode_id: str, index: int, *, mode: str, valid_from: str | None
) -> dict[str, Any]:
    """派生事实的 ``store.add`` 参数（只在一处构造，两边不会慢慢走偏）。

    ``day_key`` 必须跟着 ``valid_from``（原料那一行的 ``ts``）走，**不能**留空让
    ``store.add`` 按写入时刻自己算：日记是次日凌晨消化的，按写入时刻算会让整篇日记
    派生出来的事实集体晚一天。``valid_from`` 缺省（老调用/重建失败）时留 ``None``，
    交给 ``store.add`` 的回退分支，不猜。
    """
    return {
        "kind": "episodic",
        "grade": "C",
        "grade_reason": "日记派生：日常流水，按时间留着，不抢常驻位",
        "provenance": ("user_explicit" if mode == "template" else "observed"),
        "source_kind": "journal",
        "source_ref": episode_id,
        "episode_id": episode_id,
        "session_id": None,
        "idem_key": fact_idem_key(episode_id, index),
        "valid_from": valid_from,
        "day_key": _day_of(valid_from) if valid_from else None,
    }


def derived_count(db: Any, episode_id: str) -> int:
    """这篇日记现在**还活着**的事实条数（软删/真删都不算）。"""
    row = db.conn.execute(
        "SELECT COUNT(*) c FROM memory WHERE episode_id=? AND status<>'deleted'",
        (episode_id,),
    ).fetchone()
    return int(row["c"]) if row else 0


def _derived_counts(db: Any) -> dict[str, int]:
    """一次分组扫出每篇日记还活着的事实数（``memory.episode_id`` 没有索引）。"""
    rows = db.conn.execute(
        "SELECT episode_id, COUNT(*) c FROM memory WHERE episode_id IS NOT NULL "
        "AND status<>'deleted' GROUP BY episode_id"
    ).fetchall()
    return {str(row["episode_id"]): int(row["c"]) for row in rows}


def _attempted_counts(db: Any) -> dict[str, int]:
    """一次扫出每篇日记「提交成功过」的事实数：``{episode_id: n}``。

    幂等键是 ``store.add`` 成功时在**同一个事务**里写的，所以：键在 = 那条事实
    曾经真的写进过库（哪怕后来被停放/真删）；键不在 = 从没写成功过。
    为什么不逐篇 ``LIKE 'journal:{id}:%'``：LIKE 前缀在大小写不敏感时用不上主键
    索引，逐篇扫会把 doctor 拖成 O(篇数 × 幂等行数)。整表扫一遍按前缀分组就够便宜。
    """
    out: dict[str, int] = {}
    for row in db.conn.execute("SELECT key FROM idem WHERE key LIKE 'journal:%'"):
        parts = str(row["key"]).split(":")
        if len(parts) >= 3 and parts[1]:
            out[parts[1]] = out.get(parts[1], 0) + 1
    return out


def _fact_gap(
    db: Any, episode_id: str, claimed: int, *, derived: int | None = None, attempted: int | None = None
) -> int:
    """声称派生 ``claimed`` 条，实际最多只成功过几条，还差多少。"""
    live = derived_count(db, episode_id) if derived is None else derived
    tried = _attempted_counts(db).get(episode_id, 0) if attempted is None else attempted
    return max(0, int(claimed) - max(live, tried))


def _idem_live_memory(db: Any, key: str) -> str | None:
    """幂等键对应的记忆还活着就返回 id（活着 = 不算缺）。"""
    prior = db.conn.execute("SELECT target_id FROM idem WHERE key=?", (key,)).fetchone()
    if prior is None or not prior["target_id"]:
        return None
    row = db.conn.execute(
        "SELECT id FROM memory WHERE id=? AND status<>'deleted'", (prior["target_id"],)
    ).fetchone()
    return str(row["id"]) if row else None


def _episode_facts(
    db: Any, episode: Any
) -> tuple[list[str], list[str | None], str | None, str] | None:
    """重建一篇日记「当时应该派生」的事实清单；原料没了就返回 None。

    返回 ``(facts, skip_reasons, valid_froms, mode)``。``skip_reasons`` 与 ``facts``
    一一对应：``None`` 表示正常重放；``"noise"`` / ``"retracted"`` 表示这条的原料
    **不该写成事实**（``_strip_runtime`` 判空 / ``journal_retraction`` 里撤回过的），
    repair 只该写一条 ``journal:{episode_id}:{index}:skipped`` 幂等键交代过去。
    ``valid_froms`` 同样一一对应，是每条事实**自己那行**原话的时间（digest 与 repair
    必须一致，否则补写的事实时间会跳）。

    为什么从原文重建、而不是把事实清单存进 ``episode.extra_json``：清单就是事实
    正文的副本，存进去会让「删掉一条记忆」删不干净（日记里还留着一份），
    那就是审计里另一个洞。代价是原文流水只活 ``keep_days`` 天——但 digest 失败
    到 repair 之间通常只有几分钟；真救不回来就报 ``unrepairable``，不瞎补。
    """
    try:
        derivation = json.loads(episode["derivation"] or "{}")
    except (json.JSONDecodeError, TypeError):
        derivation = {}
    line_ids = [str(item) for item in (derivation.get("lines") or []) if item]
    if not line_ids:
        return None
    marks = ",".join("?" for _ in line_ids)
    rows = db.conn.execute(
        f"SELECT id, role, text, ts FROM journal_line WHERE id IN ({marks})", line_ids
    ).fetchall()
    by_id = {str(row["id"]): row for row in rows}
    if len(by_id) < len(line_ids):
        return None  # 原文被 prune 了，重放不出来
    ordered = [by_id[line_id] for line_id in line_ids]
    draft = DayDraft(
        title=str(episode["title"] or ""),
        lines=[line for line in str(episode["summary"] or "").splitlines() if line],
        mode=str(derivation.get("mode") or "template"),
    )
    planned = _facts_with_sources(draft, rows=ordered)
    ids = [str(row["id"]) for row in ordered]
    retracted: set[str] = set()
    if ids:
        id_marks = ",".join("?" for _ in ids)
        retracted = {
            str(row["line_id"])
            for row in db.conn.execute(
                f"SELECT line_id FROM journal_retraction WHERE line_id IN ({id_marks})", ids
            )
        }
    skip_reasons: list[str | None] = []
    for _text, source in planned:
        if source is None:
            skip_reasons.append(None)
            continue
        line_id = str(source["id"])
        # 噪声行：存进去的时候没被剥干净（老数据/直写），重放不能变事实。
        if _strip_runtime(str(source["text"] or "")) == "":
            skip_reasons.append(_SKIP_REASON_NOISE)
        elif line_id in retracted:
            skip_reasons.append(_SKIP_REASON_RETRACTED)
        else:
            skip_reasons.append(None)
    # 每条事实各自的原料时间：repair 重放时必须逐条还原，否则补写出来的事实会和
    # 当天第一条的时间绑在一起（digest 路径已经改成逐条了，两条路必须一致）。
    valid_froms: list[str | None] = [
        (str(source["ts"]) if source is not None else ordered[0]["ts"]) for _text, source in planned
    ]
    return (
        [text for text, _source in planned],
        skip_reasons,
        valid_froms,
        str(derivation.get("mode") or "template"),
    )


def _mark_fact_skipped(
    db: Any,
    *,
    subject: str,
    episode_id: str,
    index: int,
    reason: str,
    text: str,
) -> bool:
    """把「这条事实已明确跳过」写进幂等键 + 事件；已经写过就幂等返回 False。

    幂等键格式（W2 的 I20 认它，必须一字不差）：
    ``journal:{episode_id}:{index}:skipped``
    事件：``op='journal.fact_skipped'``，payload 带 episode_id / index / reason / text 摘要。
    """
    key = fact_skip_idem_key(episode_id, index)
    with db.tx() as conn:
        prior = conn.execute("SELECT 1 FROM idem WHERE key=?", (key,)).fetchone()
        if prior is not None:
            return False
        events.idem_remember(conn, key, "journal.fact_skipped", episode_id)
        events.append(
            conn,
            actor="journal",
            op="journal.fact_skipped",
            target_tbl="episode",
            target_id=episode_id,
            subject_id=subject,
            payload={
                "episode_id": episode_id,
                "index": index,
                "reason": reason,
                "text": str(text)[:120],
            },
            idem_key=key,
        )
    return True


def repair(
    db: Any,
    subject: str = "self",
    *,
    episode_id: str | None = None,
    dry_run: bool = False,
    limit: int = 50,
    store: Any = None,
    config: Any = None,
) -> dict[str, Any]:
    """把 digest 半截失败留下的缺口按幂等键补上（可重入）。

    判定一篇要不要补：``memory_count`` 声称的条数，比「现在活着的行数」和
    「提交成功过的幂等键数」都大 —— 说明有事实**从没写进去**，不是被删了。
    两种情况下不动它：

    - 事实写进去过、后来被停放/真删（幂等键还在）：那是用户/生命周期做的删除，
      重放等于复活，不干；
    - 原文流水已被 prune、重建不出清单：无法判定，报 ``unrepairable``。

    返回的计数就是 CLI 输出的人话：查了几篇 / 缺几条 / 补了几条 / 失败几条。
    """
    if store is None:
        from .store import MemoryStore  # 延迟导入：store 也在 import 本模块

        store = MemoryStore(db, config=config, subject=subject, actor="journal:repair")
    scan = max(1, int(limit or 50))
    sql = (
        "SELECT id, day_key, title, summary, memory_count, derivation FROM episode "
        "WHERE subject_id=? AND memory_count>0"
    )
    params: list[Any] = [subject]
    if episode_id:
        sql += " AND id=?"
        params.append(episode_id)
        scan = 1
    sql += " ORDER BY day_key DESC, id DESC LIMIT ?"
    params.append(scan)
    episodes = db.conn.execute(sql, params).fetchall()

    stats: dict[str, Any] = {
        "ok": True,
        "dry_run": bool(dry_run),
        "episodes": len(episodes),  # 查了几篇（有派生事实的日记）
        "gaps": 0,  # 有几篇需要补
        "missing": 0,  # 需要补几条
        "added": 0,  # 补成功几条
        "failed": 0,  # 补失败几条
        "dropped": 0,  # 写过但后来被删/停放的条数（不复活）
        "skipped": 0,  # 原料是噪声/已撤回、明确跳过不写成事实的条数
        "unrepairable": 0,  # 原料没了、补不出来的篇数
        "remaining": 0,  # 跑完之后仍然对不上的篇数
        "errors": [],
        "items": [],
    }
    # 分组扫描各一次（逐篇 COUNT/LIKE 会退化成 O(篇数 × 全表)）。
    derived_map = _derived_counts(db)
    attempted_map = _attempted_counts(db)
    # 主体归属/时间归一（默认关）：补写路径与 digest 走同一套匹配，避免两边慢慢走偏。
    links_enabled = _subject_links_enabled(config)
    time_enabled = _time_normalize_enabled(config)
    entities = _known_entities(db, subject) if links_enabled else []
    for episode in episodes:
        ep_id = str(episode["id"])
        claimed = int(episode["memory_count"] or 0)
        live = derived_map.get(ep_id, 0)
        tried = attempted_map.get(ep_id, 0)
        if _fact_gap(db, ep_id, claimed, derived=live, attempted=tried) <= 0:
            continue
        stats["gaps"] += 1
        rebuilt = _episode_facts(db, episode)
        if rebuilt is None or len(rebuilt[0]) < claimed:
            # 重建不出来：不猜、不静默，原样报出来（I20 也会一直红着）。
            stats["unrepairable"] += 1
            stats["items"].append(
                {
                    "episode_id": ep_id,
                    "day_key": episode["day_key"],
                    "claimed": claimed,
                    "live": live,
                    "attempted": tried,
                    "reason": "原文已清理，重建不出事实清单",
                }
            )
            continue
        facts, skip_reasons, valid_froms, mode = rebuilt
        for index, text in enumerate(facts):
            if index >= claimed:  # memory_count 是权威值；多出来的不补
                break
            key = fact_idem_key(ep_id, index)
            skip_key = fact_skip_idem_key(ep_id, index)
            if _idem_live_memory(db, key) is not None:
                continue  # 已经写成了
            if db.conn.execute("SELECT 1 FROM idem WHERE key=?", (key,)).fetchone() is not None:
                stats["dropped"] += 1  # 写过、后来被删：不复活
                continue
            if db.conn.execute("SELECT 1 FROM idem WHERE key=?", (skip_key,)).fetchone() is not None:
                continue  # 之前已经判定过「跳过」，幂等
            reason = skip_reasons[index]
            if reason:
                # 原料是噪声/已撤回：不能写成事实，但要明确「这条不算缺」。
                item = {
                    "episode_id": ep_id,
                    "day_key": episode["day_key"],
                    "index": index,
                    "text": text,
                    "reason": reason,
                }
                if dry_run:
                    stats["skipped"] += 1
                    stats["items"].append({**item, "status": "would-skip"})
                    continue
                try:
                    _mark_fact_skipped(
                        db,
                        subject=subject,
                        episode_id=ep_id,
                        index=index,
                        reason=reason,
                        text=text,
                    )
                except Exception as exc:  # noqa: BLE001 写跳过标记失败要留痕、可重试
                    stats["failed"] += 1
                    stats["errors"].append(
                        {"episode_id": ep_id, "index": index, "text": text, "error": str(exc)}
                    )
                    continue
                stats["skipped"] += 1
                stats["items"].append({**item, "status": "skipped"})
                continue
            stats["missing"] += 1
            item = {
                "episode_id": ep_id,
                "day_key": episode["day_key"],
                "index": index,
                "text": text,
            }
            if dry_run:
                stats["items"].append({**item, "status": "would-add"})
                continue
            kwargs = _fact_kwargs(
                ep_id, index, mode=mode, valid_from=valid_froms[index]
            )
            links = _match_subject_links(text, entities) if links_enabled else None
            # 补写时只剩事实正文（原文清单没一起返回）；按事实文本归一，换算不了就不写。
            time_info = (
                _normalize_time(text, valid_froms[index]) if time_enabled else None
            )
            extra_payload: dict[str, Any] = {}
            if links:
                extra_payload.update(links)
            if time_info:
                extra_payload.update(time_info)
            if extra_payload:
                kwargs["extra"] = extra_payload
            try:
                saved = store.add(text, **kwargs)
            except Exception as exc:  # noqa: BLE001 补写失败要留痕、可重试
                stats["failed"] += 1
                stats["errors"].append(
                    {"episode_id": ep_id, "index": index, "text": text, "error": str(exc)}
                )
                continue
            stats["added"] += 1
            memory_id = saved.get("id") if isinstance(saved, dict) else None
            item = {**item, "status": "added", "memory_id": memory_id}
            if links:
                item["subject"] = links["subject"]
                stats.setdefault("subject_links", []).append(
                    {"episode_id": ep_id, "index": index, **links}
                )
            if time_info:
                item.update(time_info)
                stats.setdefault("time_normalized", []).append(
                    {"episode_id": ep_id, "index": index, **time_info}
                )
            stats["items"].append(item)

    if not dry_run:
        # 补完再核对一遍：还剩几篇对不上（补了但失败 / 原料没了）。
        touched = {str(item["episode_id"]) for item in stats["items"] if item.get("episode_id")}
        if touched:
            derived_map = _derived_counts(db)
            attempted_map = _attempted_counts(db)
            for ep_id in touched:
                row = db.conn.execute(
                    "SELECT memory_count FROM episode WHERE id=?", (ep_id,)
                ).fetchone()
                if row is None:
                    continue
                gap = _fact_gap(
                    db,
                    ep_id,
                    int(row["memory_count"] or 0),
                    derived=derived_map.get(ep_id, 0),
                    attempted=attempted_map.get(ep_id, 0),
                )
                if gap > 0:
                    stats["remaining"] += 1
    else:
        stats["remaining"] = stats["gaps"]
    stats["ok"] = stats["failed"] == 0
    return stats


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


def purge_noise(db: Any, *, subject: str = "self", dry_run: bool = False) -> dict[str, Any]:
    """清掉流水里的「脚手架台词」（还没消化的那些）。

    为什么要单独一个动作：`_strip_runtime` 只在**写入时**生效，历史流水里已经混进来的
    运行时行（审批提示、后台任务通知、宿主人格提示）还躺在库里，一消化就会变成
    「用户Approval policy: ask…」这种记忆。它们从来不是用户说的话，删掉不算违背「原文只追加」。
    """
    rows = db.conn.execute(
        "SELECT seq, id, text FROM journal_line WHERE subject_id=? AND episode_id IS NULL",
        (subject,),
    ).fetchall()
    noise: list[Any] = []
    for row in rows:
        body = str(row["text"] or "")
        if not body.strip():
            noise.append(row)
            continue
        if _strip_runtime(body) == "":
            noise.append(row)
            continue
        if _RUNTIME_JOB.search(body) and len(body) < 400:
            noise.append(row)
    stats = {
        "scanned": len(rows),
        "noise": len(noise),
        "deleted": 0,
        "dry_run": bool(dry_run),
        "sample": [str(r["text"] or "")[:60] for r in noise[:5]],
    }
    if dry_run or not noise:
        return stats
    seqs = [int(row["seq"]) for row in noise]
    seqs = seqs[:5000]
    marks = ",".join("?" for _ in seqs)
    with db.tx() as conn:
        try:
            conn.execute(f"DELETE FROM journal_fts WHERE rowid IN ({marks})", seqs)
        except Exception:  # noqa: BLE001 没有 FTS 表也不影响清行
            pass
        cur = conn.execute(f"DELETE FROM journal_line WHERE seq IN ({marks})", seqs)
        stats["deleted"] = int(cur.rowcount or 0)
        events.append(
            conn,
            actor="maintenance",
            op="journal.purge_noise",
            target_tbl="journal_line",
            target_id=None,
            subject_id=subject,
            payload={"deleted": stats["deleted"], "scanned": len(rows)},
        )
    return stats


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


def rebuild_index(db: Any, *, dry_run: bool = False) -> dict[str, Any]:
    """重建原话兜底索引（``journal_fts``）。

    为什么需要：FTS5 的倒排索引一旦报 `malformed inverted index`，查询会静默降级
    （原话兜底失效），而 `ensure_index` 只补**缺的**行，修不了坏的行。这里做的是
    「清空 + 按 journal_line 全量重灌」，确定性、可反复跑。
    """
    total = db.conn.execute("SELECT COUNT(*) c FROM journal_line").fetchone()["c"]
    stats: dict[str, Any] = {"lines": int(total), "indexed": 0, "dry_run": bool(dry_run)}
    if dry_run:
        return stats
    rows = db.conn.execute("SELECT seq, text FROM journal_line ORDER BY seq").fetchall()
    with db.tx() as conn:
        conn.execute("DELETE FROM journal_fts")
        done = 0
        for row in rows:
            try:
                conn.execute(
                    "INSERT INTO journal_fts(rowid, text_seg) VALUES(?,?)",
                    (int(row["seq"]), tokenize(row["text"])),
                )
                done += 1
            except Exception:  # noqa: BLE001 单行失败不挡整批
                continue
        stats["indexed"] = done
        events.append(
            conn,
            actor="maintenance",
            op="journal.reindex",
            target_tbl="journal_fts",
            target_id=None,
            subject_id="self",
            payload={"lines": stats["lines"], "indexed": done},
        )
    return stats


def rebuild_episode_index(db: Any, *, dry_run: bool = False) -> dict[str, Any]:
    """重建日记索引（``episode_fts``）。

    为什么不能直接 ``INSERT INTO episode_fts(episode_fts) VALUES('rebuild')``：
    ``episode_fts`` 是**独立 FTS5 表**（没有 external content / content=），
    没有可依赖的 content 表，FTS5 的 ``rebuild`` 命令用不了。所以和
    ``rebuild_index`` 一样做「清空 + 全量重灌」。

    映射方式（与 ``index_episode`` / ``ensure_episode_index`` 完全一致，不另发明）：
    ``episode.rowid`` → ``episode_fts.rowid``；正文 = ``_episode_text(title, summary)``
    再 ``tokenize()``。``episode.rowid`` 是 SQLite 隐式 rowid，删/重建表后可能变，
    所以这里每次都按当前 ``episode`` 全表重算，保证 rowid 一致。

    返回：``{"episodes": episode 总篇数, "indexed": 实际写入的索引行数, "dry_run": bool}``。
    """
    total = db.conn.execute("SELECT COUNT(*) c FROM episode").fetchone()["c"]
    stats: dict[str, Any] = {"episodes": int(total), "indexed": 0, "dry_run": bool(dry_run)}
    if dry_run:
        return stats
    rows = db.conn.execute("SELECT rowid, title, summary FROM episode ORDER BY rowid").fetchall()
    with db.tx() as conn:
        conn.execute("DELETE FROM episode_fts")
        done = 0
        for row in rows:
            text = _episode_text(row["title"], row["summary"])
            if not text:
                continue
            try:
                conn.execute(
                    "INSERT INTO episode_fts(rowid, text_seg) VALUES(?,?)",
                    (int(row["rowid"]), tokenize(text)),
                )
                done += 1
            except Exception:  # noqa: BLE001 单行失败不挡整批
                continue
        stats["indexed"] = done
        events.append(
            conn,
            actor="maintenance",
            op="journal.episode_reindex",
            target_tbl="episode_fts",
            target_id=None,
            subject_id="self",
            payload={"episodes": stats["episodes"], "indexed": done},
        )
    return stats


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

    # 索引可观测性（1.11.x）：原文行与 FTS 行数对不上时必须看得见。
    # 老库可能没有 journal_fts 表——那不算「坏」，退化为 0。
    try:
        fts_rows = int(
            db.conn.execute(
                "SELECT COUNT(*) c FROM journal_fts f JOIN journal_line jl ON jl.seq=f.rowid "
                "WHERE jl.subject_id=?",
                (subject,),
            ).fetchone()["c"]
        )
        unindexed = int(
            db.conn.execute(
                "SELECT COUNT(*) c FROM journal_line jl WHERE jl.subject_id=? "
                "AND NOT EXISTS (SELECT 1 FROM journal_fts f WHERE f.rowid=jl.seq)",
                (subject,),
            ).fetchone()["c"]
        )
    except Exception:  # noqa: BLE001 极旧库/只读：没有 FTS 表时如实报 0，不炸 status
        fts_rows = 0
        unindexed = 0

    return {
        "episodes": _one("SELECT COUNT(*) FROM episode WHERE subject_id=?"),
        "lines": _one("SELECT COUNT(*) FROM journal_line WHERE subject_id=?"),
        "fts_rows": fts_rows,
        "unindexed": unindexed,
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
    "rebuild_episode_index",
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
