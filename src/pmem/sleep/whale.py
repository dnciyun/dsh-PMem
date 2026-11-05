"""鲸眠（Whale Sleep）：用一次真正的 LLM 会话，把当天的原话消化成日记。

和固定的 8 阶段 :mod:`pmem.sleep.orchestrator` 分工：

- **鲸眠负责「想」**：开一个新会话（session_id = ``whale-sleep:<day>``，名字叫「鲸眠」），
  把当天的原文、醒来卡、最近几天的日记交给模型，让它像人睡前过电影一样，必要时
  用只读工具核对旧记忆，最后写出一页日记。
- **orchestrator 负责「算」**：去重、关系、冲突、规律、特质、反思、快照——这些是
  确定性代码，不需要模型，也不该让模型去猜。

为什么不让模型直接写 facts：facts 由日记原文派生（``journal._finish`` 里做），
这样每条结论都能回到原话。模型只写日记正文，编造的专名/句子会被 ``validate``
整篇打回模板版——宁可流水账，也不要假日记。

思考过程不丢：整个会话（system、原文、工具调用、工具结果、模型输出、用量）
逐行写进 ``<transcript_dir>/<day>.jsonl``，可以事后翻看「鲸眠那天想了什么」。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..base import clock
from ..base.errors import PmemError
from ..memory import journal

SESSION_NAME = "鲸眠"
DEFAULT_MODEL = ""  # 空 = 继承 journal.model / PMEM_JOURNAL_MODEL

WHALE_SYSTEM = f"""你是「{SESSION_NAME}」——夜间睡眠里的那一轮记忆整理会话。

现在你把今天发生的事重新过一遍，写成日记，留给明天的自己用。

你会看到：
- 今天的全部原文（「你：」是对方说的话；「我：」是你自己说的话）
- 醒来卡（你已经记得的身份与约定）
- 最近几天的日记（已经消化过的部分）

规则（硬性，违反会被校验打回模板）：
1. 只能使用原文里出现过的信息，一个都不许新增；专名（人名、地名、作品名）照抄原文；
2. 不写猜测、不写评价、不写「可能」「似乎」「大概」；
3. 人称：原文「你：」里的「我」指对方，写进日记要改成「你」；原文「我：」是你说的，保持「我」；
4. 一行一件事，3 到 6 行，每行不超过 30 个字；标题不超过 20 字，且不要写日期；
5. 拿不准的宁可不写；没说完、没定下来的事放进「未闭环」，不要替对方补结果；
6. 想核对旧记忆时，可以调用 memory_search / memory_get（只读，不会写库）。

