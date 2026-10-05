"""Structured errors for the agent layer.

Two rules from the team contract shape this module:

1. The HTTP envelope is ``{ok, data, error, request_id}``, and ``error.details``
   is always an object -- ``{}`` when there is nothing to add, never ``null``.
2. Internal services raise; only the HTTP edge wraps. A function that returns
   ``Product | None`` must not also return an error object.

So this module provides one exception type that already knows its
:class:`~app.contracts.common.ErrorCode`, and one function that turns any
exception into an :class:`~app.contracts.common.ErrorDetail`. Nothing here
imports FastAPI: the same exception has to work from a CLI, a test and a route
handler.
"""

from __future__ import annotations

from typing import Any

from app.contracts.common import ErrorCode, ErrorDetail, is_retryable

#: Codes the agent layer raises for its own failures. Kept here rather than
#: inlined so a caller can enumerate them.
LLM_CODES: frozenset[ErrorCode] = frozenset({
    ErrorCode.LLM_PARSE_FAILED,
    ErrorCode.LLM_UNAVAILABLE,
})


class AgentError(Exception):
    """An error that carries a wire code, details and a retryable flag.

    ``retryable`` defaults from the code, because whether a caller should try
    again is a property of the failure, not of the call site. A parse failure is
    worth one repair attempt; a mandate that was revoked is not worth any.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict[str, Any] = details if details is not None else {}
        self.retryable = is_retryable(code) if retryable is None else retryable

    def to_detail(self) -> ErrorDetail:
        return ErrorDetail(
            code=self.code,
            message=self.message,
            details=self.details,
            retryable=self.retryable,
        )

    def __str__(self) -> str:
        return f"[{self.code.value}] {self.message}"


def detail_from(exc: BaseException) -> ErrorDetail:
    """Map any exception to the wire shape.

    An :class:`AgentError` keeps its code. Anything else becomes
    ``INTERNAL_ERROR`` with its type recorded in ``details``, because a raw
    traceback string can carry a file path or a query, and the envelope is
    shown to users and written to the audit log.
    """
    if isinstance(exc, AgentError):
        return exc.to_detail()
    return ErrorDetail(
        code=ErrorCode.INTERNAL_ERROR,
        message="an unexpected error occurred",
        details={"exception": type(exc).__name__},
        retryable=False,
    )


def llm_unavailable(reason: str, *, model: str | None = None) -> AgentError:
    """The model could not be reached or produced nothing usable.

    Always recoverable: the caller falls back to the deterministic parser rather
    than failing the turn, so this is ``retryable`` in the sense that the
    conversation continues.
    """
    return AgentError(
        ErrorCode.LLM_UNAVAILABLE,
        f"the language model was unavailable: {reason}",
        details={"model": model} if model else {},
        retryable=True,
    )


def llm_parse_failed(problems: list[str], *, raw_reply: str = "") -> AgentError:
    """The model answered, but not in a shape the contract accepts.

    ``problems`` is carried in full: the point of reporting a parse failure is to
    see which rule the reply broke, and a single summary string throws that away.
    The raw reply is truncated -- it is diagnostic, not evidence.
    """
    return AgentError(
        ErrorCode.LLM_PARSE_FAILED,
        "the language model did not return a usable structured result",
        details={"problems": problems, "reply_excerpt": raw_reply[:400]},
        retryable=True,
    )


__all__ = [
    "AgentError",
    "LLM_CODES",
    "detail_from",
    "llm_parse_failed",
    "llm_unavailable",
]
