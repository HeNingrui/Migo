"""Payment rails.

``base`` states the boundary and the three outcomes; ``sandbox`` is the only
implementation, and it moves no money. A real adapter implements the same
:class:`~app.commerce.adapters.base.PaymentAdapter` protocol and is selected in
``app/commerce/payment.py``'s constructor -- the settlement flow around it does
not change.
"""

from .base import PaymentAdapter, PaymentRequest, PaymentResult, PaymentStatus
from .sandbox import ADAPTER_NAME, SandboxPaymentAdapter

__all__ = [
    "ADAPTER_NAME",
    "PaymentAdapter",
    "PaymentRequest",
    "PaymentResult",
    "PaymentStatus",
    "SandboxPaymentAdapter",
]
