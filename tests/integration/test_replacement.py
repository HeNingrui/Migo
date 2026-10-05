"""The replacement test: swap the implementations, change nothing else.

The plan's S2 is that B and C can be replaced by changing an adapter and a line
in the composition root -- not by rewriting the orchestrator, the contracts or
the frontend. That claim is worth exactly as much as the evidence for it, so this
file performs the swap rather than describing it.

Three things are swapped, and each one is asserted to change nothing a user
would see apart from what the new implementation is *supposed* to change:

* **C**, for an object that is not :class:`~app.commerce.CommerceService` at all
  but a thin forwarder with the Protocol's own signatures -- the shape a future
  ``HttpCommerceClient`` would have, one request per call. If A needed anything
  outside the Protocol, this is where it would fail.
* **B**, for one that orders candidates differently. A must resolve "the first
  one" against what B returned, not against a ranking of its own -- so the
  selection changing is the *proof* the swap worked.
* **the wiring**, which is asserted to be one constructor argument.

The call log the forwarder keeps is the other half of the evidence: it records
exactly which part of the boundary A actually uses, which is what a future C has
to implement and nothing more.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_REPO_ROOT = next((p for p in [HERE, *HERE.parents] if (p / "app").is_dir()), None)
if _REPO_ROOT is not None and str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.agent.clients import CommerceClient, SearchClient  # noqa: E402
from app.agent.local_search import LocalSearchClient  # noqa: E402
from app.agent.orchestrator import AgentOrchestrator  # noqa: E402
from app.agent.session import SessionStore  # noqa: E402
from app.commerce.schema import (  # noqa: E402
    create_commerce_schema,
    initialize_wallets,
)
from app.commerce.service import CommerceService  # noqa: E402
from app.contracts.agent import AgentActionRequest, ChatRequest, Intent  # noqa: E402
from app.contracts.commerce import PurchaseProposal, Quote, SpendState  # noqa: E402
from app.contracts.mandate import ApprovalGrant, Mandate, MandateDraft  # noqa: E402
from app.contracts.policy import (  # noqa: E402
    EscalationRequest,
    PolicyDecision,
    ProposalOutcome,
)
from app.contracts.search import SearchRequest, SearchResponse  # noqa: E402
from app.db import initialize_database  # noqa: E402

AUTHORISATION = (
    "授权你在这段时间里替我买耳机：每笔不超过 320，24 小时内总共不超过 600，"
    "5 分钟最多 2 笔，一共买 3 件，超过 300 先问我，只用 FPS，"
    "送到 addr_demo_01，有效期 7 天"
)
SEARCH = "找 600 以内、无线、必须支持主动降噪的耳机"


class ForwardingCommerceClient:
    """C behind a different object.

    Every method is one line and the Protocol's own signature, which is what a
    client for a remote C looks like: one request, one response, no shared state.
    Holding the real service is an implementation detail of the test -- the
    orchestrator is never given the real one.
    """

    def __init__(self, inner: CommerceService) -> None:
        self._inner = inner
        self.calls: list[str] = []

    def _record(self, name: str) -> None:
        self.calls.append(name)

    # -- authorisation ------------------------------------------------------

    def activate_mandate(self, draft: MandateDraft, *, principal_id: str,
                         agent_id: str) -> Mandate:
        self._record("activate_mandate")
        return self._inner.activate_mandate(draft, principal_id=principal_id,
                                            agent_id=agent_id)

    def get_mandate(self, mandate_id: str) -> Mandate | None:
        self._record("get_mandate")
        return self._inner.get_mandate(mandate_id)

    def revoke_mandate(self, mandate_id: str) -> Mandate:
        self._record("revoke_mandate")
        return self._inner.revoke_mandate(mandate_id)

    # -- pricing ------------------------------------------------------------

    def create_quote(self, *, product_id: str, quantity: int = 1) -> Quote:
        self._record("create_quote")
        return self._inner.create_quote(product_id=product_id, quantity=quantity)

    def spend_state(self, mandate: Mandate) -> SpendState:
        self._record("spend_state")
        return self._inner.spend_state(mandate)

    # -- purchase -----------------------------------------------------------

    def submit_proposal(self, proposal: PurchaseProposal, *,
                        now: datetime | None = None) -> PolicyDecision:
        self._record("submit_proposal")
        return self._inner.submit_proposal(proposal, now=now)

    def approve_escalation(self, proposal_id: str, *,
                           principal_id: str) -> ApprovalGrant:
        self._record("approve_escalation")
        return self._inner.approve_escalation(proposal_id, principal_id=principal_id)

    def reject_escalation(self, proposal_id: str, *,
                          principal_id: str) -> EscalationRequest:
        self._record("reject_escalation")
        return self._inner.reject_escalation(proposal_id, principal_id=principal_id)

    def get_proposal_outcome(self, proposal_id: str) -> ProposalOutcome | None:
        self._record("get_proposal_outcome")
        return self._inner.get_proposal_outcome(proposal_id)

    # -- facts A cannot invent ---------------------------------------------

    def merchant_of_record(self) -> str:
        self._record("merchant_of_record")
        return self._inner.merchant_of_record()

    def payment_routes(self) -> list[str]:
        self._record("payment_routes")
        return self._inner.payment_routes()


class PriciestFirstSearchClient:
    """B, but it ranks the other way round.

    A second implementation of ``SearchClient`` that keeps the response
    internally consistent -- the candidates are reordered and every comparison
    row's cells are permuted with them, because the contract requires the row to
    align with the candidate list.

    The point is not the ranking. It is that a rank A resolves must follow *this*
    order, which is only true if A never re-sorts what it was given.
    """

    def __init__(self) -> None:
        self._inner = LocalSearchClient()
        self.calls = 0

    def search(self, request: SearchRequest) -> SearchResponse:
        self.calls += 1
        response = self._inner.search(request)
        order = sorted(
            range(len(response.candidates)),
            key=lambda index: -response.candidates[index].product.price_cents,
        )
        return response.model_copy(update={
            "candidates": [response.candidates[index] for index in order],
            "comparison": [
                row.model_copy(update={"values": [row.values[index] for index in order]})
                for row in response.comparison
            ],
        })


@pytest.fixture
def db_path(tmp_path) -> Path:
    path = tmp_path / "replacement.sqlite3"
    initialize_database(
        path, commerce_schema=create_commerce_schema,
        wallet_initializer=initialize_wallets,
    )
    return path


def build(db_path, *, commerce=None, search=None) -> AgentOrchestrator:
    """The one-line swap the plan promises.

    Nothing else in this function differs between the two implementations: the
    orchestrator is constructed the same way, with the client as an argument.
    """
    return AgentOrchestrator(
        sessions=SessionStore(),
        search=search or LocalSearchClient(),
        commerce=commerce or CommerceService(db_path=db_path),
    )


class Conversation:
    def __init__(self, orchestrator) -> None:
        self.orchestrator = orchestrator
        self.session_id: str | None = None

    def say(self, *turns):
        responses = []
        for text in turns:
            response = self.orchestrator.handle(
                ChatRequest(session_id=self.session_id, message=text))
            self.session_id = response.session_id
            responses.append(response)
        return responses


class TestTheProtocolsAreSatisfiable:
    def test_a_forwarder_is_a_commerce_client(self, db_path):
        """``isinstance`` checks presence, not signatures -- but presence is
        where a missing method would first show up."""
        assert isinstance(ForwardingCommerceClient(CommerceService(db_path=db_path)),
                          CommerceClient)

    def test_an_object_missing_a_method_is_not(self):
        class Almost:
            def get_mandate(self, mandate_id: str):
                return None

        assert not isinstance(Almost(), CommerceClient)

    def test_a_second_search_client_is_a_search_client(self):
        assert isinstance(PriciestFirstSearchClient(), SearchClient)

    def test_the_already_deleted_stand_in_is_not_missed(self, db_path):
        """``isinstance`` on the real service, for the same reason."""
        assert isinstance(CommerceService(db_path=db_path), CommerceClient)


class TestReplacingC:
    def test_the_whole_flow_runs_against_a_different_implementation(self, db_path):
        client = ForwardingCommerceClient(CommerceService(db_path=db_path))
        chat = Conversation(build(db_path, commerce=client))

        chat.say(AUTHORISATION, "确认授权", SEARCH, "第一款")
        (bought,) = chat.say("买吧")

        assert bought.decision.outcome == "APPROVE"
        assert bought.receipt is not None
        assert bought.receipt.cash_total_cents == 28900
        assert bought.phase.value == "completed"

    def test_the_orchestrator_never_reaches_past_the_boundary(self, db_path):
        """The call log *is* the boundary, measured rather than described.

        Every name below is a method A is allowed to have. If A ever reached for
        something else -- the payment service, a repository, the evaluator -- it
        would appear here, and the forwarder would not have it.
        """
        client = ForwardingCommerceClient(CommerceService(db_path=db_path))
        chat = Conversation(build(db_path, commerce=client))
        chat.say(AUTHORISATION, "确认授权", SEARCH, "第一款", "买吧")

        declared = {name for name in vars(CommerceClient) if not name.startswith("_")}
        assert set(client.calls) <= declared
        assert set(client.calls) == {
            "merchant_of_record",     # A proposes the merchant clause it cannot invent
            "payment_routes",         # and the rails
            "activate_mandate",
            "create_quote",
            "submit_proposal",
            "get_proposal_outcome",
        }

    def test_the_money_moved_without_a_call_that_moves_money(self, db_path):
        """The plan's strongest claim, measured: A has no payment call because
        there is no such method on the boundary to call.

        The only state-changing things A asked for are a submission and an
        answer to a question. Everything after the decision -- the reservation,
        the capability, the rail, the debit -- happened inside C, and the wallet
        proves it happened.
        """
        from app.commerce.database import read_connection
        from app.commerce.repositories import WalletRepository

        client = ForwardingCommerceClient(CommerceService(db_path=db_path))
        chat = Conversation(build(db_path, commerce=client))
        chat.say(AUTHORISATION, "确认授权", SEARCH, "第一款", "买吧")

        state_changing = {"activate_mandate", "revoke_mandate", "submit_proposal",
                          "approve_escalation", "reject_escalation"}
        assert [name for name in client.calls if name in state_changing] == [
            "activate_mandate", "submit_proposal",
        ]
        with read_connection(db_path) as conn:
            assert WalletRepository().balance(conn, "demo_user") == 500000 - 28900

    def test_the_response_a_user_sees_is_the_same_shape(self, db_path):
        """Replacement changes the implementation, not the interface.

        Two runs of the same conversation -- one against the real service, one
        against a forwarder -- produce responses with identical structure, the
        same outcome and the same amount. The identifiers differ, and they are
        supposed to: they are C's to generate.
        """
        direct = Conversation(build(db_path))
        direct.say(AUTHORISATION, "确认授权", SEARCH, "第一款")
        (from_service,) = direct.say("买吧")

        forwarded = Conversation(build(
            db_path, commerce=ForwardingCommerceClient(CommerceService(db_path=db_path))))
        forwarded.say(AUTHORISATION, "确认授权", SEARCH, "第一款")
        (from_forwarder,) = forwarded.say("买吧")

        assert from_service.decision.outcome == from_forwarder.decision.outcome
        assert from_service.decision.cash_total_cents == (
            from_forwarder.decision.cash_total_cents
        )
        assert from_service.receipt.merchant_total_cents == (
            from_forwarder.receipt.merchant_total_cents
        )
        assert from_service.phase == from_forwarder.phase
        assert from_service.next_action == from_forwarder.next_action
        assert set(from_service.model_dump()) == set(from_forwarder.model_dump())


class TestReplacingB:
    def test_a_rank_follows_the_order_b_returned(self, db_path):
        """The proof that A does not rank for itself.

        The stand-in lists cheapest first; the replacement lists dearest first.
        "The first one" selects a different product, and A's own code did not
        change by a character.
        """
        cheapest_first = Conversation(build(db_path))
        (cheap_results,) = cheapest_first.say(SEARCH)
        (cheap_pick,) = cheapest_first.say("第一款")

        dearest_first = Conversation(build(db_path, search=PriciestFirstSearchClient()))
        (dear_results,) = dearest_first.say(SEARCH)
        (dear_pick,) = dearest_first.say("第一款")

        assert cheap_pick.selected_product_id == (
            cheap_results.results.candidates[0].product_id
        )
        assert dear_pick.selected_product_id == (
            dear_results.results.candidates[0].product_id
        )
        assert cheap_pick.selected_product_id != dear_pick.selected_product_id
        assert dear_pick.quote.product_id == dear_pick.selected_product_id

    def test_the_replacement_is_still_contract_valid(self, db_path):
        """A second implementation is only a replacement if it obeys the contract."""
        response = PriciestFirstSearchClient().search(SearchRequest())
        assert response.returned == len(response.candidates)
        for row in response.comparison:
            assert len(row.values) == response.returned, (
                "a comparison row must align with the candidate list"
            )
        prices = [candidate.product.price_cents for candidate in response.candidates]
        assert prices == sorted(prices, reverse=True)

    def test_a_purchase_still_settles_through_a_different_b(self, db_path):
        chat = Conversation(build(db_path, search=PriciestFirstSearchClient()))
        chat.say(AUTHORISATION, "确认授权", SEARCH)
        response = chat.orchestrator.handle_action(AgentActionRequest(
            session_id=chat.session_id, intent=Intent.SELECT_PRODUCT,
            product_id="hp_0007",
        ))
        assert response.quote.merchant_total_cents == 31000
        (asked,) = chat.say("买吧")
        assert asked.decision.outcome == "ESCALATE"
        assert asked.escalation.cash_total_cents == 31000


class TestTheWiringIsTheOnlyChange:
    def test_the_orchestrator_holds_the_client_it_was_given(self, db_path):
        client = ForwardingCommerceClient(CommerceService(db_path=db_path))
        orchestrator = build(db_path, commerce=client)
        assert orchestrator._commerce is client  # noqa: SLF001 - the swap is the subject

    def test_the_health_endpoint_does_not_care_which_c_is_behind_it(self, db_path):
        """The transport is chosen in one place, so nothing above it branches."""
        from fastapi.testclient import TestClient

        from app.main import build_app

        app = build_app(db_path=str(db_path))
        assert isinstance(app.state.commerce, CommerceClient)
        body = TestClient(app).get("/api/v1/health").json()
        assert body["data"]["commerce"]["adapter"] == "sandbox"
