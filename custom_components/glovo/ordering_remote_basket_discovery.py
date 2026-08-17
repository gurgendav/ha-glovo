"""Bounded read-only discovery of the selected provider basket."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Final

from .api_session import ApiSessionError, DeliveryLocation
from .ordering_remote_basket import (
    BasketContractError,
    BasketIntent,
    RemoteBasketSnapshot,
    _array,
    _bool,
    _bounded_payload,
    _customer_id,
    _display_text,
    _finite_number,
    _int,
    _object,
    _opaque_id,
    _public_object,
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
_DIAGNOSTIC_PATH_RE: Final = re.compile(r"^[a-z][a-zA-Z0-9_.]{0,79}$")
_ETA_RANGE_REQUIRED: Final = frozenset({"lowerBound", "upperBound"})
_DELIVERY_FEE_REQUIRED: Final = frozenset({"deliveryFeeFormatted", "feeType"})
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
_SUMMARY_ALLOWED: Final = _SUMMARY_REQUIRED | frozenset(
    {"distance", "storeAvailability", "storeImage"}
)


class RemoteBasketDiscoveryStatus(str, Enum):
    """Closed outcomes that can install basket authority."""

    ABSENT_VERIFIED = "ABSENT_VERIFIED"
    ADOPTED = "ADOPTED"
    CONFLICT = "CONFLICT"


class RemoteBasketDiscoveryError(RuntimeError):
    """Redaction-safe provider read or cross-response contract failure."""

    def __init__(
        self,
        *,
        stage: str = "input",
        reason: str = "schema",
        path: str = "unknown",
    ) -> None:
        self.stage = stage if stage in {
            "input",
            "collection_get",
            "collection_parse",
            "selected_summary",
            "full_get",
            "full_empty",
            "full_parse",
        } else "input"
        self.reason = reason if reason in {
            "schema",
            "mismatch",
            "unsupported",
            "inconsistent",
            "transport",
        } else "schema"
        self.category = (
            "inconsistent"
            if self.reason == "inconsistent"
            else "transport"
            if self.reason == "transport"
            else "contract"
        )
        self.path = (
            path
            if isinstance(path, str) and _DIAGNOSTIC_PATH_RE.fullmatch(path)
            else "unknown"
        )
        super().__init__(
            f"remote basket discovery failed ({self.stage}/{self.reason}/{self.path})"
        )


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
        _display_text(value)
        return
    if isinstance(value, dict):
        item = _public_object(value, required=_ETA_RANGE_REQUIRED)
        _finite_number(item["lowerBound"])
        _finite_number(item["upperBound"])
        return
    _finite_number(value)


def _parse_delivery_fee_info(value: object) -> None:
    if value is None:
        return
    item = _public_object(value, required=_DELIVERY_FEE_REQUIRED)
    _display_text(item["deliveryFeeFormatted"])
    _display_text(item["feeType"])


def _parse_summary(value: object) -> _BasketSummary:
    allowed = set(_SUMMARY_ALLOWED)
    if isinstance(value, dict):
        allowed.update(value)
    item = _object(value, required=_SUMMARY_REQUIRED, allowed=allowed)
    basket_id = _opaque_id(item["basketId"])
    basket_version = _opaque_id(item["basketVersion"])
    _finite_number(item["basketItems"])
    _display_text(item["basketPriceFormatted"])
    customer_id = _customer_id(item["customerId"])
    _parse_delivery_fee_info(item["deliveryFeeInfo"])
    if "distance" in item and item["distance"] is not None:
        distance = item["distance"]
        if isinstance(distance, str):
            _display_text(distance)
        else:
            # Retain the already observed legacy numeric form.
            _finite_number(distance)
    _parse_nullable_eta(item["eta"])
    handling_strategy = _text(item["handlingStrategy"], maximum=40)
    if handling_strategy != item["handlingStrategy"]:
        raise BasketContractError("mismatch")
    _bool(item["outOfDeliveryArea"])
    store_address_id = _int(item["storeAddressId"], minimum=1)
    store_category_id = _int(item["storeCategoryId"], minimum=1)
    store_id = _int(item["storeId"], minimum=1)
    # Public Zod strips storeAvailability and unknown summary display extras.
    # They are bounded by the collection preflight and discarded.
    if "storeImage" in item and item["storeImage"] is not None:
        _display_text(item["storeImage"])
    _display_text(item["storeName"])
    _display_text(item["updatedAt"])
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
        self,
        path: str,
        delivery_location: DeliveryLocation,
        *,
        stage: str,
    ) -> Any:
        try:
            return await self._session.async_get(
                "basket", path, delivery_location=delivery_location
            )
        except asyncio.CancelledError:
            raise
        except ApiSessionError:
            raise
        except Exception:
            raise RemoteBasketDiscoveryError(
                stage=stage, reason="transport"
            ) from None

    async def async_discover(
        self, intent: BasketIntent, delivery_location: DeliveryLocation
    ) -> RemoteBasketDiscoveryResult:
        """Discover exact selected-store state without retry, polling, or mutation."""
        if (
            not isinstance(intent, BasketIntent)
            or _CUSTOMER_PATH_RE.fullmatch(intent.customer_id) is None
            or not isinstance(delivery_location, DeliveryLocation)
        ):
            raise RemoteBasketDiscoveryError()
        root = f"/v1/authenticated/customers/{intent.customer_id}/baskets"
        collection_payload = await self._async_get(
            root, delivery_location, stage="collection_get"
        )
        try:
            summaries = _parse_collection(collection_payload)
        except BasketContractError as err:
            raise RemoteBasketDiscoveryError(
                stage="collection_parse",
                reason=err.category,
                path=err.path,
            ) from None

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
        except BasketContractError as err:
            raise RemoteBasketDiscoveryError(
                stage="selected_summary", reason=err.category
            ) from None

        full_payload = await self._async_get(
            f"{root}/stores/{intent.store_id}",
            delivery_location,
            stage="full_get",
        )
        if full_payload is None:
            raise RemoteBasketDiscoveryError(
                stage="full_empty", reason="inconsistent"
            )
        try:
            snapshot = parse_remote_basket_identity(
                full_payload,
                intent,
                expected_basket_id=summary.basket_id,
                expected_basket_version=summary.basket_version,
            )
        except BasketContractError as err:
            raise RemoteBasketDiscoveryError(
                stage="full_parse",
                reason=err.category,
                path=err.path,
            ) from None
        if not remote_basket_matches_intent(snapshot, intent):
            return RemoteBasketDiscoveryResult(RemoteBasketDiscoveryStatus.CONFLICT)
        return RemoteBasketDiscoveryResult(
            RemoteBasketDiscoveryStatus.ADOPTED,
            replace(snapshot, intent_products=intent.products),
        )
