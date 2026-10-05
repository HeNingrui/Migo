"""Product and HardConstraints contracts.

Member A owns this file. It is the shared product type that D's repository can
return (``ProductRepository(product_model=Product)``) and that B and C consume.

The integrated v2.1 field set contains the original 20 HKD catalog fields plus
optional ``supported_devices``, ``color``, ``tags`` and ``estimated_delivery_days``.
Original seed files remain valid; the catalog v2 migration adds SQL columns.

Money is always an integer number of minor units (hundredths). ``29900`` means
HK$299.00.
"""

from __future__ import annotations

import json
import math
from typing import Any, ClassVar, Literal, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

CATEGORIES = ("headphones",)
CONNECTIONS = ("wired", "wireless")
FORM_FACTORS = ("in_ear", "over_ear", "open_ear")
USE_CASES = ("commute", "study", "gaming", "sports", "calls", "music")
SOURCE_TYPES = ("demo", "real_manual", "external")
CURRENCIES = ("HKD",)

#: What a product can be plugged into. New in this round, and deliberately a
#: **separate axis from ``connection``**: ``connection`` is how the headset links
#: (wired or wireless), while this is what it links *to* (a phone, a computer, a
#: console). "Wireless" does not imply "works with a games console", so one
#: field cannot answer the other's question.
#:
#: Absent means unknown, never "works with everything": a requirement for a
#: device that a product does not declare cannot be satisfied by silence, which
#: is the same rule ``anc = null`` already follows.
DEVICES = ("phone", "computer", "game_console", "tablet")

Category = Literal["headphones"]
Connection = Literal["wired", "wireless"]
FormFactor = Literal["in_ear", "over_ear", "open_ear"]
UseCase = Literal["commute", "study", "gaming", "sports", "calls", "music"]
SourceType = Literal["demo", "real_manual", "external"]
Currency = Literal["HKD"]
Device = Literal["phone", "computer", "game_console", "tablet"]

_SELLER_DESCRIPTION_MAX = 99  # fewer than 100 characters, per D's schema


