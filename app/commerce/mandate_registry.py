"""C's mandate authority: a draft becomes an enforceable, hashed authorisation.

This is the only place a :class:`~app.contracts.mandate.Mandate` comes into
existence. A produces a :class:`~app.contracts.mandate.MandateDraft` from the
user's words and obtains consent; it never produces the mandate, the version,
the ``policy_hash`` or the ``consent_event_id``. Those are C's, and the reason is
the one the contract states: the user signs *the draft's own fields*, so the
thing that is hashed has to be compiled from those fields by the party that will
enforce it, not assembled by the party that wants the purchase.

**The canonical policy is derived from the mandate, not from the draft.** The
obvious implementation builds the policy dictionary from the draft and the
``Mandate`` object beside it, and then two implementations of one shape exist.
They agree until someone adds a field to one of them, at which point the hash
commits to one rule set while a third party is shown another -- and nothing
raises. So the mandate is assembled first with its hash fields left blank (via
``model_construct``, which skips validation), ``executable_policy()`` is asked
for the rule set, and only then is the real, validated ``Mandate`` constructed
from that same object. Drift is not caught by a test here; it is not
representable.

**Revocation is a new version.** The evaluator refuses a proposal whose
``expected_mandate_version`` is not current, which is how a revocation lands on
a purchase that was already in flight. Existing rows are never rewritten: the
only field that moves is ``status``, forward, from ``ACTIVE`` to ``SUPERSEDED``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.commerce.audit import AuditLog
from app.commerce.config import DEMO_PRINCIPAL_ID
from app.commerce.database import new_id, read_connection, write_transaction
from app.commerce.repositories import MandateRepository, ReservationRepository
from app.contracts.audit import AuditEventType
from app.contracts.common import ActorType, ErrorCode, SETTLEMENT_CURRENCY
from app.contracts.mandate import (
    Mandate,
    MandateDraft,
    canonical_bytes,
    policy_hash,
)


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def build_mandate(
    draft: MandateDraft,
    *,
    mandate_id: str,
    version: int,
    principal_id: str,
    agent_id: str,
    consent_event_id: str,
    now: datetime,
) -> Mandate:
    """Compile a signed draft into an immutable, hashed authorisation. Pure.

    Every field the draft leaves null is a field the mandate cannot be built
    without: the caller validates first, and the contract refuses the rest. A
    mandate that activated with a missing expiry would authorise spending
    forever, which is worse than refusing to activate.
    """
    provisional = Mandate.model_construct(
        mandate_id=mandate_id,
        version=version,
        principal_id=principal_id,
        agent_id=agent_id,
        status="ACTIVE",
        currency=SETTLEMENT_CURRENCY,
        valid_from=now,
        expires_at=now + timedelta(seconds=draft.valid_for_seconds or 0),
        allowed_merchants=tuple(draft.allowed_merchants or ()),
        allowed_categories=tuple(draft.allowed_categories or ()),
        required_connection=draft.required_connection,
        anc_required=bool(draft.anc_required),
        required_device=draft.required_device,
        cap_per_transaction_cents=draft.cap_per_transaction_cents or 0,
        rolling_cap_cents=draft.rolling_cap_cents or 0,
        rolling_window_seconds=draft.rolling_window_seconds or 0,
        velocity_max_count=draft.velocity_max_count or 0,
        velocity_window_seconds=draft.velocity_window_seconds or 0,
        max_quantity_total=draft.max_quantity_total or 0,
        escalate_above_cents=draft.escalate_above_cents,
        allowed_payment_routes=tuple(draft.allowed_payment_routes or ()),
        shipping_address_id=draft.shipping_address_id or "",
        address_change_allowed=bool(draft.address_change_allowed),
        canonical_policy="",
        policy_hash="",
        consent_event_id=consent_event_id,
        created_at=now,
    )

    policy = provisional.executable_policy()
    return Mandate.model_validate({
        **provisional.model_dump(),
        "canonical_policy": canonical_bytes(policy).decode("utf-8"),
        "policy_hash": policy_hash(policy),
    })


class MandateRegistry:
    """Activation, lookup and revocation. Owns the transaction for each write."""

    def __init__(self, *, db_path=None, audit: AuditLog | None = None) -> None:
        self._db_path = db_path
        self._audit = audit or AuditLog()
        self._mandates = MandateRepository()

    # -- writes -------------------------------------------------------------

    def activate(
        self,
        draft: MandateDraft,
        *,
        principal_id: str = DEMO_PRINCIPAL_ID,
        agent_id: str,
        now: datetime | None = None,
    ) -> Mandate:
        """Turn a signed draft into version 1 of a mandate.

        Validation happens before the transaction opens, so a draft that cannot
        be enforced never writes an audit record claiming consent was recorded.
        """
        from app.errors import AgentError

        missing = draft.missing_required_fields()
        problems = draft.structural_problems()
        if missing or problems:
            raise AgentError(
                ErrorCode.MANDATE_VALIDATION_FAILED,
                "the mandate cannot be activated until every clause is decided",
                details={"missing_fields": missing, "structural_problems": problems},
            )

        moment = now or _utc()
        mandate_id = new_id("man")

        with write_transaction(self._db_path) as conn:
            consent = self._audit.append(
                conn,
                event_type=AuditEventType.CONSENT_RECORDED,
                actor_type=ActorType.USER,
                actor_id=principal_id,
                occurred_at=moment,
                # The mandate id is generated before the transaction opens, so
                # the consent record is attributed to the mandate it authorised.
                # Without that it would sit in the chain as an unattached
                # assertion, and the one event a reviewer most wants to find
                # would not appear in the mandate's own trail.
                mandate_id=mandate_id,
                payload={
                    "source": "mandate_activation",
                    "draft_fields": _signed_clauses(draft),
                },
            )
            mandate = build_mandate(
                draft,
                mandate_id=mandate_id,
                version=1,
                principal_id=principal_id,
                agent_id=agent_id,
                consent_event_id=consent.event_id,
                now=moment,
            )
            self._mandates.create(conn, mandate, now=moment)
            self._audit.append(
                conn,
                event_type=AuditEventType.MANDATE_ACTIVATED,
                actor_type=ActorType.AGENT,
                actor_id=agent_id,
                occurred_at=moment,
                mandate_id=mandate_id,
                payload={
                    "version": mandate.version,
                    "policy_hash": mandate.policy_hash,
                    "consent_event_id": consent.event_id,
                    "expires_at": mandate.expires_at.isoformat(),
                },
            )
        return mandate

    def revoke(self, mandate_id: str, *, now: datetime | None = None) -> Mandate:
        """Withdraw an authorisation by adding a version, never by editing one.

        The new version carries the same rule set and a different status, so
        ``policy_hash`` is unchanged -- the rules did not move, permission did.
        Any proposal still holding the old version number is refused by the
        evaluator's version check, which is the mechanism that stops an agent
        acting on a copy it read a moment ago.
        """
        from app.errors import AgentError

        moment = now or _utc()
        with write_transaction(self._db_path) as conn:
            current = self._mandates.current(conn, mandate_id)
            if current is None:
                raise AgentError(
                    ErrorCode.MANDATE_NOT_FOUND,
                    f"no mandate {mandate_id!r}",
                    details={"mandate_id": mandate_id},
                )
            if current.status == "REVOKED":
                return current  # already withdrawn; revocation is idempotent

            revoked = Mandate.model_validate({
                **current.model_dump(),
                "version": current.version + 1,
                "status": "REVOKED",
                "created_at": moment,
            })
            self._mandates.set_version_status(
                conn, mandate_id=mandate_id, version=current.version, status="SUPERSEDED"
            )
            self._mandates.add_version(conn, revoked, now=moment)
            self._mandates.advance(
                conn, mandate_id=mandate_id, version=revoked.version, now=moment
            )
            self._audit.append(
                conn,
                event_type=AuditEventType.MANDATE_REVOKED,
                actor_type=ActorType.USER,
                actor_id=revoked.principal_id,
                occurred_at=moment,
                mandate_id=mandate_id,
                payload={
                    "previous_version": current.version,
                    "version": revoked.version,
                    "policy_hash": revoked.policy_hash,
                },
            )
            self._record_missed_revocation(conn, mandate_id=mandate_id, moment=moment)
            return revoked

    # -- reads --------------------------------------------------------------

    def current(self, mandate_id: str) -> Mandate | None:
        with read_connection(self._db_path) as conn:
            return self._mandates.current(conn, mandate_id)

    def history(self, mandate_id: str) -> list[Mandate]:
        with read_connection(self._db_path) as conn:
            return self._mandates.history(conn, mandate_id)

    # -- internals ----------------------------------------------------------

    def _record_missed_revocation(self, conn, *, mandate_id: str,
                                  moment: datetime) -> None:
        """Record money that a revocation arrived too late to stop.

        Withdrawing permission does not recall a settled payment, and the plan is
        explicit about what to do instead of pretending otherwise: record
        ``REVOCATION_MISSED``, never report the revocation as a refund, and never
        claim the purchase was cancelled. A revocation with nothing settled is
        an ordinary revocation and writes no such event.
        """
        settled = ReservationRepository().settled_for_mandate(conn, mandate_id)
        if not settled:
            return
        self._audit.append(
            conn,
            event_type=AuditEventType.REVOCATION_MISSED,
            actor_type=ActorType.COMMERCE,
            actor_id="mandate_registry",
            occurred_at=moment,
            mandate_id=mandate_id,
            reservation_id=settled[-1].reservation_id,
            payload={
                "settled_reservation_ids": [r.reservation_id for r in settled],
                "settled_cents": sum(r.amount_cents for r in settled),
                "note": (
                    "the mandate was revoked after these payments settled; they are "
                    "not cancelled and this is not a refund"
                ),
            },
        )


def _signed_clauses(draft: MandateDraft) -> dict[str, object]:
    """The clauses the user actually agreed to, for the consent record.

    Recorded beside the activated mandate rather than inside it: `Mandate` is the
    compiled rule set, and this is the evidence of what was on screen when the
    user said yes.
    """
    return {
        name: value
        for name, value in draft.model_dump(mode="json").items()
        if value not in (None, [], {})
    }


__all__ = ["MandateRegistry", "build_mandate"]
