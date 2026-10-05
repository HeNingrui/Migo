"""Read-only demo account and records, scoped to the configured principal."""
from fastapi import APIRouter, Request
from app.api.envelope import ok, request_id_for

router = APIRouter(prefix="/api/v1", tags=["account-records"])


@router.get("/demo/overview")
def overview(request: Request):
    return ok(request.app.state.commerce.account_overview(request.app.state.principal_id), request_id_for(request))
