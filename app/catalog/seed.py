import json
from pathlib import Path

from .models import DataValidationError, Product


class SeedDataError(DataValidationError):
    pass


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SeedDataError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise SeedDataError(f"Invalid JSON number: {value}")


def load_seed(path):
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"),
                         object_pairs_hook=_unique_keys, parse_constant=_reject_constant)
    except (OSError, ValueError) as exc:
        raise SeedDataError(f"{path.name}: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise SeedDataError("Seed must be a nonempty JSON array")
    products, seen = [], set()
    for index, entry in enumerate(raw, start=1):
        try:
            product = Product.from_mapping(entry)
            if product.product_id in seen:
                raise DataValidationError(f"Duplicate product_id: {product.product_id}")
            seen.add(product.product_id)
            products.append(product)
        except DataValidationError as exc:
            raise SeedDataError(f"Product #{index}: {exc}") from exc
    return products


def insert_missing_products(conn, products):
    """Caller owns an active transaction; existing rows are never overwritten."""
    if not conn.in_transaction:
        raise RuntimeError("Seed import requires a caller-owned transaction")
    from dataclasses import fields
    names = [field.name for field in fields(Product)]
    placeholders = ", ".join("?" for _ in names)
    sql = (f"INSERT INTO products ({', '.join(names)}) VALUES ({placeholders}) "
           "ON CONFLICT(product_id) DO NOTHING")
    inserted = 0
    for product in products:
        data = product.as_dict()
        data["anc"] = None if product.anc is None else int(product.anc)
        data["use_cases"] = json.dumps(product.use_cases, ensure_ascii=False)
        data["tags"] = json.dumps(product.tags)
        data["supported_devices"] = json.dumps(product.supported_devices) if product.supported_devices is not None else None
        inserted += conn.execute(sql, [data[name] for name in names]).rowcount
    return {"seed_count": len(products), "inserted": inserted,
            "skipped_existing": len(products) - inserted}


def restore_products(conn, products):
    """Explicit reset only. Preserve parent IDs for immutable review history."""
    if not conn.in_transaction:
        raise RuntimeError("Catalog restore requires caller-owned transaction")
    if not products:
        raise SeedDataError("Catalog restore requires nonempty validated products")
    from dataclasses import fields
    names = [field.name for field in fields(Product)]
    ids = [p.product_id for p in products]
    # Foreign keys refuse a reset seed that removes a reviewed product.
    conn.execute("DELETE FROM products WHERE product_id NOT IN (" + ",".join("?" for _ in ids) + ")", ids)
    sql = (f"INSERT INTO products ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)}) "
           "ON CONFLICT(product_id) DO UPDATE SET " +
           ", ".join(f"{name}=excluded.{name}" for name in names if name != "product_id"))
    for product in products:
        data = product.as_dict()
        data["anc"] = None if data["anc"] is None else int(data["anc"])
        data["use_cases"] = json.dumps(data["use_cases"], ensure_ascii=False)
        data["tags"] = json.dumps(data["tags"])
        data["supported_devices"] = json.dumps(data["supported_devices"]) if data["supported_devices"] is not None else None
        conn.execute(sql, [data[name] for name in names])
    return {"seed_count": len(products), "restored": len(products)}
