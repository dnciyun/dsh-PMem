"""统一错误模型（04 §4.3）。

所有对外错误都带 ``code``（稳定标识）、``message``（人读）、可选 ``hint``（怎么改）与
``field``（哪个参数）。接口层直接把 ``to_dict()`` 塞进 ``{"ok": false, "error": ...}``。
"""

from __future__ import annotations

from typing import Any

# 错误码 → HTTP 语义（REST 层用；MCP 层只用 code 字符串）
HTTP_STATUS: dict[str, int] = {
    "E_VALIDATION": 400,
    "E_NOT_FOUND": 404,
    "E_CONFLICT": 409,
    "E_DENIED": 403,
    "E_DEGRADED_READONLY": 503,
    "E_BUDGET": 429,
    "E_RATE_LIMIT": 429,
    "E_INTERNAL": 500,
}


class PmemError(Exception):
    """所有可预期错误的基类。"""

    code = "E_INTERNAL"

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        field: str | None = None,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.field = field
        if code is not None:
            self.code = code
        self.details = details or {}

    @property
    def http_status(self) -> int:
        return HTTP_STATUS.get(self.code, 500)

    def to_dict(self) -> dict[str, Any]:
        err: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            err["hint"] = self.hint
        if self.field:
            err["field"] = self.field
        if self.details:
            err["details"] = self.details
        return err

    def __str__(self) -> str:  # pragma: no cover - 仅调试用
        return f"{self.code}: {self.message}"


class ValidationError(PmemError):
    code = "E_VALIDATION"


class NotFoundError(PmemError):
    code = "E_NOT_FOUND"


class ConflictError(PmemError):
    code = "E_CONFLICT"


class DeniedError(PmemError):
    code = "E_DENIED"


class BudgetError(PmemError):
    code = "E_BUDGET"


class InternalError(PmemError):
    code = "E_INTERNAL"


# ---- 便捷构造 -------------------------------------------------------------

def is_valid_kind(kind: str) -> bool:
    return kind in KINDS


KINDS = (
    "episodic",
    "semantic",
    "person",
    "relation",
    "experience",
    "goal",
    "meta",
)

PROVENANCE = (
    "user_explicit",
    "user_confirmed",
    "external",
    "observed",
    "inferred",
    "unconfirmed",
)

STATUSES = (
    "candidate",
    "active",
    "dormant",
    "archived",
    "contested",
    "superseded",
    "discarded",
    "deleted",
)

SENSITIVITY = ("public", "personal", "secret")
