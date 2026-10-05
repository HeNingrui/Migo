"""Shared response envelope, error codes and cross-cutting enums.

This module is owned by A. B, C and D read it; only A changes it.

Two hard rules from the team contract:

1. The HTTP envelope is ``{ok, data, error, request_id}``.
2. ``error.details`` is always an object. When there is nothing to add it is
   ``{}`` -- never ``null``.
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

T = TypeVar("T")

REQUEST_ID_PREFIX = "req_"


def new_request_id() -> str:
    """Return a fresh request identifier.

    Callers that need deterministic output (tests, fixtures) pass their own id
    instead of calling this.
    """
    return REQUEST_ID_PREFIX + uuid.uuid4().hex[:16]


# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------

class ErrorCode(str, Enum):
    """Every structured error code in the system.

    Grouped by the boundary that raises it. The value is the wire value.
    """

    # --- generic -----------------------------------------------------------
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"

    # --- catalog / search --------------------------------------------------
    PRODUCT_NOT_FOUND = "PRODUCT_NOT_FOUND"
    INVALID_CONSTRAINTS = "INVALID_CONSTRAINTS"
    SEARCH_UNAVAILABLE = "SEARCH_UNAVAILABLE"

    # --- agent -------------------------------------------------------------
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    LLM_PARSE_FAILED = "LLM_PARSE_FAILED"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    MANDATE_AMBIGUOUS = "MANDATE_AMBIGUOUS"
    MANDATE_VALIDATION_FAILED = "MANDATE_VALIDATION_FAILED"

    # --- mandate -----------------------------------------------------------
    MANDATE_NOT_FOUND = "MANDATE_NOT_FOUND"
    MANDATE_NOT_ACTIVE = "MANDATE_NOT_ACTIVE"
    MANDATE_EXPIRED = "MANDATE_EXPIRED"
    MANDATE_REVOKED = "MANDATE_REVOKED"
    MANDATE_VERSION_STALE = "MANDATE_VERSION_STALE"
    MANDATE_OWNER_MISMATCH = "MANDATE_OWNER_MISMATCH"
    MANDATE_IMMUTABLE = "MANDATE_IMMUTABLE"

    # --- quote -------------------------------------------------------------
    QUOTE_NOT_FOUND = "QUOTE_NOT_FOUND"
    QUOTE_EXPIRED = "QUOTE_EXPIRED"
    QUOTE_CHANGED = "QUOTE_CHANGED"
    QUOTE_HASH_MISMATCH = "QUOTE_HASH_MISMATCH"
    QUOTE_CURRENCY_MISMATCH = "QUOTE_CURRENCY_MISMATCH"

    # --- policy ------------------------------------------------------------
    MERCHANT_NOT_ALLOWED = "MERCHANT_NOT_ALLOWED"
    CATEGORY_NOT_ALLOWED = "CATEGORY_NOT_ALLOWED"
    CONNECTION_NOT_ALLOWED = "CONNECTION_NOT_ALLOWED"
    ANC_REQUIREMENT_NOT_MET = "ANC_REQUIREMENT_NOT_MET"
    #: The mandate names a device the product must work with, and the trusted
    #: product data does not confirm it. Raised both when the product declares a
    #: different device and when it declares none: an unrecorded device list is
    #: unknown, and unknown never satisfies a requirement. See CC-10.
    DEVICE_REQUIREMENT_NOT_MET = "DEVICE_REQUIREMENT_NOT_MET"
    QUANTITY_LIMIT_EXCEEDED = "QUANTITY_LIMIT_EXCEEDED"
    ADDRESS_CHANGE_NOT_ALLOWED = "ADDRESS_CHANGE_NOT_ALLOWED"
    PAYMENT_ROUTE_NOT_ALLOWED = "PAYMENT_ROUTE_NOT_ALLOWED"
    CAP_PER_TRANSACTION_EXCEEDED = "CAP_PER_TRANSACTION_EXCEEDED"
    ROLLING_CAP_EXCEEDED = "ROLLING_CAP_EXCEEDED"
    VELOCITY_LIMIT_EXCEEDED = "VELOCITY_LIMIT_EXCEEDED"
    QUANTITY_TOTAL_EXCEEDED = "QUANTITY_TOTAL_EXCEEDED"
    PRINCIPAL_MISMATCH = "PRINCIPAL_MISMATCH"
    AGENT_MISMATCH = "AGENT_MISMATCH"
    ESCALATION_REQUIRED = "ESCALATION_REQUIRED"
    ESCALATION_TIMEOUT = "ESCALATION_TIMEOUT"

    # --- proposal / reservation -------------------------------------------
    PROPOSAL_NOT_FOUND = "PROPOSAL_NOT_FOUND"
    RESERVATION_NOT_FOUND = "RESERVATION_NOT_FOUND"
    RESERVATION_CONFLICT = "RESERVATION_CONFLICT"
    RESERVATION_NOT_ACTIVE = "RESERVATION_NOT_ACTIVE"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"

    # --- capability --------------------------------------------------------
    CAPABILITY_MISSING = "CAPABILITY_MISSING"
    CAPABILITY_INVALID = "CAPABILITY_INVALID"
    CAPABILITY_EXPIRED = "CAPABILITY_EXPIRED"
    CAPABILITY_REPLAYED = "CAPABILITY_REPLAYED"
    CAPABILITY_CONTEXT_MISMATCH = "CAPABILITY_CONTEXT_MISMATCH"

    # --- payment -----------------------------------------------------------
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    PAYMENT_STATUS_UNKNOWN = "PAYMENT_STATUS_UNKNOWN"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    REVOCATION_MISSED = "REVOCATION_MISSED"

    # --- infrastructure ----------------------------------------------------
    DB_BUSY = "DB_BUSY"
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    INVALID_ORDER_STATE = "INVALID_ORDER_STATE"
    CONFIRMATION_MISMATCH = "CONFIRMATION_MISMATCH"


#: Codes that describe a denial of the *request itself* rather than a business
#: outcome the caller can retry as-is.
NON_RETRYABLE = frozenset({
    ErrorCode.VALIDATION_ERROR,
    ErrorCode.INVALID_CONSTRAINTS,
    ErrorCode.PRODUCT_NOT_FOUND,
    ErrorCode.MANDATE_NOT_FOUND,
    ErrorCode.MANDATE_EXPIRED,
    ErrorCode.MANDATE_REVOKED,
    ErrorCode.MANDATE_OWNER_MISMATCH,
    ErrorCode.MANDATE_VERSION_STALE,
    ErrorCode.QUOTE_EXPIRED,
    ErrorCode.QUOTE_CHANGED,
    ErrorCode.QUOTE_HASH_MISMATCH,
    ErrorCode.QUOTE_CURRENCY_MISMATCH,
    ErrorCode.CAPABILITY_INVALID,
    ErrorCode.CAPABILITY_EXPIRED,
    ErrorCode.CAPABILITY_REPLAYED,
    ErrorCode.CAPABILITY_CONTEXT_MISMATCH,
    ErrorCode.IDEMPOTENCY_KEY_REUSED,
    ErrorCode.IDEMPOTENCY_CONFLICT,
    ErrorCode.ESCALATION_TIMEOUT,
})

#: Policy violations themselves are never "retried"; they are reported.
POLICY_DENIAL_CODES = frozenset({
    ErrorCode.MERCHANT_NOT_ALLOWED,
    ErrorCode.CATEGORY_NOT_ALLOWED,
    ErrorCode.CONNECTION_NOT_ALLOWED,
    ErrorCode.ANC_REQUIREMENT_NOT_MET,
    ErrorCode.DEVICE_REQUIREMENT_NOT_MET,
    ErrorCode.QUANTITY_LIMIT_EXCEEDED,
    ErrorCode.ADDRESS_CHANGE_NOT_ALLOWED,
    ErrorCode.PAYMENT_ROUTE_NOT_ALLOWED,
    ErrorCode.CAP_PER_TRANSACTION_EXCEEDED,
    ErrorCode.ROLLING_CAP_EXCEEDED,
    ErrorCode.VELOCITY_LIMIT_EXCEEDED,
    ErrorCode.QUANTITY_TOTAL_EXCEEDED,
    ErrorCode.PRINCIPAL_MISMATCH,
    ErrorCode.AGENT_MISMATCH,
})

#: Outcomes a caller can act on, so they stay HTTP 200 with ``ok=false``.
#:
#: These describe what happened rather than a malformed request. A payment that
#: came back ``insufficient_balance`` was handled correctly; answering 400 would
#: tell the client it sent something wrong.
BUSINESS_OUTCOMES = frozenset({
    ErrorCode.INSUFFICIENT_BALANCE,
    ErrorCode.OUT_OF_STOCK,
    ErrorCode.PAYMENT_FAILED,
    ErrorCode.PAYMENT_STATUS_UNKNOWN,
    ErrorCode.REVOCATION_MISSED,
    ErrorCode.CONFIRMATION_MISMATCH,
    ErrorCode.INVALID_ORDER_STATE,
    ErrorCode.MANDATE_NOT_ACTIVE,
    ErrorCode.MANDATE_EXPIRED,
    ErrorCode.MANDATE_REVOKED,
    ErrorCode.MANDATE_VERSION_STALE,
    ErrorCode.QUOTE_EXPIRED,
    ErrorCode.QUOTE_CHANGED,
    ErrorCode.QUOTE_HASH_MISMATCH,
    ErrorCode.QUOTE_CURRENCY_MISMATCH,
    ErrorCode.CAPABILITY_INVALID,
    ErrorCode.CAPABILITY_EXPIRED,
    ErrorCode.CAPABILITY_REPLAYED,
    ErrorCode.CAPABILITY_CONTEXT_MISMATCH,
    ErrorCode.RESERVATION_NOT_ACTIVE,
    ErrorCode.ESCALATION_TIMEOUT,
    # Asking the principal is a handled outcome too: the agent did the right
    # thing by not deciding.
    ErrorCode.ESCALATION_REQUIRED,
})

DEFAULT_HTTP_STATUS: dict[ErrorCode, int] = {
    # --- the request itself is wrong --------------------------------------
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.INVALID_CONSTRAINTS: 422,
    ErrorCode.MANDATE_VALIDATION_FAILED: 422,
    ErrorCode.MANDATE_AMBIGUOUS: 422,
    ErrorCode.CAPABILITY_MISSING: 422,
    ErrorCode.MANDATE_OWNER_MISMATCH: 403,
    # --- not found --------------------------------------------------------
    ErrorCode.PRODUCT_NOT_FOUND: 404,
    ErrorCode.ORDER_NOT_FOUND: 404,
    ErrorCode.MANDATE_NOT_FOUND: 404,
    ErrorCode.QUOTE_NOT_FOUND: 404,
    ErrorCode.PROPOSAL_NOT_FOUND: 404,
    ErrorCode.RESERVATION_NOT_FOUND: 404,
    ErrorCode.SESSION_NOT_FOUND: 404,
    ErrorCode.NOT_FOUND: 404,
    # --- conflicting state ------------------------------------------------
    ErrorCode.CONFLICT: 409,
    ErrorCode.IDEMPOTENCY_KEY_REUSED: 409,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.RESERVATION_CONFLICT: 409,
    ErrorCode.MANDATE_IMMUTABLE: 409,
    ErrorCode.INVALID_STATE_TRANSITION: 409,
    # --- our side is unavailable -----------------------------------------
    ErrorCode.LLM_PARSE_FAILED: 502,
    ErrorCode.LLM_UNAVAILABLE: 503,
    ErrorCode.SEARCH_UNAVAILABLE: 503,
    ErrorCode.SERVICE_UNAVAILABLE: 503,
    ErrorCode.DB_BUSY: 503,
    ErrorCode.INTERNAL_ERROR: 500,
}


def http_status_for(code: ErrorCode) -> int:
    """Map a structured error code to its HTTP status.

    Every code resolves deliberately: a handled business outcome is 200, a
    policy denial is 200, an explicitly mapped code uses its mapping, and
    anything left over is a 400 because we did not classify it.
    """
    if code in BUSINESS_OUTCOMES:
        return 200  # handled outcome, the client should read the code
    if code in DEFAULT_HTTP_STATUS:
        return DEFAULT_HTTP_STATUS[code]
    if code in POLICY_DENIAL_CODES:
        return 200  # a denial is a valid handled outcome, not a transport error
    return 400


def is_business_outcome(code: ErrorCode) -> bool:
    """Whether ``code`` reports what happened rather than a bad request."""
    return code in BUSINESS_OUTCOMES or code in POLICY_DENIAL_CODES


def is_retryable(code: ErrorCode) -> bool:
    return code not in NON_RETRYABLE


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------

class ErrorDetail(BaseModel):
    """The ``error`` object of the envelope.

    ``details`` is an object and defaults to ``{}``; it is never ``null``.
    """

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    retryable: bool = False

    @classmethod
    def of(
        cls,
        code: ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> "ErrorDetail":
        return cls(
            code=code,
            message=message,
            details=details if details is not None else {},
            retryable=is_retryable(code) if retryable is None else retryable,
        )


class Envelope(BaseModel, Generic[T]):
    """The single HTTP response shape used by every endpoint."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    data: T | None = None
    error: ErrorDetail | None = None
    request_id: str

    @classmethod
    def success(cls, data: T, request_id: str) -> "Envelope[T]":
        return cls(ok=True, data=data, error=None, request_id=request_id)

    @classmethod
    def failure(cls, error: ErrorDetail, request_id: str) -> "Envelope[T]":
        return cls(ok=False, data=None, error=error, request_id=request_id)


