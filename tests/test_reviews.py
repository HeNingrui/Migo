import copy
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
import json
import sqlite3
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.catalog.reviews import ReviewConflict, ReviewError, ReviewRepository, load_reviews, validate_review
from app.db import connect, database_status, initialize_database
from app.db.core import PROJECT_ROOT
from app.main import build_app

SEED = PROJECT_ROOT / 'data/reviews.seed.json'


@pytest.fixture
def catalog(tmp_path):
    path = tmp_path / 'reviews.sqlite3'
    initialize_database(path, review_seed_path=SEED)
    return path, ReviewRepository(path)


def test_corpus_has_variable_reputations_and_separate_answers():
    records = load_reviews(SEED)
    truth = json.loads((PROJECT_ROOT / 'evaluation/reviews.ground_truth.json').read_text(encoding='utf-8'))
    assert len(records) == 400 and len({r['review_id'] for r in records}) == 400
    assert len({r['text'] for r in records}) == 400
    assert len(Counter(r['product_id'] for r in records)) == 40
    assert set(Counter(r['product_id'] for r in records).values()) == {10}
    assert len(truth['affected_products']) == 10
    assert sum(r['is_manipulated'] for r in truth['labels']) == 70
    assert Counter(r['product_id'] for r in truth['labels'] if r['is_manipulated']) == Counter({r['product_id']: 7 for r in truth['affected_products']})
    assert {r['review_id'] for r in records} == {r['review_id'] for r in truth['labels']}
    polarity = {pid: Counter(r['sentiment'] for r in truth['labels'] if r['product_id'] == pid) for pid in {r['product_id'] for r in records}}
    assert len({tuple(sorted(c.items())) for c in polarity.values()}) >= 12
    for r in records:
        assert not {'is_manipulated', 'sentiment', 'campaign_id', 'signals'}.intersection(r)
        assert all(ord(c) < 128 for c in r['text'] + r['title'])
        assert 0 <= r['rating'] <= 5 and Decimal(str(r['rating']))*10 == (Decimal(str(r['rating']))*10).to_integral_value()


def test_each_product_mean_is_all_ten_ratings_including_campaigns(catalog):
    _, repo = catalog
    exported = {r['product_id']:r for r in json.loads((PROJECT_ROOT / 'data/product-review-summaries.json').read_text(encoding='utf-8'))}
    summaries = repo.list_summaries()
    assert len(summaries) == 40
    for summary in summaries:
        records = repo.list_reviews(summary['product_id'])
        expected = float((sum(Decimal(str(r['rating'])) for r in records)/10).quantize(Decimal('.1'), rounding=ROUND_HALF_UP))
        assert summary['review_count'] == 10 and summary['average_rating'] == expected
        assert repo.get_summary(summary['product_id']) == exported[summary['product_id']]


@pytest.mark.parametrize('value', [True, '4.5', -0.1, 5.1, 4.55, float('nan'), float('inf')])
def test_invalid_individual_rating_rejected(value):
    raw = copy.deepcopy(load_reviews(SEED)[0])
    raw['rating'] = value
    with pytest.raises(ReviewError):
        validate_review(raw)


def test_half_up_average_and_empty_review_set(tmp_path):
    path = tmp_path / 'rounding.sqlite3'
    initialize_database(path)
    records = load_reviews(SEED)[:2]
    for index, r in enumerate(records):
        r['review_id'] = f'rounding_{index}'
        r['rating'] = 4.0 + index/10
    repo = ReviewRepository(path)
    conn = connect(path)
    conn.execute('BEGIN')
    repo.append_many(records, conn)
    conn.commit()
    conn.close()
    assert repo.get_summary('hp_0001')['average_rating'] == 4.1
    assert repo.get_summary('hp_0040')['average_rating'] is None


def test_caller_connection_reused_and_batch_conflict_is_atomic(catalog):
    path, repo = catalog
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('BEGIN IMMEDIATE')
    raw = load_reviews(SEED)[0]
    new = {**raw, 'review_id': 'new_review'}
    conflicting = {**raw, 'text': 'Different content using the same ID.'}
    with patch('app.catalog.reviews.connect', side_effect=AssertionError('Must reuse caller connection')):
        assert len(repo.list_reviews('hp_0001', conn)) == 10
        with pytest.raises(ReviewConflict):
            repo.append_many([new, conflicting], conn)
        assert conn.in_transaction
        assert conn.execute('SELECT count(*) FROM product_reviews').fetchone()[0] == 400
        repo.append_many([new], conn)
        assert conn.in_transaction
    conn.rollback()
    conn.close()
    assert len(repo.list_reviews(raw['product_id'])) == 10


