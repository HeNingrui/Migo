"""Shared sqlite3 connections. Transaction ownership belongs to the caller."""

from pathlib import Path
import os
import sqlite3

from .schema import COMPONENT, SCHEMA_VERSION


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def resolve_db_path(path=None):
    configured = path if path is not None else os.environ.get("DEMO_DB_PATH", "var/demo.sqlite3")
    result = Path(configured).expanduser()
    return result.resolve() if result.is_absolute() else (PROJECT_ROOT / result).resolve()


def connect(path=None, *, readonly=False, timeout=5.0):
    db_path = resolve_db_path(path)
    if readonly:
        conn = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True,
                               timeout=timeout, isolation_level=None)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), timeout=timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {max(0, int(timeout * 1000))}")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        conn.close()
        raise RuntimeError("SQLite foreign key enforcement is unavailable")
    if readonly:
        conn.execute("PRAGMA query_only = ON")
    return conn


def validate_schema(conn):
    from app.catalog.models import Product
    from dataclasses import fields

    version = conn.execute("SELECT version FROM schema_version WHERE component = ?",
                           (COMPONENT,)).fetchone()
    if version is None or version[0] != SCHEMA_VERSION:
        raise RuntimeError("Unsupported catalog schema version; migrate before use")
    actual = {row[1] for row in conn.execute("PRAGMA table_info(products)")}
    expected = {field.name for field in fields(Product)}
    if actual != expected:
        raise RuntimeError(f"Product schema mismatch: missing={expected - actual}, extra={actual - expected}")


def database_status(path=None):
    conn = None
    try:
        conn = connect(path, readonly=True)
        validate_schema(conn)
        check = conn.execute("PRAGMA quick_check").fetchall()
        if len(check) != 1 or check[0][0] != "ok":
            raise RuntimeError("Database integrity check failed")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("Foreign key consistency check failed")
        from app.catalog.observations import COMPONENT as OBS_COMPONENT, validate_observation_schema
        observation_count = None
        if conn.execute("SELECT 1 FROM schema_version WHERE component=?", (OBS_COMPONENT,)).fetchone():
            validate_observation_schema(conn)
            observation_count = conn.execute("SELECT count(*) FROM catalog_observations").fetchone()[0]
        from app.catalog.reviews import COMPONENT as REVIEW_COMPONENT, validate_review_schema
        review_count = None
        if conn.execute("SELECT 1 FROM schema_version WHERE component=?", (REVIEW_COMPONENT,)).fetchone():
            validate_review_schema(conn)
            review_count = conn.execute("SELECT count(*) FROM product_reviews").fetchone()[0]
        return {"db_ready": True, "schema_component": COMPONENT,
                "schema_version": SCHEMA_VERSION,
                "product_count": conn.execute("SELECT count(*) FROM products").fetchone()[0],
                "currency": "HKD", "observation_count": observation_count,
                "review_count": review_count, "error": None}
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        return {"db_ready": False, "schema_component": COMPONENT,
                "schema_version": None, "product_count": None,
                "currency": "HKD", "error": str(exc)}
    finally:
        if conn is not None:
            conn.close()
