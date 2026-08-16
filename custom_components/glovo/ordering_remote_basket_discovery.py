"""Bounded read-only discovery of the selected provider basket."""

from __future__ import annotations

import asyncio
import json
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
_SAFE_KEY_RE: Final = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_MAX_SHAPE_DEPTH: Final = 5
_MAX_SHAPE_KEYS: Final = 48
_MAX_SHAPE_ITEMS: Final = 3
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


def _safe_shape(value: object, *, depth: int = 0) -> object:
    """Return a bounded value-free JSON shape for private live diagnostics."""
    if depth >= _MAX_SHAPE_DEPTH:
        return "depth_limit"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return {
            "type": "list",
            "count": min(len(value), MAX_BASKET_SUMMARIES + 1),
            "items": [
                _safe_shape(item, depth=depth + 1)
                for item in value[:_MAX_SHAPE_ITEMS]
            ],
        }
    if isinstance(value, dict):
        entries: dict[str, object] = {}
        for key in sorted(value, key=lambda item: str(item))[:_MAX_SHAPE_KEYS]:
            safe_key = (
                key
                if isinstance(key, str) and _SAFE_KEY_RE.fullmatch(key)
                else "redacted_key"
            )
            if safe_key in entries:
                safe_key = "redacted_key_duplicate"
            entries[safe_key] = _safe_shape(value[key], depth=depth + 1)
        return {
            "type": "object",
            "count": min(len(value), _MAX_SHAPE_KEYS + 1),
            "fields": entries,
        }
    return "other"


def _safe_shape_json(value: object) -> str:
    return json.dumps(
        _safe_shape(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


class RemoteBasketDiscoveryError(RuntimeError):
    """Redaction-safe provider read or cross-response contract failure."""

    def __init__(
        self,
        *,
        stage: str = "input",
        reason: str = "schema",
        shape: str | None = None,
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
        self.shape = shape if isinstance(shape, str) and len(shape) <= 8_192 else None
        super().__init__(
            f"remote basket discovery failed ({self.stage}/{self.reason})"
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
                shape=_safe_shape_json(collection_payload),
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
                shape=_safe_shape_json(full_payload),
            ) from None
        if not remote_basket_matches_intent(snapshot, intent):
            return RemoteBasketDiscoveryResult(RemoteBasketDiscoveryStatus.CONFLICT)
        return RemoteBasketDiscoveryResult(
            RemoteBasketDiscoveryStatus.ADOPTED,
            replace(snapshot, intent_products=intent.products),
        )
