"""Strict private remote-basket contracts and single-attempt auxiliary client."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final

from .api_session import (
    ApiSessionError,
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
_PRODUCT_KEYS: Final = frozenset(
    {
        "id",
        "externalId",
        "legacyId",
        "storeProductId",
        "basketProductId",
        "quantity",
        "quantityLimit",
        "customizations",
    }
)
_CUSTOMIZATION_KEYS: Final = frozenset(
    {
        "groupId",
        "groupName",
        "groupPosition",
        "attributeId",
        "attributeName",
        "quantity",
    }
)


class BasketContractError(ValueError):
    """Sanitized request/response contract failure."""

    def __init__(self, category: str = "schema") -> None:
        self.category = category if category in {"schema", "mismatch", "unsupported"} else "schema"
        super().__init__("remote basket did not satisfy the approved contract")


class RemoteBasketRejected(RuntimeError):
    """A classified non-ambiguous provider rejection."""

    def __init__(self, purpose: MutationPurpose, status: int | None = None) -> None:
        self.purpose = purpose
        self.status = status if status in {400, 401, 403, 404, 405, 406, 409, 410, 415, 422, 429} else None
        super().__init__(f"remote basket rejected ({purpose.value})")


class RemoteBasketAmbiguous(RuntimeError):
    """The sole mutation may have taken effect and must never be replayed."""

    def __init__(self, purpose: MutationPurpose) -> None:
        self.purpose = purpose
        super().__init__(f"remote basket outcome is ambiguous ({purpose.value})")


@dataclass(frozen=True, slots=True, repr=False)
class RemoteCustomization:
    group_id: str = field(repr=False)
    group_name: str = field(repr=False)
    group_position: int = field(repr=False)
    attribute_id: str = field(repr=False)
    attribute_name: str = field(repr=False)
    quantity: int = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "group_id", _opaque_id(self.group_id))
        object.__setattr__(self, "group_name", _text(self.group_name, maximum=100))
        object.__setattr__(self, "group_position", _int(self.group_position, maximum=MAX_CUSTOMIZATIONS))
        object.__setattr__(self, "attribute_id", _opaque_id(self.attribute_id))
        object.__setattr__(self, "attribute_name", _text(self.attribute_name, maximum=100))
        object.__setattr__(self, "quantity", _int(self.quantity, minimum=1, maximum=MAX_PRODUCT_QUANTITY))

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "groupId": self.group_id,
            "groupName": self.group_name,
            "groupPosition": self.group_position,
            "attributeId": self.attribute_id,
            "attributeName": self.attribute_name,
            "quantity": self.quantity,
        }


@dataclass(frozen=True, slots=True, repr=False)
class RemoteBasketProduct:
    product_id: str = field(repr=False)
    external_id: str | None = field(default=None, repr=False)
    legacy_id: str | None = field(default=None, repr=False)
    store_product_id: str | None = field(default=None, repr=False)
    basket_product_id: str | None = field(default=None, repr=False)
    quantity: int = field(default=1, repr=False)
    quantity_limit: int | None = field(default=None, repr=False)
    customizations: tuple[RemoteCustomization, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "product_id", _opaque_id(self.product_id))
        for name in ("external_id", "legacy_id", "store_product_id", "basket_product_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _opaque_id(value))
        quantity = _int(self.quantity, minimum=1, maximum=MAX_PRODUCT_QUANTITY)
        limit = self.quantity_limit
        if limit is not None:
            limit = _int(limit, minimum=1, maximum=MAX_PRODUCT_QUANTITY)
            if quantity > limit:
                _fail()
        if (
            not isinstance(self.customizations, tuple)
            or len(self.customizations) > MAX_CUSTOMIZATIONS
            or not all(isinstance(item, RemoteCustomization) for item in self.customizations)
        ):
            _fail()
        identities = {(item.group_id, item.attribute_id) for item in self.customizations}
        positions = {(item.group_id, item.group_position) for item in self.customizations}
        if len(identities) != len(self.customizations) or len({item.group_id for item in self.customizations}) != len(positions):
            _fail()
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "quantity_limit", limit)
        object.__setattr__(self, "customizations", tuple(sorted(self.customizations, key=lambda item: (item.group_position, item.group_id, item.attribute_id))))

    @property
    def identity(self) -> tuple[str, str | None, str | None, str | None, str | None]:
        return (
            self.product_id,
            self.external_id,
            self.legacy_id,
            self.store_product_id,
            self.basket_product_id,
        )

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "id": self.product_id,
            "externalId": self.external_id,
            "legacyId": self.legacy_id,
            "storeProductId": self.store_product_id,
            "basketProductId": self.basket_product_id,
            "quantity": self.quantity,
            "quantityLimit": self.quantity_limit,
            "customizations": [item.canonical_dict() for item in self.customizations],
        }


@dataclass(frozen=True, slots=True, repr=False)
class BasketIntent:
    customer_id: int = field(repr=False)
    store_id: int = field(repr=False)
    store_address_id: int = field(repr=False)
    store_category_id: int = field(repr=False)
    handling_strategy: str = field(repr=False)
    products: tuple[RemoteBasketProduct, ...] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "customer_id", _int(self.customer_id, minimum=1))
        object.__setattr__(self, "store_id", _int(self.store_id, minimum=1))
        object.__setattr__(self, "store_address_id", _int(self.store_address_id, minimum=1))
        object.__setattr__(self, "store_category_id", _int(self.store_category_id, minimum=1))
        if self.handling_strategy != "DELIVERY" or not _valid_product_tuple(self.products):
            _fail("unsupported")
        identities = [item.identity for item in self.products]
        if len(set(identities)) != len(identities):
            _fail()
        object.__setattr__(self, "products", tuple(sorted(self.products, key=lambda item: item.identity)))

    def create_body(self) -> dict[str, Any]:
        return {
            "storeId": self.store_id,
            "storeAddressId": self.store_address_id,
            "storeCategoryId": self.store_category_id,
            "handlingStrategy": self.handling_strategy,
            "products": [item.canonical_dict() for item in self.products],
        }


@dataclass(frozen=True, slots=True, repr=False)
class BasketPrice:
    total_formatted: str = field(repr=False)
    minor: int | None = field(repr=False)
    major: float = field(repr=False)
    formatted: str = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "total_formatted", _text(self.total_formatted, maximum=100))
        if self.minor is not None:
            object.__setattr__(self, "minor", _int(self.minor, maximum=100_000_000_000))
        if isinstance(self.major, bool) or not isinstance(self.major, (int, float)) or not math.isfinite(float(self.major)) or not 0 <= float(self.major) <= 1_000_000_000:
            _fail()
        object.__setattr__(self, "major", float(self.major))
        object.__setattr__(self, "formatted", _text(self.formatted, maximum=100))


@dataclass(frozen=True, slots=True, repr=False)
class RemoteBasketSnapshot:
    basket_id: str = field(repr=False)
    basket_version: str = field(repr=False)
    customer_id: int = field(repr=False)
    store_id: int = field(repr=False)
    store_address_id: int = field(repr=False)
    store_category_id: int = field(repr=False)
    handling_strategy: str = field(repr=False)
    products: tuple[RemoteBasketProduct, ...] = field(repr=False)
    basket_price: BasketPrice = field(repr=False)
    suggestions: tuple[tuple[str, str], ...] = field(repr=False)
    city_code: str | None = field(repr=False)
    is_prime_subscription_simulated: bool | None = field(repr=False)
    using_dh_basket: bool | None = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "basket_id", _opaque_id(self.basket_id))
        object.__setattr__(self, "basket_version", _opaque_id(self.basket_version))
        # Constructing intent revalidates every order-bearing field and duplicate identity.
        intent = BasketIntent(self.customer_id, self.store_id, self.store_address_id, self.store_category_id, self.handling_strategy, self.products)
        object.__setattr__(self, "customer_id", intent.customer_id)
        object.__setattr__(self, "store_id", intent.store_id)
        object.__setattr__(self, "store_address_id", intent.store_address_id)
        object.__setattr__(self, "store_category_id", intent.store_category_id)
        object.__setattr__(self, "products", intent.products)
        if not isinstance(self.basket_price, BasketPrice):
            _fail()
        if self.city_code is not None and _CITY_RE.fullmatch(_text(self.city_code, maximum=20)) is None:
            _fail()
        if self.is_prime_subscription_simulated is not None and not isinstance(self.is_prime_subscription_simulated, bool):
            _fail()
        if self.using_dh_basket is not None and not isinstance(self.using_dh_basket, bool):
            _fail()
        if not isinstance(self.suggestions, tuple) or len(self.suggestions) > 20:
            _fail()
        ids: set[str] = set()
        for item in self.suggestions:
            if not isinstance(item, tuple) or len(item) != 2:
                _fail()
            item_id = _opaque_id(item[0])
            if item_id in ids:
                _fail()
            ids.add(item_id)
            _text(item[1], maximum=100)

    def intent(self) -> BasketIntent:
        return BasketIntent(
            self.customer_id,
            self.store_id,
            self.store_address_id,
            self.store_category_id,
            self.handling_strategy,
            self.products,
        )


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
        if not isinstance(self.intent, BasketIntent) or not _valid_product_tuple(self.products):
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
    value: object, *, required: set[str] | frozenset[str], allowed: set[str] | frozenset[str]
) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail()
    keys = set(value)
    if not set(required).issubset(keys) or not keys.issubset(set(allowed)):
        _fail()
    return value


def _int(value: object, *, minimum: int = 0, maximum: int = 2_147_483_647) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        _fail()
    return value


def _text(value: object, *, maximum: int = MAX_STRING, allow_empty: bool = False) -> str:
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


def _optional_id(value: object) -> str | None:
    return None if value is None else _opaque_id(value)


def _bool_or_none(value: object) -> bool | None:
    if value is not None and not isinstance(value, bool):
        _fail()
    return value


def _bounded_payload(value: object, *, maximum: int = MAX_RESPONSE_BYTES) -> None:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
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


def _parse_customizations(value: object) -> tuple[RemoteCustomization, ...]:
    if not isinstance(value, list) or len(value) > MAX_CUSTOMIZATIONS:
        _fail()
    result: list[RemoteCustomization] = []
    identities: set[tuple[str, str]] = set()
    positions: dict[str, int] = {}
    for raw in value:
        item = _object(raw, required=_CUSTOMIZATION_KEYS, allowed=_CUSTOMIZATION_KEYS)
        group_id = _opaque_id(item["groupId"])
        attribute_id = _opaque_id(item["attributeId"])
        identity = (group_id, attribute_id)
        position = _int(item["groupPosition"], maximum=MAX_CUSTOMIZATIONS)
        if identity in identities or (
            group_id in positions and positions[group_id] != position
        ):
            _fail()
        identities.add(identity)
        positions[group_id] = position
        result.append(
            RemoteCustomization(
                group_id=group_id,
                group_name=_text(item["groupName"], maximum=100),
                group_position=position,
                attribute_id=attribute_id,
                attribute_name=_text(item["attributeName"], maximum=100),
                quantity=_int(item["quantity"], minimum=1, maximum=MAX_PRODUCT_QUANTITY),
            )
        )
    return tuple(
        sorted(result, key=lambda item: (item.group_position, item.group_id, item.attribute_id))
    )


def _parse_product(value: object) -> RemoteBasketProduct:
    item = _object(
        value,
        required={"id", "quantity"},
        allowed=_PRODUCT_KEYS,
    )
    quantity = _int(item["quantity"], minimum=1, maximum=MAX_PRODUCT_QUANTITY)
    raw_limit = item.get("quantityLimit")
    quantity_limit = (
        None
        if raw_limit is None
        else _int(raw_limit, minimum=1, maximum=MAX_PRODUCT_QUANTITY)
    )
    if quantity_limit is not None and quantity > quantity_limit:
        _fail()
    return RemoteBasketProduct(
        product_id=_opaque_id(item["id"]),
        external_id=_optional_id(item.get("externalId")),
        legacy_id=_optional_id(item.get("legacyId")),
        store_product_id=_optional_id(item.get("storeProductId")),
        basket_product_id=_optional_id(item.get("basketProductId")),
        quantity=quantity,
        quantity_limit=quantity_limit,
        customizations=_parse_customizations(item.get("customizations", [])),
    )


def _valid_product_tuple(value: object) -> bool:
    return (
        isinstance(value, tuple)
        and 1 <= len(value) <= MAX_PRODUCTS
        and all(isinstance(item, RemoteBasketProduct) for item in value)
        and sum(item.quantity for item in value) <= MAX_TOTAL_QUANTITY
    )


def _parse_products(value: object) -> tuple[RemoteBasketProduct, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_PRODUCTS:
        _fail()
    products = [_parse_product(item) for item in value]
    if sum(item.quantity for item in products) > MAX_TOTAL_QUANTITY:
        _fail()
    seen_by_kind: list[set[str]] = [set() for _ in range(5)]
    for product in products:
        for index, identity in enumerate(product.identity):
            if identity is not None:
                if identity in seen_by_kind[index]:
                    _fail()
                seen_by_kind[index].add(identity)
    return tuple(sorted(products, key=lambda item: item.identity[0]))


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
        customer_id=_int(root["customerId"], minimum=1),
        store_id=_int(root["storeId"], minimum=1),
        store_address_id=_int(root["storeAddressId"], minimum=1),
        store_category_id=_int(root["storeCategoryId"], minimum=1),
        handling_strategy="DELIVERY",
        products=_parse_products(root["products"]),
    )


def _parse_price(value: object) -> BasketPrice:
    root = _object(
        value,
        required={"totalFormatted", "final"},
        allowed={"totalFormatted", "final"},
    )
    final = _object(
        root["final"],
        required={"minor", "major", "formatted"},
        allowed={"minor", "major", "formatted"},
    )
    minor = final["minor"]
    if minor is not None:
        minor = _int(minor, maximum=100_000_000_000)
    major = final["major"]
    if isinstance(major, bool) or not isinstance(major, (int, float)):
        _fail()
    major_float = float(major)
    if not math.isfinite(major_float) or major_float < 0 or major_float > 1_000_000_000:
        _fail()
    return BasketPrice(
        total_formatted=_text(root["totalFormatted"], maximum=100),
        minor=minor,
        major=major_float,
        formatted=_text(final["formatted"], maximum=100),
    )


def _parse_suggestions(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list) or len(value) > 20:
        _fail()
    result: list[tuple[str, str]] = []
    ids: set[str] = set()
    for raw in value:
        item = _object(raw, required={"id", "title"}, allowed={"id", "title"})
        suggestion_id = _opaque_id(item["id"])
        if suggestion_id in ids:
            _fail()
        ids.add(suggestion_id)
        result.append((suggestion_id, _text(item["title"], maximum=100)))
    return tuple(result)


def _products_match(
    expected: tuple[RemoteBasketProduct, ...],
    actual: tuple[RemoteBasketProduct, ...],
) -> bool:
    """Match intent exactly while allowing absent optional IDs to be enriched."""
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
            returned.quantity != wanted.quantity
            or (
                wanted.quantity_limit is not None
                and returned.quantity_limit != wanted.quantity_limit
            )
            or returned.customizations != wanted.customizations
        ):
            return False
        for expected_id, returned_id in zip(
            wanted.identity[1:], returned.identity[1:], strict=True
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
    """Parse one exact response and cross-check every order-bearing identity."""
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
    }
    allowed = required | {
        "suggestions",
        "mbs",
        "isPrimeSubscriptionSimulated",
        "cityCode",
        "usingDhBasket",
    }
    root = _object(payload, required=required, allowed=allowed)
    basket_id = _opaque_id(root["basketId"])
    basket_version = _opaque_id(root["basketVersion"])
    customer_id = _int(root["customerId"], minimum=1)
    store_id = _int(root["storeId"], minimum=1)
    store_address_id = _int(root["storeAddressId"], minimum=1)
    store_category_id = _int(root["storeCategoryId"], minimum=1)
    if root["handlingStrategy"] != "DELIVERY":
        _fail("unsupported")
    products = _parse_products(root["products"])
    expected = intent.products if expected_products is None else expected_products
    if not _valid_product_tuple(expected):
        _fail()
    if (
        customer_id != intent.customer_id
        or store_id != intent.store_id
        or store_address_id != intent.store_address_id
        or store_category_id != intent.store_category_id
        or not _products_match(expected, products)
        or (expected_basket_id is not None and basket_id != _opaque_id(expected_basket_id))
        or (
            expected_basket_version is not None
            and basket_version != _opaque_id(expected_basket_version)
        )
    ):
        _fail("mismatch")
    mbs = root.get("mbs")
    if mbs is not None and (not isinstance(mbs, dict) or mbs):
        _fail("unsupported")
    city_code = root.get("cityCode")
    if city_code is not None:
        city_code = _text(city_code, maximum=20)
        if _CITY_RE.fullmatch(city_code) is None:
            _fail()
    using_dh_basket = root.get("usingDhBasket")
    if "usingDhBasket" in root and not isinstance(using_dh_basket, bool):
        _fail()
    return RemoteBasketSnapshot(
        basket_id=basket_id,
        basket_version=basket_version,
        customer_id=customer_id,
        store_id=store_id,
        store_address_id=store_address_id,
        store_category_id=store_category_id,
        handling_strategy="DELIVERY",
        products=products,
        basket_price=_parse_price(root["basketPrice"]),
        suggestions=_parse_suggestions(root.get("suggestions", [])),
        city_code=city_code,
        is_prime_subscription_simulated=_bool_or_none(
            root.get("isPrimeSubscriptionSimulated")
        ),
        using_dh_basket=using_dh_basket,
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
        *,
        expected_basket_id: str | None = None,
        previous_version: str | None = None,
        expected_products: tuple[RemoteBasketProduct, ...] | None = None,
    ) -> RemoteBasketSnapshot:
        self._invalidate_authority()
        try:
            payload = await self._session.async_mutate(purpose, method, path, body)
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
            if previous_version is not None and result.basket_version == previous_version:
                _fail("mismatch")
            return result
        except BasketContractError as err:
            raise RemoteBasketAmbiguous(purpose) from err

    async def async_create(self, intent: BasketIntent) -> RemoteBasketSnapshot:
        if not isinstance(intent, BasketIntent):
            raise BasketContractError
        return await self._mutate(
            MutationPurpose.CREATE_BASKET,
            "POST",
            f"/v1/authenticated/customers/{intent.customer_id}/baskets",
            intent.create_body(),
            intent,
        )

    async def async_replace(
        self,
        current: RemoteBasketSnapshot,
        products: Sequence[RemoteBasketProduct],
    ) -> RemoteBasketSnapshot:
        if not isinstance(current, RemoteBasketSnapshot) or not isinstance(products, Sequence):
            raise BasketContractError
        proposed = tuple(products)
        if not _valid_product_tuple(proposed):
            raise BasketContractError
        intent = replace(current.intent(), products=proposed)
        return await self._mutate(
            MutationPurpose.REPLACE_BASKET_PRODUCTS,
            "PUT",
            _path(current, "/products"),
            {
                "basketVersion": current.basket_version,
                "products": [item.canonical_dict() for item in proposed],
            },
            intent,
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
        matches = [item for item in current.products if item.basket_product_id == target_id]
        if len(matches) != 1:
            raise BasketContractError
        target = matches[0]
        effective_limit = target.quantity_limit
        if requested_limit is not None:
            if effective_limit is not None and requested_limit != effective_limit:
                raise BasketContractError
            effective_limit = requested_limit
        quantity = target.quantity + step
        if quantity > MAX_PRODUCT_QUANTITY or (
            effective_limit is not None and quantity > effective_limit
        ):
            raise BasketContractError
        changed = replace(target, quantity=quantity, quantity_limit=effective_limit)
        products = tuple(changed if item is target else item for item in current.products)
        intent = replace(current.intent(), products=products)
        body: dict[str, Any] = {
            "basketVersion": current.basket_version,
            "basketProductId": target_id,
            "quantityIncrement": step,
        }
        if effective_limit is not None:
            body["quantityLimit"] = effective_limit
        return await self._mutate(
            MutationPurpose.CHANGE_BASKET_QUANTITY,
            "PATCH",
            _path(current, "/products/quantity"),
            body,
            intent,
            expected_basket_id=current.basket_id,
            previous_version=current.basket_version,
            expected_products=products,
        )

    async def async_delete(
        self,
        current: RemoteBasketSnapshot,
        *,
        explicit_user_intent: bool,
    ) -> None:
        if not isinstance(current, RemoteBasketSnapshot) or explicit_user_intent is not True:
            raise BasketContractError
        purpose = MutationPurpose.DELETE_BASKET
        self._invalidate_authority()
        try:
            payload = await self._session.async_mutate(
                purpose,
                "DELETE",
                _path(current),
                {"basketVersion": current.basket_version},
            )
        except ApiSessionError as err:
            if self._ambiguous(err):
                raise RemoteBasketAmbiguous(purpose) from err
            raise RemoteBasketRejected(purpose, err.status) from err
        if payload is not None and payload != {"deleted": True}:
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
