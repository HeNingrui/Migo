"""Read-only D evidence routes. No catalogue promotion or payment action.

This internal D extension uses A's existing envelope and error vocabulary;
the frozen Product and agent/search/commerce contracts are unchanged.
"""
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Query, Request

from app.api.envelope import ok, request_id_for
from app.catalog.observations import snapshot_gaps
from app.contracts.common import ErrorCode
from app.errors import AgentError

router = APIRouter(prefix="/api/v1/observations", tags=["observations"])


@router.get("")
def observations(request: Request,
                 kind: Literal["product_snapshot", "bank_rule"] | None = None,
                 subject_id: str | None = Query(default=None, min_length=1)):
    records = request.app.state.observations.list_observations(kind=kind, subject_id=subject_id)
    return ok({"total": len(records), "observations": records}, request_id_for(request))


@router.get("/gaps")
def gaps(request: Request, as_of: date | None = None,
         max_age_days: int = Query(default=1, ge=0, le=365),
         subject_id: str | None = Query(default=None, min_length=1)):
    day = as_of or datetime.now(timezone(timedelta(hours=8))).date()
    records = request.app.state.observations.list_observations(kind="product_snapshot", subject_id=subject_id)
    results = [{**snapshot_gaps(record, as_of=day.isoformat(), max_age_days=max_age_days),
                "title": record["payload"]["title"]} for record in records]
    return ok({"as_of": day.isoformat(), "total": len(results), "gaps": results}, request_id_for(request))


@router.get("/{observation_id}")
def observation(observation_id: str, request: Request):
    record = request.app.state.observations.get_observation(observation_id)
    if record is None:
        raise AgentError(ErrorCode.NOT_FOUND, "Observation not found",
                         details={"observation_id": observation_id}, retryable=False)
    return ok(record, request_id_for(request))