# ---------------------------------------------------------------------------
# Currency -- placeholder only
# ---------------------------------------------------------------------------
#
# Multi-currency and FX are deliberately NOT implemented yet. The names below
# are the reserved seam so that adding them later does not rename anything a
# caller already uses, and does not touch D's catalog schema.
#
# When it is built, the plan is:
#
#   contracts/money.py   Currency registry (code, minor_units, symbol),
#                        FxRate(base, quote, rate_ppm: StrictInt, as_of,
#                        valid_until, source_type, evidence_id) and a
#                        deterministic convert().
#   Quote                gains source_currency + fx_rate_ppm + fx_as_of +
#                        fx_source_type + fx_evidence_id, and those values join
#                        quote_hash so a rate change forces a re-quote.
#   policy evaluator     gains two checks: the quote's source currency must
#                        match the rate's base, and the rate must not be stale.
#
# Two rules already decided, so the seam stays compatible:
#
#   1. There is exactly one SETTLEMENT currency. Every authorisation cap is
#      compared in it. A mandate never carries two currencies, because
#      "HK$300 or US$40" cannot be evaluated.
#   2. A rate is an integer in parts per million, never a float. Money is
#      integer minor units end to end, and a float rate would make the quote
#      hash platform-dependent and therefore unverifiable.