最后只输出一个 JSON，不要 markdown 代码块：
{{"title": "今天最值得记的一件事", "lines": ["...", "..."], "loops": ["还没完成的事"], "notes": "一句话：今天最该记住什么"}}
"""

# 只给只读工具：鲸眠可以查，但不能绕过日记直接往库里塞结论。
WHALE_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": "按关键词检索已有记忆，用来核对某件事以前是否记过、说法有没有变",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "k": {"type": "integer"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_get",
            "description": "按 id 读一条记忆的全文",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    },
]


def _cfg(config: Any) -> dict[str, Any]:
    try:
        sleep_cfg = dict(config.get("sleep", {}) or {})
    except Exception:  # noqa: BLE001 配置形状不定时别炸
        sleep_cfg = {}
    whale = dict(sleep_cfg.get("whale", {}) or {})
    whale.setdefault("max_rounds", 6)
    whale.setdefault("max_chars", 6000)
    whale.setdefault("transcript_dir", "~/.dsh/pmem/whale-sleep")
    return whale


def build_model(config: Any, model: str | None = None) -> Any:
    """造一个 OpenAI 兼容客户端；没有模型名或凭证时抛错（鲸眠不能降级成假思考）。"""
    from ..bench.model import make_model

    whale = _cfg(config)
    name = (
        model
        or str(whale.get("model") or "").strip()
        or str(_journal_model(config) or "").strip()
        or os.environ.get("PMEM_WHALE_MODEL", "").strip()
    )
    if not name:
        raise PmemError(
            "鲸眠需要一个模型：配置 [sleep.whale] model 或 [journal] model，"
            "或设置 PMEM_WHALE_MODEL / PMEM_JOURNAL_MODEL",
            code="E_VALIDATION",
            field="model",
        )
    return make_model("openai", model=None if name in ("openai", "default") else name)


def _journal_model(config: Any) -> str:
    try:
        return str(dict(config.get("journal", {}) or {}).get("model") or "")
    except Exception:  # noqa: BLE001
        return ""


def _squash(text: str) -> str:
    return " ".join(str(text or "").split())


def transcript_path(config: Any, day_key: str, transcript_dir: str | Path | None = None) -> Path:
    base = Path(transcript_dir or _cfg(config).get("transcript_dir") or "~/.dsh/pmem/whale-sleep")
    return base.expanduser() / f"{day_key}.jsonl"


def recent_episodes(db: Any, *, subject: str, day_key: str, limit: int = 3) -> list[str]:
    """day 之前最近几篇日记（标题 + 正文），给鲸眠当日志连续性参考。"""
    try:
        rows = db.conn.execute(
            "SELECT day_key, title, summary FROM episode WHERE subject_id=? AND day_key < ? "
            "ORDER BY day_key DESC LIMIT ?",
            (subject, day_key, limit),
        ).fetchall()
    except Exception:  # noqa: BLE001 日记表还没建出来时不影响主流程
        return []
    items = []
    for row in rows:
        title = str(row["title"] or "").strip()
        summary = _squash(str(row["summary"] or ""))[:160]
        items.append(f"{row['day_key']} {title}：{summary}")
    return items


def collect_materials(
    db: Any,
    *,
    subject: str,
    day_key: str,
    config: Any,
    wake_text: str = "",
    recent_episodes: list[str] | None = None,
) -> dict[str, Any]:
    """当天原文 + 醒来卡 + 最近日记；超长按配置截断。"""
    whale = _cfg(config)
    max_chars = int(whale.get("max_chars") or 6000)
    rows = journal.pending(db, subject=subject, day_key=day_key, limit=int(_cfg_journal(config).get("max_lines", 200)))
    lines: list[str] = []
    used = 0
    for row in rows:
        who = "你" if row["role"] == journal.ROLE_USER else "我"
        line = f"{who}：{_squash(row['text'])[:180]}"
        if used + len(line) > max_chars:
            lines.append("（后面还有，今天原文太长，先看到这里）")
            break
        lines.append(line)
        used += len(line)
    return {
        "day": day_key,
        "raw_count": len(rows),
        "raw": "\n".join(lines),
        "wake": (wake_text or "")[:1200],
        "recent_episodes": list(recent_episodes or [])[:5],
    }


def _cfg_journal(config: Any) -> dict[str, Any]:
    try:
        return dict(config.get("journal", {}) or {})
    except Exception:  # noqa: BLE001
        return {}


def render_materials(materials: dict[str, Any]) -> str:
    parts = [f"今天：{materials['day']}", f"今天的原文（共 {materials['raw_count']} 行）：", materials["raw"] or "（今天没有留下原文）"]
    if materials.get("wake"):
        parts += ["", "醒来卡（你已经记得的）：", materials["wake"]]
    if materials.get("recent_episodes"):
        parts += ["", "最近几天的日记：", *[f"- {item}" for item in materials["recent_episodes"]]]
    return "\n".join(parts)


def _parse_json(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _dispatch(app: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """只读工具：鲸眠能查，不能直接写结论。"""
    if name == "memory_search":
        return app.search(str(args.get("query") or ""), k=int(args.get("k") or 5), record=False)
    if name == "memory_get":
        return app.get(str(args.get("id") or ""))
    return {"ok": False, "error": f"鲸眠没有这个工具：{name}"}


def _trim(outcome: Any) -> str:
    data = (outcome or {}).get("data") if isinstance(outcome, dict) else None
    data = data if data is not None else outcome
    if isinstance(data, dict) and "items" in data:
        data = {
            "items": [
                {"id": item.get("id"), "day": item.get("day"), "text": str(item.get("text") or "")[:120]}
                for item in (data.get("items") or [])[:3]
            ]
        }
    return json.dumps(data, ensure_ascii=False)[:1000]


def _record(handle: Any, payload: dict[str, Any]) -> None:
    handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def run(
    db: Any,
    *,
    subject: str,
    day_key: str,
    store: Any,
    app: Any,
    config: Any,
    model: Any,
    dry_run: bool = False,
    transcript_dir: str | Path | None = None,
    max_rounds: int | None = None,
    wake_text: str = "",
    recent_episodes: list[str] | None = None,
) -> dict[str, Any]:
    """开一次「鲸眠」会话，写当天日记；返回统计与转录路径。"""
    whale = _cfg(config)
    rounds = int(max_rounds or whale.get("max_rounds") or 6)
    materials = collect_materials(
        db, subject=subject, day_key=day_key, config=config,
        wake_text=wake_text, recent_episodes=recent_episodes,
    )
    if materials["raw_count"] == 0:
        return {"ok": False, "reason": "no_lines", "day_key": day_key, "session": SESSION_NAME}

    session_id = f"whale-sleep:{day_key}"
    started = time.perf_counter()
    path = transcript_path(config, day_key, transcript_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": WHALE_SYSTEM},
        {"role": "user", "content": render_materials(materials)},
    ]
    usage_total = 0
    tool_calls: list[str] = []
    draft: dict[str, Any] | None = None
    replies = 0
    verdict = "no_json"
    meta = {
        "session": SESSION_NAME,
        "session_id": session_id,
        "day": day_key,
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "model": getattr(model, "name", ""),
        "dry_run": dry_run,
    }
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        _record(handle, {"type": "meta", **meta})
        _record(handle, {"type": "message", "role": "system", "content": WHALE_SYSTEM})
        _record(handle, {"type": "message", "role": "user", "content": messages[1]["content"]})
        try:
            for round_index in range(rounds):
                reply = model.complete(messages, WHALE_TOOLS if round_index == 0 else None,
                                       hint={"whale": True, "round": round_index})
                replies += 1
                used = getattr(reply, "usage", None)
                usage_total += int(getattr(used, "total", 0) or 0) if used is not None else 0
                content = str(getattr(reply, "content", "") or "")
                calls = list(getattr(reply, "tool_calls", None) or [])
                assistant_message: dict[str, Any] = {"role": "assistant", "content": content}
                if calls:
                    assistant_message["tool_calls"] = calls
                messages.append(assistant_message)
                _record(handle, {"type": "message", **assistant_message})
                if calls:
                    for call in calls:
                        function = call.get("function") or {}
                        name = str(function.get("name") or "")
                        try:
                            args = json.loads(function.get("arguments") or "{}")
                        except json.JSONDecodeError:
                            args = {}
                        outcome = _dispatch(app, name, args)
                        tool_calls.append(name)
                        trimmed = _trim(outcome)
                        messages.append({"role": "tool", "tool_call_id": call.get("id") or name, "content": trimmed})
                        _record(handle, {"type": "tool", "name": name, "arguments": args, "result": trimmed})
                    continue
                parsed = _parse_json(content)
                if parsed and (parsed.get("lines") or []):
                    draft = parsed
                    verdict = "json"
                    break
                # 没给合规 JSON：提醒一次，下一轮只让它输出 JSON
                messages.append({"role": "user", "content": "只输出那个 JSON（title / lines / loops / notes），不要解释。"})
                _record(handle, {"type": "message", "role": "user", "content": "只输出那个 JSON……"})
        finally:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            applied: dict[str, Any] = {}
            if draft is None:
                verdict = "fallback_no_json"
                applied = {"ok": False, "reason": verdict}
            else:
                title = _squash(str(draft.get("title") or ""))[:40]
                lines = [_squash(str(x))[:60] for x in (draft.get("lines") or []) if _squash(str(x))][:8]
                loops = [_squash(str(x))[:60] for x in (draft.get("loops") or []) if _squash(str(x))][:5]
                if dry_run:
                    applied = {"ok": True, "dry_run": True, "title": title, "lines": lines, "loops": loops}
                else:
                    applied = journal.write_episode(
                        db, subject=subject, day_key=day_key, title=title, lines=lines,
                        store=store, config=config,
                    )
                    applied["loops"] = loops
                    applied["notes"] = _squash(str(draft.get("notes") or ""))[:200]
            _record(handle, {
                "type": "result",
                "verdict": verdict,
                "replies": replies,
                "tool_calls": tool_calls,
                "usage_total": usage_total,
                "elapsed_ms": elapsed_ms,
                "applied": applied,
            })
    return {
        "ok": bool(applied.get("ok")),
        "session": SESSION_NAME,
        "session_id": session_id,
        "day_key": day_key,
        "verdict": verdict,
        "replies": replies,
        "tool_calls": tool_calls,
        "usage_total": usage_total,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "transcript": str(path),
        "diary": applied,
        "dry_run": dry_run,
    }
