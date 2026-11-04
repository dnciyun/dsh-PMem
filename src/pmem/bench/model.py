"""被试点模型：B 方案（每轮真调模型）的接线口。

只有两个实现：

- ``OpenAICompatModel``：任何 OpenAI 兼容端点（DeepSeek / OpenAI / 本地网关都行）；
- ``StubModel``：不花钱的桩，用来验证「时钟 → 注入 → 工具 → 记账」这条流水线。

桩的 token 数是按字数估的，报告里会标 ``estimated``；真模式的数是接口返回的
``usage``，这才是能拿去签字的数。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # 命中上下文缓存的输入 token。DeepSeek 会自动缓存重复前缀，命中价是未命中的 1/4 左右，
    # 所以这一列直接决定"看起来同样的调用"到底花了多少钱。
    cached_tokens: int = 0
    estimated: bool = False

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.cached_tokens += other.cached_tokens
        self.estimated = self.estimated or other.estimated

    def cost_usd(self, *, hit: float = 0.07, miss: float = 0.27, out: float = 1.10) -> float:
        """按公开牌价估算（美元/百万 token）；只用于对比，不作结算。"""
        miss_tokens = max(0, self.prompt_tokens - self.cached_tokens)
        return (
            miss_tokens * miss / 1e6
            + self.cached_tokens * hit / 1e6
            + self.completion_tokens * out / 1e6
        )


@dataclass
class Reply:
    content: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    usage: Usage | None = None


class Model(Protocol):
    name: str
    paid: bool

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        hint: dict[str, Any] | None = None,
    ) -> Reply: ...


class StubModel:
    """不调模型：按剧本里声明的 write 造工具调用，验证流水线用。"""

    name = "stub"
    paid = False

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        hint: dict[str, Any] | None = None,
    ) -> Reply:
        hint = hint or {}
        calls: list[dict[str, Any]] = []
        # 只在第一轮发工具调用，否则会自己跟自己循环到 max_rounds。
        first_round = int(hint.get("round") or 0) == 0
        for index, item in enumerate(hint.get("write") or [] if first_round else []):
            calls.append(
                {
                    "id": f"stub-{index}",
                    "type": "function",
                    "function": {
                        "name": "memory_add",
                        "arguments": json.dumps(
                            {
                                "text": item.get("text", ""),
                                "kind": item.get("kind", "semantic"),
                                "groups": item.get("groups") or [],
                                "grade": item.get("grade"),
                            },
                            ensure_ascii=False,
                        ),
                    },
                }
            )
        prompt_chars = sum(len(str(m.get("content") or "")) for m in messages)
        return Reply(
            content=hint.get("reply") or "（桩模型：这一轮没有真实回答）",
            tool_calls=calls or None,
            usage=Usage(prompt_chars // 3, 20, estimated=True),
        )


class OpenAICompatModel:
    """真模型。``--model`` 默认走环境里的 ``PMEM_BENCH_MODEL``。"""

    paid = True

    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        temperature: float = 0.7,
        timeout: int = 120,
        retries: int = 3,
        max_tokens: int = 800,
    ) -> None:
        self.model = model or os.environ.get("PMEM_BENCH_MODEL") or "deepseek-chat"
        self.base_url = (base_url or os.environ.get("PMEM_BENCH_BASE_URL") or "https://api.deepseek.com").rstrip("/")
        self.api_key = api_key or os.environ.get("PMEM_BENCH_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or ""
        self.temperature = temperature
        self.timeout = timeout
        self.retries = retries
        self.max_tokens = max_tokens
        self.name = self.model
        if not self.api_key:
            raise ValueError(
                "没有模型凭证：设置 PMEM_BENCH_API_KEY（或 DEEPSEEK_API_KEY），"
                "或用 --model stub 先验流水线"
            )

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        hint: dict[str, Any] | None = None,
    ) -> Reply:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": (hint or {}).get("temperature", self.temperature),
            "max_tokens": self.max_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        last_error = ""
        for attempt in range(self.retries):
            request = urllib.request.Request(
                f"{self.base_url}/chat/completions",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
                message = (data.get("choices") or [{}])[0].get("message") or {}
                usage = data.get("usage") or {}
                return Reply(
                    content=message.get("content") or "",
                    tool_calls=message.get("tool_calls") or None,
                    usage=Usage(
                        int(usage.get("prompt_tokens") or 0),
                        int(usage.get("completion_tokens") or 0),
                        int(
                            usage.get("prompt_cache_hit_tokens")
                            or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                            or 0
                        ),
                    ),
                )
            except urllib.error.HTTPError as exc:
                last_error = f"HTTP {exc.code}: {exc.read()[:300].decode('utf-8', 'replace')}"
                if exc.code in (400, 401, 403, 404):
                    break
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = str(exc)
            time.sleep(min(8, 2 ** attempt))
        raise RuntimeError(f"模型调用失败：{last_error}")


def make_model(kind: str, **kwargs: Any) -> Model:
    if kind == "stub":
        return StubModel()
    return OpenAICompatModel(**kwargs)
