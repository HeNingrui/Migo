"""The composition root: build the objects once, mount the routers, nothing else.

This module's whole job is wiring, and the shape of it is a deliberate statement
about where the seams are:

    SessionStore        one per process, because a session lives in the object
                        that created it and rebuilding it per request would drop
                        every conversation
    SearchClient        B. Live shared catalog, preferences and review screening.
    CommerceClient      C. ``app.commerce.CommerceService`` today; an HTTP client
                        satisfying the same Protocol is this line.
    AgentOrchestrator   A. Constructed with the two clients and never with a
                        concrete implementation of either, so replacing one does
                        not reach into the agent.

There is no business logic here, and there must not be: the plan's rule is that
``main.py`` includes routers, wires dependencies and installs error handling.
Anything that looks like a decision belongs in ``app/agent`` or
``app/commerce``, where it can be tested without an HTTP server.

Run it with::

    uvicorn app.main:app --reload

and open ``/`` for the demo page.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.search import SearchService
from app.catalog.reviews import ReviewRepository
from app.catalog.observations import ObservationRepository
from app.api import routes_search, routes_reviews, routes_observations, routes_rewards, routes_overview
from app.agent.orchestrator import AgentOrchestrator
from app.agent.session import SessionStore
from app.api import envelope
from app.api import routes_agent, routes_records
from app.catalog import ProductRepository
from app.catalog import routes as catalog_routes
from app.commerce.schema import create_commerce_schema, initialize_wallets
from app.commerce.service import CommerceService
from app.contracts.product import Product
from app.db import initialize_database

#: The demo page. One file, and it holds no authority: it renders what
#: ``AgentResponse`` carries, sends the action the control stands for, and
#: computes nothing.
DEMO_PAGE = Path(__file__).resolve().parent / "web" / "index.html"


def build_app(*, db_path: str | None = None,
              principal_id: str | None = None,
              use_llm: bool | None = None,
              llm_env_file: str | None = None) -> FastAPI:
    """Build the application. Exposed so a test can build one on a temp database."""
    resolved = db_path or os.environ.get("DEMO_DB_PATH", "var/demo.sqlite3")
    initialize_database(
        resolved,
        commerce_schema=create_commerce_schema,
        wallet_initializer=initialize_wallets,
        review_seed_path=Path(__file__).resolve().parents[1] / "data/reviews.seed.json",
        observation_seed_path=Path(__file__).resolve().parents[1] / "data/observations.2026-10-03.json",
        metadata_seed_path=Path(__file__).resolve().parents[1] / "data/product-metadata.demo.json",
    )

    commerce = CommerceService(db_path=resolved)
    search = SearchService(db_path=resolved)
    catalog = ProductRepository(resolved, product_model=Product)
    from app.agent.llm_client import read_env_file
    env_file = Path(llm_env_file) if llm_env_file else Path(__file__).resolve().parents[1] / ".env"
    if use_llm is None:
        configured = os.environ.get("LLM_ENABLED", read_env_file(env_file).get("LLM_ENABLED", "false"))
        use_llm = configured.strip().lower() in {"1", "true", "yes"}
    llm_parser = None
    llm_client = None
    explainer = None
    if use_llm:
        from app.agent.llm_client import OpenAICompatibleClient, ProviderConfig, LLMCallError
        from app.agent.llm_parser import LLMIntentParser
        from app.agent.llm_explainer import GroundedExplainer
        try:
            llm_client = OpenAICompatibleClient(ProviderConfig.from_env(env_file))
            llm_parser = LLMIntentParser(llm_client)
            explainer = GroundedExplainer(llm_client, search.review_context)
        except LLMCallError:
            pass
    orchestrator = AgentOrchestrator(
        sessions=SessionStore(),
        search=search,
        commerce=commerce,
        llm_parser=llm_parser,
        explainer=explainer,
        principal_id=principal_id or os.environ.get("DEMO_PRINCIPAL_ID", "demo_user"),
        agent_id=os.environ.get("DEMO_AGENT_ID", "demo_agent"),
        shipping_address_id=os.environ.get("DEMO_SHIPPING_ADDRESS_ID", "addr_demo_01"),
    )

    app = FastAPI(
        title="Migo Shopping Agent",
        version="2.1",
        description=(
            "A proposes, C decides, C reserves, C pays, C records, A explains. "
            "The agent has no call that can approve, reserve or pay."
        ),
    )
    app.state.commerce = commerce
    app.state.orchestrator = orchestrator
    app.state.db_path = resolved
    app.state.parser_mode = "llm_with_fallback" if llm_parser else "local_rules"
    app.state.llm_client = llm_client
    app.state.principal_id = principal_id or os.environ.get("DEMO_PRINCIPAL_ID", "demo_user")
    app.state.search = search
    app.state.catalog = catalog
    app.state.reviews = ReviewRepository(resolved)
    app.state.observations = ObservationRepository(resolved)

    envelope.install_error_handlers(app)
    app.include_router(routes_overview.router)
    app.include_router(routes_rewards.router)
    app.include_router(routes_search.router)
    app.include_router(routes_reviews.router)
    app.include_router(routes_observations.router)
    app.include_router(routes_agent.router)
    app.include_router(routes_records.router)

    # D's catalog router, mounted with the two callables its contract asks for:
    # the shared success envelope, and a handler that *raises* the mapped 404.
    app.include_router(catalog_routes.create_router(
        catalog,
        wrap_success=envelope.wrap_success,
        product_not_found=envelope.product_not_found,
    ))

    @app.get("/", include_in_schema=False)
    def demo_page() -> FileResponse:
        return FileResponse(DEMO_PAGE)

    return app


app = build_app()


__all__ = ["app", "build_app"]
