"""Synthetic review storage, separate from A's frozen Product contract.

Ratings use integer tenths in SQLite. Product averages include every rating,
including coordinated posts; evaluation answers never enter this module.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import math

from app.db.core import connect, resolve_db_path

COMPONENT = 'catalog_reviews'
VERSION = 1
FIELDS = {'review_id', 'product_id', 'reviewer_display_name', 'rating', 'title', 'text',
          'posted_at', 'verified_purchase', 'helpful_votes', 'source_type', 'data_note'}
CREATE_TABLE = """CREATE TABLE IF NOT EXISTS product_reviews (
    review_id TEXT PRIMARY KEY NOT NULL CHECK(length(trim(review_id)) > 0),
    product_id TEXT NOT NULL REFERENCES products(product_id) ON DELETE RESTRICT,
    reviewer_display_name TEXT NOT NULL CHECK(length(trim(reviewer_display_name)) > 0),
    rating_tenths INTEGER NOT NULL CHECK(typeof(rating_tenths)='integer' AND rating_tenths BETWEEN 0 AND 50),
    title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 160),
    text TEXT NOT NULL CHECK(length(trim(text)) BETWEEN 1 AND 5000),
    posted_at TEXT NOT NULL,
    verified_purchase INTEGER NOT NULL CHECK(verified_purchase IN (0,1)),
    helpful_votes INTEGER NOT NULL CHECK(typeof(helpful_votes)='integer' AND helpful_votes >= 0),
    source_type TEXT NOT NULL CHECK(source_type='demo'),
    data_note TEXT NOT NULL CHECK(length(trim(data_note)) > 0)
) STRICT"""
STATEMENTS = (CREATE_TABLE,
    "CREATE INDEX IF NOT EXISTS idx_reviews_product_time ON product_reviews(product_id, posted_at, review_id)",
    """CREATE TRIGGER IF NOT EXISTS reviews_no_update BEFORE UPDATE ON product_reviews
       BEGIN SELECT RAISE(ABORT, 'Reviews are immutable; append a new review_id'); END""",
    """CREATE TRIGGER IF NOT EXISTS reviews_no_delete BEFORE DELETE ON product_reviews
       BEGIN SELECT RAISE(ABORT, 'Reviews are immutable; retain history'); END""")


class ReviewError(ValueError):
    pass


class ReviewConflict(ReviewError):
    pass


def rating_tenths(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ReviewError('rating: expected a finite number')
    try:
        scaled = Decimal(str(value)) * 10
        if not 0 <= scaled <= 50 or scaled != scaled.to_integral_value():
            raise ReviewError('rating: expected 0.0 to 5.0, in increments of 0.1')
        return int(scaled)
    except InvalidOperation as exc:
        raise ReviewError('Invalid rating') from exc


def validate_review(raw):
    if not isinstance(raw, dict) or set(raw) != FIELDS:
        raise ReviewError(f'Review must have exactly these fields: {sorted(FIELDS)}')
    result = dict(raw)
    for field in FIELDS - {'rating', 'verified_purchase', 'helpful_votes'}:
        if not isinstance(result[field], str) or not result[field].strip() or '\x00' in result[field]:
            raise ReviewError(f'{field}: expected nonempty text')
    if len(result['title']) > 160 or len(result['text']) > 5000:
        raise ReviewError('Review title or text is too long')
    result['rating'] = rating_tenths(result['rating']) / 10
    if type(result['verified_purchase']) is not bool:
        raise ReviewError('verified_purchase: expected boolean')
    if type(result['helpful_votes']) is not int or result['helpful_votes'] < 0:
        raise ReviewError('helpful_votes: expected nonnegative integer')
    if result['source_type'] != 'demo':
        raise ReviewError('This dataset supports synthetic demo reviews only')
    try:
        posted = datetime.fromisoformat(result['posted_at'].replace('Z', '+00:00'))
        if posted.utcoffset() is None:
            raise ValueError('timezone is required')
    except (TypeError, ValueError) as exc:
        raise ReviewError('posted_at: expected timestamp with timezone') from exc
    result['posted_at'] = posted.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    return result


def load_reviews(path):
    from pathlib import Path
    from app.catalog.seed import _unique_keys, _reject_constant
    try:
        raw = json.loads(Path(path).read_text(encoding='utf-8-sig'),
                         object_pairs_hook=_unique_keys, parse_constant=_reject_constant)
        if not isinstance(raw, list):
            raise ReviewError('Review seed must be a JSON array')
        records = [validate_review(r) for r in raw]
        if len({r['review_id'] for r in records}) != len(records):
            raise ReviewError('Duplicate review_id in seed')
        return records
    except (OSError, ValueError) as exc:
        raise ReviewError(f'{path}: {exc}') from exc


def validate_review_schema(conn):
    row = conn.execute('SELECT version FROM schema_version WHERE component=?', (COMPONENT,)).fetchone()
    if row is None or row[0] != VERSION:
        raise RuntimeError('Unsupported catalog_reviews version')
    columns = {r[1] for r in conn.execute('PRAGMA table_info(product_reviews)')}
    if columns != (FIELDS - {'rating'}) | {'rating_tenths'}:
        raise RuntimeError('Review schema mismatch')
    triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='product_reviews'")}
    if not {'reviews_no_update', 'reviews_no_delete'} <= triggers:
        raise RuntimeError('Review immutability triggers missing')
    foreign_keys = list(conn.execute('PRAGMA foreign_key_list(product_reviews)'))
    if not any(r[2] == 'products' and r[3] == 'product_id' and r[4] == 'product_id' for r in foreign_keys):
        raise RuntimeError('Review product foreign key missing')


def create_review_schema(conn):
    if not conn.in_transaction:
        raise RuntimeError('Review schema requires caller-owned transaction')
    existing = conn.execute('SELECT version FROM schema_version WHERE component=?', (COMPONENT,)).fetchone()
    if existing is not None:
        validate_review_schema(conn)
        return
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='product_reviews'").fetchone():
        raise RuntimeError('Unversioned review table; no automatic overwrite')
    for statement in STATEMENTS:
        conn.execute(statement)
    conn.execute('INSERT INTO schema_version VALUES (?, ?, ?)', (COMPONENT, VERSION, datetime.now(timezone.utc).isoformat()))
    validate_review_schema(conn)


def _decode(row, names):
    result = dict(row) if hasattr(row, 'keys') else dict(zip(names, row))
    result['rating'] = result.pop('rating_tenths') / 10
    result['verified_purchase'] = bool(result['verified_purchase'])
    return validate_review(result)


class ReviewRepository:
    def __init__(self, db_path=None):
        self.db_path = resolve_db_path(db_path)

    @contextmanager
    def _connection(self, supplied):
        if supplied is not None:
            yield supplied
        else:
            owned = connect(self.db_path, readonly=True)
            try:
                yield owned
            finally:
                owned.close()

    def list_reviews(self, product_id, connection=None):
        if not isinstance(product_id, str) or not product_id.strip():
            raise ReviewError('product_id: expected nonempty text')
        with self._connection(connection) as conn:
            cursor = conn.execute('SELECT * FROM product_reviews WHERE product_id=? ORDER BY posted_at, review_id', (product_id,))
            names = [c[0] for c in cursor.description]
            return [_decode(row, names) for row in cursor]

    def get_summary(self, product_id, connection=None):
        reviews = self.list_reviews(product_id, connection)
        count = len(reviews)
        mean = (sum((Decimal(str(r['rating'])) for r in reviews), Decimal(0)) / count).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP) if count else None
        return {'product_id': product_id, 'review_count': count,
                'average_rating': float(mean) if mean is not None else None, 'rating_scale': 5.0}

    def list_summaries(self, connection=None):
        with self._connection(connection) as conn:
            rows = conn.execute('''SELECT p.product_id, p.name, count(r.review_id), sum(r.rating_tenths)
                FROM products p LEFT JOIN product_reviews r ON r.product_id=p.product_id
                GROUP BY p.product_id, p.name ORDER BY p.product_id''').fetchall()
            return [{'product_id': row[0], 'name': row[1], 'review_count': row[2],
                     'average_rating': float((Decimal(row[3])/10/row[2]).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)) if row[2] else None,
                     'rating_scale': 5.0} for row in rows]

    def get_product_reviews(self, product_id, connection=None):
        # Reuse one read transaction when owned, so list and mean use one snapshot.
        with self._connection(connection) as conn:
            owns = connection is None
            if owns:
                conn.execute('BEGIN')
            try:
                result = self.get_summary(product_id, conn)
                result.update(reviews=self.list_reviews(product_id, conn), source_type='demo',
                              data_note='All reviews are synthetic. The mean includes every submitted rating; it is not a verified quality score.')
                return result
            finally:
                if owns and conn.in_transaction:
                    conn.rollback()

    def append_many(self, records, connection):
        if connection is None or not connection.in_transaction:
            raise RuntimeError('Review import requires caller-owned transaction')
        records = [validate_review(r) for r in records]
        if len({r['review_id'] for r in records}) != len(records):
            raise ReviewError('Duplicate review_id in batch')
        pending = []
        for record in records:
            if not connection.execute('SELECT 1 FROM products WHERE product_id=?', (record['product_id'],)).fetchone():
                raise ReviewError(f"Unknown review product_id: {record['product_id']}")
            cursor = connection.execute('SELECT * FROM product_reviews WHERE review_id=?', (record['review_id'],))
            row = cursor.fetchone()
            if row is not None:
                existing = _decode(row, [c[0] for c in cursor.description])
                if existing != record:
                    raise ReviewConflict(f"Review ID reused with different content: {record['review_id']}")
            else:
                pending.append(record)
        names = sorted(FIELDS - {'rating'}) + ['rating_tenths']
        sql = f"INSERT INTO product_reviews ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})"
        for record in pending:
            stored = {**record, 'verified_purchase': int(record['verified_purchase']), 'rating_tenths': rating_tenths(record['rating'])}
            connection.execute(sql, [stored[n] for n in names])
        return {'review_seed_count': len(records), 'reviews_inserted': len(pending), 'reviews_unchanged': len(records)-len(pending)}
