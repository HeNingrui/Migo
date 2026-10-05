"""The catalog data contract: the seed, and the v4 package it came from.

Member D owns `data/`. These tests exist because that directory changed once
already without the code knowing: `products.seed.json` was deleted and replaced
by a SQLite authoring file, which broke collection for the whole suite. The rule
they enforce is the cheap version of that lesson -- **the import path the code
actually uses and the package D published must agree**, so a future edit to one
without the other fails here instead of in a demo.

The package is read from its archive rather than from an extracted copy. The
archive is the artefact D committed, it carries its own SHA-256 in
`data/catalog-v4-package.md`, and an extracted duplicate beside it is one more
file that can silently drift from the thing it was copied out of.
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = next((p for p in [HERE, *HERE.parents] if (p / "app").is_dir()), None)
if REPO_ROOT is not None and str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.catalog.seed import load_seed  # noqa: E402

DATA = REPO_ROOT / "data"
SEED = DATA / "products.seed.json"
PACKAGE = DATA / "headphone-catalog-v4-data.zip"
PACKAGE_NOTE = DATA / "catalog-v4-package.md"

#: The digest D published in the package note. A package that does not match it
#: is not the package this repository was verified against.
PACKAGE_SHA256 = "4f85f64eedf1a2e7eb4dc76d8c156b27ba70efcb7d8fcd2b9866f0d15a01bc7c"

#: Inside the archive.
SNAPSHOT_NAME = "headphone-database-catalog-v4.sqlite3"
PACKAGE_SEED_NAME = "data/products.seed.json"

PRODUCT_FIELDS = (
    "product_id", "category", "name", "brand", "model", "variant",
    "connection", "form_factor", "price_cents", "currency", "stock", "anc",
    "battery_hours", "wearing_weight_g", "use_cases", "source_type",
    "source_url", "data_note", "seller_description", "shipping_origin",
)


@pytest.fixture(scope="module")
def package() -> zipfile.ZipFile:
    archive = zipfile.ZipFile(PACKAGE)
    yield archive
    archive.close()


@pytest.fixture(scope="module")
def snapshot(package) -> sqlite3.Connection:
    """The package's SQLite snapshot, extracted to a temporary directory.

    Extracted rather than used in place because sqlite3 needs a real file, and
    temporary because a copy left in ``data/`` is the duplication this module
    exists to avoid.
    """
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / SNAPSHOT_NAME
        target.write_bytes(package.read(SNAPSHOT_NAME))
        connection = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        try:
            yield connection
        finally:
            connection.close()


def snapshot_rows(snapshot: sqlite3.Connection) -> dict[str, dict]:
    columns = [r[1] for r in snapshot.execute("PRAGMA table_info(products)")]
    rows: dict[str, dict] = {}
    for row in snapshot.execute("select * from products"):
        data = dict(zip(columns, row))
        data["use_cases"] = json.loads(data["use_cases"])
        data["anc"] = None if data["anc"] is None else bool(data["anc"])
        rows[data["product_id"]] = data
    return rows


class TestThePackageIsTheOneWeVerified:
    def test_the_archive_matches_its_published_digest(self):
        digest = hashlib.sha256(PACKAGE.read_bytes()).hexdigest()
        assert digest == PACKAGE_SHA256, (
            "the catalog package changed. Re-verify the seed against it before "
            "updating this digest, and update data/catalog-v4-package.md too."
        )

    def test_the_note_documents_the_same_digest(self):
        assert PACKAGE_SHA256 in PACKAGE_NOTE.read_text(encoding="utf-8")

    def test_it_carries_the_snapshot_the_seed_was_derived_from(self, package):
        assert SNAPSHOT_NAME in package.namelist()
        assert PACKAGE_SEED_NAME in package.namelist()

    def test_no_extracted_copy_is_kept_beside_it(self):
        loose = [p.name for p in DATA.glob("*.sqlite3")]
        assert loose == [], (
            "the SQLite snapshot lives inside the archive; a copy in data/ is a "
            f"second source of truth that can drift: {loose}"
        )


class TestTheSeedIsTheThingTheCodeLoads:
    def test_the_seed_exists_and_parses(self):
        """Without it nothing imports: ``initialize_database`` loads it eagerly."""
        assert SEED.is_file(), (
            f"{SEED.name} is the import path the application reads. If the "
            "catalog moves to another format, the loader has to move with it in "
            "the same change."
        )
        assert len(load_seed(SEED)) == 40

    def test_every_row_carries_exactly_the_fields_the_loader_needs(self):
        rows = json.loads(SEED.read_text(encoding="utf-8-sig"))
        assert isinstance(rows, list) and rows
        for index, row in enumerate(rows, start=1):
            assert tuple(row) == PRODUCT_FIELDS, f"row #{index} has the wrong fields"

    def test_prices_are_integer_minor_units_in_hkd(self):
        for product in load_seed(SEED):
            assert product.currency == "HKD"
            assert isinstance(product.price_cents, int)
            assert product.price_cents > 0


class TestTheSeedIsThePackageSeedVerbatim:
    """The strongest statement available: not "equivalent", but "the same"."""

    def test_values_are_identical_field_by_field(self, package):
        official = json.loads(package.read(PACKAGE_SEED_NAME).decode("utf-8"))
        official = official["products"] if isinstance(official, dict) else official
        mine = json.loads(SEED.read_text(encoding="utf-8-sig"))

        assert [tuple(r) for r in official] == [tuple(r) for r in mine], (
            "field order differs; the repository seed must stay a faithful copy"
        )
        official_by_id = {r["product_id"]: r for r in official}
        mine_by_id = {r["product_id"]: r for r in mine}
        assert set(official_by_id) == set(mine_by_id)
        for product_id, row in sorted(official_by_id.items()):
            assert row == mine_by_id[product_id], f"{product_id} differs"


class TestTheSnapshotAndTheSeedAgree:
    """D's snapshot is the source; the seed is what the code reads."""

    def test_every_product_matches_by_id_and_display_fields(self, snapshot):
        seed_rows = {p.product_id: p for p in load_seed(SEED)}
        authored = snapshot_rows(snapshot)
        assert set(seed_rows) == set(authored), (
            "the seed and the snapshot list different products"
        )
        for product_id, row in sorted(authored.items()):
            product = seed_rows[product_id]
            assert product.name == row["name"], f"{product_id}: name drifted"
            assert product.brand == row["brand"], f"{product_id}: brand drifted"

    def test_every_purchasable_fact_matches(self, snapshot):
        """The fields a decision is made from, checked value by value."""
        seed_rows = {p.product_id: p for p in load_seed(SEED)}
        for product_id, row in sorted(snapshot_rows(snapshot).items()):
            product = seed_rows[product_id]
            assert product.price_cents == row["price_cents"]
            assert product.stock == row["stock"]
            assert product.anc == row["anc"]
            assert product.connection == row["connection"]
            assert product.form_factor == row["form_factor"]
            assert list(product.use_cases) == row["use_cases"]


