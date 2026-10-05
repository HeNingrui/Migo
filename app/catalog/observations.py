"""D-local evidence records. These are storage DTOs, not A's public contracts.

Sources are observations, never payment authority. No reward or FX calculator
is implemented here; C prices and authorises the simulated settlement.
"""
from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from urllib.parse import urlsplit

from app.db.core import connect, resolve_db_path


COMPONENT = "catalog_observations"
VERSION = 1
FIELDS = {"observation_id", "kind", "subject_id", "source_type", "source_url",
          "observed_on", "retrieved_at", "evidence_status", "source_note", "payload"}
PRODUCT_FIELDS = {"platform", "title", "brand", "model", "variant", "listing_condition",
                  "connection", "form_factor", "anc", "battery_hours", "wearing_weight_g",
                  "use_cases", "merchant_id", "merchant_name", "shipping_origin",
                  "listed_price_minor", "listed_currency", "price_scope", "stock",
                  "seller_description", "untrusted_texts"}
BANK_FIELDS = {"card_id", "title", "settlement_currency", "effective_from", "effective_until",
               "claims", "personal_state_fields", "application_note"}
CREATE_TABLE = """CREATE TABLE IF NOT EXISTS catalog_observations (
    observation_id TEXT PRIMARY KEY NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('product_snapshot', 'bank_rule')),
    subject_id TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK(source_type IN ('OBSERVED_PUBLIC_SOURCE', 'SYNTHETIC_DEMO')),
    source_url TEXT,
    observed_on TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    evidence_status TEXT NOT NULL CHECK(evidence_status IN ('verified', 'partial', 'unverified')),
    source_note TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_type(payload_json) = 'object'),
    record_hash TEXT NOT NULL CHECK(length(record_hash) = 71 AND substr(record_hash, 1, 7) = 'sha256:')
) STRICT"""
STATEMENTS = (
    CREATE_TABLE,
    "CREATE INDEX IF NOT EXISTS idx_observations_subject ON catalog_observations(subject_id, kind, observed_on, observation_id)",
    """CREATE TRIGGER IF NOT EXISTS observations_no_update BEFORE UPDATE ON catalog_observations
       BEGIN SELECT RAISE(ABORT, 'Observations are immutable; append a new observation_id'); END""",
    """CREATE TRIGGER IF NOT EXISTS observations_no_delete BEFORE DELETE ON catalog_observations
       BEGIN SELECT RAISE(ABORT, 'Observations are immutable; retain history'); END""",
)


class ObservationError(ValueError):
    pass


class ObservationConflict(ObservationError):
    pass


def _text(value, field, *, nullable=False):
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ObservationError(f"{field}: expected nonempty text")


def _keys(value, fields, label):
    if not isinstance(value, dict) or set(value) != fields:
        raise ObservationError(f"{label}: expected exactly {sorted(fields)}")


def _integer(value, field):
    if value is not None and (type(value) is not int or value < 0):
        raise ObservationError(f"{field}: expected nonnegative integer or null")


def _url(value, *, nullable=False):
    if value is None and nullable:
        return
    _text(value, "source_url")
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise ObservationError("source_url: expected HTTP(S) URL without credentials")


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ObservationError("Record must contain finite JSON values") from exc


