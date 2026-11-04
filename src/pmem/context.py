"""上下文水位（M19）：在爆之前提醒，而不是爆了才说。

**为什么需要**：会话窗口是有限的，而「被清空」这件事对我是没有痛感的——
直到下一个我什么都不记得。用户的原话是「不要爆了才提醒」，所以这里做的是
**提前分级预警**：到 `warn_at` 说一句「该收尾了」，到 `urgent_at` 直接催换会话。

**为什么不拿真实 usage**：宿主没有把这个数开放给插件。插件能拿到的是
**这一轮即将发送的 messages 数组**（真实、完整），加上固定的底座开销
（工具 schema、系统提示等，配置里配 `base_tokens`）——估出来的水位用来分级足够。
两条纪律：

1. **估少比估多危险**，所以底座默认给 6000，宁可早提醒；
2. **同一个档位只喊一次**：跨档（ok→warn、warn→urgent）才说话，
   一直喊只会变成背景噪音，和「不提醒」没区别。

状态落盘（`~/.dsh/pmem/context.json`），进程重启后仍然知道喊过哪一档——
否则每次刷新插件都会重复喊一遍同一句。
"""

from __future__ import annotations

import json
import os
from typing import Any

from .base.clock import now_iso
from .memory.text import estimate_tokens as _estimate_text

# 默认值（config 里 [context] 可覆盖）
# 默认 1M：实测 tokenMeter 在这个会话上报过 477k 仍在正常对话，
# 说明窗口远大于 128k（DeepSeek 这一代的长上下文档）。配错会整档偏移，
# 所以给了 [context.windows] 按模型名覆盖，并在 CLI 上开了 --model。
DEFAULT_WINDOW = 1_000_000
DEFAULT_BASE = 6_000
DEFAULT_WARN = 0.65
DEFAULT_URGENT = 0.85
# 自动交接（M19 补）：到这条线不再只是「提醒」，而是**直接动手**——写停在哪 + 压缩上下文。
# 用户的原话是「不是提醒是到 80% 时自动交接新会话，可在设置关闭」，所以它是个开关。
DEFAULT_AUTO_AT = 0.80
DEFAULT_STATE = "~/.dsh/pmem/context.json"

LEVELS = ("ok", "warn", "urgent")


def settings(cfg: Any = None) -> dict[str, Any]:
    value = cfg.get("context", {}) if cfg is not None else {}
    return value if isinstance(value, dict) else {}


def auto_enabled(cfg: Any = None) -> bool:
    """自动交接开关：配置默认开，设置页的运行开关（runtime.json）可以关。"""
    raw = settings(cfg).get("auto")
    return True if raw is None else bool(raw)


def auto_at(cfg: Any = None) -> float:
    raw = settings(cfg).get("auto_at")
    value = DEFAULT_AUTO_AT if raw is None else float(raw)
    return min(0.99, max(0.5, value))


def window_tokens(cfg: Any = None, model: str | None = None) -> int:
    """窗口大小：``[context.windows]`` 里按模型名匹配，否则用 ``window_tokens``。

    为什么要按模型配：换模型（128k ↔ 1M）时窗口差好几倍，配错整档偏移——
    128k 的窗口配 1M 不会提醒，1M 的窗口配 128k 会把 1/8 当成满。
    """
    table = settings(cfg).get("windows")
    if model and isinstance(table, dict):
        for pattern, value in table.items():
            if str(pattern).lower() in str(model).lower():
                try:
                    return max(8_000, int(value))
                except (TypeError, ValueError):
                    continue
    return max(8_000, int(settings(cfg).get("window_tokens") or DEFAULT_WINDOW))


def base_tokens(cfg: Any = None) -> int:
    # 不能写 `raw or DEFAULT_BASE`：0 是合法值（测试/校准时会关掉底座），
    # 被 `or` 吞掉就变成默认 6000，水位凭空抬高 6k。
    raw = settings(cfg).get("base_tokens")
    return max(0, int(DEFAULT_BASE if raw is None else raw))


def estimate(text: str) -> int:
    """和记忆库同一个估算口径（中文 ~0.9 token/字，英文 ~0.28 token/字符）。"""
    return _estimate_text(str(text or ""))


