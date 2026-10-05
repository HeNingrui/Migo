-- Integrated catalog v2, review/observation v1, team commerce schema. No data.

CREATE TABLE approval_grants (
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
;

CREATE TABLE audit_events (
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
;

CREATE TABLE capability_nonces (
    nonce TEXT PRIMARY KEY NOT NULL CHECK (length(trim(nonce)) > 0),
    reservation_id TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    context_hash TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    payment_id TEXT
) STRICT
;

CREATE TABLE catalog_observations (
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
) STRICT;

CREATE TABLE denial_receipts (
    denial_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(denial_id)) > 0),
    proposal_id TEXT NOT NULL UNIQUE,
    primary_reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL CHECK (json_valid(payload))
) STRICT
;

CREATE TABLE escalation_requests (
    escalation_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(escalation_id)) > 0),
    proposal_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution TEXT CHECK (resolution IS NULL OR resolution IN ('APPROVED', 'REJECTED', 'TIMEOUT')),
    payload TEXT NOT NULL CHECK (json_valid(payload))
) STRICT
;

CREATE TABLE mandate_versions (
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
;

CREATE TABLE mandates (
    mandate_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(mandate_id)) > 0),
    current_version INTEGER NOT NULL CHECK (typeof(current_version) = 'integer' AND current_version >= 1),
    principal_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT
;

CREATE TABLE orders (
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
;

CREATE TABLE payment_attempts (
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
;

CREATE TABLE policy_decisions (
    decision_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(decision_id)) > 0),
    proposal_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('APPROVE', 'DENY', 'ESCALATE')),
    cash_total_cents INTEGER NOT NULL
        CHECK (typeof(cash_total_cents) = 'integer' AND cash_total_cents >= 0),
    evaluated_at TEXT NOT NULL,
    payload TEXT NOT NULL CHECK (json_valid(payload))
) STRICT
;

CREATE TABLE product_reviews (
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
) STRICT;

CREATE TABLE products (
    product_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(product_id)) > 0),
    category TEXT NOT NULL CHECK (category = 'headphones'),
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    brand TEXT NOT NULL CHECK (length(trim(brand)) > 0),
    model TEXT NOT NULL CHECK (length(trim(model)) > 0),
    variant TEXT NOT NULL CHECK (length(trim(variant)) > 0),
    connection TEXT NOT NULL CHECK (connection IN ('wired', 'wireless')),
    form_factor TEXT NOT NULL CHECK (form_factor IN ('in_ear', 'over_ear', 'open_ear')),
    price_cents INTEGER NOT NULL CHECK (typeof(price_cents) = 'integer' AND price_cents >= 0),
    currency TEXT NOT NULL CHECK (currency = 'HKD'),
    stock INTEGER NOT NULL CHECK (typeof(stock) = 'integer' AND stock >= 0),
    anc INTEGER CHECK (anc IS NULL OR (typeof(anc) = 'integer' AND anc IN (0, 1))),
    battery_hours REAL CHECK (battery_hours IS NULL OR battery_hours >= 0),
    wearing_weight_g REAL CHECK (wearing_weight_g IS NULL OR wearing_weight_g > 0),
    use_cases TEXT NOT NULL CHECK (json_valid(use_cases) AND json_type(use_cases) = 'array'),
    source_type TEXT NOT NULL CHECK (source_type IN ('demo', 'real_manual', 'external')),
    source_url TEXT,
    data_note TEXT NOT NULL CHECK (length(trim(data_note)) > 0),
    seller_description TEXT NOT NULL
        CHECK (length(trim(seller_description)) > 0 AND length(seller_description) < 100),
    shipping_origin TEXT NOT NULL CHECK (length(trim(shipping_origin)) > 0),
    supported_devices TEXT CHECK (supported_devices IS NULL OR (json_valid(supported_devices) AND json_type(supported_devices) = 'array')),
    color TEXT,
    tags TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(tags) AND json_type(tags) = 'array'),
    estimated_delivery_days INTEGER CHECK (estimated_delivery_days IS NULL OR (typeof(estimated_delivery_days)='integer' AND estimated_delivery_days>=0)),
    CHECK (connection != 'wired' OR battery_hours IS NULL),
    CHECK (source_type != 'demo' OR source_url IS NULL)
) STRICT
;

CREATE TABLE purchase_proposals (
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
;

CREATE TABLE quotes (
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
;

CREATE TABLE reservations (
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
;

CREATE TABLE schema_version (
    component TEXT PRIMARY KEY,
    version INTEGER NOT NULL CHECK (version >= 1),
    updated_at TEXT NOT NULL
);

CREATE TABLE wallets (
    principal_id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(principal_id)) > 0),
    currency TEXT NOT NULL CHECK (currency = 'HKD'),
    balance_cents INTEGER NOT NULL
        CHECK (typeof(balance_cents) = 'integer' AND balance_cents >= 0),
    updated_at TEXT NOT NULL
) STRICT
;

CREATE UNIQUE INDEX idx_attempts_one_settlement ON payment_attempts(reservation_id) WHERE status = 'SETTLED';

CREATE INDEX idx_attempts_proposal ON payment_attempts(proposal_id, attempted_at);

CREATE INDEX idx_audit_mandate ON audit_events(mandate_id, sequence);

CREATE INDEX idx_audit_proposal ON audit_events(proposal_id, sequence);

CREATE INDEX idx_decisions_proposal ON policy_decisions(proposal_id, evaluated_at);

CREATE INDEX idx_grants_proposal ON approval_grants(proposal_id, issued_at);

CREATE INDEX idx_mandate_versions_principal ON mandate_versions(principal_id, status);

CREATE INDEX idx_observations_subject ON catalog_observations(subject_id, kind, observed_on, observation_id);

CREATE INDEX idx_products_brand ON products(brand);

CREATE INDEX idx_products_connection_form ON products(connection, form_factor);

CREATE INDEX idx_products_price ON products(price_cents);

CREATE INDEX idx_proposals_mandate ON purchase_proposals(mandate_id);

CREATE INDEX idx_reservations_mandate ON reservations(mandate_id, mandate_version, status);

CREATE INDEX idx_reservations_principal ON reservations(principal_id, status, created_at);

CREATE INDEX idx_reviews_product_time ON product_reviews(product_id, posted_at, review_id);

CREATE TRIGGER audit_events_no_delete
    BEFORE DELETE ON audit_events
    BEGIN
        SELECT RAISE(ABORT, 'audit_events is append-only');
    END;

CREATE TRIGGER audit_events_no_update
    BEFORE UPDATE ON audit_events
    BEGIN
        SELECT RAISE(ABORT, 'audit_events is append-only');
    END;

CREATE TRIGGER observations_no_delete BEFORE DELETE ON catalog_observations
       BEGIN SELECT RAISE(ABORT, 'Observations are immutable; retain history'); END;

CREATE TRIGGER observations_no_update BEFORE UPDATE ON catalog_observations
       BEGIN SELECT RAISE(ABORT, 'Observations are immutable; append a new observation_id'); END;

CREATE TRIGGER reviews_no_delete BEFORE DELETE ON product_reviews
       BEGIN SELECT RAISE(ABORT, 'Reviews are immutable; retain history'); END;

CREATE TRIGGER reviews_no_update BEFORE UPDATE ON product_reviews
       BEGIN SELECT RAISE(ABORT, 'Reviews are immutable; append a new review_id'); END;
