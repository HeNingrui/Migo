"""C's tables, created through the hook D already reserved for them.

D owns the database; C owns these tables. The contract between the two is
``app.db.initialize.initialize_database(commerce_schema=..., wallet_initializer=...)``,
and its rules are strict: the hook receives a caller-owned write transaction and
must not commit, roll back, close the connection or call ``executescript``
(which would end the transaction). D raises
``RuntimeError("C's schema hook ended the caller-owned transaction")`` if it
does, so a mistake here fails loudly rather than silently splitting the
transaction.

**How a row is stored.** Every table keeps two kinds of column:

* *index columns* -- identity, foreign keys, status, amounts and timestamps:
  the values a query has to filter, join or order on;
* ``payload`` -- the canonical serialisation of the contract object itself.

The contract object is the record; the columns are an index into it. That is a
deliberate trade, and the reason is drift: a hand-written column list is a
second definition of ``Mandate`` or ``Quote`` that nothing keeps in step, and
the failure mode is a field that silently reads back as its default. Reads go
through ``model_validate_json``, so a row is re-validated on the way out --
including ``Mandate``'s check that ``canonical_policy`` hashes to
``policy_hash``, and ``Quote``'s check that the amounts reconcile. A tampered
row is therefore detected when it is read rather than trusted.

Rows are never deleted and never rewritten, with two stated exceptions, both
forward-only:

* ``mandate_versions.status`` moves ``ACTIVE -> SUPERSEDED`` when a later
  version exists. The policy content of a version is immutable.
* ``capability_nonces.consumed_at`` is set once, which is what makes a
  capability single-use.
* ``escalation_requests`` gains ``resolution`` / ``resolved_at``, the one
  contract object C is allowed to update.

``audit_events`` has neither: two triggers reject UPDATE and DELETE outright,
so append-only is enforced by the database rather than by discipline.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.commerce.config import DEMO_PRINCIPAL_ID, DEMO_WALLET_OPENING_BALANCE_CENTS

#: Bumped when a table below changes shape. Read back by ``commerce_status``.
COMMERCE_SCHEMA_VERSION = 1

COMPONENT = "commerce"

CREATE_WALLETS = """
CREATE TABLE IF NOT EXISTS wallets (
    principal_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(principal_id)) > 0),
    currency TEXT NOT NULL CHECK (currency = 'HKD'),
    balance_cents INTEGER NOT NULL
        CHECK (typeof(balance_cents) = 'integer' AND balance_cents >= 0),
    updated_at TEXT NOT NULL
) STRICT
"""

CREATE_MANDATES = """
CREATE TABLE IF NOT EXISTS mandates (
    mandate_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(mandate_id)) > 0),
    current_version INTEGER NOT NULL CHECK (typeof(current_version) = 'integer' AND current_version >= 1),
    principal_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT
