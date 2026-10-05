"""Real boundary tests; ordinary tests never make provider calls."""
import json
import pytest
from fastapi.testclient import TestClient
from app.main import build_app
from app.agent.llm_explainer import GroundedExplainer
from app.agent.llm_client import OpenAICompatibleClient, ProviderConfig
from app.contracts.agent import LLMRequest, LLMResponse, IntentResult, Intent, ParseSource, ConstraintPatch


class Stub:
    def __init__(self, change=None):
        self.calls = []
        self.change = change

    def complete(self, request):
        context = json.loads(request.user_text)
        self.calls.append(context)
        pid, product = next(iter(context['products'].items()))
        reply = {'summary': '我会先看硬条件，再参考具体的使用反馈。',
                 'recommendations': [{'product_id': pid, 'reason': '目录支持主动降噪，适合先比较通勤需求。',
                    'caution': '评论属于模拟体验，佩戴舒适度还不能确定。',
                    'cited_fields': ['anc', 'price_cents'],
                    'cited_review_ids': [product['reviews']['sample_reviews'][0]['review_id']]}]}
        if self.change:
            self.change(reply)
        return LLMResponse(text=json.dumps(reply, ensure_ascii=False))


def configured(tmp_path, stub):
    app = build_app(db_path=str(tmp_path/'test.sqlite3'), use_llm=False)
    app.state.orchestrator._explainer = GroundedExplainer(stub, app.state.search.review_context)
    return app


def browse(client):
    response = client.post('/api/v1/agent/chat', json={'message':'找300以内无线、必须降噪的耳机'})
    assert response.status_code == 200
    return response.json()['data']


def test_explanation_keeps_candidates_and_history_and_never_runs_on_money_actions(tmp_path):
    stub = Stub()
    app = configured(tmp_path, stub)
    with TestClient(app) as client:
        r = browse(client)
        assert r['trace'][-1]['detail'] == 'grounded_explanation' and r['trace'][-1]['ok']
        assert r['results']['candidates'] and r['constraints']['max_price_cents'] == 30000
        assert r['next_action'] == 'select_product'
        session = app.state.orchestrator._sessions.get(r['session_id'])
        assert session.turns[-1].assistant_message == r['message']
        assert 'HK$' in r['message']
        original = client.get('/api/v1/demo/overview').json()['data']
        chosen = client.post('/api/v1/agent/actions', json={'session_id':r['session_id'],
            'intent':'SELECT_PRODUCT','product_id':r['results']['candidates'][0]['product']['product_id']}).json()['data']
        assert chosen['quote'] and len(stub.calls) == 1
        assert client.get('/api/v1/demo/overview').json()['data'] == original
        payload = json.dumps(stub.calls[0])
        assert 'ground_truth' not in payload and 'is_coordinated' not in payload


@pytest.mark.parametrize('change', [
    lambda r:r['recommendations'][0].update(product_id='invented_sku'),
    lambda r:r['recommendations'][0].update(cited_review_ids=['not_in_catalog']),
    lambda r:r['recommendations'][0].update(cited_fields=['waterproof_rating']),
    lambda r:r.update(summary='这款只有99港币。'),
    lambda r:r.update(summary='我已扣款并下单。'),
    lambda r:r.update(summary='佩戴重量只有三克。'),
])
def test_bad_model_prose_falls_back_without_changing_filters_results_or_authority(tmp_path, change):
    stub = Stub(change)
    with TestClient(configured(tmp_path, stub)) as client:
        r = browse(client)
        assert r['trace'][-1]['ok'] is False
        assert r['trace'][-1]['detail'].startswith('grounded_explanation_failed')
        assert r['results']['candidates'] and r['constraints']['anc_required']
        assert r['receipt'] is None and r['mandate'] is None
        assert 'invented_sku' not in r['message'] and '99港币' not in r['message']


def test_review_question_preserves_display_order_and_filters(tmp_path):
    stub = Stub()
    with TestClient(configured(tmp_path, stub)) as client:
        r = browse(client)
        class QuestionParser:
            def parse(self, text, context):
                return IntentResult(intent=Intent.ASK_ALTERNATIVES, raw_text=text,
                                    parse_source=ParseSource.LLM, search=ConstraintPatch())
        client.app.state.orchestrator._llm = QuestionParser()
        question = client.post('/api/v1/agent/chat', json={'session_id':r['session_id'],
                                'message':'这些评论靠谱吗？'}).json()['data']
        assert question['constraints'] == r['constraints']
        assert question['results'] == r['results']
        assert question['trace'][-1]['detail'] == 'grounded_explanation'


def test_thinking_option_is_opt_in_and_public_status_has_no_secret():
    request = LLMRequest(system_prompt='Reply JSON',user_text='hello',response_schema={})
    plain = OpenAICompatibleClient(ProviderConfig('https://example.test','model','secret-test-key'))
    assert 'thinking' not in plain._payload(request)
    configured = OpenAICompatibleClient(ProviderConfig('https://example.test','model','secret-test-key',thinking_mode='disabled'))
    assert configured._payload(request)['thinking'] == {'type':'disabled'}
    assert 'secret-test-key' not in json.dumps(configured.public_status())
