"""Optional FastAPI adapter. A supplies response/error policies and shared types."""

def create_router(repository, *, wrap_success, product_not_found):
    """A mounts this router after adopting HKD and the two new Product fields.

    wrap_success(product_dict, request) returns A's common success envelope.
    product_not_found(product_id, request) raises A's mapped 404 domain error.
    Database exceptions flow to A's global error handlers.
    """
    from fastapi import APIRouter, Request

    router = APIRouter(prefix="/api/v1/products", tags=["catalog"])

    @router.get("/{product_id}")
    def product_detail(product_id: str, request: Request):
        product = repository.get_product(product_id)
        if product is None:
            product_not_found(product_id, request)
            raise RuntimeError("product_not_found must raise the common PRODUCT_NOT_FOUND error")
        if hasattr(product, "model_dump"):
            data = product.model_dump(mode="json")
        else:
            data = product.as_dict()
        return wrap_success(data, request)

    return router
