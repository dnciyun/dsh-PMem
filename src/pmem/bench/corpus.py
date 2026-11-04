"""剧本格式：一个人物，若干天，每天几轮，外加考核节点。

剧本是 JSON，刻意做成**人能读、能手写**的样子：写测试最难的不是跑，是造一段
像真的聊天。字段越少越好，缺省值尽量合理。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Persona:
    id: str
    name: str
    profile: str = ""
    voice: str = ""


@dataclass
class Turn:
    time: str = "09:00"  # 本地时间 HH:MM
    user: str = ""
    # 剧本声明「这一轮用户明确陈述了这些事实」。
    # 真模型模式下它只是提示（写不写由模型决定）；桩模型模式下它就是写入指令。
    write: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""


@dataclass
class Day:
    date: str
    turns: list[Turn] = field(default_factory=list)


@dataclass
class Exam:
    """一个考核节点：`after` 这天结束时（或再过 `delay_days` 天）出这套题。"""

    after: str
    delay_days: int = 0
    questions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Corpus:
    version: int
    persona: Persona
    days: list[Day]
    exams: list[Exam] = field(default_factory=list)

    def turn_count(self) -> int:
        return sum(len(day.turns) for day in self.days)

    def dates(self) -> list[str]:
        return [day.date for day in self.days]


def _as_turn(raw: Any) -> Turn:
    if isinstance(raw, str):
        return Turn(user=raw)
    return Turn(
        time=str(raw.get("time") or "09:00"),
        user=str(raw.get("user") or ""),
        write=list(raw.get("write") or []),
        note=str(raw.get("note") or ""),
    )


def load_corpus(path: str | Path) -> Corpus:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    persona = data.get("persona") or {}
    if not persona.get("id"):
        raise ValueError("剧本缺 persona.id")
    days = [
        Day(date=str(day["date"]), turns=[_as_turn(t) for t in day.get("turns") or []])
        for day in data.get("days") or []
    ]
    if not days:
        raise ValueError("剧本没有任何一天")
    exams = [
        Exam(
            after=str(exam["after"]),
            delay_days=int(exam.get("delay_days") or 0),
            questions=list(exam.get("questions") or []),
        )
        for exam in data.get("exams") or []
    ]
    return Corpus(
        version=int(data.get("version") or 1),
        persona=Persona(
            id=str(persona["id"]),
            name=str(persona.get("name") or persona["id"]),
            profile=str(persona.get("profile") or ""),
            voice=str(persona.get("voice") or ""),
        ),
        days=days,
        exams=exams,
    )
