"""The payment capability: C's authority to move money, bound to one purchase.

A capability answers one question and no other: *may this exact transaction
proceed?* It is not a mandate, it is not a quote, and it is not a bearer token
for an account -- it is authority over one amount, on one route, against one
reservation, until one deadline, and it is spent once.

Four properties, each of them a decision rather than a default.

**C issues it, and only C.** The plan moved issuance from A to C deliberately: a
token A can sign is a token A can forge, and the whole boundary rests on A being
unable to authorise anything. There is no path to a token from ``app/agent``.

**It is bound to the transaction, not to the user.** The signed payload carries
the reservation, the proposal, the quote hash, the route and the cash total, and
the MAC covers that whole tuple. Presenting a valid capability for a different
amount is therefore not a forgery that some later comparison has to catch -- it
does not verify at all. That is why the signature check and the context check
are the same check, and why binding the mandate separately would add nothing:
the reservation it names already belongs to exactly one mandate version.

**The token is never stored and never logged.** It is a bearer credential; a
copy at rest is a copy that leaks, and the audit chain is meant to be shown to
people. ``capability_nonces`` holds the nonce and a digest of the signed facts,
which is enough to detect a replay and a substitution without keeping anything
worth stealing.

**It is spent by a conditional UPDATE, once.** The evaluator can only read a
grant; the same is true here, so single use cannot live in a pure function. It
lives in the nonce table: a nullable ``consumed_at``, and a consume that matches
at most one unconsumed row.

**SPEC GAP.** There is no shared ``Capability`` contract in ``app/contracts``.
``ErrorCode`` names five ``CAPABILITY_*`` failures and ``AuditEventType`` names
issued / consumed / rejected, but no model says what a capability *is* on the
wire. So this type is internal to C and its representation is explicitly
provisional: nothing outside ``app/commerce`` may depend on the token format,
and A is never shown one. If a shared contract is later defined, this module is
the seam and the encoding is the only thing that changes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.commerce.config import CAPABILITY_TTL_SECONDS, capability_secret
from app.commerce.policy_evaluator import as_utc
from app.commerce.repositories import CapabilityNonceRepository
from app.contracts.common import ErrorCode

#: Version of the token encoding. Present so a future format can be told from
#: this one rather than guessed at from the payload's shape.
TOKEN_VERSION = "c1"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class Capability:
    """Authority over one transaction. Internal to C; never returned to A.

    Deliberately a dataclass rather than a shared Pydantic model -- see the
    module docstring's SPEC GAP note. It holds no secret: the nonce is an
    identifier and the signing key stays in ``app.commerce.config``.
    """

    nonce: str
    reservation_id: str
    proposal_id: str
    quote_hash: str
    payment_route_id: str
    cash_total_cents: int
    currency: str
    issued_at: datetime
    expires_at: datetime

    def context(self) -> dict[str, Any]:
        """The transaction facts this capability speaks for, and signs.

        Sorted and serialised by one function, so the digest that is signed and
        the digest stored in ``capability_nonces`` cannot be computed from
        different orderings of the same facts.
        """
        return {
            "a": self.cash_total_cents,
            "c": self.currency,
            "e": as_utc(self.expires_at).isoformat(),
            "n": self.nonce,
            "p": self.proposal_id,
            "q": self.quote_hash,
            "r": self.reservation_id,
            "t": self.payment_route_id,
        }

    def context_hash(self) -> str:
        return _digest(self.context())

    def is_expired(self, now: datetime) -> bool:
        return as_utc(now) >= as_utc(self.expires_at)

    def matches(self, *, quote_hash: str, payment_route_id: str,
                cash_total_cents: int) -> bool:
        """Whether the priced facts still match what was authorised."""
        return (
            self.quote_hash == quote_hash
            and self.payment_route_id == payment_route_id
            and self.cash_total_cents == cash_total_cents
        )


class CapabilityService:
    """Issues, signs and spends capabilities. Owns no transaction of its own.

    Every method takes the caller's connection, because issuing a capability and
    writing the reservation that justifies it must be the same transaction: a
    capability for a reservation that was rolled back is authority to spend
    money that was never held.
    """

    def __init__(self, *, nonces: CapabilityNonceRepository | None = None) -> None:
        self._nonces = nonces or CapabilityNonceRepository()

    # -- issuance -----------------------------------------------------------

    def issue(self, conn, *, reservation_id: str, proposal_id: str, quote_hash: str,
              payment_route_id: str, cash_total_cents: int, currency: str,
              now: datetime) -> tuple[Capability, str]:
        """Bind authority to one transaction and record the nonce.

        Returns the capability and its signed token. The token is returned to
        the caller and written nowhere.
        """
        capability = Capability(
            nonce=secrets.token_urlsafe(18),
            reservation_id=reservation_id,
            proposal_id=proposal_id,
            quote_hash=quote_hash,
            payment_route_id=payment_route_id,
            cash_total_cents=cash_total_cents,
            currency=currency,
            issued_at=as_utc(now),
            expires_at=as_utc(now) + timedelta(seconds=CAPABILITY_TTL_SECONDS),
        )
        self._nonces.issue(
            conn,
            nonce=capability.nonce,
            reservation_id=reservation_id,
            proposal_id=proposal_id,
            context_hash=capability.context_hash(),
            issued_at=capability.issued_at,
            expires_at=capability.expires_at,
        )
        return capability, self.sign(capability)

    # -- the wire form ------------------------------------------------------

    def sign(self, capability: Capability) -> str:
        body = json.dumps(
            capability.context(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        mac = hmac.new(capability_secret(), body, hashlib.sha256).digest()
        return f"{TOKEN_VERSION}.{_b64(body)}.{_b64(mac)}"

    def verify(self, token: str, *, now: datetime) -> dict[str, Any]:
        """Check the signature and the deadline, and return the signed facts.

        Signature first, always: a payload C did not sign is not evidence about
        anything, including its own expiry.
        """
        from app.errors import AgentError

        parts = token.split(".")
        if len(parts) != 3 or parts[0] != TOKEN_VERSION:
            raise AgentError(
                ErrorCode.CAPABILITY_INVALID,
                "the payment capability is not in a recognised format",
                details={"version": parts[0] if parts else None},
            )
        try:
            body = _unb64(parts[1])
            presented = _unb64(parts[2])
        except Exception as exc:  # noqa: BLE001 - every decode failure is one failure
            raise AgentError(
                ErrorCode.CAPABILITY_INVALID,
                "the payment capability could not be decoded",
                details={"exception": type(exc).__name__},
            ) from exc

        expected = hmac.new(capability_secret(), body, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, presented):
            raise AgentError(
                ErrorCode.CAPABILITY_INVALID,
                "the payment capability does not carry C's signature",
            )

        payload = json.loads(body)
        if as_utc(now) >= as_utc(datetime.fromisoformat(payload["e"])):
            raise AgentError(
                ErrorCode.CAPABILITY_EXPIRED,
                "the payment capability has expired",
                details={"expires_at": payload["e"]},
            )
        return payload

    # -- consumption --------------------------------------------------------

    def spend(self, conn, *, token: str, payment_id: str, now: datetime,
              reservation_id: str, proposal_id: str, quote_hash: str,
              payment_route_id: str, cash_total_cents: int) -> Capability:
        """Validate and spend a capability, inside the caller's transaction.

        Five refusals, in the order that makes each one meaningful:

        1. the token does not verify or has expired -- ``CAPABILITY_INVALID`` /
           ``CAPABILITY_EXPIRED``;
        2. there is no nonce row for it -- ``CAPABILITY_MISSING``;
        3. the stored digest is not the digest of the presented facts, or the
           capability names a different reservation or proposal -- a
           ``CAPABILITY_CONTEXT_MISMATCH`` that a bare signature check cannot
           catch on its own;
        4. the priced facts moved -- ``CAPABILITY_CONTEXT_MISMATCH``;
        5. the nonce was already spent -- ``CAPABILITY_REPLAYED``, decided by the
           conditional UPDATE rather than by a read followed by a write, so two
           concurrent attempts presenting one token cannot both succeed.
        """
        from app.errors import AgentError

        payload = self.verify(token, now=now)
        row = self._nonces.get(conn, payload["n"])

        if row is None:
            raise AgentError(
                ErrorCode.CAPABILITY_MISSING,
                "the payment capability was never issued by this commerce service",
                details={"nonce": payload["n"]},
            )
        if row["consumed_at"] is not None:
            raise AgentError(
                ErrorCode.CAPABILITY_REPLAYED,
                "the payment capability has already been spent",
                details={"nonce": payload["n"], "consumed_at": row["consumed_at"]},
            )

        if row["context_hash"] != _digest(payload):
            raise AgentError(
                ErrorCode.CAPABILITY_CONTEXT_MISMATCH,
                "the payment capability does not match the transaction it was issued for",
                details={"nonce": payload["n"]},
            )
        if (payload["r"] != reservation_id or payload["p"] != proposal_id
                or payload["q"] != quote_hash or payload["t"] != payment_route_id
                or int(payload["a"]) != cash_total_cents):
            raise AgentError(
                ErrorCode.CAPABILITY_CONTEXT_MISMATCH,
                "the payment capability does not cover this transaction",
                details={
                    "nonce": payload["n"],
                    "authorised_reservation_id": payload["r"],
                    "presented_reservation_id": reservation_id,
                    "authorised_cash_total_cents": int(payload["a"]),
                    "presented_cash_total_cents": cash_total_cents,
                    "authorised_route_id": payload["t"],
                    "presented_route_id": payment_route_id,
                },
            )
        if not self._nonces.consume(conn, nonce=payload["n"], payment_id=payment_id,
                                    consumed_at=as_utc(now)):
            raise AgentError(
                ErrorCode.CAPABILITY_REPLAYED,
                "the payment capability was spent by a concurrent attempt",
                details={"nonce": payload["n"]},
            )

        return Capability(
            nonce=payload["n"],
            reservation_id=payload["r"],
            proposal_id=payload["p"],
            quote_hash=payload["q"],
            payment_route_id=payload["t"],
            cash_total_cents=int(payload["a"]),
            currency=payload["c"],
            issued_at=as_utc(datetime.fromisoformat(row["issued_at"])),
            expires_at=as_utc(datetime.fromisoformat(row["expires_at"])),
        )


__all__ = ["Capability", "CapabilityService", "TOKEN_VERSION"]
