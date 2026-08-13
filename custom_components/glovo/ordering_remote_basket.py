"""Strict current Glovo basket contracts and single-attempt auxiliary client."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final, cast

from .api_session import (
    ApiSessionError,
    DeliveryLocation,
    MutationDispatchUncertain,
    MutationPurpose,
)

MAX_PRODUCTS: Final = 50
MAX_TOTAL_QUANTITY: Final = 100
MAX_PRODUCT_QUANTITY: Final = 50
MAX_CUSTOMIZATIONS: Final = 64
MAX_STRING: Final = 160
MAX_RESPONSE_BYTES: Final = 512_000
MAX_DEPTH: Final = 12
_ID_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_CITY_RE: Final = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,19}$")
_INTEGER_STRING_RE: Final = re.compile(r"^-?\d+$")
_LIMIT_TYPES: Final = frozenset(
    {"STOCK_AMOUNT", "CART_BE_LIMIT", "MAX_SALES_QUANTITY"}
)
_PRODUCT_ID_KEYS: Final = frozenset(
    {"legacyId", "id", "externalId", "storeProductId", "basketProductId"}
)
_QUANTITY_KEYS: Final = frozenset({"increments", "incrementsLimit", "limitType"})
_CUSTOMIZATION_ID_KEYS: Final = frozenset(
    {
        "groupLegacyId",
        "groupId",
        "groupExternalId",
        "groupPosition",
        "legacyId",
        "id",
        "externalId",
    }
)
_CUSTOMIZATION_KEYS: Final = frozenset(
    {"ids", "name", "quantity", "customizationName", "groupName"}
)
_RESPONSE_PRODUCT_REQUIRED: Final = frozenset(
    {
        "ids",
        "quantity",
        "price",
        "name",
        "productName",
        "description",
        "isCustomizable",
        "productReplacement",
        "weighableInfo",
        "packaging",
    }
)
_RESPONSE_PRODUCT_ALLOWED: Final = _RESPONSE_PRODUCT_REQUIRED | frozenset(
    {"customizations", "imageUrl", "discounts"}
)


class BasketContractError(ValueError):
    """Sanitized request/response contract failure."""

    def __init__(self, category: str = "schema") -> None:
        self.category = (
            category
            if category in {"schema", "mismatch", "unsupported"}
            else "schema"
        )
        super().__init__("remote basket did not satisfy the approved contract")


class RemoteBasketRejected(RuntimeError):
    """A classified non-ambiguous provider rejection."""

    category = "provider_rejection"

    def __init__(self, purpose: MutationPurpose, status: int | None = None) -> None:
        self.purpose = purpose
        self.status = (
            status
            if status
            in {400, 401, 403, 404, 405, 406, 409, 410, 415, 422, 429}
            else None
        )
        super().__init__(f"remote basket rejected ({purpose.value})")


class RemoteBasketAmbiguous(RuntimeError):
    """The sole mutation may have taken effect and must never be replayed."""

    category = "ambiguous"
    status = None

    def __init__(self, purpose: MutationPurpose) -> None:
        self.purpose = purpose
        super().__init__(f"remote basket outcome is ambiguous ({purpose.value})")


@dataclass(frozen=True, slots=True, repr=False)
class StructuredQuantity:
    increments: int = field(repr=False)
    increments_limit: int | None = field(default=None, repr=False)
    limit_type: str | None = field(default=None, repr=False)
    include_increments_limit: bool = field(default=False, repr=False)
    include_limit_type: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        increments = _int(
            self.increments, minimum=1, maximum=MAX_PRODUCT_QUANTITY
        )
        limit = self.increments_limit
        if limit is not None:
            limit = _int(limit, minimum=1, maximum=MAX_PRODUCT_QUANTITY)
            if increments > limit:
                _fail()
        if self.limit_type is not None and self.limit_type not in _LIMIT_TYPES:
            _fail()
        if not isinstance(self.include_increments_limit, bool) or not isinstance(
            self.include_limit_type, bool
        ):
            _fail()
        object.__setattr__(self, "increments", increments)
        object.__setattr__(self, "increments_limit", limit)

    def canonical_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"increments": self.increments}
        if self.include_increments_limit:
            result["incrementsLimit"] = self.increments_limit
        if self.include_limit_type:
            result["limitType"] = self.limit_type
        return result


@dataclass(frozen=True, slots=True, repr=False)
class RemoteCustomization:
    group_id: str = field(repr=False)
    group_external_id: str = field(repr=False)
    group_position: int = field(repr=False)
    attribute_id: str = field(repr=False)
    attribute_external_id: str = field(repr=False)
    group_name: str = field(repr=False)
    attribute_name: str = field(repr=False)
    quantity: StructuredQuantity = field(repr=False)
    customization_id: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        for name in (
            "group_id",
            "group_external_id",
            "attribute_id",
            "attribute_external_id",
        ):
            object.__setattr__(self, name, _opaque_id(getattr(self, name)))
        object.__setattr__(
            self,
            "group_position",
            _int(self.group_position, maximum=MAX_CUSTOMIZATIONS),
        )
        object.__setattr__(
            self, "group_name", _text(self.group_name, maximum=100)
        )
        object.__setattr__(
            self, "attribute_name", _text(self.attribute_name, maximum=100)
        )
        if not isinstance(self.quantity, StructuredQuantity):
            _fail()
        if self.customization_id is not None:
            object.__setattr__(
                self, "customization_id", _opaque_id(self.customization_id)
            )

    def canonical_dict(self) -> dict[str, Any]:
        ids: dict[str, Any] = {
            "groupLegacyId": self.group_id,
            "groupId": self.group_id,
            "groupExternalId": self.group_external_id,
            "groupPosition": self.group_position,
            "legacyId": self.attribute_id,
            "externalId": self.attribute_external_id,
        }
        if self.customization_id is not None:
            ids["id"] = self.customization_id
        return {
            "ids": ids,
            "name": self.group_name,
            "quantity": self.quantity.canonical_dict(),
            "customizationName": self.attribute_name,
            "groupName": self.group_name,
        }


@dataclass(frozen=True, slots=True, repr=False)
class RemoteBasketProduct:
    """Outbound request product built only from pinned menu identities."""

    product_id: str = field(repr=False)
    external_id: str | None = field(default=None, repr=False)
    legacy_id: str | None = field(default=None, repr=False)
    store_product_id: str | None = field(default=None, repr=False)
    basket_product_id: str | None = field(default=None, repr=False)
    quantity: int = field(default=1, repr=False)
    customizations: tuple[RemoteCustomization, ...] = field(
        default=(), repr=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "product_id", _opaque_id(self.product_id))
        for name in (
            "external_id",
            "legacy_id",
            "store_product_id",
            "basket_product_id",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _opaque_id(value))
        quantity = _int(
            self.quantity, minimum=1, maximum=MAX_PRODUCT_QUANTITY
        )
        if (
            not isinstance(self.customizations, tuple)
            or len(self.customizations) > MAX_CUSTOMIZATIONS
            or not all(
                isinstance(item, RemoteCustomization)
                for item in self.customizations
            )
        ):
            _fail()
        identities = {
            (item.group_id, item.attribute_id) for item in self.customizations
        }
        positions = {
            (item.group_id, item.group_position)
            for item in self.customizations
        }
        if len(identities) != len(self.customizations) or len(
            {item.group_id for item in self.customizations}
        ) != len(positions):
            _fail()
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(
            self,
            "customizations",
            tuple(
                sorted(
                    self.customizations,
                    key=lambda item: (
                        item.group_position,
                        item.group_id,
                        item.attribute_id,
                    ),
                )
            ),
        )

    @property
    def identity(
        self,
    ) -> tuple[str, str | None, str | None, str | None, str | None]:
        return (
            self.product_id,
            self.external_id,
            self.legacy_id,
            self.store_product_id,
            self.basket_product_id,
        )

    def canonical_dict(self) -> dict[str, Any]:
        ids: dict[str, str] = {"id": self.product_id}
        for key, value in (
            ("legacyId", self.legacy_id),
            ("externalId", self.external_id),
            ("storeProductId", self.store_product_id),
            ("basketProductId", self.basket_product_id),
        ):
            if value is not None:
                ids[key] = value
        result: dict[str, Any] = {
            "ids": ids,
            "quantity": {"increments": self.quantity},
        }
        if self.customizations:
            result["customizations"] = [
                item.canonical_dict() for item in self.customizations
            ]
        return result


@dataclass(frozen=True, slots=True, repr=False)
class RemoteBasketResponseProduct:
    """Strict rich inbound product plus its immutable provider projection."""

    product_id: str = field(repr=False)
    external_id: str | None = field(repr=False)
    legacy_id: str | None = field(repr=False)
    store_product_id: str | None = field(repr=False)
    basket_product_id: str = field(repr=False)
    quantity: StructuredQuantity = field(repr=False)
    customizations: tuple[RemoteCustomization, ...] = field(repr=False)
    name: str | None
    product_name: str | None
    provider_projection_bytes: bytes = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "product_id", _opaque_id(self.product_id))
        for name in ("external_id", "legacy_id", "store_product_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _opaque_id(value))
        object.__setattr__(
            self, "basket_product_id", _opaque_id(self.basket_product_id)
        )
        if not isinstance(self.quantity, StructuredQuantity):
            _fail()
        if not isinstance(self.customizations, tuple) or not all(
            isinstance(item, RemoteCustomization)
            for item in self.customizations
        ):
            _fail()
        if self.name is not None:
            object.__setattr__(self, "name", _text(self.name, maximum=160))
        if self.product_name is not None:
            object.__setattr__(
                self, "product_name", _text(self.product_name, maximum=160)
            )
        _decode_projection(self.provider_projection_bytes)

    @property
    def identity(
        self,
    ) -> tuple[str, str | None, str | None, str | None, str]:
        return (
            self.product_id,
            self.external_id,
            self.legacy_id,
            self.store_product_id,
            self.basket_product_id,
        )

    def canonical_dict(self) -> dict[str, Any]:
        return _decode_projection(self.provider_projection_bytes)


@dataclass(frozen=True, slots=True, repr=False)
class BasketIntent:
    customer_id: str = field(repr=False)
    store_id: int = field(repr=False)
    store_address_id: int = field(repr=False)
    store_category_id: int = field(repr=False)
    handling_strategy: str = field(repr=False)
    products: tuple[RemoteBasketProduct, ...] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "customer_id", _customer_id(self.customer_id))
        object.__setattr__(self, "store_id", _int(self.store_id, minimum=1))
        object.__setattr__(
            self,
            "store_address_id",
            _int(self.store_address_id, minimum=1),
        )
        object.__setattr__(
            self,
            "store_category_id",
            _int(self.store_category_id, minimum=1),
        )
        if self.handling_strategy != "DELIVERY" or not _valid_product_tuple(
            self.products
        ):
            _fail("unsupported")
        identities = [item.identity for item in self.products]
        if len(set(identities)) != len(identities):
            _fail()
        object.__setattr__(
            self,
            "products",
            tuple(sorted(self.products, key=lambda item: item.identity)),
        )

    def create_body(self) -> dict[str, Any]:
        return {
            "products": [item.canonical_dict() for item in self.products],
            "storeId": self.store_id,
            "storeAddressId": self.store_address_id,
            "storeCategoryId": self.store_category_id,
            "handlingStrategy": self.handling_strategy,
        }


@dataclass(frozen=True, slots=True, repr=False)
class BasketPrice:
    total_formatted: str = field(repr=False)
    minor: int | None = field(repr=False)
    major: float = field(repr=False)
    formatted: str = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "total_formatted",
            _text(self.total_formatted, maximum=100, allow_empty=True),
        )
        if self.minor is not None:
            object.__setattr__(
                self,
                "minor",
                _int(self.minor, maximum=100_000_000_000),
            )
        object.__setattr__(
            self,
            "major",
            _number(self.major, minimum=0, maximum=1_000_000_000),
        )
        object.__setattr__(
            self,
            "formatted",
            _text(self.formatted, maximum=100, allow_empty=True),
        )


@dataclass(frozen=True, slots=True, repr=False)
class RemoteBasketSnapshot:
    basket_id: str = field(repr=False)
    basket_version: str = field(repr=False)
    customer_id: str = field(repr=False)
    store_id: int = field(repr=False)
    store_address_id: int = field(repr=False)
    store_category_id: int = field(repr=False)
    handling_strategy: str = field(repr=False)
    products: tuple[RemoteBasketResponseProduct, ...] = field(repr=False)
    intent_products: tuple[RemoteBasketProduct, ...] = field(repr=False)
    basket_price: BasketPrice = field(repr=False)
    product_suggestions: tuple[RemoteBasketResponseProduct, ...] = field(
        repr=False
    )
    city_code: str | None = field(repr=False)
    is_prime_subscription_simulated: bool | None = field(repr=False)
    using_dh_basket: bool = field(repr=False)
    provider_projection_bytes: bytes = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "basket_id", _opaque_id(self.basket_id))
        object.__setattr__(
            self, "basket_version", _opaque_id(self.basket_version)
        )
        object.__setattr__(self, "customer_id", _customer_id(self.customer_id))
        if self.handling_strategy != "DELIVERY":
            _fail("unsupported")
        if (
            not isinstance(self.products, tuple)
            or not self.products
            or not all(
                isinstance(item, RemoteBasketResponseProduct)
                for item in self.products
            )
            or not _valid_product_tuple(self.intent_products)
        ):
            _fail()
        if not isinstance(self.basket_price, BasketPrice):
            _fail()
        if not isinstance(self.product_suggestions, tuple) or not all(
            isinstance(item, RemoteBasketResponseProduct)
            for item in self.product_suggestions
        ):
            _fail()
        if self.city_code is not None and _CITY_RE.fullmatch(
            _text(self.city_code, maximum=20)
        ) is None:
            _fail()
        if (
            self.is_prime_subscription_simulated is not None
            and not isinstance(self.is_prime_subscription_simulated, bool)
        ):
            _fail()
        if not isinstance(self.using_dh_basket, bool):
            _fail()
        _decode_projection(self.provider_projection_bytes)

    def intent(self) -> BasketIntent:
        return BasketIntent(
            self.customer_id,
            self.store_id,
            self.store_address_id,
            self.store_category_id,
            self.handling_strategy,
            self.intent_products,
        )

    def replace_body(
        self,
        products: Sequence[RemoteBasketProduct | RemoteBasketResponseProduct],
    ) -> dict[str, Any]:
        """Clone the current validated basket and replace only its products."""
        if (
            not isinstance(products, Sequence)
            or not products
            or not all(
                isinstance(item, (RemoteBasketProduct, RemoteBasketResponseProduct))
                for item in products
            )
        ):
            _fail()
        body = _decode_projection(self.provider_projection_bytes)
        body["products"] = [item.canonical_dict() for item in products]
        return body


@dataclass(frozen=True, slots=True, repr=False)
class ReconciliationExpectation:
    purpose: MutationPurpose
    intent: BasketIntent = field(repr=False)
    basket_id: str = field(repr=False)
    basket_version: str = field(repr=False)
    products: tuple[RemoteBasketProduct, ...] = field(repr=False)
    deleted: bool

    def __post_init__(self) -> None:
        if not isinstance(self.purpose, MutationPurpose):
            raise BasketContractError
        _opaque_id(self.basket_id)
        _opaque_id(self.basket_version)
        if not isinstance(self.intent, BasketIntent) or not _valid_product_tuple(
            self.products
        ):
            raise BasketContractError
        if not isinstance(self.deleted, bool):
            raise BasketContractError


@dataclass(frozen=True, slots=True, repr=False)
class ReconciliationResult:
    proven: bool
    snapshot: RemoteBasketSnapshot | None = field(repr=False)
    evidence: str = field(default="single_fresh_get", repr=False)


def _fail(category: str = "schema") -> None:
    raise BasketContractError(category)


def _object(
    value: object,
    *,
    required: set[str] | frozenset[str],
    allowed: set[str] | frozenset[str],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail()
    keys = set(value)
    if not set(required).issubset(keys) or not keys.issubset(set(allowed)):
        _fail()
    return value


def _array(
    value: object, *, minimum: int = 0, maximum: int
) -> list[Any]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        _fail()
    return value


def _int(
    value: object, *, minimum: int = 0, maximum: int = 2_147_483_647
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        _fail()
    return value


def _number(value: object, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail()
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        _fail()
    return result


def _text(
    value: object, *, maximum: int = MAX_STRING, allow_empty: bool = False
) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        _fail()
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        _fail()
    normalized = " ".join(value.split())
    if not allow_empty and not normalized:
        _fail()
    return normalized


def _opaque_id(value: object) -> str:
    text = _text(value, maximum=128)
    if _ID_RE.fullmatch(text) is None:
        _fail()
    return text


def _customer_id(value: object) -> str:
    if isinstance(value, bool):
        _fail()
    if isinstance(value, int):
        return str(value)
    text = _text(value, maximum=128)
    if _INTEGER_STRING_RE.fullmatch(text) is None:
        _fail()
    return text


def _legacy_id(value: object) -> str:
    """Normalize the validator's private legacy-ID primitive union to text."""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return str(value)
    return _opaque_id(value)


