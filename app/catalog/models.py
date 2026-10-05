"""D-local models, pending integration with A's shared HKD contracts."""

from dataclasses import MISSING, asdict, dataclass, field, fields
import math
from collections.abc import Mapping
from urllib.parse import urlparse


USE_CASES = {"commute", "study", "gaming", "sports", "calls", "music"}
CONNECTIONS = {"wired", "wireless"}
FORM_FACTORS = {"in_ear", "over_ear", "open_ear"}


class DataValidationError(ValueError):
    pass


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise DataValidationError(f"{name}: expected integer >= {minimum}")


def _number(value, name, *, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise DataValidationError(f"{name}: expected a finite number")
    if (positive and value <= 0) or (not positive and value < 0):
        raise DataValidationError(f"{name}: invalid range")


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise DataValidationError(f"{name}: expected nonempty text")


def _enum(value, name, allowed):
    if not isinstance(value, str) or value not in allowed:
        raise DataValidationError(f"{name}: expected one of {sorted(allowed)}")


def _exact_mapping(value, model):
    if not isinstance(value, Mapping):
        raise DataValidationError("Expected a JSON object")
    expected = {field.name for field in fields(model)}
    required = {f.name for f in fields(model) if f.default is MISSING and f.default_factory is MISSING}
    missing, extra = required - value.keys(), value.keys() - expected
    if missing or extra:
        raise DataValidationError(f"Missing fields: {sorted(missing)}; extra fields: {sorted(extra)}")
    return dict(value)


@dataclass
class Product:
    product_id: str
    category: str
    name: str
    brand: str
    model: str
    variant: str
    connection: str
    form_factor: str
    price_cents: int
    currency: str
    stock: int
    anc: bool | None
    battery_hours: float | None
    wearing_weight_g: float | None
    use_cases: list[str]
    source_type: str
    source_url: str | None
    data_note: str
    seller_description: str
    shipping_origin: str
    supported_devices: list[str] | None = None
    color: str | None = None
    tags: list[str] = field(default_factory=list)
    estimated_delivery_days: int | None = None

    def __post_init__(self):
        if self.supported_devices is not None:
            if not isinstance(self.supported_devices, list) or len(set(self.supported_devices)) != len(self.supported_devices):
                raise DataValidationError("supported_devices: expected unique array or null")
            for value in self.supported_devices:
                _enum(value, "supported_devices", {"phone", "computer", "tablet", "game_console"})
        if self.color is not None:
            _enum(self.color, "color", {"black", "white", "blue", "pink", "silver", "grey", "green", "red", "purple", "gold"})
        if not isinstance(self.tags, list) or any(not isinstance(t, str) or not t.strip() for t in self.tags) or len(set(self.tags)) != len(self.tags):
            raise DataValidationError("tags: expected unique nonempty strings")
        if self.estimated_delivery_days is not None:
            _integer(self.estimated_delivery_days, "estimated_delivery_days")
        for name in ("product_id", "name", "brand", "model", "variant", "data_note",
                     "seller_description", "shipping_origin"):
            _text(getattr(self, name), name)
        _enum(self.category, "category", {"headphones"})
        _enum(self.connection, "connection", CONNECTIONS)
        _enum(self.form_factor, "form_factor", FORM_FACTORS)
        _enum(self.currency, "currency", {"HKD"})
        _enum(self.source_type, "source_type", {"demo", "real_manual", "external"})
        _integer(self.price_cents, "price_cents")
        _integer(self.stock, "stock")
        if self.anc is not None and type(self.anc) is not bool:
            raise DataValidationError("anc: expected true, false or null")
        if self.battery_hours is not None:
            _number(self.battery_hours, "battery_hours")
        if self.connection == "wired" and self.battery_hours is not None:
            raise DataValidationError("battery_hours: wired products must use null")
        if self.wearing_weight_g is not None:
            _number(self.wearing_weight_g, "wearing_weight_g", positive=True)
        if not isinstance(self.use_cases, list):
            raise DataValidationError("use_cases: expected an array")
        for value in self.use_cases:
            _enum(value, "use_cases", USE_CASES)
        if len(set(self.use_cases)) != len(self.use_cases):
            raise DataValidationError("use_cases: duplicate entries")
        if len(self.seller_description) >= 100:
            raise DataValidationError("seller_description: must contain fewer than 100 characters")
        if self.source_url is not None:
            _text(self.source_url, "source_url")
            url = urlparse(self.source_url)
            if url.scheme not in {"http", "https"} or not url.netloc:
                raise DataValidationError("source_url: expected an HTTP(S) URL")
        if self.source_type == "demo" and self.source_url is not None:
            raise DataValidationError("source_url: demo products must use null")

    @classmethod
    def from_mapping(cls, value):
        return cls(**_exact_mapping(value, cls))

    def as_dict(self):
        return asdict(self)


@dataclass
class HardConstraints:
    category: str = "headphones"
    min_price_cents: int | None = None
    max_price_cents: int | None = None
    brand_allowlist: list[str] = field(default_factory=list)
    connection: str | None = None
    form_factor: str | None = None
    anc_required: bool = False
    min_battery_hours: float | None = None
    max_wearing_weight_g: float | None = None
    in_stock_only: bool = True
    color: str | None = None
    required_device: str | None = None
    tags: list[str] = field(default_factory=list)
    max_estimated_delivery_days: int | None = None

    def __post_init__(self):
        _enum(self.category, "category", {"headphones"})
        if self.color is not None:
            _enum(self.color, "color", {"black", "white", "blue", "pink", "silver", "grey", "green", "red", "purple", "gold"})
        if self.required_device is not None:
            _enum(self.required_device, "required_device", {"phone", "computer", "tablet", "game_console"})
        if not isinstance(self.tags, list) or any(not isinstance(t, str) or not t.strip() for t in self.tags) or len(set(self.tags)) != len(self.tags):
            raise DataValidationError("tags: expected unique nonempty strings")
        if self.max_estimated_delivery_days is not None:
            _integer(self.max_estimated_delivery_days, "max_estimated_delivery_days")
        for name in ("min_price_cents", "max_price_cents"):
            if getattr(self, name) is not None:
                _integer(getattr(self, name), name)
        if (self.min_price_cents is not None and self.max_price_cents is not None
                and self.min_price_cents > self.max_price_cents):
            raise DataValidationError("INVALID_CONSTRAINTS: minimum price exceeds maximum")
        if not isinstance(self.brand_allowlist, list):
            raise DataValidationError("brand_allowlist: expected an array")
        for brand in self.brand_allowlist:
            _text(brand, "brand_allowlist")
        if len(set(self.brand_allowlist)) != len(self.brand_allowlist):
            raise DataValidationError("brand_allowlist: duplicate entries")
        if self.connection is not None:
            _enum(self.connection, "connection", CONNECTIONS)
        if self.form_factor is not None:
            _enum(self.form_factor, "form_factor", FORM_FACTORS)
        for name in ("anc_required", "in_stock_only"):
            if type(getattr(self, name)) is not bool:
                raise DataValidationError(f"{name}: expected a boolean")
        if self.min_battery_hours is not None:
            _number(self.min_battery_hours, "min_battery_hours")
        if self.max_wearing_weight_g is not None:
            _number(self.max_wearing_weight_g, "max_wearing_weight_g", positive=True)

    @classmethod
    def from_mapping(cls, value):
        return cls(**_exact_mapping(value, cls))

    def as_dict(self):
        return asdict(self)

    @classmethod
    def coerce(cls, value):
        if isinstance(value, cls):
            # Revalidate even if a caller mutated an existing dataclass.
            return cls.from_mapping(value.as_dict())
        if isinstance(value, Mapping):
            return cls.from_mapping(value)
        if hasattr(value, "model_dump"):
            return cls.from_mapping(value.model_dump())
        raise DataValidationError("Expected HardConstraints, mapping or shared Pydantic model")
