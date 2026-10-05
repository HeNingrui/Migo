"""The HTTP edge: one envelope, and the only place an exception becomes a reply.

Three rules from the team contract shape this module.

**The envelope is ``{ok, data, error, request_id}``.** Every endpoint returns it,
success or failure, so a client parses one shape.

**``error.details`` is always an object**, ``{}`` when there is nothing to add
and never ``null``. :meth:`~app.contracts.common.ErrorDetail.of` enforces it, and
:func:`app.errors.detail_from` never puts a traceback in it -- the envelope is
shown to users and copied into demos.

**Internal services raise; only this edge wraps.** A repository that returns
``Product | None`` must not also be able to return an error object, or every
caller has to check both. So the handlers below are the single translation point
between an exception and a status code, and they are installed on the app rather
than repeated in each route.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from app.contracts.common import (
    Envelope,
    ErrorCode,
    ErrorDetail,
    http_status_for,
    new_request_id,
)

#: Header a client may use to correlate its own tracing with ours.
REQUEST_ID_HEADER = "X-Request-ID"


def request_id_for(request: Request | None = None) -> str:
    """The request id for this call: the client's if it sent one, else a fresh one."""
    if request is not None:
        supplied = request.headers.get(REQUEST_ID_HEADER)
        if supplied:
            return supplied
    return new_request_id()


def ok(data: Any, request_id: str, *, status_code: int = 200) -> JSONResponse:
    envelope = Envelope.success(data, request_id)
    return JSONResponse(status_code=status_code,
                        content=envelope.model_dump(mode="json"))


def failed(detail: ErrorDetail, request_id: str) -> JSONResponse:
    """A failure envelope, at the status its code maps to.

    ``http_status_for`` is what decides, and it is deliberately generous: a
    handled business outcome -- a policy denial, an insufficient balance, an
    expired mandate -- is a 200 with ``ok=false``, because the request was
    understood and answered. Only a malformed or unclassifiable request is a 4xx.
    """
    envelope = Envelope.failure(detail, request_id)
    return JSONResponse(status_code=http_status_for(detail.code),
                        content=envelope.model_dump(mode="json"))


def product_not_found(product_id: str, request: Request) -> None:
    """D's router calls this when a product is missing, and it must raise.

    ``app.catalog.routes`` documents the contract: the handler raises A's mapped
    404 rather than returning an error object, because the router's own return
    value is the success payload. Raising here keeps D's module free of any
    knowledge of the envelope.
    """
    from app.errors import AgentError

    raise AgentError(
        ErrorCode.PRODUCT_NOT_FOUND,
        f"no product {product_id!r}",
        details={"product_id": product_id},
    )


def wrap_success(data: Any, request: Request) -> dict[str, Any]:
    """D's router hands a product dict here and gets the shared envelope back."""
    return Envelope.success(data, request_id_for(request)).model_dump(mode="json")


def install_error_handlers(app) -> None:
    """Translate every escaping exception into the envelope, once.

    Three handlers, because there are three kinds of failure and they need
    different answers:

    * a malformed body is ``VALIDATION_ERROR`` with the field list in
      ``details`` -- an object, as the contract requires, not FastAPI's default
      ``{"detail": [...]}``, which is a second envelope shape a client would
      have to learn;
    * a raised :class:`~app.errors.AgentError` keeps its code and maps to its
      status through ``http_status_for``;
    * anything else is ``INTERNAL_ERROR``. Without this last one Starlette would
      answer a plain-text 500 that a client parsing JSON cannot read, and a demo
      that fails opaquely is worse than one that fails with a code.
    """
    from fastapi.exceptions import RequestValidationError

    from app.errors import AgentError, detail_from

    @app.exception_handler(RequestValidationError)
    async def _invalid_body(request: Request,
                            exc: RequestValidationError) -> JSONResponse:
        problems = [
            {"field": ".".join(str(part) for part in error.get("loc", ())) or "<body>",
             "problem": error.get("msg", "invalid")}
            for error in exc.errors()
        ]
        return failed(
            ErrorDetail.of(
                ErrorCode.VALIDATION_ERROR,
                "the request body did not match the contract",
                details={"problems": problems},
                retryable=False,
            ),
            request_id_for(request),
        )

    @app.exception_handler(AgentError)
    async def _agent_error(request: Request, exc: AgentError) -> JSONResponse:
        return failed(exc.to_detail(), request_id_for(request))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        return failed(detail_from(exc), request_id_for(request))


__all__ = [
    "REQUEST_ID_HEADER",
    "failed",
    "install_error_handlers",
    "ok",
    "product_not_found",
    "request_id_for",
    "wrap_success",
]
