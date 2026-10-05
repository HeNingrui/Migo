"""Additive catalog migration. Never changes prices, stock, or commerce rows."""
from dataclasses import fields
import json
from datetime import datetime, timezone
from pathlib import Path

from app.catalog.models import Product
from .schema import COMPONENT, SCHEMA_VERSION

EXTENSIONS = {
    "supported_devices": "TEXT CHECK (supported_devices IS NULL OR (json_valid(supported_devices) AND json_type(supported_devices)='array'))",
    "color": "TEXT",
    "tags": "TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(tags) AND json_type(tags)='array')",
    "estimated_delivery_days": "INTEGER CHECK (estimated_delivery_days IS NULL OR (typeof(estimated_delivery_days)='integer' AND estimated_delivery_days>=0))",
}


def migrate_catalog(conn):
    if not conn.in_transaction:
        raise RuntimeError("Migration requires caller-owned transaction")
    row = conn.execute("SELECT version FROM schema_version WHERE component=?", (COMPONENT,)).fetchone()
    if row is None or row[0] == SCHEMA_VERSION:
        return
    if row[0] != 1:
        raise RuntimeError("Unsupported catalog version; explicit migration required")
    actual = {r[1] for r in conn.execute("PRAGMA table_info(products)")}
    expected = {f.name for f in fields(Product)} - EXTENSIONS.keys()
    if actual != expected:
        raise RuntimeError("Catalog v1 schema mismatch; migration refused")
    for name, definition in EXTENSIONS.items():
        conn.execute(f"ALTER TABLE products ADD COLUMN {name} {definition}")
    conn.execute("UPDATE schema_version SET version=?, updated_at=? WHERE component=?",
                 (SCHEMA_VERSION, datetime.now(timezone.utc).isoformat(), COMPONENT))


def import_demo_metadata(conn, path):
    records = json.loads(Path(path).read_text(encoding="utf-8"))
    seen = set()
    for record in records:
        product_id = record["product_id"]
        if product_id in seen:
            raise ValueError("Duplicate metadata product_id")
        seen.add(product_id)
        row = conn.execute("SELECT * FROM products WHERE product_id=?", (product_id,)).fetchone()
        if row is None or row["source_type"] != "demo" or record["source_type"] != "demo":
            continue
        # Validate before filling absent fields. Existing recorded data wins.
        data = dict(row)
        data["anc"] = None if data["anc"] is None else bool(data["anc"])
        data["use_cases"] = json.loads(data["use_cases"])
        data["tags"] = record["tags"]
        data["supported_devices"] = record["supported_devices"]
        data["color"] = record["color"]
        data["estimated_delivery_days"] = record["estimated_delivery_days"]
        validated = Product.from_mapping(data)
        conn.execute("""UPDATE products SET
            color=COALESCE(color, ?), supported_devices=COALESCE(supported_devices, ?),
            estimated_delivery_days=COALESCE(estimated_delivery_days, ?),
            tags=CASE WHEN tags='[]' THEN ? ELSE tags END
            WHERE product_id=? AND source_type='demo'""",
            (validated.color, json.dumps(validated.supported_devices),
             validated.estimated_delivery_days, json.dumps(validated.tags), product_id))
