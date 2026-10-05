"""Cross-member acceptance: one catalog, one wallet, live filters and records."""
from decimal import Decimal, ROUND_HALF_UP
import sqlite3
from fastapi.testclient import TestClient
import pytest
from app.main import build_app


@pytest.fixture
def client(tmp_path):
    with TestClient(build_app(db_path=str(tmp_path / "integration.sqlite3"), use_llm=False)) as c:
        yield c


def data(response):
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert set(envelope) == {"ok", "data", "error", "request_id"}
    assert envelope["ok"] and envelope["error"] is None
    return envelope["data"]


def search(client, constraints):
    return data(client.post("/api/v1/products/search", json={"constraints": constraints, "limit": 20}))


def action(client, sid, intent, **handles):
    return data(client.post("/api/v1/agent/actions", json={"session_id": sid, "intent": intent, **handles}))


def authorize(client, sid, cap, rail="Visa"):
    draft = data(client.post("/api/v1/agent/chat", json={"session_id": sid, "message":
        f"授权你替我买耳机：每笔不超过{cap}港币，24小时内总共不超过{cap}港币，5分钟最多1笔，"
        f"一共买1件，超过{cap}港币先问我，只用{rail}，送到addr_demo_01，有效期1小时"}))
    assert draft["mandate_draft"] is not None
    assert draft["clarification"] is not None and not draft["clarification"]["blocking"], draft["message"]
    signed = action(client, sid, "ACTIVATE_MANDATE")
    assert signed["mandate"]["status"] == "ACTIVE"


def test_color_search_to_payment_persists_and_repeat_cannot_double_pay(client):
    starting = data(client.get("/api/v1/demo/overview"))
    response = data(client.post("/api/v1/agent/chat", json={"message":"我要白色无线耳机，300以内，必须降噪"}))
    assert response["constraints"]["color"] == "white"
    candidates = response["results"]["candidates"]
    assert candidates and all(c["product"]["color"] == "white" for c in candidates)
    pid = candidates[0]["product"]["product_id"]
    stock = data(client.get(f"/api/v1/products/{pid}"))["stock"]
    sid = response["session_id"]
    priced = action(client, sid, "SELECT_PRODUCT", product_id=pid, quantity=1)
    quote = priced["quote"]
    assert quote["merchant_total_cents"] == quote["unit_price_cents"] + quote["shipping_cents"]
    authorize(client, sid, quote["merchant_total_cents"] / 100)
    paid = action(client, sid, "RUN_DELEGATED_PURCHASE")
    assert paid["receipt"] is not None, paid["message"]
    receipt = paid["receipt"]
    assert receipt["rail"] == "VISA"
    after = data(client.get("/api/v1/demo/overview"))
    assert after["balance_cents"] == starting["balance_cents"] - receipt["cash_total_cents"]
    assert data(client.get(f"/api/v1/products/{pid}"))["stock"] == stock-1
    assert after["orders"][0]["order_id"] == receipt["order_id"]
    record = data(client.get("/api/v1/transactions/" + after["orders"][0]["proposal_id"]))
    assert record["outcome"]["receipt"] == receipt
    assert record["reconciliation"]["clean"] is True
    assert action(client, sid, "RUN_DELEGATED_PURCHASE")["receipt"] is None
    assert data(client.get("/api/v1/demo/overview"))["balance_cents"] == after["balance_cents"]
    # A new application process keeps C/D data, though chat sessions are ephemeral.
    with TestClient(build_app(db_path=str(client.app.state.db_path), use_llm=False)) as restarted:
        assert data(restarted.get("/api/v1/demo/overview")) == after
        assert data(restarted.get(f"/api/v1/products/{pid}"))["stock"] == stock-1


def test_explicit_database_path_binds_search_detail_quote_and_stock(client):
    db = client.app.state.db_path
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE products SET stock=0, price_cents=12345 WHERE product_id='hp_0001'")
    assert data(client.get("/api/v1/products/hp_0001"))["price_cents"] == 12345
    assert "hp_0001" not in {c["product"]["product_id"] for c in search(client,{})["candidates"]}
    sid=data(client.post("/api/v1/agent/chat",json={"message":"帮我找耳机"}))["session_id"]
    denied=action(client,sid,"SELECT_PRODUCT",product_id="hp_0001",quantity=1)
    assert denied["quote"] is None
    assert "缺货" in denied["message"] or "stock" in denied["message"]


def test_tags_are_all_required_unknown_devices_and_delivery_fail_closed(client):
    constraints={"color":"white", "required_device":"computer", "tags":["study","music"], "max_estimated_delivery_days":5}
    candidates=search(client,constraints)["candidates"]
    assert candidates
    for c in candidates:
        p=c["product"]
        assert p["color"]=="white" and "computer" in p["supported_devices"]
        assert {"study","music"} <= set(p["tags"])
    pid=candidates[0]["product"]["product_id"]
    with sqlite3.connect(client.app.state.db_path) as conn:
        conn.execute("UPDATE products SET supported_devices=NULL, estimated_delivery_days=NULL WHERE product_id=?",(pid,))
    assert pid not in {c["product"]["product_id"] for c in search(client,constraints)["candidates"]}
    chat=data(client.post("/api/v1/agent/chat",json={"message":"帮我找耳机，白色，必须兼容电脑，必须适合学习，5天内送达"}))
    assert chat["constraints"]["required_device"]=="computer"
    assert chat["constraints"]["tags"]==["study"]
    assert chat["constraints"]["max_estimated_delivery_days"]==5


def test_reviews_display_all_ratings_and_analysis_has_no_answer_labels(client):
    summaries=data(client.get("/api/v1/review-summaries"))["products"]
    assert len(summaries)==40
    affected=0
    total=0
    for summary in summaries:
        pid=summary["product_id"]
        reviews=data(client.get(f"/api/v1/products/{pid}/reviews"))
        total+=reviews["review_count"]
        mean=sum(Decimal(str(r["rating"])) for r in reviews["reviews"])/reviews["review_count"]
        assert reviews["average_rating"]==float(mean.quantize(Decimal("0.1"),rounding=ROUND_HALF_UP))
        analysis=data(client.get(f"/api/v1/products/{pid}/review-analysis"))
        assert analysis["average_rating"]==reviews["average_rating"]
        affected+=bool(analysis["suspicious_count"])
        assert all("is_coordinated" not in r and "ground_truth" not in r for r in reviews["reviews"])
    assert total==400 and affected==10


def test_cancel_selection_never_moves_money_or_consumes_stock(client):
    sid=data(client.post("/api/v1/agent/chat",json={"message":"帮我找耳机"}))["session_id"]
    action(client,sid,"SELECT_PRODUCT",product_id="hp_0001",quantity=1)
    before=data(client.get("/api/v1/demo/overview"))
    cancelled=action(client,sid,"CANCEL_SELECTION")
    assert cancelled["quote"] is None
    assert action(client,sid,"RUN_DELEGATED_PURCHASE")["receipt"] is None
    assert data(client.get("/api/v1/demo/overview"))==before


def test_conditional_card_estimates_never_credit_wallet_or_reduce_quote(client):
    before=data(client.get("/api/v1/demo/overview"))
    result=data(client.post("/api/v1/rewards/estimate",json={"amount_cents":100000}))
    assert all(c["estimated_reward_cents"] is None for c in result["cards"])
    assert not result["credited_to_wallet"] and not result["reduces_current_payment"]
    assert result["cash_payable_cents"]==100000
    assert data(client.get("/api/v1/demo/overview"))==before