def _iso_date(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ObservationError("Expected YYYY-MM-DD date")
    return date.fromisoformat(value)


def _validate_observation(raw):
    """Return a detached validated JSON object; reject unexpected fields."""
    _keys(raw, FIELDS, "observation")
    for field in ("observation_id", "subject_id", "source_note"):
        _text(raw[field], field)
    if raw["kind"] not in {"product_snapshot", "bank_rule"}:
        raise ObservationError("Unknown observation kind")
    if raw["source_type"] not in {"OBSERVED_PUBLIC_SOURCE", "SYNTHETIC_DEMO"}:
        raise ObservationError("Unknown source_type")
    if raw["evidence_status"] not in {"verified", "partial", "unverified"}:
        raise ObservationError("Unknown evidence_status")
    _url(raw["source_url"], nullable=raw["source_type"] == "SYNTHETIC_DEMO")
    if raw["source_type"] == "SYNTHETIC_DEMO" and raw["evidence_status"] == "verified":
        raise ObservationError("Synthetic observations cannot be verified public evidence")
    try:
        _iso_date(raw["observed_on"])
        fetched = datetime.fromisoformat(raw["retrieved_at"].replace("Z", "+00:00"))
        if fetched.utcoffset() is None:
            raise ObservationError("retrieved_at must include timezone")
    except (ValueError, TypeError, AttributeError) as exc:
        raise ObservationError(f"Invalid observation date/time: {exc}") from exc
    p = raw["payload"]
    if raw["kind"] == "product_snapshot":
        _keys(p, PRODUCT_FIELDS, "product_snapshot.payload")
        if p["platform"] != "JD":
            raise ObservationError("This snapshot schema supports JD only")
        for field in ("title", "brand", "model", "variant", "seller_description"):
            _text(p[field], field)
        if len(p["seller_description"]) >= 100:
            raise ObservationError("seller_description must contain fewer than 100 characters")
        for field in ("merchant_id", "merchant_name", "shipping_origin"):
            _text(p[field], field, nullable=True)
        for field in ("listed_price_minor", "stock"):
            _integer(p[field], field)
        if p["listed_currency"] not in {None, "CNY", "HKD"}:
            raise ObservationError("Unsupported source currency")
        if p["listed_price_minor"] is not None and p["listed_currency"] is None:
            raise ObservationError("A price must have its original currency")
        for field, values in (("connection", {None, "wired", "wireless"}),
                              ("form_factor", {None, "in_ear", "over_ear", "open_ear"}),
                              ("listing_condition", {"new", "used", "unknown"}),
                              ("price_scope", {"displayed_public", "login_required", "unknown"})):
            if p[field] not in values:
                raise ObservationError(f"Invalid {field}")
        if p["anc"] is not None and type(p["anc"]) is not bool:
            raise ObservationError("anc must be boolean or null")
        for field in ("battery_hours", "wearing_weight_g"):
            value = p[field]
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value)
                                      or value < 0 or (field == "wearing_weight_g" and value == 0)):
                raise ObservationError(f"Invalid {field}")
        if p["connection"] == "wired" and p["battery_hours"] is not None:
            raise ObservationError("Wired product battery must be null")
        if not isinstance(p["use_cases"], list) or any(v not in {"commute", "study", "gaming", "sports", "calls", "music"} for v in p["use_cases"]):
            raise ObservationError("Invalid use_cases")
        if len(set(p["use_cases"])) != len(p["use_cases"]):
            raise ObservationError("Duplicate use_cases")
        if not isinstance(p["untrusted_texts"], list) or any(not isinstance(v, str) for v in p["untrusted_texts"]):
            raise ObservationError("untrusted_texts must be a list of strings")
    else:
        _keys(p, BANK_FIELDS, "bank_rule.payload")
        for field in ("card_id", "title", "application_note"):
            _text(p[field], field)
        if p["settlement_currency"] != "HKD" or p["card_id"] != raw["subject_id"]:
            raise ObservationError("Bank card identity/currency mismatch")
        try:
            start, end = _iso_date(p["effective_from"]), _iso_date(p["effective_until"])
            if start > end:
                raise ObservationError("Invalid bank rule effective interval")
        except (TypeError, ValueError) as exc:
            raise ObservationError(f"Invalid effective dates: {exc}") from exc
        if not isinstance(p["claims"], list) or not p["claims"]:
            raise ObservationError("Bank claims must be a nonempty list")
        keys = set()
        for claim in p["claims"]:
            _keys(claim, {"key", "value", "verification", "source_url", "source_locator"}, "claim")
            for field in ("key", "source_locator"):
                _text(claim[field], field)
            _url(claim["source_url"])
            if claim["verification"] not in {"verified", "inferred", "user_provided"}:
                raise ObservationError("Invalid claim verification")
            if claim["key"] in keys:
                raise ObservationError("Duplicate bank claim key")
            keys.add(claim["key"])
        if not isinstance(p["personal_state_fields"], list) or any(not isinstance(v, str) or not v for v in p["personal_state_fields"]):
            raise ObservationError("Invalid personal_state_fields")
    return json.loads(canonical(raw))


def validate_observation(raw):
    try:
        return _validate_observation(raw)
    except ObservationError:
        raise
    except (TypeError, ValueError, AttributeError, OverflowError) as exc:
        raise ObservationError(f"Invalid observation value: {exc}") from exc


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ObservationError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load_observations(path):
    def reject_constant(value):
        raise ObservationError(f"Invalid JSON number: {value}")
    try:
        values = json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_pairs,
                            parse_constant=reject_constant)
        if not isinstance(values, list):
            raise ObservationError("Observations must be a JSON array")
        records = [validate_observation(value) for value in values]
        if len({r["observation_id"] for r in records}) != len(records):
            raise ObservationError("Duplicate observation_id in import")
        return records
    except (OSError, ValueError) as exc:
        raise ObservationError(f"{Path(path).name}: {exc}") from exc


def validate_observation_schema(conn):
    row = conn.execute("SELECT version FROM schema_version WHERE component = ?", (COMPONENT,)).fetchone()
    if row is None or row[0] != VERSION:
        raise RuntimeError("Unsupported catalog_observations version")
    expected = (FIELDS - {"payload"}) | {"payload_json", "record_hash"}
    actual = {row[1] for row in conn.execute("PRAGMA table_info(catalog_observations)")}
    if actual != expected:
        raise RuntimeError("Observation schema mismatch")
    triggers = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='catalog_observations'")}
    if not {"observations_no_update", "observations_no_delete"} <= triggers:
        raise RuntimeError("Observation immutability triggers missing")


