"""The HTTP package.

A's routes are thin: they parse a body, call the orchestrator or a read service,
and wrap the answer in the shared envelope. No domain logic lives here, and no
route may reach past the boundaries the rest of the repository keeps -- in
particular, nothing here calls the policy evaluator, computes an amount, or
reserves anything.
"""

from app.api.envelope import (
    failed,
    install_error_handlers,
    ok,
    product_not_found,
    request_id_for,
    wrap_success,
)

__all__ = [
    "failed",
    "install_error_handlers",
    "ok",
    "product_not_found",
    "request_id_for",
    "wrap_success",
]
