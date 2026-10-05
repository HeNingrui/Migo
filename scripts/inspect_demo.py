import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.catalog import HardConstraints, ProductRepository
from app.db import database_status


def main():
    parser = argparse.ArgumentParser(description="Inspect products without changing the database")
    parser.add_argument("--db")
    parser.add_argument("--product")
    parser.add_argument("--max-price-cents", type=int)
    parser.add_argument("--wireless", action="store_true")
    parser.add_argument("--anc", action="store_true")
    args = parser.parse_args()
    status = database_status(args.db)
    if not status["db_ready"]:
        print(json.dumps(status, indent=2))
        return 1
    repo = ProductRepository(args.db)
    if args.product:
        product = repo.get_product(args.product)
        result = {"product": product.as_dict() if product else None}
    else:
        constraints = HardConstraints(max_price_cents=args.max_price_cents,
                                      connection="wireless" if args.wireless else None,
                                      anc_required=args.anc)
        products = repo.list_candidates(constraints)
        result = {"status": status, "constraints": constraints.as_dict(),
                  "total_matches": len(products), "products": [p.as_dict() for p in products]}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