#: The one currency every cap, reservation and settlement is expressed in.
SETTLEMENT_CURRENCY = "HKD"

#: Reserved registry. Only the settlement currency is present for now; adding
#: an entry here is what turns a currency into a supported one.
SUPPORTED_CURRENCIES: dict[str, int] = {
    "HKD": 2,   # code -> minor units in one major unit (2 means 100 cents)
}


class Money(BaseModel):
    """Placeholder value object for an amount in an explicit currency.

    Not used by the current models, which still pin ``HKD`` directly. It exists
    so that the FX work has a place to land without reshaping call sites.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    amount_minor: int
    currency: str

    @model_validator(mode="after")
    def _currency_is_known(self) -> "Money":
        if self.currency not in SUPPORTED_CURRENCIES:
            raise ValueError(f"unsupported currency: {self.currency!r}")
        return self


# ---------------------------------------------------------------------------
# Cross-cutting enums
# ---------------------------------------------------------------------------

class SourceType(str, Enum):
    """Where a piece of data or a rate came from.

    ``OBSERVED_PUBLIC_SOURCE`` means a human recorded it from a public source
    with a timestamp. The two demo values must never be presented as observed
    market data.
    """

    SANDBOX = "SANDBOX"
    SYNTHETIC_DEMO = "SYNTHETIC_DEMO"
    OBSERVED_PUBLIC_SOURCE = "OBSERVED_PUBLIC_SOURCE"


class PaymentRail(str, Enum):
    FPS = "FPS"
    MASTERCARD = "MASTERCARD"
    VISA = "VISA"
    VIRTUAL_WALLET = "VIRTUAL_WALLET"


class ActorType(str, Enum):
    USER = "USER"
    AGENT = "AGENT"
    COMMERCE = "COMMERCE"
    SYSTEM = "SYSTEM"


# ``UserActionType`` used to be a standalone enum here. It is now an alias of
# ``Intent`` in ``agent.py``, because the natural-language parser and the
# explicit action dispatcher must share one vocabulary -- a second copy of the
# same list drifts silently, and the parser learns values the dispatcher has
# never heard of.
#
# The name stays importable from this module so existing call sites keep
# working. It resolves lazily to avoid a circular import: ``agent`` imports
# from ``common``, so ``common`` cannot import ``agent`` at module level.
_USER_ACTION_TYPE_NOTE = (
    "UserActionType is an alias of agent.Intent; import it from app.contracts "
    "or app.contracts.agent"
)

def __getattr__(name: str):
    if name == "UserActionType":
        from .agent import Intent

        return Intent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({*globals(), "UserActionType"})


class AgentPhase(str, Enum):
    """Orchestration/UI state only. Never a source of financial authority."""

    IDLE = "idle"
    AWAITING_REQUIREMENTS = "awaiting_requirements"
    SHOWING_PRODUCTS = "showing_products"

    BUILDING_MANDATE = "building_mandate"
    AWAITING_MANDATE_CONFIRMATION = "awaiting_mandate_confirmation"
    MANDATE_ACTIVE = "mandate_active"

    RUNNING_DELEGATED_PURCHASE = "running_delegated_purchase"
    AWAITING_ESCALATION = "awaiting_escalation"

    PAYMENT_RESERVED = "payment_reserved"
    PAYMENT_PROCESSING = "payment_processing"
    COMPLETED = "completed"
    DENIED = "denied"
    CANCELLED = "cancelled"
    PAYMENT_UNKNOWN = "payment_unknown"


class NextAction(str, Enum):
    """What the UI should offer next. Drives which buttons are shown."""

    ANSWER_QUESTION = "answer_question"
    SELECT_PRODUCT = "select_product"
    CONFIRM_MANDATE = "confirm_mandate"
    CONFIRM_PAYMENT = "confirm_payment"
    RUN_PURCHASE = "run_purchase"
    APPROVE_ESCALATION = "approve_escalation"
    CHECK_STATUS = "check_status"
    #: A is asking about the user rather than about a product: the profile is
    #: what the next search is missing, so the useful control is "answer this",
    #: not "pick one of these". Distinct from ANSWER_QUESTION because the subject
    #: is the person, and the client may want to present it differently.
    DESCRIBE_PREFERENCES = "describe_preferences"
    NONE = "none"


__all__ = [
    "ActorType",
    "AgentPhase",
    "BUSINESS_OUTCOMES",
    "DEFAULT_HTTP_STATUS",
    "Envelope",
    "ErrorCode",
    "ErrorDetail",
    "Money",
    "NON_RETRYABLE",
    "NextAction",
    "POLICY_DENIAL_CODES",
    "PaymentRail",
    "REQUEST_ID_PREFIX",
    "SETTLEMENT_CURRENCY",
    "SUPPORTED_CURRENCIES",
    "SourceType",
    "http_status_for",
    "is_business_outcome",
    "is_retryable",
    "new_request_id",
]