def _optional_id(value: object) -> str | None:
    return None if value is None else _opaque_id(value)


def _bool_or_none(value: object) -> bool | None:
    if value is not None and not isinstance(value, bool):
        _fail()
    return value


def _bounded_payload(value: object, *, maximum: int = MAX_RESPONSE_BYTES) -> None:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError):
        _fail()
    if len(encoded.encode()) > maximum:
        _fail()

    def walk(item: object, depth: int) -> None:
        if depth > MAX_DEPTH:
            _fail()
        if isinstance(item, dict):
            if len(item) > 200:
                _fail()
            for key, child in item.items():
                _text(key, maximum=100)
                walk(child, depth + 1)
        elif isinstance(item, list):
            if len(item) > 200:
                _fail()
            for child in item:
                walk(child, depth + 1)
        elif isinstance(item, str) and len(item) > 1_000:
            _fail()
        elif isinstance(item, float) and not math.isfinite(item):
            _fail()

    walk(value, 0)


def _projection_bytes(value: dict[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError, OverflowError) as err:
        raise BasketContractError from err


def _decode_projection(value: bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as err:
        raise BasketContractError from err
    if not isinstance(decoded, dict):
        _fail()
    _bounded_payload(decoded)
    return cast(dict[str, Any], decoded)


def _parse_quantity(value: object) -> tuple[StructuredQuantity, dict[str, Any]]:
    item = _object(value, required={"increments"}, allowed=_QUANTITY_KEYS)
    limit = item.get("incrementsLimit")
    if limit is not None:
        limit = _int(limit, minimum=1, maximum=MAX_PRODUCT_QUANTITY)
    limit_type = item.get("limitType")
    if limit_type is not None:
        limit_type = _text(limit_type, maximum=40)
        if limit_type not in _LIMIT_TYPES:
            _fail()
    quantity = StructuredQuantity(
        increments=_int(
            item["increments"], minimum=1, maximum=MAX_PRODUCT_QUANTITY
        ),
        increments_limit=limit,
        limit_type=limit_type,
        include_increments_limit="incrementsLimit" in item,
        include_limit_type="limitType" in item,
    )
    return quantity, quantity.canonical_dict()


def _parse_product_ids(
    value: object, *, basket_product_required: bool
) -> tuple[dict[str, str | None], dict[str, Any]]:
    required = {"id", "basketProductId"} if basket_product_required else {"id"}
    item = _object(value, required=required, allowed=_PRODUCT_ID_KEYS)
    result: dict[str, str | None] = {
        "id": _opaque_id(item["id"]),
        "legacyId": None,
        "externalId": None,
        "storeProductId": None,
        "basketProductId": None,
    }
    projection: dict[str, Any] = {"id": result["id"]}
    if "legacyId" in item:
        result["legacyId"] = _legacy_id(item["legacyId"])
        projection["legacyId"] = result["legacyId"]
    for key in ("externalId", "storeProductId", "basketProductId"):
        if key in item:
            result[key] = _opaque_id(item[key])
            projection[key] = result[key]
    return result, projection


def _parse_customizations(
    value: object,
) -> tuple[tuple[RemoteCustomization, ...], list[dict[str, Any]]]:
    raw_items = _array(value, maximum=MAX_CUSTOMIZATIONS)
    result: list[RemoteCustomization] = []
    projection: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    positions: dict[str, int] = {}
    for raw in raw_items:
        item = _object(
            raw,
            required=_CUSTOMIZATION_KEYS,
            allowed=_CUSTOMIZATION_KEYS,
        )
        ids = _object(
            item["ids"],
            required=_CUSTOMIZATION_ID_KEYS - {"id"},
            allowed=_CUSTOMIZATION_ID_KEYS,
        )
        group_legacy_id = _legacy_id(ids["groupLegacyId"])
        group_id = _opaque_id(ids["groupId"])
        if group_legacy_id != group_id:
            _fail("mismatch")
        attribute_id = _legacy_id(ids["legacyId"])
        identity = (group_id, attribute_id)
        position = _int(ids["groupPosition"], maximum=MAX_CUSTOMIZATIONS)
        if identity in identities or (
            group_id in positions and positions[group_id] != position
        ):
            _fail()
        identities.add(identity)
        positions[group_id] = position
        quantity, _ = _parse_quantity(item["quantity"])
        group_name = _text(item["groupName"], maximum=100)
        if _text(item["name"], maximum=100) != group_name:
            _fail("mismatch")
        parsed = RemoteCustomization(
            group_id=group_id,
            group_external_id=_opaque_id(ids["groupExternalId"]),
            group_position=position,
            attribute_id=attribute_id,
            attribute_external_id=_opaque_id(ids["externalId"]),
            group_name=group_name,
            attribute_name=_text(item["customizationName"], maximum=100),
            quantity=quantity,
            customization_id=(
                _opaque_id(ids["id"]) if "id" in ids else None
            ),
        )
        result.append(parsed)
        projection.append(parsed.canonical_dict())
    ordered = sorted(
        zip(result, projection, strict=True),
        key=lambda pair: (
            pair[0].group_position,
            pair[0].group_id,
            pair[0].attribute_id,
        ),
    )
    return tuple(item for item, _ in ordered), [raw for _, raw in ordered]


def _valid_product_tuple(value: object) -> bool:
    return (
        isinstance(value, tuple)
        and 1 <= len(value) <= MAX_PRODUCTS
        and all(isinstance(item, RemoteBasketProduct) for item in value)
        and sum(item.quantity for item in value) <= MAX_TOTAL_QUANTITY
    )


def _parse_request_product(value: object) -> RemoteBasketProduct:
    item = _object(
        value,
        required={"ids", "quantity"},
        allowed={"ids", "quantity", "customizations"},
    )
    ids, _ = _parse_product_ids(item["ids"], basket_product_required=False)
    quantity, _ = _parse_quantity(item["quantity"])
    customizations, _ = _parse_customizations(item.get("customizations", []))
    return RemoteBasketProduct(
        product_id=cast(str, ids["id"]),
        external_id=ids["externalId"],
        legacy_id=ids["legacyId"],
        store_product_id=ids["storeProductId"],
        basket_product_id=ids["basketProductId"],
        quantity=quantity.increments,
        customizations=customizations,
    )


def _parse_request_products(value: object) -> tuple[RemoteBasketProduct, ...]:
    raw_items = _array(value, minimum=1, maximum=MAX_PRODUCTS)
    products = [_parse_request_product(item) for item in raw_items]
    if sum(item.quantity for item in products) > MAX_TOTAL_QUANTITY:
        _fail()
    if len({item.identity for item in products}) != len(products):
        _fail()
    return tuple(sorted(products, key=lambda item: item.identity))


def parse_basket_intent(payload: object) -> BasketIntent:
    """Parse a complete delivery-only basket creation intent."""
    _bounded_payload(payload, maximum=256_000)
    required = {
        "customerId",
        "storeId",
        "storeAddressId",
        "storeCategoryId",
        "handlingStrategy",
        "products",
    }
    root = _object(payload, required=required, allowed=required)
    if root["handlingStrategy"] != "DELIVERY":
        _fail("unsupported")
    return BasketIntent(
        customer_id=_customer_id(root["customerId"]),
        store_id=_int(root["storeId"], minimum=1),
        store_address_id=_int(root["storeAddressId"], minimum=1),
        store_category_id=_int(root["storeCategoryId"], minimum=1),
        handling_strategy="DELIVERY",
        products=_parse_request_products(root["products"]),
    )


def _parse_money_amount(value: object) -> dict[str, Any]:
    item = _object(
        value,
        required={"minor", "major", "formatted"},
        allowed={"minor", "major", "formatted"},
    )
    minor = item["minor"]
    if minor is not None:
        minor = _int(minor, maximum=100_000_000_000)
    return {
        "minor": minor,
        "major": _number(
            item["major"], minimum=0, maximum=1_000_000_000
        ),
        "formatted": _text(
            item["formatted"], maximum=100, allow_empty=True
        ),
    }


def _parse_basket_price(value: object) -> tuple[BasketPrice, dict[str, Any]]:
    root = _object(
        value,
        required={"totalFormatted", "final"},
        allowed={"totalFormatted", "final"},
    )
    final = _parse_money_amount(root["final"])
    total = _text(root["totalFormatted"], maximum=100, allow_empty=True)
    return (
        BasketPrice(
            total_formatted=total,
            minor=cast(int | None, final["minor"]),
            major=cast(float, final["major"]),
            formatted=cast(str, final["formatted"]),
        ),
        {"totalFormatted": total, "final": final},
    )


def _parse_product_price(value: object) -> dict[str, Any]:
    item = _object(
        value,
        required={
            "totalFormatted",
            "final",
            "unitaryBasePrice",
            "unitaryTotalPrice",
            "productTotalDiscount",
        },
        allowed={
            "totalFormatted",
            "final",
            "unitaryBasePrice",
            "unitaryTotalPrice",
            "productTotalDiscount",
        },
    )
    discount = item["productTotalDiscount"]
    if discount is not None:
        discount = _number(discount, minimum=0, maximum=1_000_000_000)
    return {
        "totalFormatted": _text(
            item["totalFormatted"], maximum=100, allow_empty=True
        ),
        "final": _parse_money_amount(item["final"]),
        "unitaryBasePrice": _parse_money_amount(item["unitaryBasePrice"]),
        "unitaryTotalPrice": _parse_money_amount(
            item["unitaryTotalPrice"]
        ),
        "productTotalDiscount": discount,
    }


def _parse_discounts(value: object) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    required = {
        "promotionId",
        "type",
        "name",
        "isPrimeDiscount",
        "label",
        "origin",
        "quantity",
        "finalPriceMajorWhenInDiscountResponse",
        "percentage",
    }
    allowed = required | {"isFake"}
    for raw in _array(value, maximum=32):
        item = _object(raw, required=required, allowed=allowed)
        parsed: dict[str, Any] = {
            "promotionId": _opaque_id(item["promotionId"]),
            "type": _text(item["type"], maximum=80),
            "name": _text(item["name"], maximum=160, allow_empty=True),
            "isPrimeDiscount": _bool(item["isPrimeDiscount"]),
            "label": _text(item["label"], maximum=160, allow_empty=True),
            "origin": _text(item["origin"], maximum=80, allow_empty=True),
            "quantity": _number(
                item["quantity"], minimum=0, maximum=1_000_000_000
            ),
            "finalPriceMajorWhenInDiscountResponse": _number(
                item["finalPriceMajorWhenInDiscountResponse"],
                minimum=0,
                maximum=1_000_000_000,
            ),
            "percentage": _number(
                item["percentage"], minimum=0, maximum=100_000
            ),
        }
        if "isFake" in item:
            parsed["isFake"] = _bool(item["isFake"])
        result.append(parsed)
    return result


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        _fail()
    return value


def _parse_replacement(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    item = _object(
        value,
        required={"customerChosenRefund", "ids"},
        allowed={"customerChosenRefund", "ids"},
    )
    _, ids = _parse_product_ids(item["ids"], basket_product_required=False)
    return {"customerChosenRefund": _bool(item["customerChosenRefund"]), "ids": ids}


def _parse_weighable(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    item = _object(
        value,
        required={"metric", "incrementByWeight", "incrementByPiece"},
        allowed={"metric", "incrementByWeight", "incrementByPiece"},
    )
    return {
        "metric": _text(item["metric"], maximum=40),
        "incrementByWeight": _number(
            item["incrementByWeight"], minimum=0, maximum=1_000_000
        ),
        "incrementByPiece": _number(
            item["incrementByPiece"], minimum=0, maximum=1_000_000
        ),
    }


def _parse_packaging(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    item = _object(
        value,
        required={"type", "price", "isEco", "isReturnable", "priceFormatted"},
        allowed={"type", "price", "isEco", "isReturnable", "priceFormatted"},
    )
    packaging_type = item["type"]
    if packaging_type is not None:
        packaging_type = _text(packaging_type, maximum=80, allow_empty=True)
    return {
        "type": packaging_type,
        "price": _number(item["price"], minimum=0, maximum=1_000_000_000),
        "isEco": _bool_or_none(item["isEco"]),
        "isReturnable": _bool_or_none(item["isReturnable"]),
        "priceFormatted": _text(
            item["priceFormatted"], maximum=100, allow_empty=True
        ),
    }


def _parse_response_product(
    value: object, *, sponsored_required: bool = False
) -> tuple[RemoteBasketResponseProduct, dict[str, Any]]:
    # Basket products are explicitly `productSchema.partial()` in the current
    # first-party validator. IDs and quantity remain mandatory here because
    # they are the minimum authority needed to prove the returned basket is
    # exactly the requested selection. Suggestions use the full product
    # schema and therefore retain every required field below.
    required = (
        set(_RESPONSE_PRODUCT_REQUIRED)
        if sponsored_required
        else {"ids", "quantity"}
    )
    allowed = set(_RESPONSE_PRODUCT_ALLOWED)
    if sponsored_required:
        required.add("sponsored")
        allowed.add("sponsored")
    item = _object(value, required=required, allowed=allowed)
    ids, ids_projection = _parse_product_ids(
        item["ids"], basket_product_required=True
    )
    quantity, quantity_projection = _parse_quantity(item["quantity"])
    customizations: tuple[RemoteCustomization, ...] = ()
    customizations_projection: list[dict[str, Any]] = []
    if "customizations" in item:
        customizations, customizations_projection = _parse_customizations(
            item["customizations"]
        )
    projection: dict[str, Any] = {
        "ids": ids_projection,
        "quantity": quantity_projection,
    }
    if "price" in item:
        projection["price"] = _parse_product_price(item["price"])
    if "name" in item:
        projection["name"] = _text(item["name"], maximum=160)
    if "productName" in item:
        projection["productName"] = _text(item["productName"], maximum=160)
    if "description" in item:
        projection["description"] = _text(
            item["description"], maximum=1_000, allow_empty=True
        )
    if "isCustomizable" in item:
        projection["isCustomizable"] = _bool(item["isCustomizable"])
    if "productReplacement" in item:
        projection["productReplacement"] = _parse_replacement(
            item["productReplacement"]
        )
    if "weighableInfo" in item:
        projection["weighableInfo"] = _parse_weighable(item["weighableInfo"])
    if "packaging" in item:
        projection["packaging"] = _parse_packaging(item["packaging"])
    if "customizations" in item:
        projection["customizations"] = customizations_projection
    if "imageUrl" in item:
        image_url = item["imageUrl"]
        if image_url is not None:
            image_url = _text(image_url, maximum=1_000, allow_empty=True)
        projection["imageUrl"] = image_url
    if "discounts" in item:
        projection["discounts"] = _parse_discounts(item["discounts"])
    if sponsored_required:
        projection["sponsored"] = _bool(item["sponsored"])
    product = RemoteBasketResponseProduct(
        product_id=cast(str, ids["id"]),
        external_id=ids["externalId"],
        legacy_id=ids["legacyId"],
        store_product_id=ids["storeProductId"],
        basket_product_id=cast(str, ids["basketProductId"]),
        quantity=quantity,
        customizations=customizations,
        name=cast(str | None, projection.get("name")),
        product_name=cast(str | None, projection.get("productName")),
        provider_projection_bytes=_projection_bytes(projection),
    )
    return product, projection


def _parse_response_products(
    value: object,
) -> tuple[tuple[RemoteBasketResponseProduct, ...], list[dict[str, Any]]]:
    parsed = [
        _parse_response_product(item)
        for item in _array(value, minimum=1, maximum=MAX_PRODUCTS)
    ]
    products = [item for item, _ in parsed]
    if sum(item.quantity.increments for item in products) > MAX_TOTAL_QUANTITY:
        _fail()
    for index in range(5):
        identities = [item.identity[index] for item in products]
        present = [item for item in identities if item is not None]
        if len(set(present)) != len(present):
            _fail()
    ordered = sorted(
        parsed, key=lambda pair: (pair[0].product_id, pair[0].basket_product_id)
    )
    return tuple(item for item, _ in ordered), [raw for _, raw in ordered]


def _parse_product_suggestions(
    value: object,
) -> tuple[tuple[RemoteBasketResponseProduct, ...], list[dict[str, Any]]]:
    parsed = [
        _parse_response_product(item, sponsored_required=True)
        for item in _array(value, maximum=20)
    ]
    ids = [item.product_id for item, _ in parsed]
    if len(set(ids)) != len(ids):
        _fail()
    return tuple(item for item, _ in parsed), [raw for _, raw in parsed]


def _parse_mbs(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    item = _object(
        value,
        required={
            "surchargePrice",
            "savedSurchargePrice",
            "barThreshold",
            "currentPrice",
            "currentThreshold",
        },
        allowed={
            "surchargePrice",
            "savedSurchargePrice",
            "barThreshold",
            "currentPrice",
            "currentThreshold",
        },
    )
    _, bar = _parse_basket_price(item["barThreshold"])
    _, current = _parse_basket_price(item["currentPrice"])
    return {
        "surchargePrice": _text(
            item["surchargePrice"], maximum=100, allow_empty=True
        ),
        "savedSurchargePrice": _text(
            item["savedSurchargePrice"], maximum=100, allow_empty=True
        ),
        "barThreshold": bar,
        "currentPrice": current,
        "currentThreshold": _text(
            item["currentThreshold"], maximum=100, allow_empty=True
        ),
    }


def _customizations_match(
    expected: tuple[RemoteCustomization, ...],
    actual: tuple[RemoteCustomization, ...],
) -> bool:
    if len(expected) != len(actual):
        return False
    actual_by_id = {
        (item.group_id, item.attribute_id): item for item in actual
    }
    for wanted in expected:
        returned = actual_by_id.get((wanted.group_id, wanted.attribute_id))
        if returned is None:
            return False
        if (
            returned.group_external_id != wanted.group_external_id
            or returned.group_position != wanted.group_position
            or returned.attribute_external_id != wanted.attribute_external_id
            or returned.group_name != wanted.group_name
            or returned.attribute_name != wanted.attribute_name
            or returned.quantity.increments != wanted.quantity.increments
        ):
            return False
    return True


def _products_match(
    expected: tuple[RemoteBasketProduct, ...],
    actual: tuple[RemoteBasketResponseProduct, ...],
) -> bool:
    """Match exact intent while permitting only provider ID enrichment."""
    if len(expected) != len(actual):
        return False
    by_id = {item.product_id: item for item in actual}
    if len(by_id) != len(actual):
        return False
    for wanted in expected:
        returned = by_id.get(wanted.product_id)
        if returned is None:
            return False
        if (
            returned.quantity.increments != wanted.quantity
            or not _customizations_match(
                wanted.customizations, returned.customizations
            )
        ):
            return False
        for expected_id, returned_id in (
            (wanted.external_id, returned.external_id),
            (wanted.legacy_id, returned.legacy_id),
            (wanted.store_product_id, returned.store_product_id),
            (wanted.basket_product_id, returned.basket_product_id),
        ):
            if expected_id is not None and expected_id != returned_id:
                return False
    return True


def parse_remote_basket(
    payload: object,
    intent: BasketIntent,
    *,
    expected_basket_id: str | None = None,
    expected_basket_version: str | None = None,
    expected_products: tuple[RemoteBasketProduct, ...] | None = None,
) -> RemoteBasketSnapshot:
    """Parse one current rich response and cross-check every order identity."""
    if not isinstance(intent, BasketIntent):
        _fail()
    _bounded_payload(payload)
    required = {
        "basketId",
        "basketVersion",
        "customerId",
        "storeId",
        "storeAddressId",
        "storeCategoryId",
        "handlingStrategy",
        "products",
        "basketPrice",
        "mbs",
        "isPrimeSubscriptionSimulated",
        "usingDhBasket",
    }
    allowed = required | {"productSuggestions", "cityCode"}
    root = _object(payload, required=required, allowed=allowed)
    basket_id = _opaque_id(root["basketId"])
    basket_version = _opaque_id(root["basketVersion"])
    customer_id = _customer_id(root["customerId"])
    store_id = _int(root["storeId"], minimum=1)
    store_address_id = _int(root["storeAddressId"], minimum=1)
    store_category_id = _int(root["storeCategoryId"], minimum=1)
    if root["handlingStrategy"] != "DELIVERY":
        _fail("unsupported")
    products, products_projection = _parse_response_products(root["products"])
    expected = intent.products if expected_products is None else expected_products
    if not _valid_product_tuple(expected):
        _fail()
    if (
        customer_id != intent.customer_id
        or store_id != intent.store_id
        or store_address_id != intent.store_address_id
        or store_category_id != intent.store_category_id
        or not _products_match(expected, products)
        or (
            expected_basket_id is not None
            and basket_id != _opaque_id(expected_basket_id)
        )
        or (
            expected_basket_version is not None
            and basket_version != _opaque_id(expected_basket_version)
        )
    ):
        _fail("mismatch")
    basket_price, basket_price_projection = _parse_basket_price(
        root["basketPrice"]
    )
    mbs = _parse_mbs(root["mbs"])
    prime = _bool_or_none(root["isPrimeSubscriptionSimulated"])
    using_dh_basket = _bool(root["usingDhBasket"])
    city_code = root.get("cityCode")
    if city_code is not None:
        city_code = _text(city_code, maximum=20)
        if _CITY_RE.fullmatch(city_code) is None:
            _fail()
    suggestions: tuple[RemoteBasketResponseProduct, ...] = ()
    suggestions_projection: list[dict[str, Any]] = []
    if "productSuggestions" in root:
        suggestions, suggestions_projection = _parse_product_suggestions(
            root["productSuggestions"]
        )
    projection: dict[str, Any] = {
        "handlingStrategy": "DELIVERY",
        "products": products_projection,
        "storeAddressId": store_address_id,
        "storeCategoryId": store_category_id,
        "storeId": store_id,
        "basketId": basket_id,
        "basketVersion": basket_version,
        "customerId": customer_id,
        "basketPrice": basket_price_projection,
        "mbs": mbs,
        "isPrimeSubscriptionSimulated": prime,
        "usingDhBasket": using_dh_basket,
    }
    if "productSuggestions" in root:
        projection["productSuggestions"] = suggestions_projection
    if "cityCode" in root:
        projection["cityCode"] = city_code
    return RemoteBasketSnapshot(
        basket_id=basket_id,
        basket_version=basket_version,
        customer_id=customer_id,
        store_id=store_id,
        store_address_id=store_address_id,
        store_category_id=store_category_id,
        handling_strategy="DELIVERY",
        products=products,
        intent_products=expected,
        basket_price=basket_price,
        product_suggestions=suggestions,
        city_code=cast(str | None, city_code),
        is_prime_subscription_simulated=prime,
        using_dh_basket=using_dh_basket,
        provider_projection_bytes=_projection_bytes(projection),
    )


def _path(snapshot: RemoteBasketSnapshot, suffix: str = "") -> str:
    return (
        f"/v1/authenticated/customers/{snapshot.customer_id}/baskets/"
        f"{snapshot.basket_id}{suffix}"
    )


class RemoteBasketClient:
    """Private one-shot client; reconciliation is a separate explicit GET path."""

    def __init__(
        self,
        session: Any,
        *,
        invalidate_authority: Callable[[], None] | None = None,
    ) -> None:
        self._session = session
        self._invalidate_authority = invalidate_authority or (lambda: None)

    @staticmethod
    def _ambiguous(error: ApiSessionError) -> bool:
        return (
            isinstance(error, MutationDispatchUncertain)
            or error.category in {"transport", "schema"}
            or error.status in {500, 502, 503, 504}
        )

    async def _mutate(
        self,
        purpose: MutationPurpose,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        intent: BasketIntent,
        delivery_location: DeliveryLocation,
        *,
        expected_basket_id: str | None = None,
        previous_version: str | None = None,
        expected_products: tuple[RemoteBasketProduct, ...] | None = None,
    ) -> RemoteBasketSnapshot:
        self._invalidate_authority()
        try:
            payload = await self._session.async_mutate(
                purpose,
                method,
                path,
                body,
                delivery_location=delivery_location,
            )
        except ApiSessionError as err:
            if self._ambiguous(err):
                raise RemoteBasketAmbiguous(purpose) from err
            raise RemoteBasketRejected(purpose, err.status) from err
        try:
            result = parse_remote_basket(
                payload,
                intent,
                expected_basket_id=expected_basket_id,
                expected_products=expected_products,
            )
            if (
                previous_version is not None
                and result.basket_version == previous_version
            ):
                _fail("mismatch")
            return result
        except BasketContractError as err:
            raise RemoteBasketAmbiguous(purpose) from err

    async def async_create(
        self, intent: BasketIntent, delivery_location: DeliveryLocation
    ) -> RemoteBasketSnapshot:
        if not isinstance(intent, BasketIntent):
            raise BasketContractError
        return await self._mutate(
            MutationPurpose.CREATE_BASKET,
            "POST",
            f"/v1/authenticated/customers/{intent.customer_id}/baskets",
            intent.create_body(),
            intent,
            delivery_location,
        )

    async def async_replace(
        self,
        current: RemoteBasketSnapshot,
        products: Sequence[RemoteBasketProduct],
        delivery_location: DeliveryLocation,
    ) -> RemoteBasketSnapshot:
        if not isinstance(current, RemoteBasketSnapshot) or not isinstance(
            products, Sequence
        ):
            raise BasketContractError
        proposed = tuple(products)
        if not _valid_product_tuple(proposed):
            raise BasketContractError
        intent = replace(current.intent(), products=proposed)
        proposed = intent.products
        current_by_id = {item.product_id: item for item in current.products}
        projection_products: list[
            RemoteBasketProduct | RemoteBasketResponseProduct
        ] = []
        for item in proposed:
            returned = current_by_id.get(item.product_id)
            # Preserve the exact current rich provider projection only when it
            # still represents the proposed line. New or changed lines use the
            # current request-product projection accepted by the first-party
            # partial PUT schema; no display/price/provider fields are invented.
            projection_products.append(
                returned
                if returned is not None
                and _products_match((item,), (returned,))
                else item
            )
        return await self._mutate(
            MutationPurpose.REPLACE_BASKET_PRODUCTS,
            "PUT",
            _path(current, "/products"),
            current.replace_body(projection_products),
            intent,
            delivery_location,
            expected_basket_id=current.basket_id,
            previous_version=current.basket_version,
            expected_products=proposed,
        )

    async def async_change_quantity(
        self,
        current: RemoteBasketSnapshot,
        *,
        basket_product_id: str,
        increment: int,
        delivery_location: DeliveryLocation,
        limit: int | None = None,
    ) -> RemoteBasketSnapshot:
        if not isinstance(current, RemoteBasketSnapshot):
            raise BasketContractError
        target_id = _opaque_id(basket_product_id)
        step = _int(increment, minimum=1, maximum=MAX_PRODUCT_QUANTITY)
        requested_limit = (
            None
            if limit is None
            else _int(limit, minimum=1, maximum=MAX_PRODUCT_QUANTITY)
        )
        matches = [
            item
            for item in current.products
            if item.basket_product_id == target_id
        ]
        if len(matches) != 1:
            raise BasketContractError
        target = matches[0]
        effective_limit = target.quantity.increments_limit
        if requested_limit is not None:
            if (
                effective_limit is not None
                and requested_limit != effective_limit
            ):
                raise BasketContractError
            effective_limit = requested_limit
        quantity = target.quantity.increments + step
        if quantity > MAX_PRODUCT_QUANTITY or (
            effective_limit is not None and quantity > effective_limit
        ):
            raise BasketContractError
        request_matches = [
            item
            for item in current.intent_products
            if item.product_id == target.product_id
        ]
        if len(request_matches) != 1:
            raise BasketContractError
        request_target = request_matches[0]
        changed = replace(request_target, quantity=quantity)
        products = tuple(
            changed if item is request_target else item
            for item in current.intent_products
        )
        intent = replace(current.intent(), products=products)
        body = {
            "handlingStrategy": current.handling_strategy,
            "basketVersion": current.basket_version,
            "products": [
                {"basketProductId": target_id, "quantity": quantity}
            ],
        }
        return await self._mutate(
            MutationPurpose.CHANGE_BASKET_QUANTITY,
            "PATCH",
            _path(current, "/products/quantity"),
            body,
            intent,
            delivery_location,
            expected_basket_id=current.basket_id,
            previous_version=current.basket_version,
            expected_products=products,
        )

    async def async_delete(
        self,
        current: RemoteBasketSnapshot,
        *,
        explicit_user_intent: bool,
        delivery_location: DeliveryLocation,
    ) -> None:
        if (
            not isinstance(current, RemoteBasketSnapshot)
            or explicit_user_intent is not True
        ):
            raise BasketContractError
        purpose = MutationPurpose.DELETE_BASKET
        self._invalidate_authority()
        try:
            payload = await self._session.async_mutate(
                purpose,
                "DELETE",
                _path(current),
                None,
                delivery_location=delivery_location,
            )
        except ApiSessionError as err:
            if self._ambiguous(err):
                raise RemoteBasketAmbiguous(purpose) from err
            raise RemoteBasketRejected(purpose, err.status) from err
        if payload is not None:
            raise RemoteBasketAmbiguous(purpose)

    async def async_reconcile_ambiguous(
        self,
        ambiguity: RemoteBasketAmbiguous,
        expected: ReconciliationExpectation,
    ) -> ReconciliationResult:
        """Issue at most one fresh GET and prove exact expected state or fail closed."""
        if (
            not isinstance(ambiguity, RemoteBasketAmbiguous)
            or not isinstance(expected, ReconciliationExpectation)
            or ambiguity.purpose is not expected.purpose
        ):
            raise BasketContractError
        path = (
            f"/v1/authenticated/customers/{expected.intent.customer_id}/baskets/"
            f"{expected.basket_id}"
        )
        try:
            payload = await self._session.async_get("basket", path)
        except ApiSessionError as err:
            if expected.deleted and err.status == 404:
                return ReconciliationResult(True, None)
            raise RemoteBasketAmbiguous(expected.purpose) from err
        if expected.deleted:
            raise RemoteBasketAmbiguous(expected.purpose)
        try:
            snapshot = parse_remote_basket(
                payload,
                expected.intent,
                expected_basket_id=expected.basket_id,
                expected_basket_version=expected.basket_version,
                expected_products=expected.products,
            )
        except BasketContractError as err:
            raise RemoteBasketAmbiguous(expected.purpose) from err
        return ReconciliationResult(True, snapshot)