class TestWhatTheCatalogStillCannotAnswer:
    """Gaps that are D's to close, pinned so they cannot be forgotten."""

    def test_no_device_data_exists_yet(self, snapshot):
        """The device requirement is carried and checked, but unanswerable.

        ``Mandate.required_device`` is enforced by C against
        ``Quote.supported_devices``, which is copied from ``Product``, which the
        catalog does not populate. So a mandate that names a device refuses every
        purchase -- correct, and worth failing here if it silently changes.
        """
        columns = [r[1] for r in snapshot.execute("PRAGMA table_info(products)")]
        assert "supported_devices" not in columns, (
            "the catalog now records device support: wire it through "
            "app/catalog/models.py and Product, and update the CC-10 note in "
            "docs/A_C_contract_changes.md"
        )

    def test_the_package_carries_data_the_import_does_not_use(self, package):
        """Stated rather than silently dropped.

        The package ships reviews, review summaries, observations, a
        reviews-joined product file and a ground-truth evaluation set. None of it
        is imported: the products table is the only thing the current loader
        reads. The counts are asserted so their absence is a known state, not an
        oversight someone rediscovers later.
        """
        reviews = json.loads(package.read("data/reviews.seed.json").decode("utf-8"))
        summaries = json.loads(
            package.read("data/product-review-summaries.json").decode("utf-8"))
        observations = json.loads(
            package.read("data/observations.2026-10-03.json").decode("utf-8"))
        assert len(reviews) == 400
        assert len(summaries) == 40
        assert len(observations) == 8

    def test_manipulated_review_labels_are_kept_out_of_the_products(self, package):
        """D's rule: the evaluation labels must not reach a detection agent.

        The ground truth lives under ``evaluation/`` precisely so that it is not
        part of the data an agent would be handed. This asserts the separation
        still holds, because a future "just join them" change would quietly
        destroy the point of the evaluation set.
        """
        names = package.namelist()
        assert "evaluation/reviews.ground_truth.json" in names
        assert not [n for n in names if n.startswith("data/") and "ground_truth" in n]
        rows = json.loads(SEED.read_text(encoding="utf-8-sig"))
        blob = json.dumps(rows, ensure_ascii=False)
        for leak in ("ground_truth", "manipulated", "is_fake", "label"):
            assert leak not in blob, f"{leak!r} leaked into the product seed"


__all__ = [
    "TestThePackageIsTheOneWeVerified",
    "TestTheSeedIsThePackageSeedVerbatim",
    "TestTheSeedIsTheThingTheCodeLoads",
    "TestTheSnapshotAndTheSeedAgree",
    "TestWhatTheCatalogStillCannotAnswer",
]