def _content_text(content: Any) -> str:
    """messages[].content 可能是字符串，也可能是 [{"type":"text","text":...}] 数组。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text") or part.get("content") or ""
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return str(content)


def estimate_messages(messages: Any) -> int:
    """把整轮 messages 估成 token（每条加一点角色/结构开销）。"""
    if not isinstance(messages, list):
        return 0
    total = 0
    for message in messages:
        if isinstance(message, dict):
            total += estimate(_content_text(message.get("content")))
            total += 6  # role + 结构开销
        else:
            total += estimate(str(message)) + 6
    return int(total)


def level_of(ratio: float, cfg: Any = None) -> str:
    s = settings(cfg)
    urgent = float(s.get("urgent_at") or DEFAULT_URGENT)
    warn = float(s.get("warn_at") or DEFAULT_WARN)
    if ratio >= urgent:
        return "urgent"
    if ratio >= warn:
        return "warn"
    return "ok"


def status(
    tokens: int,
    *,
    cfg: Any = None,
    window: int | None = None,
    actual: int | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """算一次水位。

    ``tokens`` 是 messages 的估算（底座自动加上）；``actual`` 是宿主 `tokenMeter`
    量出来的真实值——**有真值就不用估算**（估算只留作没有 tokenMeter 的宿主）。
    """
    win = int(window or window_tokens(cfg, model))
    base = base_tokens(cfg)
    if actual is not None and int(actual) >= 0:
        used = max(0, int(actual))
        source = "meter"
    else:
        used = max(0, int(tokens)) + base
        source = "estimate"
    ratio = min(1.0, used / win) if win else 0.0
    return {
        "tokens": int(tokens),
        "actual": None if actual is None else int(actual),
        "source": source,
        "base": base,
        "used": used,
        "window": win,
        "ratio": round(ratio, 4),
        "percent": int(round(ratio * 100)),
        "remaining": max(0, win - used),
        "level": level_of(ratio, cfg),
    }


def state_path(cfg: Any = None) -> str:
    raw = os.environ.get("PMEM_CONTEXT_STATE") or settings(cfg).get("state_file") or DEFAULT_STATE
    return os.path.expanduser(str(raw))


def load_state(path: str | None = None, *, cfg: Any = None) -> dict[str, Any]:
    target = path or state_path(cfg)
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict[str, Any], path: str | None = None, *, cfg: Any = None) -> None:
    target = path or state_path(cfg)
    try:
        directory = os.path.dirname(target)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
    except OSError:
        # 落盘失败只意味着下次可能重复喊一次，不影响这一轮的提醒
        return


def render(found: dict[str, Any], *, cfg: Any = None, auto: bool = False) -> str:
    """把水位说成一句人话。空串表示不需要提醒（ok 档）。

    ``auto=True`` 是「已经自动交接过了」的文案：这时候不该再说「你该收尾了」，
    而该说「收在哪、已经做了什么」，否则用户不知道刚刚发生了什么。
    """
    if auto:
        # 这里只报「到线了」，不要说「已完成」——真正有没有写成、压没压成，
        # 只有插件那边知道（它才是动手的人），文案由它覆盖。
        return (
            f"◐ 上下文约 {found.get('percent')}%：到交接线了——先写「停在哪」，再压缩历史。"
        )
    if found.get("level") == "ok":
        return ""
    percent = found.get("percent")
    window = found.get("window") or 0
    if found.get("level") == "urgent":
        return (
            f"⚠ 上下文约 {percent}%（{found.get('used')}/{window}）：**现在就该收尾**—— "
            "先更新自我层「停在哪 / 下次」，把该记的写下来，然后开新会话；"
            "继续聊下去，这一段有被清掉的风险。"
        )
    return (
        f"◐ 上下文约 {percent}%（{found.get('used')}/{window}）：合适收个尾——"
        "更新「停在哪」、把重要的先记下来；还没做完的写进「未完」。"
    )


def record(
    session_id: str,
    tokens: int,
    *,
    cfg: Any = None,
    window: int | None = None,
    path: str | None = None,
    actual: int | None = None,
    auto: bool | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """记录一次水位，返回 ``{"status", "speak", "text", "crossed", "auto"}``。

    - ``speak`` 只在**跨档**时为真：同一档位反复提醒等于没提醒；
    - ``auto.do`` 表示「该自动交接了」，每个会话只给一次（``auto_done`` 落盘）；
    - ``actual`` 是宿主量出来的真值，有它就不看估算；
    - ``auto=None`` 时读配置里的开关（设置页可以关掉）。
    """
    found = status(tokens, cfg=cfg, window=window, actual=actual, model=model)
    state = load_state(path, cfg=cfg)
    sessions = state.get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}
    key = str(session_id)
    previous = sessions.get(key) if isinstance(sessions.get(key), dict) else {}
    prev_level = str(previous.get("level") or "ok")
    new_level = str(found["level"])
    order = {name: index for index, name in enumerate(LEVELS)}
    crossed = order.get(new_level, 0) > order.get(prev_level, 0)

    auto_on = auto_enabled(cfg) if auto is None else bool(auto)
    already = bool(previous.get("auto_done"))
    do_auto = bool(auto_on and not already and found["ratio"] >= auto_at(cfg))

    text = ""
    if do_auto:
        text = render(found, cfg=cfg, auto=True)
    elif crossed and not already:
        # 已经交接过的会话不再喊「你该收尾了」：动作做完了，再喊就是噪音。
        text = render(found, cfg=cfg)

    sessions[key] = {
        "tokens": found["tokens"],
        "used": found["used"],
        "source": found["source"],
        "window": found["window"],
        "percent": found["percent"],
        "level": new_level,
        "auto_done": bool(already or do_auto),
        "text": text or previous.get("text") or "",
        "updated_at": now_iso(),
    }
    state["sessions"] = sessions
    state["updated_at"] = now_iso()
    save_state(state, path, cfg=cfg)
    return {
        "status": found,
        "session": key,
        "speak": bool(text),
        "text": text,
        "crossed": f"{prev_level}→{new_level}" if crossed else "",
        "auto": {
            "do": do_auto,
            "done": bool(already or do_auto),
            "at": round(auto_at(cfg), 3),
            "text": text if do_auto else "",
        },
    }


def report(*, session_id: str | None = None, cfg: Any = None, path: str | None = None) -> dict[str, Any]:
    """给 CLI / 设置页看的：已知各会话的水位。"""
    state = load_state(path, cfg=cfg)
    sessions = state.get("sessions") if isinstance(state.get("sessions"), dict) else {}
    items = []
    for sid, info in sessions.items():
        if not isinstance(info, dict):
            continue
        if session_id and str(sid) != str(session_id):
            continue
        items.append({"session": str(sid), **info})
    items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return {
        "sessions": items,
        "window": window_tokens(cfg),
        "base": base_tokens(cfg),
        "warn_at": float(settings(cfg).get("warn_at") or DEFAULT_WARN),
        "urgent_at": float(settings(cfg).get("urgent_at") or DEFAULT_URGENT),
    }


__all__ = [
    "estimate",
    "estimate_messages",
    "level_of",
    "load_state",
    "record",
    "render",
    "report",
    "status",
]
