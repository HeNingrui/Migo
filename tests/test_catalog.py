import copy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from app.catalog import HardConstraints, ProductRepository
from app.catalog.models import DataValidationError, Product
from app.catalog.seed import SeedDataError, load_seed
from app.db import connect, database_status, initialize_database, resolve_db_path
from app.db.core import PROJECT_ROOT


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "test.sqlite3"
        self.seed = PROJECT_ROOT / "data" / "products.seed.json"
        self.products = load_seed(self.seed)
        self.initial = initialize_database(self.db)
        self.repo = ProductRepository(self.db)

    def ids(self, **kwargs):
        return {p.product_id for p in self.repo.list_candidates(HardConstraints(**kwargs))}

    def write_seed(self, raw):
        path = self.root / "custom.seed.json"
        path.write_text(json.dumps(raw, allow_nan=False), encoding="utf-8")
        return path

    def test_empty_database_imports_exactly_40_products(self):
        self.assertEqual(self.initial["inserted"], 40)
        self.assertEqual(database_status(self.db)["product_count"], 40)
        for expected in self.products:
            self.assertEqual(self.repo.get_product(expected.product_id).as_dict(), expected.as_dict())

    def test_english_hkd_descriptions_and_shipping(self):
        for product in self.products:
            self.assertEqual(product.currency, "HKD")
            self.assertLess(len(product.seller_description), 100)
            self.assertTrue(product.shipping_origin)
            self.assertTrue(json.dumps(product.as_dict(), ensure_ascii=False).isascii())

    def test_get_missing_and_parameterized_ids(self):
        self.assertIsNone(self.repo.get_product("missing"))
        self.assertIsNone(self.repo.get_product("hp_0001' OR 1=1 --"))
        self.assertEqual(database_status(self.db)["product_count"], 40)

    def test_price_boundary_and_complete_qualified_list(self):
        found = self.repo.list_candidates(HardConstraints(max_price_cents=30000,
                                                         connection="wireless", anc_required=True))
        self.assertEqual([p.product_id for p in found], ["hp_0001", "hp_0007", "hp_0008", "hp_0018"])
        self.assertIn("hp_0007", self.ids(min_price_cents=30000, max_price_cents=30000))
        self.assertNotIn("hp_0007", self.ids(max_price_cents=29999))

    def test_all_hard_filters_match_seed_oracle(self):
        cases = [
            ({"min_price_cents": 20000, "max_price_cents": 40000},
             lambda p: 20000 <= p.price_cents <= 40000),
            ({"brand_allowlist": ["Yunsheng Demo"]}, lambda p: p.brand == "Yunsheng Demo"),
            ({"connection": "wired"}, lambda p: p.connection == "wired"),
            ({"form_factor": "open_ear"}, lambda p: p.form_factor == "open_ear"),
            ({"anc_required": True}, lambda p: p.anc is True),
            ({"min_battery_hours": 20}, lambda p: p.battery_hours is not None and p.battery_hours >= 20),
            ({"max_wearing_weight_g": 10}, lambda p: p.wearing_weight_g is not None and p.wearing_weight_g <= 10),
        ]
        for kwargs, qualifies in cases:
            with self.subTest(kwargs=kwargs):
                expected = {p.product_id for p in self.products if p.stock > 0 and qualifies(p)}
                self.assertEqual(self.ids(**kwargs), expected)

    def test_unknown_parameters_never_pass_required_filter(self):
        self.assertNotIn("hp_0006", self.ids(anc_required=True))
        self.assertNotIn("hp_0006", self.ids(min_battery_hours=0))
        self.assertNotIn("hp_0017", self.ids(max_wearing_weight_g=1000))
        self.assertIn("hp_0006", self.ids())

    def test_optional_anc_does_not_prohibit_anc(self):
        self.assertIn("hp_0001", self.ids(anc_required=False))

    def test_stock_filter_and_no_implicit_relaxation(self):
        self.assertNotIn("hp_0005", self.ids())
        self.assertIn("hp_0005", self.ids(in_stock_only=False))
        self.assertEqual(self.ids(max_price_cents=0, anc_required=True), set())

    def test_empty_brand_allowlist_and_bound_brand_value(self):
        self.assertEqual(self.ids(brand_allowlist=[]), self.ids())
        self.assertEqual(self.ids(brand_allowlist=["Yunsheng Demo' OR 1=1 --"]), set())

    def test_mapping_and_shared_model_constraints(self):
        constraints = HardConstraints(max_price_cents=30000).as_dict()
        self.assertEqual([p.product_id for p in self.repo.list_candidates(constraints)],
                         [p.product_id for p in self.repo.list_candidates(HardConstraints(max_price_cents=30000))])
        class SharedModel:
            def model_dump(self):
                return constraints
        self.assertEqual(len(self.repo.list_candidates(SharedModel())), len(self.ids(max_price_cents=30000)))

    def test_shared_product_model_can_be_injected_without_changing_fields(self):
        class SharedProduct:
            @classmethod
            def model_validate(cls, data):
                instance = cls()
                instance.data = Product.from_mapping(data).as_dict()
                return instance

            def model_dump(self, *, mode="python"):
                return copy.deepcopy(self.data)

        repo = ProductRepository(self.db, product_model=SharedProduct)
        product = repo.get_product("hp_0001")
        self.assertIsInstance(product, SharedProduct)
        self.assertEqual(product.model_dump(mode="json"), self.products[0].as_dict())
        candidates = repo.list_candidates(HardConstraints(max_price_cents=30000,
                                                          connection="wireless", anc_required=True))
        self.assertTrue(all(isinstance(p, SharedProduct) for p in candidates))
        self.assertIsNone(repo.get_product("missing"))

    def test_original_cny_contract_is_not_silently_coerced(self):
        class OriginalProduct:
            @classmethod
            def model_validate(cls, data):
                if data["currency"] != "CNY" or "seller_description" in data:
                    raise ValueError("Shared Product contract must be updated by A")
        repo = ProductRepository(self.db, product_model=OriginalProduct)
        with self.assertRaisesRegex(ValueError, "updated by A"):
            repo.get_product("hp_0001")
        self.assertEqual(self.repo.get_product("hp_0001").currency, "HKD")

    def test_invalid_constraints_rejected(self):
        for kwargs in ({"min_price_cents": 400, "max_price_cents": 300},
                       {"max_price_cents": 3.5}, {"min_price_cents": True},
                       {"anc_required": 1}, {"connection": "bluetooth"},
                       {"brand_allowlist": [""]}, {"brand_allowlist": None},
                       {"min_battery_hours": float("inf")},
                       {"max_wearing_weight_g": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(DataValidationError):
                HardConstraints(**kwargs)
        bad = HardConstraints().as_dict()
        bad["surprise"] = 1
        with self.assertRaises(DataValidationError):
            self.repo.list_candidates(bad)

    def test_repeated_initialization_preserves_stock_and_price(self):
        conn = connect(self.db)
        try:
            conn.execute("BEGIN IMMEDIATE")
            self.assertTrue(self.repo.decrease_stock("hp_0001", 2, conn))
            conn.execute("UPDATE products SET price_cents = 34500 WHERE product_id = 'hp_0001'")
            conn.commit()
        finally:
            conn.close()
        result = initialize_database(self.db)
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(result["skipped_existing"], 40)
        self.assertEqual(self.repo.get_product("hp_0001").stock, 8)
        self.assertEqual(self.repo.get_product("hp_0001").price_cents, 34500)

    def test_default_init_only_adds_missing_products(self):
        other = Product.from_mapping(self.products[0].as_dict())
        other.product_id = "hp_extra"
        path = self.write_seed([other.as_dict()] + [p.as_dict() for p in self.products])
        result = initialize_database(self.db, path)
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(self.repo.get_product("hp_extra").name, other.name)

    def test_invalid_seed_is_rejected_before_any_database_mutation(self):
        original = [p.as_dict() for p in self.products]
        alterations = [
            ("stock", -1), ("price_cents", 1.5), ("currency", "CNY"),
            ("anc", 1), ("use_cases", ["fake"]), ("use_cases", ["music", "music"]),
            ("seller_description", "x" * 100), ("shipping_origin", ""),
        ]
        for key, value in alterations:
            with self.subTest(key=key, value=value):
                raw = copy.deepcopy(original)
                raw[0][key] = value
                with self.assertRaises(SeedDataError):
                    initialize_database(self.db, self.write_seed(raw))
                self.assertEqual(self.repo.get_product("hp_0001").as_dict(), self.products[0].as_dict())
        for raw in ([original[0], original[0]], [{}], []):
            with self.assertRaises(SeedDataError):
                initialize_database(self.root / "invalid.sqlite3", self.write_seed(raw))
            self.assertFalse((self.root / "invalid.sqlite3").exists())

    def test_bad_json_duplicate_keys_and_nan(self):
        for text in ('[{"product_id": "a", "product_id": "b"}]', '[NaN]', '{', '{}'):
            path = self.root / "bad.json"
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(SeedDataError):
                initialize_database(self.db, path)

    def test_database_constraints_protect_direct_writes(self):
        conn = connect(self.db)
        try:
            statements = [
                "UPDATE products SET stock = -1 WHERE product_id = 'hp_0001'",
                "UPDATE products SET price_cents = 1.5 WHERE product_id = 'hp_0001'",
                "UPDATE products SET currency = 'CNY' WHERE product_id = 'hp_0001'",
                "UPDATE products SET anc = 2 WHERE product_id = 'hp_0001'",
                "UPDATE products SET connection = 'fake' WHERE product_id = 'hp_0001'",
                "UPDATE products SET use_cases = '{}' WHERE product_id = 'hp_0001'",
                "UPDATE products SET use_cases = 'bad json' WHERE product_id = 'hp_0001'",
                "UPDATE products SET battery_hours = 2 WHERE product_id = 'hp_0002'",
            ]
            for statement in statements:
                with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(statement)
        finally:
            conn.close()

    def test_stock_requires_transaction_and_positive_integer(self):
        conn = connect(self.db)
        try:
            with self.assertRaises(RuntimeError):
                self.repo.decrease_stock("hp_0001", 1, conn)
            conn.execute("BEGIN IMMEDIATE")
            for value in (0, -1, 1.5, True):
                with self.assertRaises(DataValidationError):
                    self.repo.decrease_stock("hp_0001", value, conn)
            self.assertFalse(self.repo.decrease_stock("missing", 1, conn))
            self.assertFalse(self.repo.decrease_stock("hp_0001", 11, conn))
            self.assertEqual(self.repo.get_product("hp_0001", conn).stock, 10)
            self.assertTrue(conn.in_transaction)
            conn.rollback()
        finally:
            conn.close()

    def test_caller_connection_reads_own_changes_and_repository_never_commits(self):
        conn = connect(self.db)
        try:
            conn.execute("BEGIN IMMEDIATE")
            self.assertTrue(self.repo.decrease_stock("hp_0001", 1, conn))
            self.assertEqual(self.repo.get_product("hp_0001", conn).stock, 9)
            self.assertEqual(self.repo.get_product("hp_0001").stock, 10)
            found = self.repo.list_candidates(HardConstraints(), conn)
            self.assertEqual(next(p for p in found if p.product_id == "hp_0001").stock, 9)
            self.assertTrue(conn.in_transaction)
            conn.rollback()
        finally:
            conn.close()
        self.assertEqual(self.repo.get_product("hp_0001").stock, 10)

    def test_plain_sqlite_connection_supported(self):
        conn = sqlite3.connect(self.db, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            self.assertTrue(self.repo.decrease_stock("hp_0001", 1, conn))
            self.assertEqual(self.repo.get_product("hp_0001", conn).stock, 9)
            self.assertTrue(self.repo.list_candidates(HardConstraints(), conn))
            conn.rollback()
        finally:
            conn.close()

    def test_wallet_and_inventory_can_rollback_together(self):
        conn = connect(self.db)
        try:
            conn.execute("CREATE TABLE fixture_wallet(user_id TEXT PRIMARY KEY, balance_cents INTEGER NOT NULL)")
            conn.execute("INSERT INTO fixture_wallet VALUES ('demo_user', 100000)")
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("UPDATE fixture_wallet SET balance_cents = balance_cents - 29900")
                self.assertTrue(self.repo.decrease_stock("hp_0001", 1, conn))
                raise RuntimeError("Injected failure after wallet and inventory updates")
            except RuntimeError:
                conn.rollback()
            self.assertEqual(conn.execute("SELECT balance_cents FROM fixture_wallet").fetchone()[0], 100000)
            self.assertEqual(self.repo.get_product("hp_0001", conn).stock, 10)
        finally:
            conn.close()

    def test_concurrent_last_item_has_one_success(self):
        conn = connect(self.db)
        conn.execute("UPDATE products SET stock = 1 WHERE product_id = 'hp_0001'")
        conn.close()
        barrier = threading.Barrier(2)
        def purchase():
            conn = connect(self.db)
            try:
                barrier.wait(timeout=10)
                conn.execute("BEGIN IMMEDIATE")
                success = self.repo.decrease_stock("hp_0001", 1, conn)
                conn.commit()
                return success
            finally:
                conn.close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: purchase(), range(2)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.repo.get_product("hp_0001").stock, 0)

    def test_foreign_keys_and_readonly_connections(self):
        conn = connect(self.db)
        try:
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            conn.execute("CREATE TABLE fixture_orders(product_id TEXT REFERENCES products(product_id))")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO fixture_orders VALUES ('missing')")
        finally:
            conn.close()
        conn = connect(self.db, readonly=True)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("UPDATE products SET stock = 0")
        finally:
            conn.close()

    def test_c_hooks_and_reinit_preserve_wallet(self):
        def schema(conn):
            conn.execute("CREATE TABLE IF NOT EXISTS fixture_wallet(user_id TEXT PRIMARY KEY, balance_cents INTEGER)")
        def wallet(conn):
            conn.execute("INSERT INTO fixture_wallet VALUES ('demo_user', 100000) ON CONFLICT(user_id) DO NOTHING")
        initialize_database(self.db, commerce_schema=schema, wallet_initializer=wallet)
        conn = connect(self.db)
        conn.execute("UPDATE fixture_wallet SET balance_cents = 70100")
        conn.close()
        initialize_database(self.db, commerce_schema=schema, wallet_initializer=wallet)
        conn = connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT balance_cents FROM fixture_wallet").fetchone()[0], 70100)
        finally:
            conn.close()

    def test_failed_c_hook_rolls_back_initialization(self):
        target = self.root / "failed-hook.sqlite3"
        def bad_hook(conn):
            conn.execute("CREATE TABLE fixture_wallet(id TEXT)")
            raise RuntimeError("C hook failure")
        with self.assertRaises(RuntimeError):
            initialize_database(target, commerce_schema=bad_hook)
        conn = connect(target)
        try:
            tables = list(conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
            self.assertEqual(tables, [])
        finally:
            conn.close()

    def test_reset_backs_up_and_restores_seed_inventory(self):
        conn = connect(self.db)
        conn.execute("UPDATE products SET stock = 3 WHERE product_id = 'hp_0001'")
        conn.close()
        result = initialize_database(self.db, reset=True)
        self.assertEqual(self.repo.get_product("hp_0001").stock, 10)
        self.assertTrue(Path(result["backup_path"]).exists())
        self.assertEqual(ProductRepository(result["backup_path"]).get_product("hp_0001").stock, 3)

    def test_reset_refuses_c_business_tables(self):
        conn = connect(self.db)
        conn.execute("CREATE TABLE fixture_wallet(user_id TEXT, balance_cents INTEGER)")
        conn.execute("INSERT INTO fixture_wallet VALUES ('demo_user', 70100)")
        conn.close()
        with self.assertRaisesRegex(RuntimeError, "Reset blocked"):
            initialize_database(self.db, reset=True)
        conn = connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT balance_cents FROM fixture_wallet").fetchone()[0], 70100)
        finally:
            conn.close()

    def test_schema_version_conflict_does_not_overwrite(self):
        conn = connect(self.db)
        conn.execute("UPDATE schema_version SET version = 99")
        conn.close()
        self.assertFalse(database_status(self.db)["db_ready"])
        with self.assertRaises(RuntimeError):
            initialize_database(self.db)
        conn = connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT version FROM schema_version").fetchone()[0], 99)
        finally:
            conn.close()

    def test_db_path_environment_and_relative_resolution(self):
        with patch.dict(os.environ, {"DEMO_DB_PATH": str(self.root / "configured.sqlite3")}):
            initialize_database()
            self.assertTrue(database_status()["db_ready"])
            self.assertEqual(resolve_db_path(), (self.root / "configured.sqlite3").resolve())
        self.assertEqual(resolve_db_path("var/relative.sqlite3"), PROJECT_ROOT / "var" / "relative.sqlite3")

    def test_reading_missing_database_does_not_create_file(self):
        path = self.root / "missing.sqlite3"
        self.assertFalse(database_status(path)["db_ready"])
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
