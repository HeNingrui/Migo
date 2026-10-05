"""The sandbox rail. It moves no money, and it says so.

This adapter never opens a socket, never reads a clock and never returns a
random result. Given the same :class:`PaymentRequest` it always answers the
same way, which is what makes a settlement reproducible in a test and honest in
a demo.

**It must never be presented as a real settlement.** ``name`` is
``"sandbox"``, and every settlement records it in the audit payload, so a
reviewer can tell what happened without reading configuration. The problem
statement is explicit that a fabricated rate is fabrication; a fabricated
*sandbox result* is fine only as long as nothing implies otherwise, which is
the whole reason this paragraph exists.

Three modes, in priority order, because a demo needs to be able to show the
system stopping:

* ``outcome`` -- always answer with this result. Used to force a decline or an
  unknown outcome in a test or a scripted demo.
* ``decline_over_cents`` -- decline anything above a threshold, so the happy
  path and the failure path can both be exercised without scripting each call.
* otherwise -- settle, with a reference derived from the attempt id, so the same
  attempt always reconciles to the same reference.
"""

from __future__ import annotations

from app.commerce.adapters.base import PaymentRequest, PaymentResult
from app.contracts.common import ErrorCode, SourceType

#: Reported in the audit payload beside every settlement this rail makes.
ADAPTER_NAME = "sandbox"


class SandboxPaymentAdapter:
    """A deterministic stand-in for a payment rail. Implements ``PaymentAdapter``."""

    name = ADAPTER_NAME

    #: Every settlement this rail produces is labelled as sandbox, on the
    #: receipt's outcome and in the audit payload. There is no configuration
    #: that turns that off, because there is no configuration in which this
    #: adapter moves real money.
    source_type = SourceType.SANDBOX

    def __init__(self, *, outcome: PaymentResult | None = None,
                 decline_over_cents: int | None = None) -> None:
        self._outcome = outcome
        self._decline_over_cents = decline_over_cents
        self._calls: list[PaymentRequest] = []

    @property
    def calls(self) -> list[PaymentRequest]:
        """Every request this adapter was handed, in order.

        Read-only introspection for tests: a test that asserts "the adapter was
        never called" needs to be able to check it, and the alternative -- a
        mock -- would stop exercising the real adapter's contract.
        """
        return list(self._calls)

    def charge(self, request: PaymentRequest) -> PaymentResult:
        self._calls.append(request)

        if self._outcome is not None:
            return self._outcome

        if (self._decline_over_cents is not None
                and request.amount_cents > self._decline_over_cents):
            return PaymentResult(
                status="FAILED",
                code=ErrorCode.PAYMENT_FAILED,
                message=(
                    "the sandbox rail declines anything above "
                    f"{self._decline_over_cents} cents"
                ),
            )

        return PaymentResult(
            status="SETTLED",
            provider_reference=f"sandbox_{request.attempt_id}",
        )


__all__ = ["ADAPTER_NAME", "SandboxPaymentAdapter"]