def create_observation_schema(conn):
    if not conn.in_transaction:
        raise RuntimeError("Schema creation requires caller-owned transaction")
    existing = conn.execute("SELECT version FROM schema_version WHERE component = ?", (COMPONENT,)).fetchone()
    if existing is not None:
        validate_observation_schema(conn)
        return
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='catalog_observations'").fetchone():
        raise RuntimeError("Unversioned observation table; no automatic overwrite")
    for statement in STATEMENTS:
        conn.execute(statement)
    conn.execute("INSERT INTO schema_version VALUES (?, ?, ?)",
                 (COMPONENT, VERSION, datetime.now(timezone.utc).isoformat()))
    validate_observation_schema(conn)


def _hash(record):
    return "sha256:" + hashlib.sha256(canonical(record).encode("utf-8")).hexdigest()


def _decode(row, names=None):
    value = dict(row) if hasattr(row, "keys") else dict(zip(names, row))
    digest = value.pop("record_hash")
    value["payload"] = json.loads(value.pop("payload_json"))
    record = validate_observation(value)
    if _hash(record) != digest:
        raise ObservationError("Stored observation hash mismatch")
    return {**record, "record_hash": digest}


class ObservationRepository:
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

    def get_observation(self, observation_id, connection=None):
        _text(observation_id, "observation_id")
        with self._connection(connection) as conn:
            cursor = conn.execute("SELECT * FROM catalog_observations WHERE observation_id = ?", (observation_id,))
            row = cursor.fetchone()
            return None if row is None else _decode(row, [col[0] for col in cursor.description])

    def list_observations(self, *, kind=None, subject_id=None, connection=None):
        conditions, values = [], []
        if kind is not None:
            if kind not in {"product_snapshot", "bank_rule"}:
                raise ObservationError("Unknown kind")
            conditions.append("kind = ?")
            values.append(kind)
        if subject_id is not None:
            _text(subject_id, "subject_id")
            conditions.append("subject_id = ?")
            values.append(subject_id)
        sql = "SELECT * FROM catalog_observations"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY observed_on, retrieved_at, observation_id"
        with self._connection(connection) as conn:
            cursor = conn.execute(sql, values)
            names = [col[0] for col in cursor.description]
            return [_decode(row, names) for row in cursor]

    def append_many(self, records, connection):
        """Idempotent append in caller's transaction; conflict never overwrites.

        Validate and check the entire batch before writing. Caller must roll
        back on database errors, just as with stock/commerce operations.
        """
        if connection is None or not connection.in_transaction:
            raise RuntimeError("Evidence append requires caller-owned transaction")
        records = [validate_observation(raw) for raw in records]
        if len({r["observation_id"] for r in records}) != len(records):
            raise ObservationError("Duplicate observation_id in batch")
        pending = []
        for record in records:
            current = self.get_observation(record["observation_id"], connection)
            if current is not None:
                if current["record_hash"] != _hash(record):
                    raise ObservationConflict(f"Observation ID reused with different content: {record['observation_id']}")
            else:
                pending.append(record)
        names = sorted(FIELDS - {"payload"}) + ["payload_json", "record_hash"]
        sql = f"INSERT INTO catalog_observations ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})"
        for record in pending:
            data = {**record, "payload_json": canonical(record["payload"]), "record_hash": _hash(record)}
            connection.execute(sql, [data[name] for name in names])
        return {"observation_count": len(records), "observations_inserted": len(pending),
                "observations_unchanged": len(records) - len(pending)}


def snapshot_gaps(record, *, as_of, max_age_days=1):
    """Data completeness only. Never a payment approval or live stock check."""
    value = {k: v for k, v in record.items() if k != "record_hash"}
    value = validate_observation(value)
    if value["kind"] != "product_snapshot":
        raise ObservationError("Expected a product_snapshot")
    if type(max_age_days) is not int or max_age_days < 0:
        raise ObservationError("max_age_days must be nonnegative integer")
    current = _iso_date(as_of)
    age = (current - _iso_date(value["observed_on"])).days
    p, missing = value["payload"], []
    for field in ("listed_price_minor", "stock", "merchant_id", "shipping_origin", "connection", "form_factor"):
        if p[field] is None:
            missing.append(field)
    if p["listed_currency"] != "HKD":
        missing.append("C_authoritative_HKD_quote_and_FX_evidence")
    if p["price_scope"] != "displayed_public":
        missing.append("confirmed_price_conditions")
    if value["evidence_status"] != "verified" or value["source_type"] != "OBSERVED_PUBLIC_SOURCE":
        missing.append("verified_public_evidence")
    if age < 0 or age > max_age_days:
        missing.append("fresh_observation")
    return {"observation_id": value["observation_id"], "subject_id": value["subject_id"],
            "as_of": as_of, "has_minimum_product_data": not missing, "missing": missing,
            "requires_C_quote_and_policy_check": True, "settlement_mode": "simulation"}