class Product(BaseModel):
    """One purchasable SKU.

    Unknown specs stay ``null``. They are never invented and never treated as
    ``0`` or ``False`` -- an unknown ``anc`` does not satisfy a mandate that
    requires active noise cancelling.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    product_id: str = Field(min_length=1)
    category: Category

    name: str = Field(min_length=1)
    brand: str = Field(min_length=1)
    model: str = Field(min_length=1)
    variant: str = Field(min_length=1)

    connection: Connection
    form_factor: FormFactor

    price_cents: StrictInt = Field(
        ge=0, description="Integer hundredths of HKD; 29900 means HK$299.00. Never a float."
    )
    currency: Currency
    stock: StrictInt = Field(ge=0)

    anc: bool | None = None
    battery_hours: float | None = Field(default=None, ge=0)
    wearing_weight_g: float | None = Field(default=None, gt=0)
    use_cases: list[UseCase] = Field(default_factory=list)

    #: What this product can be plugged into. ``None`` means **not recorded**,
    #: and a not-recorded device list never satisfies a requirement that names a
    #: device -- "we did not write it down" is not "it supports yours".
    #:
    #: The catalog does not populate this yet (see
    #: ``docs/A_C_contract_changes.md`` CC-10): the field exists so a requirement
    #: can be *checked* rather than assumed, and the check fails closed until D
    #: records the data.
    supported_devices: list[Device] | None = None

    color: str | None = None
    tags: list[str] = Field(default_factory=list)
    estimated_delivery_days: StrictInt | None = Field(default=None, ge=0)

    source_type: SourceType
    source_url: str | None = None
    data_note: str = Field(min_length=1)

    seller_description: str = Field(min_length=1, max_length=_SELLER_DESCRIPTION_MAX)
    shipping_origin: str = Field(min_length=1)

    # -- validators --------------------------------------------------------

    @field_validator("use_cases")
    @classmethod
    def _unique_use_cases(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("use_cases: duplicate entries")
        return value

    @field_validator("supported_devices")
    @classmethod
    def _unique_devices(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if len(set(value)) != len(value):
            raise ValueError("supported_devices: duplicate entries")
        return value

    @field_validator("source_url")
    @classmethod
    def _http_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith(("http://", "https://")):
            raise ValueError("source_url: expected an HTTP(S) URL")
        return value

    @field_validator("seller_description")
    @classmethod
    def _seller_description_length(cls, value: str) -> str:
        # Mirrors the database CHECK: length < 100.
        if len(value) >= 100:
            raise ValueError("seller_description: must contain fewer than 100 characters")
        return value

    @field_validator("battery_hours", "wearing_weight_g", mode="before")
    @classmethod
    def _finite_numbers(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("expected a finite number")
        if not math.isfinite(value):
            raise ValueError("expected a finite number")
        return float(value)

    @model_validator(mode="after")
    def _cross_field_rules(self) -> "Product":
        if self.connection == "wired" and self.battery_hours is not None:
            raise ValueError("battery_hours: wired products must use null")
        if self.source_type == "demo" and self.source_url is not None:
            raise ValueError("source_url: demo products must use null")
        return self

    # -- helpers -----------------------------------------------------------

    @property
    def price_hkd(self) -> float:
        """Display-only convenience. Never use for arithmetic."""
        return self.price_cents / 100

    @property
    def anc_is_known(self) -> bool:
        return self.anc is not None

    def to_row(self) -> dict[str, Any]:
        """Flatten into the exact column set of D's ``products`` table.

        ``use_cases`` becomes a JSON text and ``anc`` an integer, matching the
        STRICT table's CHECK constraints. Device compatibility and tags become JSON text under catalog schema v2.
        """
        data = self.model_dump(mode="json")
        data["anc"] = None if self.anc is None else int(self.anc)
        data["use_cases"] = json.dumps(list(self.use_cases), ensure_ascii=False)
        data["supported_devices"] = json.dumps(self.supported_devices) if self.supported_devices is not None else None
        data["tags"] = json.dumps(self.tags)
        return data


class HardConstraints(BaseModel):
    """Hard filters. A product violating any of these is eliminated.

    Field names, order and semantics match D's dataclass so that
    ``HardConstraints.coerce()`` in ``app.catalog.models`` accepts this model.
    """

    model_config = ConfigDict(extra="forbid")

    category: Category = "headphones"
    min_price_cents: StrictInt | None = Field(default=None, ge=0)
    max_price_cents: StrictInt | None = Field(default=None, ge=0)
    brand_allowlist: list[str] = Field(default_factory=list)
    connection: Connection | None = None
    form_factor: FormFactor | None = None
    anc_required: bool = False
    min_battery_hours: float | None = Field(default=None, ge=0)
    max_wearing_weight_g: float | None = Field(default=None, gt=0)
    in_stock_only: bool = True
    color: str | None = None
    required_device: Device | None = None
    tags: list[str] = Field(default_factory=list)
    max_estimated_delivery_days: StrictInt | None = Field(default=None, ge=0)

    @field_validator("tags")
    @classmethod
    def _unique_tags(cls, value: list[str]) -> list[str]:
        if any(not t.strip() for t in value) or len(set(value)) != len(value):
            raise ValueError("tags: expected unique nonempty strings")
        return value

    @field_validator("color")
    @classmethod
    def _color(cls, value: str | None) -> str | None:
        if value is not None and value not in {"black", "white", "blue", "pink", "silver", "grey", "green", "red", "purple", "gold"}:
            raise ValueError("unsupported color")
        return value

    @field_validator("brand_allowlist")
    @classmethod
    def _unique_brands(cls, value: list[str]) -> list[str]:
        if any(not b.strip() for b in value):
            raise ValueError("brand_allowlist: entries must be nonempty")
        if len(set(value)) != len(value):
            raise ValueError("brand_allowlist: duplicate entries")
        return value

    @model_validator(mode="after")
    def _price_range(self) -> "HardConstraints":
        if (
            self.min_price_cents is not None
            and self.max_price_cents is not None
            and self.min_price_cents > self.max_price_cents
        ):
            raise ValueError("INVALID_CONSTRAINTS: minimum price exceeds maximum")
        return self

    # -- interoperability with D ------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        """Return raw values, not JSON-encoded ones.

        D's ``HardConstraints.coerce()`` calls ``as_dict()`` on dataclasses and
        then re-validates every field, so this must yield primitives (plain
        ``str``/``bool``/``int``), not enums.
        """
        return self.model_dump(mode="python")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "HardConstraints":
        return cls.model_validate(dict(value))

    # -- introspection -----------------------------------------------------

    #: Fields that only describe the category rather than a user's choice, plus
    #: the one constraint whose default is already the common case. They are not
    #: treated as "the user asked for this".
    _IMPLIED_DEFAULTS: ClassVar[frozenset[str]] = frozenset(
        {"category", "in_stock_only", "anc_required"}
    )

    def explicitly_set_fields(self) -> list[str]:
        """Fields the caller actually populated, in a stable order.

        Used to check that a comparison dimension is one the user had a reason
        to care about. ``model_fields_set`` is the right signal: it distinguishes
        "the user asked for wireless" from "wireless happens to be the default
        shape of the field", which a value comparison cannot.
        """
        return sorted(
            name
            for name in self.model_fields_set
            if name not in self._IMPLIED_DEFAULTS and getattr(self, name) is not None and getattr(self, name) != []
        )

    def asks_for(self, field: str) -> bool:
        """Whether this constraint expresses a real requirement on ``field``."""
        if field == "anc":
            return self.anc_required is True
        return field in self.explicitly_set_fields()


__all__ = [
    "CATEGORIES",
    "CONNECTIONS",
    "CURRENCIES",
    "Category",
    "Connection",
    "Currency",
    "DEVICES",
    "Device",
    "FORM_FACTORS",
    "FormFactor",
    "HardConstraints",
    "Product",
    "SOURCE_TYPES",
    "SourceType",
    "USE_CASES",
    "UseCase",
]
