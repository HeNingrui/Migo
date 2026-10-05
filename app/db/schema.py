SCHEMA_VERSION = 2
COMPONENT = "catalog_hkd"

CREATE_VERSION = """
CREATE TABLE IF NOT EXISTS schema_version (
    component TEXT PRIMARY KEY,
    version INTEGER NOT NULL CHECK (version >= 1),
    updated_at TEXT NOT NULL
)
"""

CREATE_PRODUCTS = """
CREATE TABLE IF NOT EXISTS products (
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
"""

INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_products_price ON products(price_cents)",
    "CREATE INDEX IF NOT EXISTS idx_products_brand ON products(brand)",
    "CREATE INDEX IF NOT EXISTS idx_products_connection_form ON products(connection, form_factor)",
)
