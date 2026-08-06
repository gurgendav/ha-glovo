"""Strict bounded response contracts for read-only live account and catalog APIs."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Final

from .ordering_models import ISO_4217_EXPONENTS

MAX_RESPONSE_BYTES: Final = 512_000
MAX_DEPTH: Final = 12
MAX_ADDRESSES: Final = 64
MAX_ADDRESS_FIELDS: Final = 24
MAX_PAYMENT_METHODS: Final = 32
MAX_BODY_ELEMENTS: Final = 200
MAX_PRODUCTS: Final = 100
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
    display_description: str = field(repr=False)
    last_four_digits: str | None = field(repr=False)
    selected: bool = field(repr=False)

    def __repr__(self) -> str:
        return "SavedPayment(<private>)"


@dataclass(frozen=True, slots=True)
class ExactMoney:
    amount_minor: int
    currency: str


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
    image_id: str = field(repr=False)
    view_type: str
    schedule: tuple[StoreSchedule, ...]
    scheduling_enabled: bool
    next_opening: str | None
    delivery_fee: ExactMoney
    service_fee: ExactMoney
    items_type: str


@dataclass(frozen=True, slots=True)
class CatalogOption:
    key: str
    label: str
    price: ExactMoney
    selected: bool

    def public_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "priceCents": self.price.amount_minor,
            "currencyCode": self.price.currency,
            "selected": self.selected,
        }


@dataclass(frozen=True, slots=True)
class CatalogOptionGroup:
    key: str
    label: str
    minimum: int
    maximum: int
    position: int
    multiple_selection: bool
    collapsed: bool
    options: tuple[CatalogOption, ...]

    def public_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
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
    root = _object(payload, required={"id"}, allowed={"id"})
    return CustomerIdentity(_int(root["id"], minimum=1))


def parse_saved_addresses(payload: Any) -> tuple[AddressSnapshot, ...]:
    _bounded_payload(payload)
    addresses = _array(_envelope(payload, "addresses"), maximum=MAX_ADDRESSES)
    parsed: list[AddressSnapshot] = []
    ids: set[int] = set()
    for item in addresses:
        row = _object(item, required={"entryType", "entry"}, allowed={"entryType", "entry"})
        if row["entryType"] != "SAVED_ADDRESS":
            _fail()
        entry = _object(row["entry"], required={"address"}, allowed={"address"})
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
        address = _object(entry["address"], required=required, allowed=required)
        remote_id = _int(address["id"], minimum=1)
        if remote_id in ids:
            _fail()
        ids.add(remote_id)
        kind = _text(address["kind"], maximum=20)
        if kind not in ADDRESS_KINDS:
            _fail()
        tag = address["tag"]
        if tag is not None:
            tag = _text(tag, maximum=80)
        raw_fields = _array(address["fields"], maximum=MAX_ADDRESS_FIELDS)
        fields: list[AddressField] = []
        field_types: set[str] = set()
        for raw_field in raw_fields:
            field_data = _object(
                raw_field,
                required={"type", "value"},
                allowed={"type", "value"},
            )
            field_type = _text(field_data["type"], maximum=40)
            if field_type not in ADDRESS_FIELD_TYPES or field_type in field_types:
                _fail()
            field_types.add(field_type)
            fields.append(
                AddressField(field_type, _text(field_data["value"], maximum=250, allow_empty=True))
            )
        parsed.append(
            AddressSnapshot(
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
            )
        )
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
    client_supports: tuple[str, ...] = (),
    client_ready: bool | None = None,
) -> dict[str, str]:
    amount = _int(amount_minor, maximum=100_000_000_000)
    if not isinstance(currency, str) or currency not in ISO_4217_EXPONENTS:
        _fail()
    query = {"amount": str(amount), "currency": currency, "context": "checkout"}
    if checkout_session is not None:
        query["checkoutSessionId"] = _opaque_id(checkout_session)
    if store_address_id is not None:
        query["storeAddressId"] = str(_int(store_address_id, minimum=1))
    if not isinstance(client_supports, tuple) or len(client_supports) > 4:
        _fail()
    if client_supports:
        if len(set(client_supports)) != len(client_supports) or any(
            item != "CREDIT_CARD" for item in client_supports
        ):
            _fail()
        query["clientSupports"] = ",".join(client_supports)
    if client_ready is not None:
        query["clientReady"] = "true" if _bool(client_ready) else "false"
    if len(json.dumps(query, separators=(",", ":"))) > 1_000:
        _fail()
    return query


def parse_saved_payments(payload: Any) -> tuple[SavedPayment, ...]:
    _bounded_payload(payload)
    root = _object(payload, required={"data"}, allowed={"data"})
    outer = _object(root["data"], required={"data"}, allowed={"data"})
    inner = _object(
        outer["data"],
        required={"paymentMethods", "actions"},
        allowed={"paymentMethods", "actions"},
    )
    actions = _array(inner["actions"], maximum=8)
    if actions:
        _fail()
    methods = _array(inner["paymentMethods"], maximum=MAX_PAYMENT_METHODS)
    result: list[SavedPayment] = []
    instrument_ids: set[str] = set()
    metadata_ids: set[int] = set()
    required = {
        "type",
        "paymentInstrumentId",
        "selected",
        "metadata",
        "display",
    }
    forbidden_fragments = (
        "cardnumber",
        "pan",
        "cvv",
        "cryptogram",
        "tokenization",
        "wallet",
        "paypal",
        "rawcard",
    )
    for item in methods:
        method = _object(item, required=required, allowed=required)
        normalized_keys = "".join(str(key).casefold() for key in method)
        if any(fragment in normalized_keys for fragment in forbidden_fragments):
            _fail()
        if method["type"] != "CREDIT_CARD":
            _fail()
        instrument_id = _opaque_id(method["paymentInstrumentId"])
        metadata = _object(
            method["metadata"],
            required={"id"},
            allowed={"id", "lastFourDigits"},
        )
        metadata_id = _int(metadata["id"], minimum=1)
        last_four = metadata.get("lastFourDigits")
        if last_four is not None:
            if not isinstance(last_four, str) or not re.fullmatch(r"\d{1,4}", last_four):
                _fail()
        display = _object(
            method["display"],
            required={"name", "description"},
            allowed={"name", "description"},
        )
        if instrument_id in instrument_ids or metadata_id in metadata_ids:
            _fail()
        instrument_ids.add(instrument_id)
        metadata_ids.add(metadata_id)
        result.append(
            SavedPayment(
                payment_instrument_id=instrument_id,
                metadata_id=metadata_id,
                display_name=_text(display["name"], maximum=40),
                display_description=_text(display["description"], maximum=80),
                last_four_digits=last_four,
                selected=_bool(method["selected"]),
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


def parse_store(payload: Any) -> LiveStore:
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
    if _bool(root["open"]) is not True or _bool(root["enabled"]) is not True:
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
    )


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
        _opaque_id(group["externalId"])
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
                    option_id,
                    _text(option["name"], maximum=100),
                    price,
                    selected,
                )
            )
        if selected_count > maximum or (selected_count and selected_count < minimum):
            _fail()
        result.append(
            CatalogOptionGroup(
                group_id,
                _text(group["name"], maximum=100),
                minimum,
                maximum,
                position,
                multiple,
                _bool(group["collapsed"]),
                tuple(options),
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


def parse_menu(payload: Any, *, expected_store_address_id: int) -> CatalogMenu:
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
        if len(products) > MAX_PRODUCTS:
            _fail()
    return CatalogMenu(layout, store_address_id, tuple(products))
