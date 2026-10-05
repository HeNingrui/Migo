"""The HTTP edge: the envelope, the status mapping, and a thin pass-through.

Two things are worth testing at this layer and nothing else is: that every
response -- success, refusal, missing product, malformed body -- is the one
envelope shape the team agreed on, and that the route does not lose any of the
structured payload the orchestrator produced. A route that flattened a decision
into a sentence would pass a smoke test and break the frontend.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_REPO_ROOT = next((p for p in [HERE, *HERE.parents] if (p / "app").is_dir()), None)
if _REPO_ROOT is not None and str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.contracts.common import Envelope, ErrorCode, http_status_for  # noqa: E402
from app.main import build_app  # noqa: E402

AUTHORISATION = (
    "授权你在这段时间里替我买耳机：每笔不超过 320，24 小时内总共不超过 600，"
    "5 分钟最多 2 笔，一共买 3 件，超过 300 先问我，只用 FPS，"
    "送到 addr_demo_01，有效期 7 天"
)
SEARCH = "找 600 以内、无线、必须支持主动降噪的耳机"


@pytest.fixture
def client(tmp_path) -> TestClient:
    return TestClient(build_app(db_path=str(tmp_path / "api.sqlite3")))


def envelope_of(response) -> Envelope:
    """Parse the body into the contract type, so a shape drift fails here."""
    return Envelope.model_validate(response.json())


def chat(client, message, session_id=None) -> dict:
    payload = {"message": message}
    if session_id is not None:
        payload["session_id"] = session_id
    response = client.post("/api/v1/agent/chat", json=payload)
    assert response.status_code == 200
    body = envelope_of(response)
    assert body.ok and body.error is None
    return body.data


def click(client, intent, **handles) -> dict:
    response = client.post("/api/v1/agent/actions",
                           json={"session_id": handles.pop("session_id"),
                                 "intent": intent, **handles})
    assert response.status_code == 200
    body = envelope_of(response)
    assert body.ok and body.error is None, body.error
    return body.data


class TestTheEnvelope:
    def test_a_success_is_the_agreed_shape(self, client):
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert set(response.json()) == {"ok", "data", "error", "request_id"}
        assert response.json()["ok"] is True
        assert response.json()["error"] is None

    def test_a_missing_product_is_a_mapped_404(self, client):
        """D's handler raises; this layer turns it into the envelope."""
        response = client.get("/api/v1/products/hp_9999")
        assert response.status_code == 404
        body = envelope_of(response)
        assert body.ok is False
        assert body.data is None
        assert body.error.code is ErrorCode.PRODUCT_NOT_FOUND
        assert isinstance(body.error.details, dict)

    def test_a_malformed_body_is_the_envelope_and_not_fastapis(self, client):
        """One shape for every answer, including the ones FastAPI raises itself."""
        response = client.post("/api/v1/agent/chat", json={"message": ""})
        assert response.status_code == 422
        body = envelope_of(response)
        assert body.error.code is ErrorCode.VALIDATION_ERROR
        assert isinstance(body.error.details, dict)
        assert body.error.details["problems"]

    def test_details_is_always_an_object(self, client):
        """The team contract's bluntest rule: never null."""
        for path, method, payload in (
            ("/api/v1/products/hp_9999", "get", None),
            ("/api/v1/agent/chat", "post", {"message": ""}),
            ("/api/v1/transactions/prop_nope", "get", None),
        ):
            call = getattr(client, method)
            response = call(path, json=payload) if payload else call(path)
            body = envelope_of(response)
            assert isinstance(body.error.details, dict), path
            assert body.error.details is not None

    def test_a_business_outcome_is_a_200_with_ok_false(self, client):
        """A handled refusal is not a transport error.

        Answering 400 to "the mandate was revoked" would tell the client it sent
        something malformed, when the system understood it perfectly and said no.
        """
        assert http_status_for(ErrorCode.MANDATE_REVOKED) == 200
        assert http_status_for(ErrorCode.CAP_PER_TRANSACTION_EXCEEDED) == 200
        assert http_status_for(ErrorCode.PAYMENT_STATUS_UNKNOWN) == 200

    def test_the_request_id_is_echoed_from_the_header(self, client):
        response = client.get("/api/v1/health", headers={"X-Request-ID": "req_mine"})
        assert response.json()["request_id"] == "req_mine"


class TestChat:
    def test_a_search_returns_the_structured_payload(self, client):
        data = chat(client, SEARCH)
        assert data["results"] is not None
        assert data["results"]["candidates"]
        assert data["next_action"] == "select_product"

    def test_the_response_carries_what_the_ui_needs_to_render(self, client):
        """``requires_user_action()`` is a field, not something the UI infers."""
        data = chat(client, AUTHORISATION)
        assert data["next_action"] == "confirm_mandate"
        assert data["mandate_draft"] is not None
        assert data["clarification"] is not None

    def test_a_session_is_reused_across_requests(self, client):
        first = chat(client, SEARCH)
        second = chat(client, "第二款", session_id=first["session_id"])
        assert second["session_id"] == first["session_id"]
        assert second["selected_product_id"] == first["results"]["candidates"][1]["product"]["product_id"]

    def test_an_unknown_session_is_a_named_404(self, client):
        response = client.post("/api/v1/agent/chat",
                               json={"session_id": "sess_nope", "message": "你好"})
        assert response.status_code == 404
        assert envelope_of(response).error.code is ErrorCode.SESSION_NOT_FOUND


