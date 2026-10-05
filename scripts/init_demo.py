import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.catalog.seed import SeedDataError
from app.db import initialize_database
from app.db.core import PROJECT_ROOT
from app.commerce.schema import create_commerce_schema, initialize_wallets


def main():
    parser = argparse.ArgumentParser(description="Initialize the HKD demo product database")
    parser.add_argument("--db", help="Database path; default uses DEMO_DB_PATH or project/var/demo.sqlite3")
    parser.add_argument("--seed", type=Path, help="Alternative seed JSON")
    parser.add_argument("--catalog-only", action="store_true", help="Create only D's product schema")
    parser.add_argument("--reset", action="store_true", help="Back up and rebuild catalog-only demo data")
    args = parser.parse_args()
    try:
        full = not args.catalog_only
        seeds = full and args.seed is None
        result = initialize_database(args.db, args.seed, reset=args.reset,
            commerce_schema=create_commerce_schema if full else None,
            wallet_initializer=initialize_wallets if full else None,
            review_seed_path=PROJECT_ROOT / "data/reviews.seed.json" if seeds else None,
            observation_seed_path=PROJECT_ROOT / "data/observations.2026-10-03.json" if seeds else None,
            metadata_seed_path=PROJECT_ROOT / "data/product-metadata.demo.json" if seeds else None)
    except (SeedDataError, sqlite3.Error, OSError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "data": result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
