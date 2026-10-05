"""Read-only synthetic reviews. No ground-truth answers or payment authority."""
from fastapi import APIRouter, Request
from app.api.envelope import ok, product_not_found, request_id_for
from app.catalog import ProductRepository

router = APIRouter(prefix='/api/v1', tags=['reviews'])


@router.get('/products/{product_id}/reviews')
def product_reviews(product_id: str, request: Request):
    if ProductRepository(request.app.state.db_path).get_product(product_id) is None:
        product_not_found(product_id, request)
    result = request.app.state.reviews.get_product_reviews(product_id)
    return ok(result, request_id_for(request))


@router.get('/review-summaries')
def review_summaries(request: Request):
    records = request.app.state.reviews.list_summaries()
    return ok({'total': len(records), 'products': records, 'source_type': 'demo'}, request_id_for(request))
