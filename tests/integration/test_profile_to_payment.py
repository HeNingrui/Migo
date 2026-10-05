"""The chain the round exists to prove, end to end.

    hard constraints -> profile -> soft preferences -> A's request to B
        -> B's answer -> A explains -> the user accepts
        -> C authorises -> C's final check -> payment

Three things are asserted here that no unit test can assert on its own:

* the whole path runs, with A carrying a profile it derived rather than stated;
* a **wrong recommendation** cannot become a payment -- not because A notices,
  but because C re-reads the trusted product and refuses;
* an **old confirmation cannot cross a mandate version**, because the amount the
  reservation holds is re-priced and re-checked against the current mandate.

B is not implemented and is not implemented here either: the "wrong
recommendation" is a stub ``SearchClient`` standing in for a B that returned
something ineligible, which is exactly what a test double is for.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_REPO_ROOT = next((p for p in [HERE, *HERE.parents] if (p / "app").is_dir()), None)
if _REPO_ROOT is not None and str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.agent.local_search import LocalSearchClient  # noqa: E402
from app.agent.orchestrator import AgentOrchestrator  # noqa: E402
from app.agent.session import SessionStore  # noqa: E402
from app.commerce.database import read_connection  # noqa: E402
from app.commerce.repositories import WalletRepository  # noqa: E402
from app.commerce.schema import (  # noqa: E402
    create_commerce_schema,
    initialize_wallets,
)
from app.commerce.service import CommerceService  # noqa: E402
from app.contracts.agent import ChatRequest  # noqa: E402
from app.contracts.common import ErrorCode  # noqa: E402
from app.contracts.search import SearchRequest, SearchResponse  # noqa: E402
from app.db import initialize_database  # noqa: E402

AUTHORISATION = (
    "授权你替我买耳机：每笔不超过 320，24 小时内总共不超过 600，"
    "5 分钟最多 2 笔，一共买 3 件，超过 300 先问我，只用 FPS，"
    "送到 addr_demo_01，有效期 7 天"
)
SELF_DESCRIPTION = "我主要在通勤时用，平时会连接手机和电脑，比较喜欢轻一点的耳机，比较重视降噪"
SEARCH = "找 350 以内、无线、必须支持主动降噪的耳机"


def balance(db_path) -> int:
    with read_connection(db_path) as conn:
        return WalletRepository().balance(conn, "demo_user")


#: The frozen demo clock the commerce fixtures use, so a quote is not "issued in
#: the future" relative to the moment the proposal is decided.
from tests.commerce.conftest import NOW as _NOW  # noqa: E402
from tests.commerce.conftest import a_draft as _a_draft  # noqa: E402
from tests.commerce.conftest import a_proposal as _a_proposal  # noqa: E402


@pytest.fixture
def db_path(tmp_path) -> Path:
    path = tmp_path / "integration.sqlite3"
    initialize_database(path, commerce_schema=create_commerce_schema,
                        wallet_initializer=initialize_wallets)
    return path


def build(db_path, *, search=None) -> AgentOrchestrator:
    return AgentOrchestrator(
        sessions=SessionStore(),
        search=search or LocalSearchClient(),
        commerce=CommerceService(db_path=db_path),
    )


class Conversation:
    """One conversation, so each test reads as the turns a user took."""

    def __init__(self, orchestrator: AgentOrchestrator) -> None:
        self.orchestrator = orchestrator
        self.session_id: str | None = None
        self.responses = []

    def say(self, *turns: str):
        last = None
        for text in turns:
            last = self.orchestrator.handle(
                ChatRequest(session_id=self.session_id, message=text))
            self.session_id = last.session_id
            self.responses.append(last)
        return last

    def authorise(self):
        return self.say(AUTHORISATION, "确认授权")

    def choose(self, product_id: str):
        return self.orchestrator.handle_action(self._action("SELECT_PRODUCT",
                                                            product_id=product_id))

    def buy(self):
        return self.orchestrator.handle_action(self._action("RUN_DELEGATED_PURCHASE"))

    def _action(self, intent: str, **handles):
        from app.contracts.agent import AgentActionRequest

        return AgentActionRequest(session_id=self.session_id, intent=intent, **handles)


class TestTheWholeChainRuns:
    def test_profile_then_search_then_buy_then_pay(self, db_path):
        """The prescribed path, in order, with the profile derived on the way."""
        chat = Conversation(build(db_path))

        # 1. A cold start shows something and asks -- proof there is a shelf.
        cold = chat.say("我想买个耳机")
        assert cold.results is not None and cold.results.candidates

        # 2. The user describes themselves. Nothing is filtered; the ordering moves.
        described = chat.say(SELF_DESCRIPTION)
        assert described.profile is not None
        fields = {f.field for f in described.profile.facts}
        assert {"use_case", "device"} <= fields
        assert described.results is not None
        assert described.results.applied_criteria, (
            "a stated preference has to reach B as a criterion, or it changed nothing"
        )

        # 3. A hard requirement, then the two consent gates. The cheapest eligible
        #    product is chosen deliberately: the mandate escalates above HK$300,
        #    and HK$299 + HK$10 shipping is HK$309, so "the first one" is the one
        #    that asks. The last one is under the threshold and buys outright.
        chat.authorise()
        found = chat.say(SEARCH)
        assert found.results is not None and len(found.results.candidates) >= 3
        cheapest = found.results.candidates[-1].product_id

        priced = chat.choose(cheapest)
        assert priced.quote is not None, "the price is C's, and A shows it verbatim"
        assert priced.quote.merchant_total_cents <= 30000
        paid = chat.buy()

        assert paid.decision is not None
        assert paid.decision.outcome == "APPROVE"
        assert paid.receipt is not None
        assert paid.receipt.cash_total_cents == priced.quote.merchant_total_cents
        assert balance(db_path) == 500000 - paid.receipt.cash_total_cents

    def test_the_explanation_is_built_from_cs_numbers(self, db_path):
        chat = Conversation(build(db_path))
        chat.authorise()
        found = chat.say("找 400 以内、无线、必须支持主动降噪的耳机")
        assert found.results is not None
        # The cheapest eligible one, so the mandate's escalation threshold is not
        # what this test ends up about.
        cheapest = min(found.results.candidates,
                       key=lambda c: c.product.price_cents)
        chosen = chat.choose(cheapest.product_id)
        paid = chat.buy()

        from app.agent.response_renderer import money

        assert paid.receipt is not None, paid.message
        assert money(chosen.quote.merchant_total_cents) in chosen.message
        assert money(paid.receipt.cash_total_cents) in paid.message
        assert "SANDBOX" in paid.message, (
            "a sandbox settlement has to be labelled wherever it is reported"
        )

    def test_a_rejection_reaches_a_new_requirement_rather_than_a_rerank(self, db_path):
        chat = Conversation(build(db_path))
        chat.authorise()
        found = chat.say(SEARCH)
        before = balance(db_path)
        rejected = chat.say("太重了")

        assert rejected.intent is not None
        assert rejected.intent.value == "REJECT_RECOMMENDATION"
        assert rejected.profile is not None
        assert any(f.field == "weight" for f in rejected.profile.facts)
        # The re-analysis produced a *derived bound* from what was on screen, and
        # said so -- it did not ask B to re-rank the same shelf.
        assert any("重量上限" in note for note in rejected.notes)
        # A recommendation the user rejected is not a purchase, and answering it
        # moves no money: the re-analysis stays entirely inside A.
        assert balance(db_path) == before
        assert rejected.receipt is None and rejected.reservation is None


class WrongSearchClient:
    """A test double for a B that returned a product violating a hard constraint.

    Not a second implementation of B: it delegates every filter and every figure
    to the real stand-in and then *corrupts one field* of the answer, which is the
    shape of the failure under test. It deliberately does not sort, score or
    filter anything on its own.
    """

    def __init__(self) -> None:
        self._inner = LocalSearchClient()

    def search(self, request: SearchRequest) -> SearchResponse:
        response = self._inner.search(request)
        if not response.candidates:
            return response
        # The price the recommender reports is not the price in the catalog. A
        # uses it to decide whether to proceed; C has to notice.
        wrong = response.candidates[0].product.model_copy(
            update={"price_cents": 8900, "name": "Cheapest Impostor"})
        patched = response.candidates[0].model_copy(update={"product": wrong})
        return response.model_copy(
            update={"candidates": [patched, *response.candidates[1:]]})


class TestAWrongRecommendationCannotBecomeAPayment:
    def test_c_refuses_on_its_own_read_of_the_product(self, db_path):
        """A proceeds on what B said; C refuses on what the catalog says.

        The agent is not asked to be right. It is asked to be unable to turn
        being wrong into money, which is the property this asserts.
        """
        chat = Conversation(build(db_path, search=WrongSearchClient()))
        chat.authorise()
        found = chat.say(SEARCH)
        assert found.results is not None

        # A shows the corrupted price (it has nothing else to show) ...
        shown = found.results.candidates[0].product.price_cents
        assert shown == 8900, "the double must actually corrupt the response"

        # ... and C prices the real product when asked, because A never supplies
        # an amount.
        priced = chat.choose(found.results.candidates[0].product_id)
        assert priced.quote is not None
        assert priced.quote.unit_price_cents != 8900, (
            "the amount is re-read from the catalog, not taken from the answer A saw"
        )

        paid = chat.buy()
        assert paid.decision is not None
        # The purchase is either approved at the *real* price, or refused; what
        # it can never be is settled at the number the model/recommender claimed.
        if paid.receipt is not None:
            assert paid.receipt.cash_total_cents == priced.quote.merchant_total_cents
        else:
            assert paid.decision.outcome in ("DENY", "ESCALATE")

    def test_a_recommendation_that_violates_the_mandate_is_denied_by_c(self, db_path):
        """The hard-constraint mismatch C must catch with nobody's help.

        ``hp_0016`` has ``anc = null`` -- honestly recorded as unknown, which is
        the trap this test is built around. The mandate requires active noise
        cancelling, the user accepts the recommendation, and C refuses at the
        quote rather than assuming an unknown specification satisfies it.
        """
        commerce = CommerceService(db_path=db_path)
        mandate = commerce.activate_mandate(
            _a_draft(), principal_id="demo_user", agent_id="demo_agent", now=_NOW)
        quote = commerce.create_quote(product_id="hp_0016", now=_NOW)
        assert quote.anc is None, "the fixture is only meaningful while anc is unknown"

        result = commerce.submit_proposal(
            _a_proposal(mandate, quote, proposal_id="prop_anc"), now=_NOW)
        assert result.outcome == "DENY"
        assert ErrorCode.ANC_REQUIREMENT_NOT_MET in {v.code for v in result.violations}
        assert result.reservation_created is False
        assert result.payment_adapter_called is False
        assert balance(db_path) == 500000, "a denied recommendation moves no money"


class MandateSearchClient:
    """A conforming ``SearchClient`` used to put one named product in front of A.

    It performs no ranking and no filtering of its own -- it answers with a single
    candidate, which is what a test needs in order to prove that *C*, rather than
    A, is what stops an ineligible product. Whether that candidate is eligible is
    exactly what the test is about, so this deliberately does not check it.
    """

    def __init__(self, product_id: str) -> None:
        self._inner = LocalSearchClient()
        self._product_id = product_id

    def search(self, request: SearchRequest) -> SearchResponse:
        from app.agent.clients import SearchClient

        assert isinstance(self, SearchClient), "the double must satisfy the Protocol"
        from app.catalog import ProductRepository

        product = ProductRepository(product_model=type(
            self._inner.search(SearchRequest()).candidates[0].product)).get_product(
                self._product_id)
        assert product is not None, f"{self._product_id} is not in the catalog"
        from app.contracts.search import Candidate

        return SearchResponse(
            total_matches=1, returned=1, applied_constraints=request.constraints,
            candidates=[Candidate(product=product, match_score=0)], currency="HKD",
        )


class TestAnOldConfirmationCannotCrossAMandateVersion:
    def test_a_purchase_approved_under_v1_is_not_payable_once_v2_exists(self, db_path):
        """The version the reservation recorded is checked before anything moves.

        Driven at C's own boundary, because that is where the property lives. The
        purchase is approved and settled under V1; the user then replaces the
        authorisation, which in this system is a *revocation* producing version 2.
        The receipt that already exists stands -- an authorisation change does not
        rewrite history -- and nothing further can settle against the old version.
        """
        from datetime import timedelta

        from app.commerce.payment import PaymentService
        from tests.commerce.conftest import HP_APPROVE, NOW, a_draft, a_proposal

        commerce = CommerceService(db_path=db_path)
        v1 = commerce.activate_mandate(a_draft(), principal_id="demo_user",
                                       agent_id="demo_agent", now=NOW)
        quote = commerce.create_quote(product_id=HP_APPROVE, now=NOW)
        approved = commerce.submit_proposal(
            a_proposal(v1, quote, proposal_id="prop_v1"), now=NOW)
        assert approved.outcome == "APPROVE"
        settled = commerce.get_proposal_outcome("prop_v1")
        assert settled.receipt is not None
        spent = 500000 - balance(db_path)
        assert spent == settled.receipt.cash_total_cents

        # The authorisation is replaced. The old confirmation now names a version
        # that is no longer current.
        v2 = commerce.revoke_mandate(v1.mandate_id, now=NOW)
        assert v2.version == v1.version + 1
        assert v2.status == "REVOKED"

        # A fresh confirmation is refused outright: the evaluator reads the
        # current mandate, and a stale version is not a purchase.
        another = commerce.create_quote(product_id=HP_APPROVE, now=NOW)
        refused = commerce.submit_proposal(
            a_proposal(v1, another, proposal_id="prop_v1_stale", attempt="2"),
            now=NOW)
        assert refused.outcome == "DENY"
        assert refused.primary_reason in (
            ErrorCode.MANDATE_REVOKED, ErrorCode.MANDATE_VERSION_STALE,
        )
        assert refused.reservation_created is False
        assert refused.payment_adapter_called is False
        assert balance(db_path) == 500000 - spent, (
            "the settled purchase stands and the stale one moves nothing"
        )

        # And the payment path re-checks the same thing: naming the settled
        # reservation again cannot produce a second settlement, and a reservation
        # that somehow reached payment under the old version would be refused
        # rather than paid.
        payment = PaymentService(db_path=db_path)
        assert settled.reservation is not None
        try:
            again = payment.pay_reservation(settled.reservation.reservation_id,
                                            now=NOW + timedelta(seconds=1))
        except Exception as exc:  # noqa: BLE001 - a refusal is the point
            assert getattr(exc, "code", None) is ErrorCode.RESERVATION_NOT_ACTIVE
        else:
            assert again.receipt is None
            assert again.failure is not None
        assert balance(db_path) == 500000 - spent


__all__ = [
    "Conversation",
    "MandateSearchClient",
    "TestAWrongRecommendationCannotBecomeAPayment",
    "TestAnOldConfirmationCannotCrossAMandateVersion",
    "TestTheWholeChainRuns",
    "WrongSearchClient",
]