class TestActions:
    def test_a_button_skips_the_parser(self, client):
        found = chat(client, SEARCH)
        product_id = found["results"]["candidates"][0]["product"]["product_id"]
        data = click(client, "SELECT_PRODUCT", session_id=found["session_id"],
                     product_id=product_id)
        assert data["parse_source"] is None
        assert data["trace"] == []
        assert data["quote"] is not None

    def test_a_button_can_sign_the_mandate(self, client):
        draft = chat(client, AUTHORISATION)
        data = click(client, "ACTIVATE_MANDATE", session_id=draft["session_id"])
        assert data["mandate"]["status"] == "ACTIVE"
        assert data["mandate"]["policy_hash"].startswith("sha256:")

    def test_an_action_with_a_missing_handle_is_refused_by_the_contract(self, client):
        found = chat(client, SEARCH)
        response = client.post("/api/v1/agent/actions",
                               json={"session_id": found["session_id"],
                                     "intent": "SELECT_PRODUCT"})
        assert response.status_code == 422
        assert envelope_of(response).error.code is ErrorCode.VALIDATION_ERROR


class TestHealth:
    def test_it_reports_the_parts_rather_than_a_single_ok(self, client):
        data = client.get("/api/v1/health").json()["data"]
        assert data["database"]["db_ready"] is True
        assert data["database"]["product_count"] == 40
        assert data["commerce"]["adapter"] == "sandbox"
        assert data["commerce"]["settlement_source_type"] == "SANDBOX", (
            "a sandbox settlement has to be labelled wherever it is reported"
        )
        assert data["commerce"]["audit_chain"]["ok"] is True
        assert "integrated B" in data["search"]


class TestTheProductRoute:
    def test_it_returns_a_complete_product(self, client):
        data = client.get("/api/v1/products/hp_0018").json()["data"]
        assert data["product_id"] == "hp_0018"
        assert data["currency"] == "HKD"
        assert data["price_cents"] == 27900

    def test_unknown_values_stay_null(self, client):
        """A spec nobody measured is not zero."""
        data = client.get("/api/v1/products/hp_0018").json()["data"]
        assert data["anc"] is True
        assert "battery_hours" in data


class TestTheRecordsRoutes:
    def test_a_transaction_view_carries_the_outcome_and_the_audit(self, client):
        draft = chat(client, AUTHORISATION)
        click(client, "ACTIVATE_MANDATE", session_id=draft["session_id"])
        found = chat(client, SEARCH, session_id=draft["session_id"])
        chat(client, "第一款", session_id=draft["session_id"])
        bought = chat(client, "买吧", session_id=draft["session_id"])
        proposal_id = bought["receipt"] and bought["decision"]["proposal_id"]

        data = client.get(f"/api/v1/transactions/{proposal_id}").json()["data"]
        assert data["outcome"]["receipt"] is not None
        assert data["outcome"]["settlement_source_type"] == "SANDBOX"
        assert [e["event_type"] for e in data["audit_events"]][:2] == [
            "PROPOSAL_CREATED", "POLICY_EVALUATED",
        ]
        assert data["reconciliation"]["clean"] is True
        assert found["session_id"] == draft["session_id"]

    def test_an_unknown_proposal_is_a_named_404(self, client):
        response = client.get("/api/v1/transactions/prop_nope")
        assert response.status_code == 404
        assert envelope_of(response).error.code is ErrorCode.PROPOSAL_NOT_FOUND

    def test_the_chain_verifies_and_names_its_head(self, client):
        draft = chat(client, AUTHORISATION)
        signed = click(client, "ACTIVATE_MANDATE", session_id=draft["session_id"])
        mandate_id = signed["mandate"]["mandate_id"]

        listed = client.get(f"/api/v1/audit/{mandate_id}").json()["data"]
        assert listed["count"] >= 2

        verified = client.get(f"/api/v1/audit/{mandate_id}/verify").json()["data"]
        assert verified["ok"] is True
        assert verified["breaks"] == []
        assert verified["head_hash"] == listed["events"][-1]["event_hash"]


class TestTheDemoPageMatchesTheContract:
    """Validate actual fields consumed by the integrated UI, independent of names."""

    @staticmethod
    def page():
        from app.main import DEMO_PAGE
        return DEMO_PAGE.read_text(encoding="utf-8")

    def test_response_fields_and_nested_quote_receipt_fields_exist(self):
        import re
        from app.contracts.agent import AgentResponse
        from app.contracts.commerce import Quote
        from app.contracts.policy import PaymentReceipt
        page = self.page()
        present = page.split("function present(d)", 1)[1].split("function draftForQuote", 1)[0]
        assert set(re.findall(r"\bd\.([a-z_]+)", present)) <= set(AgentResponse.model_fields)
        for prefix, model in (("d.quote", Quote), ("d.receipt", PaymentReceipt)):
            fields = set(re.findall(re.escape(prefix) + r"\.([a-z_]+)", present))
            assert fields
            assert fields <= set(model.model_fields)

    def test_controls_send_declared_intents_and_stable_handles(self):
        import re
        from app.contracts.agent import Intent
        page = self.page()
        sent = set(re.findall(r"(?:act\(|intent:)\s*'([A-Z_]+)'", page))
        assert {"SELECT_PRODUCT", "ACTIVATE_MANDATE", "RUN_DELEGATED_PURCHASE"} <= sent
        assert sent <= {i.value for i in Intent}
        assert "product_id:state.lastSelected" in page
        assert "proposal_id:d.escalation.proposal_id" in page
        assert "product.id" not in page

    def test_authoritative_amounts_are_displayed_and_never_recomputed(self):
        page = self.page()
        assert "money(d.receipt.cash_total_cents)" in page
        assert "money(d.quote.merchant_total_cents)" in page
        assert "(c/100).toFixed(2)" in page
        for forbidden in ("reserve(", "policy_hash =", "d.decision.outcome=", "innerHTML"):
            assert forbidden not in page
