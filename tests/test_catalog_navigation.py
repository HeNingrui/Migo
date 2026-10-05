"""User regressions: price direction and browsing the complete eligible catalog."""
import sqlite3
import pytest
from fastapi.testclient import TestClient
from app.main import build_app


@pytest.fixture
def client(tmp_path):
    with TestClient(build_app(db_path=str(tmp_path/'browse.sqlite3'),use_llm=False)) as c:
        yield c


def chat(client, text, sid=None):
    r=client.post('/api/v1/agent/chat',json={'session_id':sid,'message':text})
    assert r.status_code==200,r.text
    return r.json()['data']


def ids(r):
    return [c['product']['product_id'] for c in r['results']['candidates']]


def test_expensive_first_ranks_whole_catalog_and_can_change_direction(client):
    r=chat(client,'1000元以内最好的耳机，价格越高越好')
    assert r['constraints']['max_price_cents']==100000
    with sqlite3.connect(client.app.state.db_path) as db:
        expected=[p[0] for p in db.execute('SELECT product_id FROM products WHERE stock>0 AND price_cents<=100000 ORDER BY price_cents DESC LIMIT 3')]
    assert ids(r)==expected
    assert [c['product']['price_cents'] for c in r['results']['candidates']]==[59900,58900,54900]
    cheaper=chat(client,'现在价格越低越好',r['session_id'])
    assert [c['product']['price_cents'] for c in cheaper['results']['candidates']]==[8900,9900,12900]
    assert cheaper['constraints']['max_price_cents']==100000


def test_price_sort_without_budget_is_still_actionable(client):
    r=chat(client,'最贵的耳机')
    assert r['results']['candidates'][0]['product']['price_cents']==59900


def test_new_batch_preserves_filters_and_exhaustion_preserves_rank_handles(client):
    r=chat(client,'500以内，黑色无线耳机，必须降噪')
    before=ids(r)
    more=chat(client,'换一批',r['session_id'])
    assert more['constraints']==r['constraints']
    assert more['results']['total_matches']==4
    assert not set(before)&set(ids(more))
    assert len(ids(more))==1
    for c in more['results']['candidates']:
        assert c['product']['color']=='black' and c['product']['anc'] is True
    exhausted=chat(client,'还有别的吗？',r['session_id'])
    assert '已经看完' in exhausted['message'] and ids(exhausted)==ids(more)
    selected=client.post('/api/v1/agent/actions',json={'session_id':r['session_id'],'intent':'SELECT_PRODUCT','product_id':ids(more)[0]}).json()['data']
    assert selected['quote']['product_id']==ids(more)[0] and selected['receipt'] is None


def test_browsing_reaches_products_beyond_twenty_without_repeats(client):
    r=chat(client,'1000以内，价格越高越好')
    sid=r['session_id']
    seen=set(ids(r));prices=[c['product']['price_cents'] for c in r['results']['candidates']]
    total=r['results']['total_matches']
    while len(seen)<total:
        r=chat(client,'查看更多',sid)
        assert not seen&set(ids(r)) and ids(r)
        assert all(c['product']['name'] in r['message'] for c in r['results']['candidates'])
        seen.update(ids(r))
        prices.extend(c['product']['price_cents'] for c in r['results']['candidates'])
    assert len(seen)==total==36 and prices==sorted(prices,reverse=True)
    all_page=chat(client,'显示全部',sid)
    assert len(ids(all_page))==20 and all_page['results']['total_matches']==36


def test_paging_does_not_call_language_model_or_change_money(client):
    class MustNotRun:
        def parse(self,*a):raise AssertionError('navigation called LLM')
    client.app.state.orchestrator._llm=MustNotRun()
    before=client.get('/api/v1/demo/overview').json()['data']
    r=chat(client,'查看更多')
    assert len(ids(r))==6
    assert r['trace'][0]['detail']=='catalog_navigation'
    assert client.get('/api/v1/demo/overview').json()['data']==before
