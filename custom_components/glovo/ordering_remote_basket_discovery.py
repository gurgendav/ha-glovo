"""Bounded read-only discovery of the selected provider basket."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Final

from .api_session import DeliveryLocation
from .ordering_remote_basket import (
    BasketContractError,
    BasketIntent,
    RemoteBasketSnapshot,
    _array,
    _bool,
    _bounded_payload,
    _customer_id,
    _int,
    _number,
    _object,
    _opaque_id,
    _text,
    parse_remote_basket_identity,
    remote_basket_matches_intent,
)

# The provider schema does not publish a collection cardinality. Discovery is
# deliberately more conservative than the generic response bounds: a customer
# may have at most 20 summary records in one accepted local proof.
MAX_BASKET_SUMMARIES: Final = 20
_MAX_COLLECTION_BYTES: Final = 128_000
_CUSTOMER_PATH_RE: Final = re.compile(r"^[1-9]\d{0,9}$")
_SUMMARY_REQUIRED: Final = frozenset(
    {
        "basketId",
        "basketVersion",
        "basketItems",
        "basketPriceFormatted",
        "customerId",
        "deliveryFeeInfo",
        "eta",
        "handlingStrategy",
        "outOfDeliveryArea",
        "storeAddressId",
        "storeCategoryId",
        "storeId",
        "storeName",
        "updatedAt",
    }
)
_SUMMARY_ALLOWED: Final = _SUMMARY_REQUIRED | frozenset({"distance", "storeImage"})


class RemoteBasketDiscoveryStatus(str, Enum):
    """Closed outcomes that can install basket authority."""

    ABSENT_VERIFIED = "ABSENT_VERIFIED"
    ADOPTED = "ADOPTED"
    CONFLICT = "CONFLICT"


class RemoteBasketDiscoveryError(RuntimeError):
    """Redaction-safe provider read or cross-response contract failure."""

    def __init__(self, category: str = "contract") -> None:
        self.category = (
            category
            if category in {"contract", "inconsistent", "transport"}
            else "contract"
        )
        super().__init__(f"remote basket discovery failed ({self.category})")


@dataclass(frozen=True, slots=True, repr=False)
class RemoteBasketDiscoveryResult:
    """Provider-ID-private result of one bounded discovery attempt."""

    status: RemoteBasketDiscoveryStatus
    snapshot: RemoteBasketSnapshot | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.status, RemoteBasketDiscoveryStatus) or (
            self.status is RemoteBasketDiscoveryStatus.ADOPTED
        ) != isinstance(self.snapshot, RemoteBasketSnapshot):
            raise RemoteBasketDiscoveryError


@dataclass(frozen=True, slots=True, repr=False)
class _BasketSummary:
    basket_id: str = field(repr=False)
    basket_version: str = field(repr=False)
    customer_id: str = field(repr=False)
    store_id: int = field(repr=False)
    store_address_id: int = field(repr=False)
    store_category_id: int = field(repr=False)
    handling_strategy: str = field(repr=False)


def _parse_nullable_eta(value: object) -> None:
    if value is None:
        return
    if isinstance(value, str):
        _text(value, maximum=100)
        return
    _int(value, maximum=86_400)


def _parse_delivery_fee_info(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise BasketContractError
    _bounded_payload(value, maximum=32_000)


def _parse_summary(value: object) -> _BasketSummary:
    item = _object(value, required=_SUMMARY_REQUIRED, allowed=_SUMMARY_ALLOWED)
    basket_id = _opaque_id(item["basketId"])
    basket_version = _opaque_id(item["basketVersion"])
    _int(item["basketItems"], maximum=100)
    _text(item["basketPriceFormatted"], maximum=100, allow_empty=True)
    customer_id = _customer_id(item["customerId"])
    _parse_delivery_fee_info(item["deliveryFeeInfo"])
    if "distance" in item and item["distance"] is not None:
        _number(item["distance"], minimum=0, maximum=1_000_000)
    _parse_nullable_eta(item["eta"])
    handling_strategy = _text(item["handlingStrategy"], maximum=40)
    if handling_strategy != item["handlingStrategy"]:
        raise BasketContractError("mismatch")
    _bool(item["outOfDeliveryArea"])
    store_address_id = _int(item["storeAddressId"], minimum=1)
    store_category_id = _int(item["storeCategoryId"], minimum=1)
    store_id = _int(item["storeId"], minimum=1)
    if "storeImage" in item and item["storeImage"] is not None:
        _text(item["storeImage"], maximum=1_000, allow_empty=True)
    _text(item["storeName"], maximum=160)
    _text(item["updatedAt"], maximum=80)
    return _BasketSummary(
        basket_id=basket_id,
        basket_version=basket_version,
        customer_id=customer_id,
        store_id=store_id,
        store_address_id=store_address_id,
        store_category_id=store_category_id,
        handling_strategy=handling_strategy,
    )


def _parse_collection(payload: object) -> tuple[_BasketSummary, ...]:
    _bounded_payload(payload, maximum=_MAX_COLLECTION_BYTES)
    return tuple(
        _parse_summary(item) for item in _array(payload, maximum=MAX_BASKET_SUMMARIES)
    )


def _validate_selected_summary(selected: _BasketSummary, intent: BasketIntent) -> None:
    if (
        selected.customer_id != intent.customer_id
        or selected.store_address_id != intent.store_address_id
        or selected.store_category_id != intent.store_category_id
        or selected.handling_strategy != intent.handling_strategy
    ):
        raise BasketContractError("mismatch")


class RemoteBasketDiscoveryClient:
    """Exactly one collection GET plus at most one selected-store full GET."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def _async_get(
        self, path: str, delivery_location: DeliveryLocation
    ) -> Any:
        try:
            return await self._session.async_get(
                "basket", path, delivery_location=delivery_location
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise RemoteBasketDiscoveryError("transport") from None

    async def async_discover(
        self, intent: BasketIntent, delivery_location: DeliveryLocation
    ) -> RemoteBasketDiscoveryResult:
        """Discover exact selected-store state without retry, polling, or mutation."""
        if (
            not isinstance(intent, BasketIntent)
            or _CUSTOMER_PATH_RE.fullmatch(intent.customer_id) is None
            or not isinstance(delivery_location, DeliveryLocation)
        ):
            raise RemoteBasketDiscoveryError
        root = f"/v1/authenticated/customers/{intent.customer_id}/baskets"
        collection_payload = await self._async_get(root, delivery_location)
        try:
            summaries = _parse_collection(collection_payload)
        except BasketContractError:
            raise RemoteBasketDiscoveryError from None

        selected = tuple(
            summary for summary in summaries if summary.store_id == intent.store_id
        )
        if not selected:
            return RemoteBasketDiscoveryResult(
                RemoteBasketDiscoveryStatus.ABSENT_VERIFIED
            )
        if len(selected) > 1:
            return RemoteBasketDiscoveryResult(RemoteBasketDiscoveryStatus.CONFLICT)
        summary = next(iter(selected))
        try:
            _validate_selected_summary(summary, intent)
        except BasketContractError:
            raise RemoteBasketDiscoveryError from None

        full_payload = await self._async_get(
            f"{root}/stores/{intent.store_id}", delivery_location
        )
        if full_payload is None:
            raise RemoteBasketDiscoveryError("inconsistent")
        try:
            snapshot = parse_remote_basket_identity(
                full_payload,
                intent,
                expected_basket_id=summary.basket_id,
                expected_basket_version=summary.basket_version,
            )
        except BasketContractError:
            raise RemoteBasketDiscoveryError from None
        if not remote_basket_matches_intent(snapshot, intent):
            return RemoteBasketDiscoveryResult(RemoteBasketDiscoveryStatus.CONFLICT)
        return RemoteBasketDiscoveryResult(
            RemoteBasketDiscoveryStatus.ADOPTED,
            replace(snapshot, intent_products=intent.products),
        )
