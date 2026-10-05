"""Model-produced card names must reach C as configured route IDs."""
import pytest
from fastapi.testclient import TestClient

from app.agent.fallback_parser import FallbackIntentParser
from app.agent.mandate_flow import canonical_payment_routes
from app.contracts.agent import ParseSource
from app.main import build_app


@pytest.mark.parametrize('label,route', [
    ('Mastercard', 'mastercard_demo'), ('Visa', 'visa_demo'), ('FPS', 'fps_demo'),
])
def test_model_display_name_can_be_confirmed_and_settled(tmp_path, label, route):
    app = build_app(db_path=str(tmp_path/'routes.sqlite3'), use_llm=False)
    parser = FallbackIntentParser(route_ids=[route])

    class ModelWithDisplayName:
        def parse(self, text, context):
            result = parser.parse(text, context)
            return result.model_copy(update={
                'parse_source': ParseSource.LLM,
                'mandate': result.mandate.model_copy(update={'allowed_payment_routes': [label]}),
            })

    with TestClient(app) as client:
        app.state.orchestrator._llm = ModelWithDisplayName()
        draft = client.post('/api/v1/agent/chat', json={'message':
            f'授权你替我买耳机：每笔不超过399港币，24小时内总共不超过399港币，'
            f'5分钟最多1笔，一共买1件，超过399港币先问我，只用{label}，'
            '送到addr_demo_01，有效期1小时'}).json()['data']
        assert draft['mandate_draft']['allowed_payment_routes'] == [route]
        sid = draft['session_id']

        def act(intent, **handles):
            response = client.post('/api/v1/agent/actions', json={
                'session_id': sid, 'intent': intent, **handles})
            assert response.status_code == 200, response.text
            return response.json()['data']

        signed = act('ACTIVATE_MANDATE')
        assert signed['mandate']['allowed_payment_routes'] == [route]
        # Normal catalog filtering before product selection, with no model needed.
        app.state.orchestrator._llm = None
        client.post('/api/v1/agent/chat', json={'session_id': sid, 'message': '600以内耳机'})
        selected = act('SELECT_PRODUCT', product_id='hp_0018')
        assert selected['quote']['merchant_total_cents'] == 28900
        bought = act('RUN_DELEGATED_PURCHASE')
        assert bought['receipt']['payment_route_id'] == route
        assert bought['receipt']['cash_total_cents'] == 28900
        assert bought['decision']['policy_hash'] == signed['mandate']['policy_hash']


def test_unknown_route_cannot_activate_or_fall_back(tmp_path):
    app = build_app(db_path=str(tmp_path/'unknown.sqlite3'), use_llm=False)
    with TestClient(app) as client:
        draft = client.post('/api/v1/agent/chat', json={'message':
            '授权你替我买耳机：每笔不超过399港币，24小时内总共不超过399港币，'
            '5分钟最多1笔，一共买1件，超过399港币先问我，只用Mastercard，'
            '送到addr_demo_01，有效期1小时'}).json()['data']
        session = app.state.orchestrator._sessions.get(draft['session_id'])
        session.mandate_draft = session.mandate_draft.model_copy(
            update={'allowed_payment_routes': ['unknown_card']})
        before = client.get('/api/v1/demo/overview').json()['data']
        refused = client.post('/api/v1/agent/actions', json={
            'session_id': draft['session_id'], 'intent': 'ACTIVATE_MANDATE'}).json()['data']
        assert refused['mandate'] is None and refused['clarification']['blocking']
        assert refused['next_action'] == 'answer_question'
        assert 'unknown_card' in refused['message']
        assert client.get('/api/v1/demo/overview').json()['data'] == before


def test_alias_only_maps_to_configured_route_and_empty_stays_empty():
    assert canonical_payment_routes(['Mastercard'], ['visa_demo']) == ['Mastercard']
    assert canonical_payment_routes([], ['mastercard_demo']) == []
    assert canonical_payment_routes(['MASTERCARD', 'mastercard_demo'],
                                    ['mastercard_demo']) == ['mastercard_demo']
