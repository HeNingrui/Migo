from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from app.catalog.seed import load_seed, insert_missing_products, restore_products
from app.catalog.reviews import (COMPONENT as REVIEW_COMPONENT, ReviewRepository,
                                 create_review_schema, load_reviews)
from app.catalog.observations import (COMPONENT as OBS_COMPONENT, ObservationRepository,
                                      create_observation_schema, load_observations)
from .core import PROJECT_ROOT, connect, resolve_db_path, validate_schema
from .schema import COMPONENT, CREATE_PRODUCTS, CREATE_VERSION, INDEXES, SCHEMA_VERSION


def initialize_database(path=None, seed_path=None, *, reset=False,
                        commerce_schema=None, wallet_initializer=None, observation_seed_path=None,
                        review_seed_path=None, metadata_seed_path=None):
    """C hooks receive this connection and must not commit, rollback or executescript.

    commerce_schema(conn) creates C's tables before wallet_initializer(conn).
    Wallet initialization must insert missing users only, preserving balances.
    Reset restores catalog seeds, retains immutable observations/reviews, and refuses
    other business tables to protect C's state. Evidence import is opt-in.
    """
    products = load_seed(seed_path or PROJECT_ROOT / "data" / "products.seed.json")
    observations = load_observations(observation_seed_path) if observation_seed_path is not None else []
    reviews = load_reviews(review_seed_path) if review_seed_path is not None else []
    db_path = resolve_db_path(path)
    conn = connect(db_path)
    backup_path = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
        if existing and "schema_version" not in existing:
            raise RuntimeError("Existing unversioned database; no automatic conversion or overwrite")
        if "schema_version" in existing:
            from .migrations import migrate_catalog
            migrate_catalog(conn)
            validate_schema(conn)
        if reset:
            other = existing - {"products", "schema_version", "catalog_observations", "product_reviews"}
            if other:
                raise RuntimeError(f"Reset blocked: C's business tables exist: {sorted(other)}. "
                                   "Use the team's coordinated reset tool.")
            if "schema_version" in existing:
                other_versions = conn.execute(
                    "SELECT component FROM schema_version WHERE component NOT IN (?, ?, ?)",
                    (COMPONENT, OBS_COMPONENT, REVIEW_COMPONENT)).fetchall()
                if other_versions:
                    raise RuntimeError("Reset blocked: other schema components exist")
            if existing:
                # This read connection sees the committed state while our write lock
                # prevents other writers. Back up before resetting any catalog data.
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                backup_path = Path(str(db_path) + f".before-reset-{stamp}.bak")
                source, target = connect(db_path, readonly=True), sqlite3.connect(backup_path)
                try:
                    source.backup(target)
                finally:
                    source.close()
                    target.close()
        conn.execute(CREATE_VERSION)
        conn.execute(CREATE_PRODUCTS)
        for statement in INDEXES:
            conn.execute(statement)
        conn.execute("INSERT INTO schema_version(component, version, updated_at) VALUES (?, ?, ?) "
                     "ON CONFLICT(component) DO NOTHING",
                     (COMPONENT, SCHEMA_VERSION, datetime.now(timezone.utc).isoformat()))
        validate_schema(conn)
        create_observation_schema(conn)
        create_review_schema(conn)
        if commerce_schema is not None:
            commerce_schema(conn)
            if not conn.in_transaction:
                raise RuntimeError("C's schema hook ended the caller-owned transaction")
        result = restore_products(conn, products) if reset else insert_missing_products(conn, products)
        if metadata_seed_path is not None:
            from .migrations import import_demo_metadata
            import_demo_metadata(conn, metadata_seed_path)
        result.update(ObservationRepository(db_path).append_many(observations, conn))
        result.update(ReviewRepository(db_path).append_many(reviews, conn))
        if wallet_initializer is not None:
            wallet_initializer(conn)
            if not conn.in_transaction:
                raise RuntimeError("C's wallet hook ended the caller-owned transaction")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("Foreign key consistency check failed")
        conn.commit()
        result.update(db_path=str(db_path), currency="HKD", schema_version=SCHEMA_VERSION,
                      reset=reset, backup_path=str(backup_path) if backup_path else None)
        return result
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()
