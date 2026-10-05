"""Explicit coordinated demo reset: back up, retain observations/reviews, rebuild D+C.

Only the CLI's --yes authorises discarding demo transactions. Normal startup
never calls this tool. All schema/data changes share one caller-owned transaction.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.catalog.observations import (COMPONENT as OBS_COMPONENT, ObservationRepository,
                                      create_observation_schema, load_observations)
from app.catalog.seed import restore_products, load_seed
from app.catalog.reviews import (COMPONENT as REVIEW_COMPONENT, ReviewRepository,
                                 create_review_schema, load_reviews)
from app.commerce.schema import TABLE_NAMES as COMMERCE_TABLES, create_commerce_schema, initialize_wallets
from app.db import connect, resolve_db_path
from app.db.core import PROJECT_ROOT, validate_schema
from app.db.schema import COMPONENT, CREATE_PRODUCTS, INDEXES, SCHEMA_VERSION

KNOWN_TABLES = frozenset({"products", "schema_version", "catalog_observations", "product_reviews", *COMMERCE_TABLES})


def reset_database(path, seed_path=None):
    """For the explicit reset CLI. Validate before mutation; preserve evidence.

    A full SQLite backup includes C's old records, even in WAL mode. A held write
    lock prevents concurrent writes between backup and reset. Failure rolls back
    the catalog and commerce rebuild together.
    """
    products = load_seed(seed_path or PROJECT_ROOT / "data/products.seed.json")
    observations = load_observations(PROJECT_ROOT / "data/observations.2026-10-03.json")
    reviews = load_reviews(PROJECT_ROOT / "data/reviews.seed.json")
    db_path = resolve_db_path(path)
    if not db_path.is_file():
        raise RuntimeError(f"No database at {db_path}")
    conn = connect(db_path)
    backup = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        present = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        unknown = present - KNOWN_TABLES
        if unknown:
            raise RuntimeError(f"Reset refused: unknown tables {sorted(unknown)}")
        validate_schema(conn)
        components = {row[0] for row in conn.execute("SELECT component FROM schema_version")}
        if components - {COMPONENT, OBS_COMPONENT, REVIEW_COMPONENT}:
            raise RuntimeError("Reset refused: unknown schema components")
        # For D v1, create evidence storage within this transaction. Preserve
        # existing history and validate all stored hashes before resetting.
        create_observation_schema(conn)
        ObservationRepository(db_path).list_observations(connection=conn)
        create_review_schema(conn)
        # Validate all retained reviews before writing the backup or resetting.
        for (product_id,) in conn.execute("SELECT DISTINCT product_id FROM product_reviews"):
            ReviewRepository(db_path).list_reviews(product_id, conn)

        backup = db_path.with_name(f"{db_path.name}.before-reset-"
                                  f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.bak")
        source, target = connect(db_path, readonly=True), sqlite3.connect(backup)
        try:
            source.backup(target)
        finally:
            source.close()
            target.close()

        for table in reversed(COMMERCE_TABLES):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute(CREATE_PRODUCTS)
        for statement in INDEXES:
            conn.execute(statement)
        conn.execute("UPDATE schema_version SET version=?, updated_at=? WHERE component=?",
                     (SCHEMA_VERSION, datetime.now(timezone.utc).isoformat(), COMPONENT))
        create_commerce_schema(conn)
        result = restore_products(conn, products)
        from app.db.migrations import import_demo_metadata
        import_demo_metadata(conn, PROJECT_ROOT / "data/product-metadata.demo.json")
        result.update(ObservationRepository(db_path).append_many(observations, conn))
        result.update(ReviewRepository(db_path).append_many(reviews, conn))
        initialize_wallets(conn)
        if not conn.in_transaction:
            raise RuntimeError("An initialization hook ended the caller-owned transaction")
        validate_schema(conn)
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("Foreign key consistency check failed")
        conn.commit()
        return {"ok": True, "backup_path": str(backup), "data": result}
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", help="Database path; defaults to DEMO_DB_PATH")
    parser.add_argument("--yes", action="store_true", help="explicitly discard demo commerce records after backup")
    parser.add_argument("--seed", type=Path, help="Alternative product seed JSON")
    args = parser.parse_args()
    if not args.yes:
        print(json.dumps({"ok": False, "error": "Reset refused without --yes: demo transaction records will be discarded."}), file=sys.stderr)
        return 2
    try:
        result = reset_database(args.db, args.seed)
    except (ValueError, sqlite3.Error, OSError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
