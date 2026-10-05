from contextlib import contextmanager
import json

from app.db.core import connect, resolve_db_path
from .models import HardConstraints, Product, _integer, _text


def _product_from_row(row, column_names=None):
    data = dict(row) if hasattr(row, "keys") else dict(zip(column_names, row))
    data["use_cases"] = json.loads(data["use_cases"])
    data["tags"] = json.loads(data["tags"])
    data["supported_devices"] = json.loads(data["supported_devices"]) if data["supported_devices"] is not None else None
    data["anc"] = None if data["anc"] is None else bool(data["anc"])
    return Product.from_mapping(data)


class ProductRepository:
    def __init__(self, db_path=None, *, product_model=None):
        self.db_path = resolve_db_path(db_path)
        # A can inject the shared Product class without D changing contracts/.
        # Contract conflicts raise during validation; no fields/currency are dropped.
        self.product_model = product_model

    def _output_product(self, row, names):
        product = _product_from_row(row, names)
        if self.product_model is None:
            return product
        validator = getattr(self.product_model, "model_validate", None)
        if not callable(validator):
            raise TypeError("product_model must provide Pydantic v2 model_validate")
        return validator(product.as_dict())

    @contextmanager
    def _connection(self, supplied):
        if supplied is not None:
            yield supplied
        else:
            owned = connect(self.db_path, readonly=True)
            try:
                yield owned
            finally:
                owned.close()

    def get_product(self, product_id: str, connection=None) -> Product | None:
        _text(product_id, "product_id")
        with self._connection(connection) as conn:
            cursor = conn.execute("SELECT * FROM products WHERE product_id = ?", (product_id,))
            row = cursor.fetchone()
            return None if row is None else self._output_product(row, [col[0] for col in cursor.description])

    def list_candidates(self, constraints: HardConstraints, connection=None) -> list[Product]:
        request = HardConstraints.coerce(constraints)
        clauses, values = ["category = ?"], [request.category]
        for field, operator, value in (
            ("price_cents", ">=", request.min_price_cents),
            ("price_cents", "<=", request.max_price_cents),
            ("connection", "=", request.connection),
            ("form_factor", "=", request.form_factor),
            ("battery_hours", ">=", request.min_battery_hours),
            ("wearing_weight_g", "<=", request.max_wearing_weight_g),
        ):
            if value is not None:
                clauses.append(f"{field} {operator} ?")
                values.append(value)
        if request.color is not None:
            clauses.append("color = ?")
            values.append(request.color)
        if request.required_device is not None:
            clauses.append("EXISTS (SELECT 1 FROM json_each(products.supported_devices) WHERE value = ?)")
            values.append(request.required_device)
        for tag in request.tags:
            clauses.append("EXISTS (SELECT 1 FROM json_each(products.tags) WHERE value = ?)")
            values.append(tag)
        if request.max_estimated_delivery_days is not None:
            clauses.append("estimated_delivery_days <= ?")
            values.append(request.max_estimated_delivery_days)
        if request.anc_required:
            clauses.append("anc = 1")
        if request.in_stock_only:
            clauses.append("stock > 0")
        if request.brand_allowlist:
            clauses.append("brand IN (" + ", ".join("?" for _ in request.brand_allowlist) + ")")
            values.extend(request.brand_allowlist)
        # Only fixed column names/operators are interpolated. User values are bound.
        sql = "SELECT * FROM products WHERE " + " AND ".join(clauses) + " ORDER BY product_id"
        with self._connection(connection) as conn:
            cursor = conn.execute(sql, values)
            names = [col[0] for col in cursor.description]
            return [self._output_product(row, names) for row in cursor.fetchall()]

    def decrease_stock(self, product_id: str, quantity: int, connection) -> bool:
        _text(product_id, "product_id")
        _integer(quantity, "quantity", minimum=1)
        if connection is None or not connection.in_transaction:
            raise RuntimeError("Stock updates require C's caller-owned write transaction")
        cursor = connection.execute(
            "UPDATE products SET stock = stock - ? WHERE product_id = ? AND stock >= ?",
            (quantity, product_id, quantity))
        return cursor.rowcount == 1


# Exact service signatures from the plan, plus an injectable repository for B/C.
def get_product(product_id: str, connection=None) -> Product | None:
    return ProductRepository().get_product(product_id, connection)


def list_candidates(constraints: HardConstraints, connection=None) -> list[Product]:
    return ProductRepository().list_candidates(constraints, connection)


def decrease_stock(product_id: str, quantity: int, connection) -> bool:
    return ProductRepository().decrease_stock(product_id, quantity, connection)