"""

#: One row per version. The policy content never changes; ``status`` moves
#: forward only, from ACTIVE to SUPERSEDED once a later version exists.
CREATE_MANDATE_VERSIONS = """
CREATE TABLE IF NOT EXISTS mandate_versions (
    mandate_id TEXT NOT NULL REFERENCES mandates(mandate_id),
    version INTEGER NOT NULL CHECK (typeof(version) = 'integer' AND version >= 1),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'REVOKED', 'EXPIRED', 'SUPERSEDED')),
    principal_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (mandate_id, version)
) STRICT
"""

CREATE_QUOTES = """
CREATE TABLE IF NOT EXISTS quotes (
    quote_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(quote_id)) > 0),
    merchant_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (typeof(quantity) = 'integer' AND quantity >= 1),
    merchant_total_cents INTEGER NOT NULL
        CHECK (typeof(merchant_total_cents) = 'integer' AND merchant_total_cents >= 0),
    currency TEXT NOT NULL CHECK (currency = 'HKD'),
    quote_hash TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    payload TEXT NOT NULL CHECK (json_valid(payload))
) STRICT
"""

#: ``idempotency_key`` is unique across every proposal in the database, which is
#: what makes a replay detectable rather than merely likely.
CREATE_PURCHASE_PROPOSALS = """
CREATE TABLE IF NOT EXISTS purchase_proposals (
    proposal_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(proposal_id)) > 0),
    mandate_id TEXT NOT NULL,
    expected_mandate_version INTEGER NOT NULL
        CHECK (typeof(expected_mandate_version) = 'integer' AND expected_mandate_version >= 1),
    principal_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (typeof(quantity) = 'integer' AND quantity >= 1),
    merchant_id TEXT NOT NULL,
    quote_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    payload TEXT NOT NULL CHECK (json_valid(payload))
) STRICT
"""

#: ``PolicyDecision`` carries no decision id of its own, so the row gets a
#: surrogate key and a proposal may legitimately have several decisions: an
#: escalation that the principal then approved is re-evaluated, and the first
#: decision is evidence that must survive the second.
CREATE_POLICY_DECISIONS = """
CREATE TABLE IF NOT EXISTS policy_decisions (
    decision_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(decision_id)) > 0),
    proposal_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('APPROVE', 'DENY', 'ESCALATE')),
    cash_total_cents INTEGER NOT NULL
        CHECK (typeof(cash_total_cents) = 'integer' AND cash_total_cents >= 0),
    evaluated_at TEXT NOT NULL,
    payload TEXT NOT NULL CHECK (json_valid(payload))
) STRICT
"""

CREATE_DENIAL_RECEIPTS = """
CREATE TABLE IF NOT EXISTS denial_receipts (
    denial_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(denial_id)) > 0),
    proposal_id TEXT NOT NULL UNIQUE,
    primary_reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL CHECK (json_valid(payload))
) STRICT
"""

#: The one contract object C is allowed to update: ``resolution`` and
#: ``resolved_at`` are set once, and an open escalation past ``expires_at`` is a
#: timeout rather than an approval (DC12, fail closed).
CREATE_ESCALATION_REQUESTS = """
CREATE TABLE IF NOT EXISTS escalation_requests (
    escalation_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(escalation_id)) > 0),
    proposal_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution TEXT CHECK (resolution IS NULL OR resolution IN ('APPROVED', 'REJECTED', 'TIMEOUT')),
    payload TEXT NOT NULL CHECK (json_valid(payload))
) STRICT
"""

#: ``consumed_at`` is the whole point of the table: the evaluator can only read
#: a grant, so single use has to be enforced here, by a conditional UPDATE that
#: matches at most one unconsumed row.
CREATE_APPROVAL_GRANTS = """
CREATE TABLE IF NOT EXISTS approval_grants (
    grant_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(grant_id)) > 0),
    proposal_id TEXT NOT NULL,
    mandate_id TEXT NOT NULL,
    mandate_version INTEGER NOT NULL CHECK (typeof(mandate_version) = 'integer' AND mandate_version >= 1),
    quote_hash TEXT NOT NULL,
    payment_route_id TEXT NOT NULL,
    cash_total_cents INTEGER NOT NULL
        CHECK (typeof(cash_total_cents) = 'integer' AND cash_total_cents >= 0),
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    payload TEXT NOT NULL CHECK (json_valid(payload))
) STRICT
"""

#: One reservation per proposal. ``status`` values that still hold budget are
#: listed in ``app.contracts.commerce.HOLDING_STATUSES``; they are what
#: ``SpendState`` counts, which is how two concurrent proposals cannot both see
#: the full remaining budget.
CREATE_RESERVATIONS = """
CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(reservation_id)) > 0),
    proposal_id TEXT NOT NULL UNIQUE,
    principal_id TEXT NOT NULL,
    mandate_id TEXT NOT NULL,
    mandate_version INTEGER NOT NULL CHECK (typeof(mandate_version) = 'integer' AND mandate_version >= 1),
    quote_id TEXT NOT NULL,
    quote_hash TEXT NOT NULL,
    payment_route_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (typeof(amount_cents) = 'integer' AND amount_cents >= 0),
    quantity INTEGER NOT NULL CHECK (typeof(quantity) = 'integer' AND quantity >= 1),
    currency TEXT NOT NULL CHECK (currency = 'HKD'),
    status TEXT NOT NULL CHECK (status IN (
        'ACTIVE', 'PAYMENT_SUBMITTING', 'CAPTURED', 'SETTLED',
        'RELEASED', 'EXPIRED', 'FAILED', 'UNKNOWN')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    payload TEXT NOT NULL CHECK (json_valid(payload))
) STRICT
"""

#: A capability is a bearer token, so the only safe place to record its identity
#: is a nonce that is spent once. The full token is never stored and never
#: enters the audit payload.
CREATE_CAPABILITY_NONCES = """
CREATE TABLE IF NOT EXISTS capability_nonces (
    nonce TEXT PRIMARY KEY NOT NULL CHECK (length(trim(nonce)) > 0),
    reservation_id TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    context_hash TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    payment_id TEXT
) STRICT
"""

CREATE_ORDERS = """
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(order_id)) > 0),
    proposal_id TEXT NOT NULL UNIQUE,
    principal_id TEXT NOT NULL,
    merchant_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (typeof(quantity) = 'integer' AND quantity >= 1),
    amount_cents INTEGER NOT NULL CHECK (typeof(amount_cents) = 'integer' AND amount_cents >= 0),
    currency TEXT NOT NULL CHECK (currency = 'HKD'),
    status TEXT NOT NULL CHECK (status IN (
        'CREATED', 'PAID', 'PAYMENT_FAILED', 'PAYMENT_UNKNOWN', 'CANCELLED')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT
"""

#: Every settlement attempt, successful or not. A failed attempt is a record,
#: not an absence: "the payment did not go through" is exactly the kind of thing
#: a third party should be able to check afterwards. The partial unique index
#: below allows many failed attempts but at most one settled payment per
#: reservation.
CREATE_PAYMENT_ATTEMPTS = """
CREATE TABLE IF NOT EXISTS payment_attempts (
    attempt_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(attempt_id)) > 0),
    proposal_id TEXT NOT NULL,
    reservation_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('SETTLED', 'FAILED', 'UNKNOWN')),
    code TEXT,
    message TEXT,
    retryable INTEGER CHECK (retryable IS NULL OR retryable IN (0, 1)),
    provider_reference TEXT,
    attempted_at TEXT NOT NULL,
    payload TEXT CHECK (payload IS NULL OR json_valid(payload))
) STRICT
"""

CREATE_AUDIT_EVENTS = """
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY NOT NULL CHECK (typeof(sequence) = 'integer' AND sequence >= 0),
    event_id TEXT NOT NULL UNIQUE CHECK (length(trim(event_id)) > 0),
    event_type TEXT NOT NULL,
    actor_type TEXT NOT NULL CHECK (actor_type IN ('USER', 'AGENT', 'COMMERCE', 'SYSTEM')),
    actor_id TEXT NOT NULL,
    mandate_id TEXT,
    proposal_id TEXT,
    reservation_id TEXT,
    transaction_id TEXT,
    payload TEXT NOT NULL CHECK (json_valid(payload)),
    previous_hash TEXT,
    event_hash TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    CHECK ((sequence = 0) = (previous_hash IS NULL))
) STRICT
"""

AUDIT_APPEND_ONLY_TRIGGERS = (
    """
    CREATE TRIGGER IF NOT EXISTS audit_events_no_update
    BEFORE UPDATE ON audit_events
    BEGIN
        SELECT RAISE(ABORT, 'audit_events is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
    BEFORE DELETE ON audit_events
    BEGIN
        SELECT RAISE(ABORT, 'audit_events is append-only');
    END
    """,
)

INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_mandate_versions_principal "
    "ON mandate_versions(principal_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_proposals_mandate ON purchase_proposals(mandate_id)",
    "CREATE INDEX IF NOT EXISTS idx_decisions_proposal "
    "ON policy_decisions(proposal_id, evaluated_at)",
    "CREATE INDEX IF NOT EXISTS idx_grants_proposal "
    "ON approval_grants(proposal_id, issued_at)",
    "CREATE INDEX IF NOT EXISTS idx_reservations_principal "
    "ON reservations(principal_id, status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_reservations_mandate "
    "ON reservations(mandate_id, mandate_version, status)",
    "CREATE INDEX IF NOT EXISTS idx_attempts_proposal "
    "ON payment_attempts(proposal_id, attempted_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_attempts_one_settlement "
    "ON payment_attempts(reservation_id) WHERE status = 'SETTLED'",
    "CREATE INDEX IF NOT EXISTS idx_audit_mandate ON audit_events(mandate_id, sequence)",
    "CREATE INDEX IF NOT EXISTS idx_audit_proposal ON audit_events(proposal_id, sequence)",
)

#: In creation order. Foreign keys and the audit CHECK require the referenced
#: table to exist first, so the order is not cosmetic.
TABLES: tuple[tuple[str, str], ...] = (
    ("wallets", CREATE_WALLETS),
    ("mandates", CREATE_MANDATES),
    ("mandate_versions", CREATE_MANDATE_VERSIONS),
    ("quotes", CREATE_QUOTES),
    ("purchase_proposals", CREATE_PURCHASE_PROPOSALS),
    ("policy_decisions", CREATE_POLICY_DECISIONS),
    ("denial_receipts", CREATE_DENIAL_RECEIPTS),
    ("escalation_requests", CREATE_ESCALATION_REQUESTS),
    ("approval_grants", CREATE_APPROVAL_GRANTS),
    ("reservations", CREATE_RESERVATIONS),
    ("capability_nonces", CREATE_CAPABILITY_NONCES),
    ("orders", CREATE_ORDERS),
    ("payment_attempts", CREATE_PAYMENT_ATTEMPTS),
    ("audit_events", CREATE_AUDIT_EVENTS),
)

#: Every table C owns. Used by the status reader and by the coordinated reset
#: tool, so the reset tool cannot drift from the schema.
TABLE_NAMES: tuple[str, ...] = tuple(name for name, _ in TABLES)


def create_commerce_schema(conn) -> None:
    """Create C's tables on D's caller-owned transaction.

    Idempotent: every statement is ``IF NOT EXISTS``, so this runs on every
    start and never rewrites an existing table. It does not commit, roll back,
    close, or call ``executescript`` -- D checks ``conn.in_transaction``
    immediately afterwards and raises if the hook ended it.
    """
    for _, statement in TABLES:
        conn.execute(statement)
    for statement in AUDIT_APPEND_ONLY_TRIGGERS:
        conn.execute(statement)
    for statement in INDEXES:
        conn.execute(statement)


def initialize_wallets(conn) -> None:
    """Create the demo principal's wallet if it is missing, and nothing else.

    Never overwrites a balance: spending accumulates across runs, and a
    re-initialisation that reset the balance would silently undo the ledger
    while leaving the reservations and receipts that explain it. D calls this
    second, after the product import.
    """
    conn.execute(
        "INSERT INTO wallets(principal_id, currency, balance_cents, updated_at) "
        "VALUES (?, 'HKD', ?, ?) "
        "ON CONFLICT(principal_id) DO NOTHING",
        (
            DEMO_PRINCIPAL_ID,
            DEMO_WALLET_OPENING_BALANCE_CENTS,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


__all__ = [
    "COMMERCE_SCHEMA_VERSION",
    "COMPONENT",
    "TABLE_NAMES",
    "TABLES",
    "create_commerce_schema",
    "initialize_wallets",
]
