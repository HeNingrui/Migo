from fastapi import APIRouter, Request
from app.api.envelope import ok, request_id_for, product_not_found
from app.contracts.search import SearchRequest

router = APIRouter(prefix="/api/v1", tags=["search"])


@router.post("/products/search")
def search(payload: SearchRequest, request: Request):
    return ok(request.app.state.search.search(payload), request_id_for(request))


@router.post("/products/summarize")
def summarize(payload: SearchRequest, request: Request):
    return ok(request.app.state.search.summarize(payload), request_id_for(request))


@router.get("/products/{product_id}/review-analysis")
def review_analysis(product_id: str, request: Request):
    if request.app.state.catalog.get_product(product_id) is None:
        product_not_found(product_id, request)
    return ok(request.app.state.search.analyze(product_id), request_id_for(request))
