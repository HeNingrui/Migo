"""Mandate contracts: what a principal authorises an agent to do.

Ownership boundary (A/C Delegated Commerce v2.0, sections 3-4):

* A parses the user's words into a :class:`MandateDraft` and obtains consent.
* C owns the activated :class:`Mandate`, its version history, its canonical
  policy and its hash. A never invents a mandate id, a version or a hash.

Neither type is a payment instruction. A mandate permits; it never pays.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from .product import Category, Connection, Device

MandateStatus = Literal["ACTIVE", "REVOKED", "EXPIRED", "SUPERSEDED"]

HASH_PREFIX = "sha256:"


def canonical_bytes(policy: dict[str, Any]) -> bytes:
    """Serialise an executable policy deterministically.

    The byte string is what the policy hash is computed over, so every choice
    here is load-bearing:

    * keys sorted, so dict insertion order cannot change the hash
    * no whitespace, so reformatting cannot change the hash
    * ``ensure_ascii=False`` so non-ASCII merchant names are stable
    * no timestamps, no random values, no display copy

    Lists keep their given order. Callers must therefore emit lists in a stable
    order (the repositories sort before hashing).
    """
    return json.dumps(
        policy, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def policy_hash(policy: dict[str, Any]) -> str:
    """Content digest of an executable policy.

    This is a *content digest*, not a cryptographic signature and not an
    identity proof. It answers "is the rule set I am looking at the same one
    that was executed?" -- it does not prove who wrote it. Authority comes from
    C holding the mandate, not from this string.
    """
    return HASH_PREFIX + hashlib.sha256(canonical_bytes(policy)).hexdigest()


class MandateDraft(BaseModel):
    """A's parse of the user's words. Not yet enforceable.

    Every unset field is a question A must resolve before activation. Nothing
    here reaches the payment path: C only ever receives an activated
    :class:`Mandate`.
    """

    model_config = ConfigDict(extra="forbid")

    # --- scope ---
    allowed_merchants: list[str] | None = None
    allowed_categories: list[Category] | None = None
    required_connection: Connection | None = None
    anc_required: bool | None = None

    #: The device the purchase must work with (phone / computer / game_console /
    #: tablet). Part of the authorisation scope rather than of the search
    #: constraints, because C has to *check* it against trusted product data --
    #: and because "it must work with my console" is a statement about what may
    #: be bought, not about how to rank what may be bought.
    #:
    #: Only the user may set this. A device the profile merely mentions
    #: ("我平时连手机") stays profile context: inferring a hard requirement from
    #: how someone describes their life is exactly the promotion this design
    #: forbids. See ``app/contracts/profile.py``.
    required_device: Device | None = None

    # --- money (all minor units, all measured against cash total) ---
    cap_per_transaction_cents: StrictInt | None = Field(default=None, ge=0)
    rolling_cap_cents: StrictInt | None = Field(default=None, ge=0)
    rolling_window_seconds: StrictInt | None = Field(default=None, gt=0)

    # --- behaviour limits ---
    velocity_max_count: StrictInt | None = Field(default=None, ge=1)
    velocity_window_seconds: StrictInt | None = Field(default=None, gt=0)
    max_quantity_total: StrictInt | None = Field(default=None, ge=1)

    # --- time and escalation ---
    valid_for_seconds: StrictInt | None = Field(default=None, gt=0)
    escalate_above_cents: StrictInt | None = Field(default=None, ge=0)

    # --- payment and delivery ---
    allowed_payment_routes: list[str] | None = None
    shipping_address_id: str | None = None
    address_change_allowed: bool | None = None

    # --- parse quality, surfaced to the user ---
    ambiguities: list[str] = Field(default_factory=list)
    unsupported_conditions: list[str] = Field(default_factory=list)
    source_spans: dict[str, str] = Field(default_factory=dict)

    # -- consistency checks A runs before showing the summary ---------------

    def structural_problems(self) -> list[str]:
        """Contradictions that must be resolved before activation.

        These are not policy violations. They are reasons the draft cannot
        become a mandate yet.
        """
        problems: list[str] = []

        cap = self.cap_per_transaction_cents
        rolling = self.rolling_cap_cents
        escalate = self.escalate_above_cents

        if cap is not None and rolling is not None and cap > rolling:
            problems.append("transaction cap exceeds rolling cap")
        if escalate is not None and cap is not None and escalate > cap:
            # An escalation threshold above the hard cap can never fire.
            problems.append("escalation threshold exceeds transaction cap")
        if self.rolling_cap_cents is not None and self.rolling_window_seconds is None:
            problems.append("rolling cap given without a rolling window")
        if self.velocity_max_count is not None or self.velocity_window_seconds is not None:
            if self.velocity_max_count is None or self.velocity_window_seconds is None:
                problems.append("velocity limit needs both a count and a window")
        if self.valid_for_seconds is None:
            problems.append("no expiry")
        if self.allowed_merchants is not None and not self.allowed_merchants:
            problems.append("merchant allowlist is present but empty")
        if self.allowed_categories is not None and not self.allowed_categories:
            problems.append("category allowlist is present but empty")
        if self.allowed_payment_routes is not None and not self.allowed_payment_routes:
            problems.append("payment route allowlist is present but empty")
        if self.shipping_address_id is None:
            problems.append("no shipping address")
        return problems

    def missing_required_fields(self) -> list[str]:
        """Fields with no value at all.

        A missing field is a clarification, not a contradiction.
        """
        required = {
            "cap_per_transaction_cents": self.cap_per_transaction_cents,
            "rolling_cap_cents": self.rolling_cap_cents,
            "rolling_window_seconds": self.rolling_window_seconds,
            "velocity_max_count": self.velocity_max_count,
            "velocity_window_seconds": self.velocity_window_seconds,
            "max_quantity_total": self.max_quantity_total,
            "valid_for_seconds": self.valid_for_seconds,
            "allowed_merchants": self.allowed_merchants,
            "allowed_categories": self.allowed_categories,
            "allowed_payment_routes": self.allowed_payment_routes,
            "shipping_address_id": self.shipping_address_id,
            "address_change_allowed": self.address_change_allowed,
        }
        return [name for name, value in required.items() if value is None]

    def can_activate(self) -> bool:
        return not self.missing_required_fields() and not self.structural_problems()


class Mandate(BaseModel):
    """An activated, immutable authorisation.

    One row per version. A later amendment creates a new version and marks the
    previous one ``SUPERSEDED``; existing rows are never rewritten.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mandate_id: str = Field(min_length=1)
    version: StrictInt = Field(ge=1)

    principal_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    status: MandateStatus

    currency: Literal["HKD"]
    valid_from: datetime
    expires_at: datetime

    allowed_merchants: tuple[str, ...]
    allowed_categories: tuple[Category, ...]
    required_connection: Connection | None = None
    anc_required: bool = False
    #: The product must declare support for this device, or it is refused. Absent
    #: on the product side means unknown, and unknown does not satisfy a
    #: requirement -- see ``DEVICE_REQUIREMENT_NOT_MET``.
    required_device: Device | None = None

    cap_per_transaction_cents: StrictInt = Field(ge=0)
    rolling_cap_cents: StrictInt = Field(ge=0)
    rolling_window_seconds: StrictInt = Field(gt=0)

    velocity_max_count: StrictInt = Field(ge=1)
    velocity_window_seconds: StrictInt = Field(gt=0)
    max_quantity_total: StrictInt = Field(ge=1)

    escalate_above_cents: StrictInt | None = Field(default=None, ge=0)
    allowed_payment_routes: tuple[str, ...]

    shipping_address_id: str = Field(min_length=1)
    address_change_allowed: bool = False

    canonical_policy: str = Field(min_length=1)
    policy_hash: str = Field(min_length=1)
    consent_event_id: str = Field(min_length=1)

    created_at: datetime

    @model_validator(mode="after")
    def _internally_consistent(self) -> "Mandate":
        if self.expires_at <= self.valid_from:
            raise ValueError("expires_at must be after valid_from")
        if self.cap_per_transaction_cents > self.rolling_cap_cents:
            raise ValueError("cap_per_transaction_cents must not exceed rolling_cap_cents")
        if (
            self.escalate_above_cents is not None
            and self.escalate_above_cents > self.cap_per_transaction_cents
        ):
            raise ValueError("escalate_above_cents must not exceed cap_per_transaction_cents")

        # An empty allowlist would mean "permit nothing", so every proposal
        # would be denied and the mandate could never do anything. That is not a
        # useful state to reach by accident: the evaluator treats the tuple as a
        # filter, and a builder that drops the list would produce a mandate that
        # silently refuses everything. Refused at construction instead.
        for name, values in (
            ("allowed_merchants", self.allowed_merchants),
            ("allowed_categories", self.allowed_categories),
            ("allowed_payment_routes", self.allowed_payment_routes),
        ):
            if not values:
                raise ValueError(
                    f"{name} must not be empty: an empty allowlist permits nothing, "
                    "so the mandate could never authorise a purchase"
                )
        if any(not str(v).strip() for v in self.allowed_merchants):
            raise ValueError("allowed_merchants entries must be nonempty")
        if any(not str(v).strip() for v in self.allowed_payment_routes):
            raise ValueError("allowed_payment_routes entries must be nonempty")

        if not self.policy_hash.startswith(HASH_PREFIX):
            raise ValueError(f"policy_hash must start with {HASH_PREFIX!r}")
        # The stored canonical policy must match the stored hash, so a tampered
        # row is detected on read rather than trusted.
        try:
            parsed = json.loads(self.canonical_policy)
        except ValueError as exc:  # pragma: no cover - guarded by construction
            raise ValueError(f"canonical_policy is not valid JSON: {exc}") from exc
        if policy_hash(parsed) != self.policy_hash:
            raise ValueError("canonical_policy does not match policy_hash")
        return self

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at

    def is_active(self, now: datetime) -> bool:
        return self.status == "ACTIVE" and self.valid_from <= now < self.expires_at

    def executable_policy(self) -> dict[str, Any]:
        """The rule set that gets hashed and shown to a third party.

        Contains only enforceable fields: no display copy, no ids that would
        change between environments, no timestamps.
        """
        return {
            "allowed_categories": sorted(self.allowed_categories),
            "allowed_merchants": sorted(self.allowed_merchants),
            "allowed_payment_routes": sorted(self.allowed_payment_routes),
            "anc_required": self.anc_required,
            "cap_per_transaction_cents": self.cap_per_transaction_cents,
            "currency": self.currency,
            "escalate_above_cents": self.escalate_above_cents,
            "max_quantity_total": self.max_quantity_total,
            "required_connection": self.required_connection,
            "required_device": self.required_device,
            "rolling_cap_cents": self.rolling_cap_cents,
            "rolling_window_seconds": self.rolling_window_seconds,
            "velocity_max_count": self.velocity_max_count,
            "velocity_window_seconds": self.velocity_window_seconds,
        }


class ApprovalGrant(BaseModel):
    """A one-off, single-use approval for a specific escalated proposal.

    Bound to the exact quote hash, route and amount the user approved. If any of
    them changes, the grant is void and the purchase must be escalated again.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    grant_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)

    principal_id: str = Field(min_length=1)
    mandate_id: str = Field(min_length=1)
    mandate_version: StrictInt = Field(ge=1)

    quote_id: str = Field(min_length=1)
    quote_hash: str = Field(min_length=1)
    payment_route_id: str = Field(min_length=1)
    cash_total_cents: StrictInt = Field(ge=0)
    currency: Literal["HKD"]

    issued_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None

    def is_usable(self, now: datetime) -> bool:
        return self.consumed_at is None and now < self.expires_at

    def covers(self, *, quote_hash_: str, payment_route_id: str, cash_total_cents: int) -> bool:
        return (
            self.quote_hash == quote_hash_
            and self.payment_route_id == payment_route_id
            and self.cash_total_cents == cash_total_cents
        )


__all__ = [
    "ApprovalGrant",
    "HASH_PREFIX",
    "Mandate",
    "MandateDraft",
    "MandateStatus",
    "canonical_bytes",
    "policy_hash",
]