def test_idempotent_startup_preserves_inventory_and_reviews(catalog):
    path, repo = catalog
    conn = connect(path)
    conn.execute("UPDATE products SET stock=2 WHERE product_id='hp_0001'")
    conn.close()
    before = repo.list_reviews('hp_0001')
    result = initialize_database(path, review_seed_path=SEED)
    assert result['reviews_inserted'] == 0 and result['reviews_unchanged'] == 400
    assert repo.list_reviews('hp_0001') == before
    assert database_status(path)['review_count'] == 400
    conn = connect(path)
    assert conn.execute("SELECT stock FROM products WHERE product_id='hp_0001'").fetchone()[0] == 2
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE product_reviews SET rating_tenths=50")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM product_reviews")
    conn.close()


def test_unknown_product_import_rolls_back_whole_initializer(tmp_path):
    seed = tmp_path / 'bad.json'
    raw = load_reviews(SEED)[0]
    raw['product_id'] = 'unknown'
    seed.write_text(json.dumps([raw]), encoding='utf-8')
    path = tmp_path / 'bad.sqlite3'
    with pytest.raises(ReviewError):
        initialize_database(path, review_seed_path=seed)
    conn = connect(path)
    assert conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == 0
    conn.close()


def test_catalog_and_coordinated_reset_preserve_added_review_history(catalog):
    from scripts.reset_demo import reset_database
    path, repo = catalog
    raw = {**load_reviews(SEED)[0], 'review_id': 'later_review', 'rating': 2.5}
    conn = connect(path)
    conn.execute('BEGIN')
    repo.append_many([raw], conn)
    conn.commit()
    conn.close()
    before = repo.list_reviews('hp_0001')
    initialize_database(path, reset=True, review_seed_path=SEED)
    assert repo.list_reviews('hp_0001') == before
    result = reset_database(path)
    assert repo.list_reviews('hp_0001') == before
    assert ReviewRepository(result['backup_path']).list_reviews('hp_0001') == before


def test_review_api_reuses_envelope_never_leaks_answers_or_changes_state(tmp_path):
    path = tmp_path / 'http.sqlite3'
    app = build_app(db_path=str(path))
    client = TestClient(app)
    def state():
        conn = connect(path, readonly=True)
        try:
            return tuple(conn.execute('SELECT sum(stock) FROM products').fetchone()) + tuple(conn.execute('SELECT sum(balance_cents) FROM wallets').fetchone()) + tuple(conn.execute('SELECT count(*) FROM purchase_proposals').fetchone())
        finally:
            conn.close()
    before = state()
    result = client.get('/api/v1/products/hp_0001/reviews', headers={'X-Request-ID':'req_reviews_check'})
    body = result.json()
    assert result.status_code == 200 and body['request_id'] == 'req_reviews_check'
    assert set(body) == {'ok','data','error','request_id'}
    assert body['data']['review_count'] == len(body['data']['reviews']) == 10
    assert client.get('/api/v1/review-summaries').json()['data']['total'] == 40
    serialized = result.text
    assert all(key not in serialized for key in ['is_manipulated', 'campaign_id', 'signals', 'ground_truth'])
    assert client.get('/evaluation/reviews.ground_truth.json').status_code == 404
    missing = client.get('/api/v1/products/missing/reviews')
    assert missing.status_code == 404 and missing.json()['error']['code'] == 'PRODUCT_NOT_FOUND'
    assert client.post('/api/v1/products/hp_0001/reviews', json={}).status_code == 405
    assert state() == before


def test_offline_evaluator_reports_false_positives_and_negatives():
    from scripts.evaluate_reviews import evaluate
    truth = json.loads((PROJECT_ROOT / 'evaluation/reviews.ground_truth.json').read_text(encoding='utf-8'))
    predictions = [{'review_id':r['review_id'], 'is_manipulated':r['is_manipulated']} for r in truth['labels']]
    positive = next(r for r in predictions if r['is_manipulated'])
    negative = next(r for r in predictions if not r['is_manipulated'])
    positive['is_manipulated'], negative['is_manipulated'] = False, True
    result = evaluate(predictions, truth)
    assert (result['true_positive'], result['false_positive'], result['false_negative'], result['true_negative']) == (69,1,1,329)
    assert result['f1'] == 0.9857 and result['accuracy'] == 0.995


def test_offline_evaluator_refuses_incomplete_or_duplicate_predictions():
    from scripts.evaluate_reviews import evaluate
    truth = json.loads((PROJECT_ROOT / 'evaluation/reviews.ground_truth.json').read_text(encoding='utf-8'))
    predictions = [{'review_id':r['review_id'], 'is_manipulated':False} for r in truth['labels']]
    with pytest.raises(ValueError, match='exactly all reviews'):
        evaluate(predictions[:-1], truth)
    with pytest.raises(ValueError, match='Duplicate'):
        evaluate(predictions + [predictions[0]], truth)
