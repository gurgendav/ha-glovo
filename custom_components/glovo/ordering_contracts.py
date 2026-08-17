"""Strict bounded response contracts for read-only live account and catalog APIs."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from .ordering_models import ISO_4217_EXPONENTS

MAX_RESPONSE_BYTES: Final = 512_000
MAX_DEPTH: Final = 18
MAX_ADDRESSES: Final = 64
MAX_ADDRESS_FIELDS: Final = 24
MAX_PAYMENT_METHODS: Final = 32
MAX_BODY_ELEMENTS: Final = 200
MAX_CATALOG_PRODUCTS: Final = 200
MAX_OPTION_GROUPS: Final = 24
MAX_OPTIONS_PER_GROUP: Final = 64
MAX_STRING: Final = 500
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")

ADDRESS_KINDS = frozenset({"HOUSE", "APARTMENT", "OFFICE", "OTHER"})
ADDRESS_FIELD_TYPES = frozenset(
    {
        "STREET_NAME",
        "STREET_NUMBER",
        "ADDITIONAL_INFORMATION",
        "FLOOR_NUMBER",
        "DOOR_NUMBER",
        "BUILDING_NAME",
        "POSTAL_CODE",
        "STAIRCASE",
        "DISTRICT",
        "INTERSECTION",
        "PROVINCE",
    }
)
STORE_VIEW_TYPES = frozenset({"GRID_VIEW", "LIST_VIEW", "LEGACY_VIEW"})
MENU_LAYOUT_TYPES = frozenset(
    {"GRID_VIEW_LAYOUT", "LIST_VIEW_LAYOUT", "LEGACY_VIEW_LAYOUT"}
)
SUPPORTED_ITEMS_TYPES = frozenset({"FOOD", "GROCERIES", "RETAIL"})
RESTRICTED_ITEMS_TYPES = frozenset(
    {"ALCOHOL", "PHARMACY", "TOBACCO", "WEAPONS", "AGE_RESTRICTED"}
)
DIRECT_PRODUCT_TYPES = frozenset({"PRODUCT_TILE", "PRODUCT_ROW"})
DISPLAY_ONLY_ELEMENTS = frozenset(
    {"SECTION_HEADER", "IMAGE_BANNER", "TEXT_BANNER", "DIVIDER"}
)
DAYS = frozenset(
    {
        "MONDAY",
        "TUESDAY",
        "WEDNESDAY",
        "THURSDAY",
        "FRIDAY",
        "SATURDAY",
        "SUNDAY",
    }
)
PROMOTION_TYPES = frozenset({"PERCENTAGE", "FIXED", "BOGO"})


class ContractError(ValueError):
    """Fail-closed parser error with no provider data in its message."""

    def __init__(self, category: str = "schema") -> None:
        self.category = category
        super().__init__("live API response did not satisfy the approved contract")


@dataclass(frozen=True, slots=True, repr=False)
class CustomerIdentity:
    customer_id: int = field(repr=False)

    def __repr__(self) -> str:
        return "CustomerIdentity(<private>)"


@dataclass(frozen=True, slots=True, repr=False)
class AddressField:
    field_type: str = field(repr=False)
    value: str = field(repr=False)

    def __post_init__(self) -> None:
        field_type = _text(self.field_type, maximum=40)
        if field_type not in ADDRESS_FIELD_TYPES:
            _fail()
        object.__setattr__(self, "field_type", field_type)
        object.__setattr__(self, "value", _text(self.value, maximum=250, allow_empty=True))


@dataclass(frozen=True, slots=True, repr=False)
class AddressSnapshot:
    remote_id: int = field(repr=False)
    address_line: str = field(repr=False)
    details: str = field(repr=False)
    latitude: float = field(repr=False)
    longitude: float = field(repr=False)
    country_code: str = field(repr=False)
    city_code: str = field(repr=False)
    city_name: str = field(repr=False)
    kind: str = field(repr=False)
    tag: str | None = field(repr=False)
    fields: tuple[AddressField, ...] = field(repr=False)
    display_title: str | None = field(default=None, repr=False)
    display_subtitle: str | None = field(default=None, repr=False)
    is_live_saved_address: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "remote_id", _int(self.remote_id, minimum=1))
        object.__setattr__(self, "address_line", _text(self.address_line, maximum=500))
        object.__setattr__(self, "details", _text(self.details, maximum=500, allow_empty=True))
        object.__setattr__(self, "latitude", _number(self.latitude, minimum=-90, maximum=90))
        object.__setattr__(self, "longitude", _number(self.longitude, minimum=-180, maximum=180))
        object.__setattr__(self, "country_code", _text(self.country_code, maximum=3))
        object.__setattr__(self, "city_code", _text(self.city_code, maximum=20))
        object.__setattr__(self, "city_name", _text(self.city_name, maximum=100))
        kind = _text(self.kind, maximum=20)
        if kind not in ADDRESS_KINDS:
            _fail()
        object.__setattr__(self, "kind", kind)
        if self.tag is not None:
            object.__setattr__(self, "tag", _text(self.tag, maximum=80))
        if self.display_title is not None:
            object.__setattr__(
                self, "display_title", _text(self.display_title, maximum=80)
            )
        if self.display_subtitle is not None:
            object.__setattr__(
                self, "display_subtitle", _text(self.display_subtitle, maximum=160)
            )
        if not isinstance(self.is_live_saved_address, bool):
            _fail()
        if not isinstance(self.fields, tuple) or len(self.fields) > MAX_ADDRESS_FIELDS:
            _fail()
        if not all(isinstance(item, AddressField) for item in self.fields):
            _fail()
        if len({item.field_type for item in self.fields}) != len(self.fields):
            _fail()

    @property
    def canonical_fingerprint(self) -> str:
        canonical = {
            "id": self.remote_id,
            "addressLine": self.address_line,
            "details": self.details,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "countryCode": self.country_code,
            "cityCode": self.city_code,
            "cityName": self.city_name,
            "kind": self.kind,
            "tag": self.tag,
            "fields": [
                {"type": item.field_type, "value": item.value} for item in self.fields
            ],
        }
        encoded = json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def __repr__(self) -> str:
        return "AddressSnapshot(<private>)"


@dataclass(frozen=True, slots=True, repr=False)
class SavedPayment:
    payment_instrument_id: str = field(repr=False)
    metadata_id: int = field(repr=False)
    display_name: str = field(repr=False)
    display_description: str | None = field(repr=False)
    last_four_digits: str | None = field(repr=False)
    selected: bool = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payment_instrument_id", _opaque_id(self.payment_instrument_id))
        object.__setattr__(self, "metadata_id", _int(self.metadata_id, minimum=1))
        object.__setattr__(self, "display_name", _text(self.display_name, maximum=100))
        if self.display_description is not None:
            object.__setattr__(
                self,
                "display_description",
                _text(self.display_description, maximum=250, allow_empty=True),
            )
        if self.last_four_digits is not None:
            if not isinstance(self.last_four_digits, str) or not re.fullmatch(r"\d{1,4}", self.last_four_digits):
                _fail()
        if not isinstance(self.selected, bool):
            _fail()

    def __repr__(self) -> str:
        return "SavedPayment(<private>)"


@dataclass(frozen=True, slots=True)
class ExactMoney:
    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount_minor", _int(self.amount_minor, maximum=100_000_000_000))
        if not isinstance(self.currency, str) or self.currency not in ISO_4217_EXPONENTS:
            _fail()


@dataclass(frozen=True, slots=True, repr=False)
class StoreSchedule:
    day: str
    opening_time: str
    closing_time: str


@dataclass(frozen=True, slots=True, repr=False)
class LiveStore:
    store_id: int = field(repr=False)
    name: str
    slug: str = field(repr=False)
    address_id: int = field(repr=False)
    city_code: str = field(repr=False)
    rating: float | None
    category_id: int = field(repr=False)
    category: str
    prime_available: bool
    image_id: str | None = field(repr=False)
    view_type: str
    schedule: tuple[StoreSchedule, ...]
    scheduling_enabled: bool
    next_opening: str | None
    delivery_fee: ExactMoney | None
    service_fee: ExactMoney | None
    items_type: str
    is_open: bool = True


@dataclass(frozen=True, slots=True)
class CatalogOption:
    """Private provider option identity; public handles are issued separately."""

    key: str = field(repr=False)
    external_id: str = field(repr=False)
    label: str
    price: ExactMoney
    selected: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _opaque_id(self.key))
        object.__setattr__(self, "external_id", _opaque_id(self.external_id))
        object.__setattr__(self, "label", _text(self.label, maximum=100))
        if not isinstance(self.price, ExactMoney) or not isinstance(self.selected, bool):
            _fail()

    def public_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "priceCents": self.price.amount_minor,
            "currencyCode": self.price.currency,
            "selected": self.selected,
        }


@dataclass(frozen=True, slots=True)
class CatalogOptionGroup:
    """Private provider group identity; public handles are issued separately."""

    key: str = field(repr=False)
    external_id: str = field(repr=False)
    label: str
    minimum: int
    maximum: int
    position: int
    multiple_selection: bool
    collapsed: bool
    options: tuple[CatalogOption, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _opaque_id(self.key))
        object.__setattr__(self, "external_id", _opaque_id(self.external_id))
        object.__setattr__(self, "label", _text(self.label, maximum=100))
        minimum = _int(self.minimum, maximum=MAX_OPTIONS_PER_GROUP)
        maximum = _int(self.maximum, maximum=MAX_OPTIONS_PER_GROUP)
        position = _int(self.position, maximum=MAX_OPTION_GROUPS)
        if (
            not isinstance(self.multiple_selection, bool)
            or not isinstance(self.collapsed, bool)
            or not isinstance(self.options, tuple)
            or not self.options
            or len(self.options) > MAX_OPTIONS_PER_GROUP
            or not all(isinstance(item, CatalogOption) for item in self.options)
            or len({item.key for item in self.options}) != len(self.options)
            or len({item.external_id for item in self.options}) != len(self.options)
            or minimum > maximum
            or maximum > len(self.options)
            or (not self.multiple_selection and maximum > 1)
        ):
            _fail()
        object.__setattr__(self, "minimum", minimum)
        object.__setattr__(self, "maximum", maximum)
        object.__setattr__(self, "position", position)

    def public_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "min": self.minimum,
            "max": self.maximum,
            "position": self.position,
            "multipleSelection": self.multiple_selection,
            "collapsed": self.collapsed,
            "options": [item.public_dict() for item in self.options],
        }


@dataclass(frozen=True, slots=True)
class CatalogPromotion:
    promotion_id: str
    promotion_type: str
    label: str


@dataclass(frozen=True, slots=True, repr=False)
class CatalogProduct:
    product_id: str = field(repr=False)
    external_id: str = field(repr=False)
    store_product_id: str | None = field(repr=False)
    name: str
    price: ExactMoney
    sponsored: bool
    option_groups: tuple[CatalogOptionGroup, ...]
    promotions: tuple[CatalogPromotion, ...] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "product_id", _opaque_id(self.product_id))
        object.__setattr__(self, "external_id", _opaque_id(self.external_id))
        if self.store_product_id is not None:
            object.__setattr__(self, "store_product_id", _opaque_id(self.store_product_id))
        object.__setattr__(self, "name", _text(self.name, maximum=120))
        if (
            not isinstance(self.price, ExactMoney)
            or not isinstance(self.sponsored, bool)
            or not isinstance(self.option_groups, tuple)
            or not all(isinstance(item, CatalogOptionGroup) for item in self.option_groups)
            or len({item.key for item in self.option_groups}) != len(self.option_groups)
            or len({item.position for item in self.option_groups}) != len(self.option_groups)
            or not isinstance(self.promotions, tuple)
            or not all(isinstance(item, CatalogPromotion) for item in self.promotions)
        ):
            _fail()

    def public_dict(self) -> dict[str, Any]:
        return {
            "key": self.product_id,
            "label": self.name,
            "priceCents": self.price.amount_minor,
            "currencyCode": self.price.currency,
            "sponsored": self.sponsored,
            "optionGroups": [item.public_dict() for item in self.option_groups],
        }


@dataclass(frozen=True, slots=True, repr=False)
class CatalogMenu:
    layout_type: str
    store_address_id: int = field(repr=False)
    products: tuple[CatalogProduct, ...]


def _fail(category: str = "schema") -> None:
    raise ContractError(category)


def _object(
    value: Any,
    *,
    required: frozenset[str] | set[str] | None = None,
    allowed: frozenset[str] | set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail()
    keys = set(value)
    if required is not None and not set(required).issubset(keys):
        _fail()
    if allowed is not None and not keys.issubset(set(allowed)):
        _fail()
    return value


def _array(value: Any, *, maximum: int) -> list[Any]:
    if not isinstance(value, list) or len(value) > maximum:
        _fail()
    return value


def _text(value: Any, *, maximum: int = MAX_STRING, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        _fail()
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        _fail()
    normalized = " ".join(value.split())
    if not allow_empty and not normalized:
        _fail()
    return normalized


def _opaque_id(value: Any) -> str:
    text = _text(value, maximum=128)
    if not _ID_RE.fullmatch(text):
        _fail()
    return text


def _int(value: Any, *, minimum: int = 0, maximum: int = 2_147_483_647) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail()
    if not minimum <= value <= maximum:
        _fail()
    return value


def _number(value: Any, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail()
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        _fail()
    return number


def _bool(value: Any) -> bool:
    if not isinstance(value, bool):
        _fail()
    return value


def _bounded_payload(value: Any, *, maximum: int = MAX_RESPONSE_BYTES) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError):
        _fail()
    if len(encoded.encode()) > maximum:
        _fail()

    def walk(item: Any, depth: int) -> None:
        if depth > MAX_DEPTH:
            _fail()
        if isinstance(item, dict):
            if len(item) > MAX_BODY_ELEMENTS:
                _fail()
            for key, child in item.items():
                _text(key, maximum=100)
                walk(child, depth + 1)
        elif isinstance(item, list):
            if len(item) > MAX_BODY_ELEMENTS * 2:
                _fail()
            for child in item:
                walk(child, depth + 1)
        elif isinstance(item, str):
            if len(item) > MAX_STRING * 2:
                _fail()
        elif isinstance(item, float) and not math.isfinite(item):
            _fail()

    walk(value, 0)


def _envelope(payload: Any, leaf: str) -> Any:
    root = _object(payload, required={"data"}, allowed={"data"})
    outer = _object(root["data"], required={"data"}, allowed={"data"})
    inner = _object(outer["data"], required={leaf}, allowed={leaf})
    return inner[leaf]


def parse_customer(payload: Any) -> CustomerIdentity:
    _bounded_payload(payload, maximum=16_000)
    if not isinstance(payload, dict) or "id" not in payload:
        _fail()
    # /v3/me is a full customer profile whose unrelated fields evolve over
    # time.  Only the response-derived positive integer ID is authoritative
    # for ordering; the bounded profile remainder is deliberately ignored and
    # never projected, persisted, or logged.
    return CustomerIdentity(_int(payload["id"], minimum=1))


def parse_saved_addresses(payload: Any) -> tuple[AddressSnapshot, ...]:
    _bounded_payload(payload)
    root = _object(payload, required={"data"}, allowed={"data"})
    outer_raw = root["data"]
    if not isinstance(outer_raw, dict):
        _fail()
    address_items: Any = None
    live_shape = set(outer_raw) == {"addresses"}
    legacy_shape = set(outer_raw) == {"data"}
    if live_shape:
        outer = _object(outer_raw, required={"addresses"}, allowed={"addresses"})
        address_items = outer["addresses"]
    elif legacy_shape:
        outer = _object(outer_raw, required={"data"}, allowed={"data"})
        inner = _object(
            outer["data"], required={"addresses"}, allowed={"addresses"}
        )
        address_items = inner["addresses"]
    else:
        _fail()
    addresses = _array(address_items, maximum=MAX_ADDRESSES)
    parsed: list[AddressSnapshot] = []
    ids: set[int] = set()
    required = {
        "id",
        "addressLine",
        "details",
        "latitude",
        "longitude",
        "countryCode",
        "cityCode",
        "cityName",
        "kind",
        "tag",
        "fields",
    }
    live_row_fields = {
        "entryType",
        "title",
        "subtitle",
        "coachmark",
        "editIcon",
        "icon",
        "notice",
        "redirectOnTap",
        "address",
    }
    live_address_fields = required | {
        "faulty",
        "originalLatitude",
        "originalLongitude",
    }
    for item in addresses:
        display_title: str | None = None
        display_subtitle: str | None = None
        if live_shape:
            row = _object(
                item,
                required={"entryType", "address"},
                allowed=live_row_fields,
            )
            address = _object(
                row["address"],
                required=required,
                allowed=live_address_fields,
            )
            if "title" in row and row["title"] is not None:
                candidate_title = _text(
                    row["title"], maximum=80, allow_empty=True
                )
                display_title = candidate_title or None
            if "subtitle" in row and row["subtitle"] is not None:
                candidate_subtitle = _text(
                    row["subtitle"], maximum=160, allow_empty=True
                )
                display_subtitle = candidate_subtitle or None
            if "faulty" in address and address["faulty"] is not None:
                try:
                    _bool(address["faulty"])
                except ContractError:
                    raise ContractError("saved_address_metadata") from None
            if "originalLatitude" in address and address["originalLatitude"] is not None:
                _number(address["originalLatitude"], minimum=-90, maximum=90)
            if "originalLongitude" in address and address["originalLongitude"] is not None:
                _number(address["originalLongitude"], minimum=-180, maximum=180)
        else:
            row = _object(
                item,
                required={"entryType", "entry"},
                allowed={"entryType", "entry"},
            )
            entry = _object(row["entry"], required={"address"}, allowed={"address"})
            address = _object(entry["address"], required=required, allowed=required)
        if row["entryType"] != "SAVED_ADDRESS":
            _fail()
        try:
            remote_id = _int(address["id"], minimum=1)
        except ContractError:
            raise ContractError("saved_address_identity") from None
        if remote_id in ids:
            _fail()
        ids.add(remote_id)
        try:
            kind = _text(address["kind"], maximum=20)
            if kind not in ADDRESS_KINDS:
                _fail()
        except ContractError:
            raise ContractError("saved_address_kind") from None
        tag = address["tag"]
        if tag is not None:
            tag = _text(tag, maximum=80)
        try:
            raw_fields = _array(address["fields"], maximum=MAX_ADDRESS_FIELDS)
            fields: list[AddressField] = []
            field_types: set[str] = set()
            for raw_field in raw_fields:
                field_keys = {"type", "value", "externalId"} if live_shape else {"type", "value"}
                field_data = _object(
                    raw_field,
                    required=field_keys,
                    allowed=field_keys,
                )
                if live_shape:
                    external_id = field_data["externalId"]
                    if external_id is None:
                        pass
                    elif isinstance(external_id, int) and not isinstance(external_id, bool):
                        _int(external_id, minimum=0)
                    else:
                        _opaque_id(external_id)
                field_type = _text(field_data["type"], maximum=40)
                if field_type not in ADDRESS_FIELD_TYPES or field_type in field_types:
                    _fail()
                field_types.add(field_type)
                fields.append(
                    AddressField(
                        field_type,
                        _text(field_data["value"], maximum=250, allow_empty=True),
                    )
                )
        except ContractError:
            raise ContractError("saved_address_fields") from None
        try:
            snapshot = AddressSnapshot(
                remote_id=remote_id,
                address_line=_text(address["addressLine"], maximum=500),
                details=_text(address["details"], maximum=500, allow_empty=True),
                latitude=_number(address["latitude"], minimum=-90, maximum=90),
                longitude=_number(address["longitude"], minimum=-180, maximum=180),
                country_code=_text(address["countryCode"], maximum=3),
                city_code=_text(address["cityCode"], maximum=20),
                city_name=_text(address["cityName"], maximum=100),
                kind=kind,
                tag=tag,
                fields=tuple(fields),
                display_title=display_title,
                display_subtitle=display_subtitle,
                is_live_saved_address=live_shape,
            )
        except ContractError:
            raise ContractError("saved_address_snapshot") from None
        parsed.append(snapshot)
    return tuple(parsed)


def address_snapshots_equal(first: AddressSnapshot, second: AddressSnapshot) -> bool:
    if not isinstance(first, AddressSnapshot) or not isinstance(second, AddressSnapshot):
        return False
    return first.canonical_fingerprint == second.canonical_fingerprint


def build_payment_query(
    *,
    amount_minor: int,
    currency: str,
    checkout_session: str | None = None,
    store_address_id: int | None = None,
) -> dict[str, str]:
    """Build the exact headless browser-fallback payment-method query."""

    amount = _int(amount_minor, maximum=100_000_000_000)
    if not isinstance(currency, str) or currency not in ISO_4217_EXPONENTS:
        _fail()
    query = {
        "amount": str(amount),
        "currency": currency,
        "clientSupports": "",
        "clientReady": "",
        "context": "checkout",
    }
    if checkout_session is not None:
        query["checkoutSessionId"] = _opaque_id(checkout_session)
    if store_address_id is not None:
        query["storeAddressId"] = str(_int(store_address_id, minimum=1))
    if len(json.dumps(query, separators=(",", ":"))) > 1_000:
        _fail()
    return query


def _contains_sensitive_payment_key(value: Any) -> bool:
    """Return whether a retained card object exposes raw payment material."""

    sensitive = {
        "cardnumber",
        "fullcardnumber",
        "pan",
        "cvv",
        "cvc",
        "cryptogram",
        "tokenization",
        "rawcard",
    }

    def walk(item: Any) -> bool:
        if isinstance(item, dict):
            for key, child in item.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
                if normalized in sensitive or walk(child):
                    return True
        elif isinstance(item, list):
            return any(walk(child) for child in item)
        return False

    return walk(value)


_PAYMENT_TYPES: Final = frozenset({"CREDIT_CARD", "CASH", "ALTERNATIVE"})
_ALTERNATIVE_PLATFORMS: Final = frozenset(
    {"KaspiDirect", "Edenred", "PayPal", "GooglePay", "ApplePay", "BLIK", "DotPay"}
)
_PAYMENT_MESSAGE_TYPES: Final = frozenset({"DISABLED", "ERROR", "NORMAL", "WARNING"})
_PAYMENT_ACTION_PROVIDERS: Final = frozenset({"ProcessOut", "DH"})


def _nullable_payment_text(value: Any, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _text(value, maximum=maximum, allow_empty=True)


def _validate_payment_display(
    value: Any, *, allow_message: bool
) -> tuple[str, str | None]:
    display = _object(value, required={"displayName", "icon", "tags"})
    name = _text(display["displayName"], maximum=100)
    description = _nullable_payment_text(display.get("description"), maximum=250)
    icon = _object(display["icon"], required={"lightImageId", "darkImageId"})
    _text(icon["lightImageId"], maximum=250)
    _text(icon["darkImageId"], maximum=250)
    for raw_tag in _array(display["tags"], maximum=16):
        tag = _object(raw_tag, required={"text", "style"})
        _text(tag["text"], maximum=100, allow_empty=True)
        _text(tag["style"], maximum=50)
    if allow_message and display.get("message") is not None:
        message = _object(display["message"], required={"text", "type"})
        _text(message["text"], maximum=250, allow_empty=True)
        message_type = message["type"]
        _text(message_type, maximum=20)
        if message_type not in _PAYMENT_MESSAGE_TYPES:
            _fail()
    return name, description


def _validate_payment_action(value: Any) -> None:
    action = _object(
        value,
        required={"type", "paymentMethod", "provider", "displayAttributes"},
    )
    _text(action["type"], maximum=80)
    _text(action["paymentMethod"], maximum=80)
    provider = action["provider"]
    _text(provider, maximum=20)
    if provider not in _PAYMENT_ACTION_PROVIDERS:
        _fail()
    _validate_payment_display(action["displayAttributes"], allow_message=True)
    metadata = action.get("metadata")
    if metadata is not None:
        metadata_obj = _object(metadata)
        for key in ("hostedPaymentPageUrl", "returnUrl", "resultEndpoint"):
            if key in metadata_obj:
                _nullable_payment_text(metadata_obj[key], maximum=2_000)


def parse_saved_payments(payload: Any) -> tuple[SavedPayment, ...]:
    """Validate the mixed first-party schema, then return eligible saved cards."""

    _bounded_payload(payload)
    # Axios ``response.data.data`` is one JSON envelope, not two.
    root = _object(payload, required={"data"})
    inner = _object(root["data"], required={"paymentMethods", "actions"})
    for action in _array(inner["actions"], maximum=8):
        _validate_payment_action(action)
    methods = _array(inner["paymentMethods"], maximum=MAX_PAYMENT_METHODS)
    result: list[SavedPayment] = []
    instrument_ids: set[str] = set()
    metadata_ids: set[int] = set()
    required = {
        "type",
        "selected",
        "metadata",
        "displayAttributes",
    }
    for item in methods:
        method = _object(item, required=required)
        method_type = method["type"]
        _text(method_type, maximum=40)
        if method_type not in _PAYMENT_TYPES:
            _fail()
        instrument_raw = method.get("paymentInstrumentId")
        if instrument_raw is not None:
            _text(instrument_raw, maximum=250, allow_empty=True)
        selected = _bool(method["selected"])
        display_name, display_description = _validate_payment_display(
            method["displayAttributes"], allow_message=True
        )
        metadata = _object(method["metadata"])

        if method_type == "CASH":
            if "maxAmount" not in metadata:
                _fail()
            _number(metadata["maxAmount"], minimum=-1_000_000_000, maximum=1_000_000_000)
            continue
        if method_type == "ALTERNATIVE":
            platform = metadata.get("platform")
            _text(platform, maximum=40)
            if platform not in _ALTERNATIVE_PLATFORMS:
                _fail()
            _nullable_payment_text(metadata.get("apmUser"), maximum=250)
            continue

        if _contains_sensitive_payment_key(method):
            _fail()
        metadata_raw = metadata.get("id")
        if metadata_raw is not None:
            _number(metadata_raw, minimum=-1_000_000_000, maximum=1_000_000_000)
        last_four_raw = metadata.get("lastFourDigits")
        if last_four_raw is not None:
            _text(last_four_raw, maximum=250, allow_empty=True)

        # A schema-valid card becomes headless payment authority only when its
        # private identifiers and masked suffix are exact and usable.
        try:
            instrument_id = _opaque_id(instrument_raw)
        except ContractError:
            continue
        if instrument_id != instrument_raw:
            continue
        if (
            isinstance(metadata_raw, bool)
            or not isinstance(metadata_raw, int)
            or metadata_raw < 1
            or not isinstance(last_four_raw, str)
            or re.fullmatch(r"\d{1,4}", last_four_raw) is None
        ):
            continue
        if instrument_id in instrument_ids or metadata_raw in metadata_ids:
            _fail()
        instrument_ids.add(instrument_id)
        metadata_ids.add(metadata_raw)
        result.append(
            SavedPayment(
                payment_instrument_id=instrument_id,
                metadata_id=metadata_raw,
                display_name=display_name,
                display_description=display_description,
                last_four_digits=last_four_raw,
                selected=selected,
            )
        )
    return tuple(result)


def _money(value: Any) -> ExactMoney:
    data = _object(value, required={"amount", "currency"}, allowed={"amount", "currency"})
    amount = _int(data["amount"], maximum=100_000_000_000)
    currency = data["currency"]
    if not isinstance(currency, str) or currency not in ISO_4217_EXPONENTS:
        _fail()
    return ExactMoney(amount, currency)


def _parse_legacy_store(payload: Any) -> LiveStore:
    _bounded_payload(payload)
    required = {
        "id",
        "name",
        "slug",
        "open",
        "rating",
        "filters",
        "categoryId",
        "category",
        "addressId",
        "cityCode",
        "enabled",
        "primeAvailable",
        "imageId",
        "viewType",
        "schedule",
        "schedulingEnabled",
        "nextOpening",
        "deliveryFeeInfo",
        "serviceFee",
        "itemsType",
    }
    root = _object(payload, required=required, allowed=required)
    is_open = _bool(root["open"])
    if _bool(root["enabled"]) is not True:
        _fail("ineligible")
    slug = _text(root["slug"], maximum=100)
    if not _SLUG_RE.fullmatch(slug):
        _fail()
    rating = root["rating"]
    if rating is not None:
        rating = _number(rating, minimum=0, maximum=5)
    filters = _array(root["filters"], maximum=32)
    parsed_filters = [_text(item, maximum=50) for item in filters]
    if len(set(parsed_filters)) != len(parsed_filters):
        _fail()
    view_type = _text(root["viewType"], maximum=30)
    if view_type not in STORE_VIEW_TYPES:
        _fail()
    items_type = _text(root["itemsType"], maximum=40)
    if items_type in RESTRICTED_ITEMS_TYPES or items_type not in SUPPORTED_ITEMS_TYPES:
        _fail("restricted")
    raw_schedule = _array(root["schedule"], maximum=14)
    schedule: list[StoreSchedule] = []
    seen_schedule: set[tuple[str, str, str]] = set()
    for item in raw_schedule:
        value = _object(
            item,
            required={"day", "openingTime", "closingTime"},
            allowed={"day", "openingTime", "closingTime"},
        )
        day = _text(value["day"], maximum=12)
        opening = _text(value["openingTime"], maximum=5)
        closing = _text(value["closingTime"], maximum=5)
        if day not in DAYS or not _TIME_RE.fullmatch(opening) or not _TIME_RE.fullmatch(closing):
            _fail()
        row = (day, opening, closing)
        if row in seen_schedule:
            _fail()
        seen_schedule.add(row)
        schedule.append(StoreSchedule(*row))
    next_opening = root["nextOpening"]
    if next_opening is not None:
        next_opening = _text(next_opening, maximum=80)
    fee_info = _object(
        root["deliveryFeeInfo"], required={"fee"}, allowed={"fee"}
    )
    return LiveStore(
        store_id=_int(root["id"], minimum=1),
        name=_text(root["name"], maximum=100),
        slug=slug,
        address_id=_int(root["addressId"], minimum=1),
        city_code=_text(root["cityCode"], maximum=20),
        rating=rating,
        category_id=_int(root["categoryId"], minimum=1),
        category=_text(root["category"], maximum=50),
        prime_available=_bool(root["primeAvailable"]),
        image_id=_opaque_id(root["imageId"]),
        view_type=view_type,
        schedule=tuple(schedule),
        scheduling_enabled=_bool(root["schedulingEnabled"]),
        next_opening=next_opening,
        delivery_fee=_money(fee_info["fee"]),
        service_fee=_money(root["serviceFee"]),
        items_type=items_type,
        is_open=is_open,
    )


_CURRENT_STORE_REQUIRED = frozenset(
    {
        "id", "name", "slug", "open", "rating", "filters", "categoryId",
        "category", "addressId", "cityCode", "enabled", "primeAvailable",
        "imageId", "viewType", "schedulingEnabled", "deliveryFeeInfo",
        "serviceFee", "itemsType", "food",
    }
)
_CURRENT_STORE_ALLOWED = _CURRENT_STORE_REQUIRED | frozenset(
    {
        "address", "adsTrackingToken", "allergiesInformationAllowed",
        "availability", "availabilityVM", "canDisplayPrimeTierUpsell",
        "cartTotalElements", "cartUniqueElements", "cashSupported",
        "closedStatusMessage", "customDescriptionAllowed", "cutleryRequestAllowed",
        "dataSharingRequested", "deliveryNotAvailable",
        "deliveryNotAvailableMessage", "description", "disabledStatus", "distance",
        "emulateOpen", "etaEnabled", "favorite", "feesPricingCalculationId",
        "fiscalName", "ghostStore", "lastOrderingTime", "legalCheckboxRequired",
        "location", "logoImageId", "marketplace", "mcdPartner", "nextOpeningTime",
        "note", "phoneNumber", "pricingInfo", "productsInformationLink",
        "productsInformationText", "promotions", "purchasesCommission",
        "rankingScore", "ratingInfo", "scheduleMessage", "schedulingPossible",
        "selectedStrategyType", "shopSponsoring", "shopSponsoringPlacement",
        "specialRequirementsAllowed", "sponsoringInfo", "storeDimensions",
        "storeViewVersion", "structuredData", "suggestionKeywords",
        "supportedStrategies", "tags", "topPerformer", "urn",
    }
)


def _parse_current_store(payload: Any) -> LiveStore:
    root = _object(
        payload,
        required=_CURRENT_STORE_REQUIRED,
        allowed=_CURRENT_STORE_ALLOWED,
    )
    is_open = _bool(root["open"])
    if not _bool(root["enabled"]):
        _fail("ineligible")
    if _text(root["category"], maximum=50) != "RESTAURANT" or not _bool(root["food"]):
        _fail("restricted")
    slug = _text(root["slug"], maximum=100)
    if not _SLUG_RE.fullmatch(slug):
        _fail()
    rating_value = root["rating"]
    rating: float | None = None
    if rating_value is not None:
        if not isinstance(rating_value, str) or not re.fullmatch(r"(?:100|[1-9]?\d)%", rating_value):
            _fail()
        rating = int(rating_value[:-1]) / 20
    filters = _array(root["filters"], maximum=32)
    for item in filters:
        value = _object(
            item,
            required={"displayName", "icon", "id", "name", "slug", "translations"},
            allowed={"displayName", "icon", "id", "name", "slug", "translations"},
        )
        _int(value["id"], minimum=1)
        _text(value["displayName"], maximum=80)
        _text(value["icon"], maximum=250, allow_empty=True)
        _text(value["name"], maximum=80)
        _text(value["slug"], maximum=100)
        if not isinstance(value["translations"], dict):
            _fail()
    view_type = _text(root["viewType"], maximum=30)
    if view_type not in MENU_LAYOUT_TYPES:
        _fail()
    items_type = _text(root["itemsType"], maximum=40)
    if items_type != "CATEGORIZED":
        _fail("restricted")
    fee_info = _object(
        root["deliveryFeeInfo"],
        required={"fee", "style"},
        allowed={"fee", "style"},
    )
    _number(fee_info["fee"], minimum=0, maximum=1_000_000)
    _text(fee_info["style"], maximum=30)
    _number(root["serviceFee"], minimum=0, maximum=1_000_000)
    _text(root["imageId"], maximum=250, allow_empty=True)
    return LiveStore(
        store_id=_int(root["id"], minimum=1),
        name=_text(root["name"], maximum=100),
        slug=slug,
        address_id=_int(root["addressId"], minimum=1),
        city_code=_text(root["cityCode"], maximum=20),
        rating=rating,
        category_id=_int(root["categoryId"], minimum=1),
        category="RESTAURANT",
        prime_available=_bool(root["primeAvailable"]),
        image_id=None,
        view_type=view_type,
        schedule=(),
        scheduling_enabled=_bool(root["schedulingEnabled"]),
        next_opening=None,
        delivery_fee=None,
        service_fee=None,
        items_type=items_type,
        is_open=is_open,
    )


def parse_store(payload: Any) -> LiveStore:
    """Parse either the approved legacy fixture or current web-store schema."""
    _bounded_payload(payload)
    if isinstance(payload, dict) and "schedule" not in payload:
        return _parse_current_store(payload)
    return _parse_legacy_store(payload)


def _parse_promotions(value: Any) -> tuple[CatalogPromotion, ...]:
    raw = _array(value, maximum=16)
    result: list[CatalogPromotion] = []
    ids: set[str] = set()
    for item in raw:
        promotion = _object(
            item,
            required={"id", "type", "label"},
            allowed={"id", "type", "label"},
        )
        promotion_id = _opaque_id(promotion["id"])
        promotion_type = _text(promotion["type"], maximum=30)
        if promotion_id in ids or promotion_type not in PROMOTION_TYPES:
            _fail()
        ids.add(promotion_id)
        result.append(
            CatalogPromotion(
                promotion_id,
                promotion_type,
                _text(promotion["label"], maximum=80),
            )
        )
    return tuple(result)


def _parse_option_groups(value: Any, *, product_currency: str) -> tuple[CatalogOptionGroup, ...]:
    groups = _array(value, maximum=MAX_OPTION_GROUPS)
    result: list[CatalogOptionGroup] = []
    group_ids: set[str] = set()
    positions: set[int] = set()
    required = {
        "id",
        "externalId",
        "name",
        "min",
        "max",
        "position",
        "multipleSelection",
        "collapsed",
        "attributes",
    }
    for item in groups:
        group = _object(item, required=required, allowed=required)
        group_id = _opaque_id(group["id"])
        group_external_id = _opaque_id(group["externalId"])
        minimum = _int(group["min"], maximum=MAX_OPTIONS_PER_GROUP)
        maximum = _int(group["max"], maximum=MAX_OPTIONS_PER_GROUP)
        position = _int(group["position"], maximum=MAX_OPTION_GROUPS)
        multiple = _bool(group["multipleSelection"])
        raw_options = _array(group["attributes"], maximum=MAX_OPTIONS_PER_GROUP)
        if not raw_options or minimum > maximum or maximum > len(raw_options):
            _fail()
        if not multiple and maximum > 1:
            _fail()
        if group_id in group_ids or position in positions:
            _fail()
        group_ids.add(group_id)
        positions.add(position)
        options: list[CatalogOption] = []
        option_ids: set[str] = set()
        option_external_ids: set[str] = set()
        selected_count = 0
        option_required = {
            "id",
            "externalId",
            "name",
            "priceImpact",
            "selected",
        }
        for raw_option in raw_options:
            option = _object(raw_option, required=option_required, allowed=option_required)
            option_id = _opaque_id(option["id"])
            external_id = _opaque_id(option["externalId"])
            if option_id in option_ids or external_id in option_external_ids:
                _fail()
            option_ids.add(option_id)
            option_external_ids.add(external_id)
            price = _money(option["priceImpact"])
            if price.currency != product_currency:
                _fail()
            selected = _bool(option["selected"])
            selected_count += int(selected)
            options.append(
                CatalogOption(
                    key=option_id,
                    external_id=external_id,
                    label=_text(option["name"], maximum=100),
                    price=price,
                    selected=selected,
                )
            )
        if selected_count > maximum or (selected_count and selected_count < minimum):
            _fail()
        result.append(
            CatalogOptionGroup(
                key=group_id,
                external_id=group_external_id,
                label=_text(group["name"], maximum=100),
                minimum=minimum,
                maximum=maximum,
                position=position,
                multiple_selection=_bool(group["multipleSelection"]),
                collapsed=_bool(group["collapsed"]),
                options=tuple(options),
            )
        )
    return tuple(sorted(result, key=lambda item: item.position))


def _parse_product(value: Any) -> CatalogProduct:
    required = {
        "id",
        "externalId",
        "storeProductId",
        "name",
        "price",
        "sponsored",
        "requiresProductView",
        "attributes",
        "promotions",
        "optionGroups",
    }
    product = _object(value, required=required, allowed=required)
    if _bool(product["requiresProductView"]):
        _fail("unsupported")
    attributes_required = {
        "directlyOrderable",
        "customizationComplete",
        "variableWeight",
        "openPrice",
        "substitutionsAllowed",
        "freeFormInstructions",
        "legalVerificationRequired",
    }
    attributes = _object(
        product["attributes"],
        required=attributes_required,
        allowed=attributes_required,
    )
    if not _bool(attributes["directlyOrderable"]) or not _bool(
        attributes["customizationComplete"]
    ):
        _fail("unsupported")
    if any(
        _bool(attributes[key])
        for key in (
            "variableWeight",
            "openPrice",
            "substitutionsAllowed",
            "freeFormInstructions",
            "legalVerificationRequired",
        )
    ):
        _fail("restricted")
    price = _money(product["price"])
    store_product_id = product["storeProductId"]
    if store_product_id is not None:
        store_product_id = _opaque_id(store_product_id)
    return CatalogProduct(
        product_id=_opaque_id(product["id"]),
        external_id=_opaque_id(product["externalId"]),
        store_product_id=store_product_id,
        name=_text(product["name"], maximum=120),
        price=price,
        sponsored=_bool(product["sponsored"]),
        option_groups=_parse_option_groups(
            product["optionGroups"], product_currency=price.currency
        ),
        promotions=_parse_promotions(product["promotions"]),
    )


def _parse_legacy_menu(payload: Any, *, expected_store_address_id: int) -> CatalogMenu:
    _bounded_payload(payload)
    expected = _int(expected_store_address_id, minimum=1)
    root = _object(payload, required={"type", "data"}, allowed={"type", "data"})
    layout = _text(root["type"], maximum=30)
    if layout not in MENU_LAYOUT_TYPES:
        _fail()
    data = _object(
        root["data"],
        required={"storeAddressId", "body"},
        allowed={"storeAddressId", "body"},
    )
    store_address_id = _int(data["storeAddressId"], minimum=1)
    if store_address_id != expected:
        _fail("mismatch")
    body = _array(data["body"], maximum=MAX_BODY_ELEMENTS)
    products: list[CatalogProduct] = []
    product_ids: set[str] = set()
    external_ids: set[str] = set()
    for item in body:
        element = _object(item, required={"type", "data"}, allowed={"type", "data"})
        element_type = _text(element["type"], maximum=40)
        if element_type in DISPLAY_ONLY_ELEMENTS:
            # Display-only data is intentionally not projected or trusted.
            continue
        if element_type not in DIRECT_PRODUCT_TYPES:
            _fail("unsupported")
        product = _parse_product(element["data"])
        if product.product_id in product_ids or product.external_id in external_ids:
            _fail()
        product_ids.add(product.product_id)
        external_ids.add(product.external_id)
        products.append(product)
        if len(products) > MAX_CATALOG_PRODUCTS:
            _fail()
    return CatalogMenu(layout, store_address_id, tuple(products))


def _major_money(value: Any, price_info: Any) -> ExactMoney:
    amount = _number(value, minimum=0, maximum=1_000_000_000)
    info = _object(
        price_info,
        required={"amount", "currencyCode", "displayText"},
        allowed={"amount", "currencyCode", "displayText"},
    )
    info_amount = _number(info["amount"], minimum=0, maximum=1_000_000_000)
    if Decimal(str(info_amount)) != Decimal(str(amount)):
        _fail()
    currency = _text(info["currencyCode"], maximum=3)
    if currency not in ISO_4217_EXPONENTS:
        _fail()
    _text(info["displayText"], maximum=80, allow_empty=True)
    try:
        scaled = Decimal(str(amount)) * (Decimal(10) ** ISO_4217_EXPONENTS[currency])
    except (InvalidOperation, OverflowError):
        _fail()
    if not scaled.is_finite() or scaled != scaled.to_integral_value():
        _fail()
    return ExactMoney(_int(int(scaled), maximum=100_000_000_000), currency)


def _current_opaque_id(value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        if value < 1 or value >= 10**128:
            _fail()
        return _opaque_id(str(value))
    return _opaque_id(value)


def _parse_current_option_groups(
    value: Any, *, product_currency: str
) -> tuple[CatalogOptionGroup, ...]:
    groups = _array(value, maximum=MAX_OPTION_GROUPS)
    result: list[CatalogOptionGroup] = []
    group_ids: set[str] = set()
    external_ids: set[str] = set()
    required = {
        "id", "attributeGroupId", "externalId", "name", "min", "max",
        "position", "multipleSelection", "collapsedByDefault", "attributes",
    }
    option_required = {
        "id", "attributeId", "externalId", "name", "priceImpact",
        "priceInfo", "selected",
    }
    for raw_group in groups:
        group = _object(raw_group, required=required, allowed=required)
        group_id = _current_opaque_id(group["id"])
        external_id = _opaque_id(group["externalId"])
        _opaque_id(group["attributeGroupId"])
        if group_id in group_ids or external_id in external_ids:
            _fail()
        group_ids.add(group_id)
        external_ids.add(external_id)
        options: list[CatalogOption] = []
        option_ids: set[str] = set()
        option_external_ids: set[str] = set()
        for raw_option in _array(group["attributes"], maximum=MAX_OPTIONS_PER_GROUP):
            option = _object(
                raw_option, required=option_required, allowed=option_required
            )
            option_id = _current_opaque_id(option["id"])
            option_external_id = _opaque_id(option["externalId"])
            _opaque_id(option["attributeId"])
            if option_id in option_ids or option_external_id in option_external_ids:
                _fail()
            option_ids.add(option_id)
            option_external_ids.add(option_external_id)
            price = _major_money(option["priceImpact"], option["priceInfo"])
            if price.currency != product_currency:
                _fail()
            options.append(
                CatalogOption(
                    key=option_id,
                    external_id=option_external_id,
                    label=_text(option["name"], maximum=100),
                    price=price,
                    selected=_bool(option["selected"]),
                )
            )
        # Glovo uses bounded values above the concrete option count as an "all
        # options" sentinel. Bound the provider value, then clamp authority to
        # the strictly parsed options actually present (at most 64).
        provider_maximum = _int(group["max"], maximum=1000)
        maximum = min(provider_maximum, len(options))
        result.append(
            CatalogOptionGroup(
                key=group_id,
                external_id=external_id,
                label=_text(group["name"], maximum=100),
                minimum=_int(group["min"], maximum=MAX_OPTIONS_PER_GROUP),
                maximum=maximum,
                position=_int(group["position"], maximum=MAX_OPTION_GROUPS),
                multiple_selection=(
                    _bool(group["multipleSelection"]) or maximum > 1
                ),
                collapsed=_bool(group["collapsedByDefault"]),
                options=tuple(options),
            )
        )
    return tuple(sorted(result, key=lambda item: item.position))


_CURRENT_PRODUCT_REQUIRED = frozenset(
    {
        "id", "externalId", "storeProductId", "name", "price", "priceInfo",
        "sponsored", "restricted", "outOfStock", "attributeGroups", "promotions",
    }
)
_CURRENT_PRODUCT_ALLOWED = _CURRENT_PRODUCT_REQUIRED | frozenset(
    {
        "description", "imageId", "imageSize", "imageUrl", "images", "indicators",
        "labels", "productTileFooter", "showQuantifiers", "tags", "tracking", "urn",
    }
)


def _parse_current_product(value: Any) -> CatalogProduct | None:
    product = _object(
        value,
        required=_CURRENT_PRODUCT_REQUIRED,
        allowed=_CURRENT_PRODUCT_ALLOWED,
    )
    if _bool(product["restricted"]) or _bool(product["outOfStock"]):
        return None
    price = _major_money(product["price"], product["priceInfo"])
    promotions = _array(product["promotions"], maximum=16)
    if any(not isinstance(item, dict) for item in promotions):
        _fail()
    store_product_id = product["storeProductId"]
    if store_product_id is not None:
        store_product_id = _opaque_id(str(store_product_id))
    return CatalogProduct(
        product_id=_current_opaque_id(product["id"]),
        external_id=_opaque_id(product["externalId"]),
        store_product_id=store_product_id,
        name=_text(product["name"], maximum=120),
        price=price,
        sponsored=_bool(product["sponsored"]),
        option_groups=_parse_current_option_groups(
            product["attributeGroups"], product_currency=price.currency
        ),
        promotions=(),
    )


def _parse_current_menu(payload: Any, *, expected_store_address_id: int) -> CatalogMenu:
    expected = _int(expected_store_address_id, minimum=1)
    root = _object(payload, required={"type", "data"}, allowed={"type", "data"})
    layout = _text(root["type"], maximum=30)
    if layout not in MENU_LAYOUT_TYPES:
        _fail()
    data = _object(
        root["data"],
        required={"body", "otcLabelsNavigationLinks", "styles", "tracking"},
        allowed={"body", "otcLabelsNavigationLinks", "styles", "tracking"},
    )
    navigation_links = data["otcLabelsNavigationLinks"]
    if navigation_links not in ({}, []):
        _fail()
    if not isinstance(data["styles"], dict) or not isinstance(data["tracking"], dict):
        _fail()
    products_by_id: dict[str, CatalogProduct] = {}
    external_ids: dict[str, str] = {}
    for raw_section in _array(data["body"], maximum=MAX_BODY_ELEMENTS):
        section = _object(
            raw_section,
            required={"type", "data"},
            allowed={"type", "data", "id"},
        )
        if _text(section["type"], maximum=40) != "LIST":
            _fail("unsupported")
        section_data = _object(
            section["data"],
            required={"elements", "slug", "title", "tracking"},
            allowed={"action", "elements", "icon", "slug", "title", "tracking"},
        )
        _text(section_data["slug"], maximum=120, allow_empty=True)
        _text(section_data["title"], maximum=120, allow_empty=True)
        if not isinstance(section_data["tracking"], dict):
            _fail()
        for raw_element in _array(
            section_data["elements"], maximum=MAX_BODY_ELEMENTS
        ):
            element = _object(
                raw_element,
                required={"type", "data", "actions"},
                allowed={"type", "data", "actions"},
            )
            if _text(element["type"], maximum=40) not in DIRECT_PRODUCT_TYPES:
                _fail("unsupported")
            _array(element["actions"], maximum=16)
            product = _parse_current_product(element["data"])
            if product is None:
                continue
            existing = products_by_id.get(product.product_id)
            existing_id = external_ids.get(product.external_id)
            if existing is not None:
                if existing != product:
                    _fail("mismatch")
                continue
            if existing_id is not None and existing_id != product.product_id:
                _fail()
            products_by_id[product.product_id] = product
            external_ids[product.external_id] = product.product_id
            if len(products_by_id) > MAX_CATALOG_PRODUCTS:
                _fail()
    return CatalogMenu(layout, expected, tuple(products_by_id.values()))


def parse_menu(payload: Any, *, expected_store_address_id: int) -> CatalogMenu:
    """Parse either the approved legacy fixture or current web-menu schema."""
    _bounded_payload(payload)
    if (
        isinstance(payload, dict)
        and isinstance(payload.get("data"), dict)
        and "storeAddressId" not in payload["data"]
    ):
        return _parse_current_menu(
            payload, expected_store_address_id=expected_store_address_id
        )
    return _parse_legacy_menu(
        payload, expected_store_address_id=expected_store_address_id
    )
