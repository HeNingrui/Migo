from fastapi import APIRouter, Request
from app.api.envelope import ok, request_id_for
from app.commerce.rewards import RewardRequest, estimate_rewards

router = APIRouter(prefix="/api/v1/rewards", tags=["reward-estimates"])


@router.post("/estimate")
def estimate(payload: RewardRequest, request: Request):
    return ok(estimate_rewards(payload), request_id_for(request))
