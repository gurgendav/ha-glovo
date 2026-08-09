"""Private fixture-only final-checkout contract; deliberately not runtime wired.

This module has no network implementation and is not imported by the integration.
Its injected transport must explicitly identify itself as a sanitized offline fixture.
It models one prospective POST and one separately requested, bounded status GET;
it never performs a completion mutation, retry, refresh, replay, or reconciliation.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final, NoReturn, cast

from .ordering_live_quote import AuthoritativeQuote

FINAL_CHECKOUT_PATH: Final = "/v3/checkouts/order/1"
FINAL_STATUS_PATH_PREFIX: Final = "/v3/checkouts/order/1/"
_MAX_BYTES: Final = 128_000
_MAX_DEPTH: Final = 10
_MAX_PRODUCTS: Final = 100
_MAX_STRING: Final = 300
_ID_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_DIGEST_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_SUBMIT_STATES: Final = frozenset({"SUBMITTED", "PENDING"})
_STATUS_STATES: Final = frozenset({"SUBMITTED", "PENDING", "COMPLETED", "REJECTED"})
_HANDLING_STRATEGY: Final = "DELIVERY"


class FinalCheckoutContractError(ValueError):
    """Sanitized fixture contract failure with no provider detail."""

    def __init__(self, category: str = "schema") -> None:
        self.category = category if category in {"schema", "mismatch", "unsupported", "stale"} else "schema"
        super().__init__("final checkout did not satisfy the private fixture contract")


class FinalCheckoutAmbiguous(RuntimeError):
    """The sole submit/status call has an unknown outcome and is never replayed."""

    def __init__(self) -> None:
        super().__init__("final checkout outcome is ambiguous; reconcile manually")


class FinalCheckoutRejected(RuntimeError):
    """A fixture exposed a deterministic pre-dispatch rejection classification."""

    def __init__(self, status: int | None = None) -> None:
        self.status = status if status in {400, 401, 403, 404, 405, 406, 415, 422} else None
        super().__init__("final checkout was rejected")


class FinalCheckoutUnsupported(RuntimeError):
    """A required next mutation/action is intentionally unavailable."""

    def __init__(self) -> None:
        super().__init__("final checkout action is unsupported; complete manually")


def _fail(category: str = "schema") -> NoReturn:
    raise FinalCheckoutContractError(category)


def _id(value: object) -> str:
    if not isinstance(value, str) or len(value) > 128 or _ID_RE.fullmatch(value) is None:
        _fail()
    return value


def _integer(value: object, *, minimum: int = 0, maximum: int = 2_147_483_647) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        _fail()
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        _fail()
    return value


def _bounded(value: object) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError):
        _fail()
    if len(encoded.encode()) > _MAX_BYTES:
        _fail()

    def walk(item: object, depth: int) -> None:
        if depth > _MAX_DEPTH:
            _fail()
        if isinstance(item, dict):
            if len(item) > 40:
                _fail()
            for key, child in item.items():
                if not isinstance(key, str) or not key or len(key) > 100:
                    _fail()
                walk(child, depth + 1)
        elif isinstance(item, list):
            if len(item) > _MAX_PRODUCTS:
                _fail()
            for child in item:
                walk(child, depth + 1)
        elif isinstance(item, str):
            if len(item) > _MAX_STRING or any(ord(char) < 32 for char in item):
                _fail()
        elif isinstance(item, float):
            if not math.isfinite(item):
                _fail()
        elif item is not None and not isinstance(item, (bool, int)):
            _fail()

    walk(value, 0)


def _exact_object(value: object, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        _fail()
    return cast(dict[str, Any], value)


def _products(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= _MAX_PRODUCTS:
        _fail()
    expected_product = {
        "id", "externalId", "legacyId", "storeProductId", "basketProductId",
        "quantity", "quantityLimit", "customizations",
    }
    expected_customization = {
        "groupId", "groupName", "groupPosition", "attributeId", "attributeName", "quantity",
    }
    result: list[dict[str, Any]] = []
    for product in value:
        row = _exact_object(product, expected_product)
        for key in ("id", "externalId", "legacyId", "storeProductId", "basketProductId"):
            if row[key] is not None:
                _id(row[key])
        quantity = _integer(row["quantity"], minimum=1, maximum=1_000)
        limit = row["quantityLimit"]
        if limit is not None and quantity > _integer(limit, minimum=1, maximum=1_000):
            _fail("mismatch")
        customizations = row["customizations"]
        if not isinstance(customizations, list) or len(customizations) > 50:
            _fail()
        for customization in customizations:
            custom = _exact_object(customization, expected_customization)
            for key in ("groupId", "groupName", "attributeId", "attributeName"):
                _id(custom[key])
            _integer(custom["groupPosition"], maximum=1_000)
            _integer(custom["quantity"], minimum=1, maximum=1_000)
        result.append(row)
    return result


def _safe_status(error: BaseException) -> int | None:
    status = getattr(error, "status", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


@dataclass(frozen=True, slots=True, repr=False)
class FinalCheckoutRequest:
    """Immutable private submit request derived only from an authoritative quote."""

    quote: AuthoritativeQuote = field(repr=False)
    quote_fingerprint: str = field(repr=False)

    @classmethod
    def from_quote(cls, quote: object, *, now: object) -> FinalCheckoutRequest:
        if not isinstance(quote, AuthoritativeQuote):
            _fail()
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(float(now)):
            _fail()
        try:
            fresh = quote.is_fresh(float(now))
            fingerprint = quote.fingerprint
        except (TypeError, ValueError, OverflowError):
            _fail("stale")
        if not fresh:
            _fail("stale")
        return cls(quote=quote, quote_fingerprint=_digest(fingerprint))

    def __post_init__(self) -> None:
        quote = self.quote
        if not isinstance(quote, AuthoritativeQuote):
            _fail()
        _digest(self.quote_fingerprint)
        try:
            valid = (
                self.quote_fingerprint == quote.fingerprint
                and _id(quote.checkout_session_id) == quote.checkout_session_id
                and _integer(quote.version_id, minimum=1) == quote.version_id
                and (quote.template_id is None or _integer(quote.template_id, minimum=1) == quote.template_id)
                and _id(quote.basket_id) == quote.basket_id
                and _id(quote.basket_version) == quote.basket_version
                and _integer(quote.customer_id, minimum=1) == quote.customer_id
                and _integer(quote.store_id, minimum=1) == quote.store_id
                and _integer(quote.store_address_id, minimum=1) == quote.store_address_id
                and _integer(quote.store_category_id, minimum=1) == quote.store_category_id
                and _id(quote.city_code) == quote.city_code
                and _id(quote.handling_strategy) == _HANDLING_STRATEGY
                and _id(quote.owner_key) == quote.owner_key
                and _integer(quote.generation) == quote.generation
                and _id(quote.intent_key) == quote.intent_key
                and _digest(quote.address_fingerprint) == quote.address_fingerprint
                and _digest(quote.payment_fingerprint) == quote.payment_fingerprint
                and _digest(quote.capability_fingerprint) == quote.capability_fingerprint
                and isinstance(quote.total.amount_minor, int)
                and not isinstance(quote.total.amount_minor, bool)
                and quote.total.amount_minor >= 0
                and isinstance(quote.total.currency, str)
                and 1 <= len(quote.exact_products) <= _MAX_PRODUCTS
            )
        except (AttributeError, TypeError, ValueError):
            valid = False
        if not valid:
            _fail("mismatch")
        products = _products([product.canonical_dict() for product in quote.exact_products])
        _bounded(products)

    def private_body(self) -> dict[str, Any]:
        """Exact fixture-only shape; amount is copied from quote authority only."""
        # Recheck the immutable quote/fingerprint binding before every fixture body
        # projection so a bypassed frozen instance cannot alter submitted authority.
        self.__post_init__()
        quote = self.quote
        body = {
            "checkout": {
                "checkoutSessionId": quote.checkout_session_id,
                "versionId": quote.version_id,
                "templateId": quote.template_id,
                "basket": {
                    "id": quote.basket_id,
                    "version": quote.basket_version,
                    "customerId": quote.customer_id,
                    "storeId": quote.store_id,
                    "storeAddressId": quote.store_address_id,
                    "storeCategoryId": quote.store_category_id,
                    "cityCode": quote.city_code,
                    "handlingStrategy": quote.handling_strategy,
                    "products": [product.canonical_dict() for product in quote.exact_products],
                },
                "address": {"fingerprint": quote.address_fingerprint},
                "payment": {"fingerprint": quote.payment_fingerprint},
                "total": {"minor": quote.total.amount_minor, "currency": quote.total.currency},
                "authority": {
                    "quoteFingerprint": self.quote_fingerprint,
                    "generation": quote.generation,
                    "owner": quote.owner_key,
                    "intent": quote.intent_key,
                },
            }
        }
        _bounded(body)
        return body


@dataclass(frozen=True, slots=True, repr=False)
class FinalCheckoutStatus:
    checkout_id: str = field(repr=False)
    state: str

    def __post_init__(self) -> None:
        _id(self.checkout_id)
        if self.state not in _STATUS_STATES:
            _fail("unsupported")

    @property
    def requires_manual_completion(self) -> bool:
        # No completion POST exists in this private fixture adapter.
        return self.state in {"SUBMITTED", "PENDING"}


def _parse_status(payload: object, *, expected_checkout_id: str, submit: bool) -> FinalCheckoutStatus:
    _bounded(payload)
    root = _exact_object(payload, {"checkout"})
    checkout = _exact_object(root["checkout"], {"checkoutId", "state"})
    checkout_id = _id(checkout["checkoutId"])
    state = checkout["state"]
    if checkout_id != expected_checkout_id or not isinstance(state, str):
        _fail("mismatch")
    if submit and state not in _SUBMIT_STATES:
        _fail("unsupported")
    return FinalCheckoutStatus(checkout_id=checkout_id, state=state)


FixtureTransport = Callable[[str, str, dict[str, Any] | None], Awaitable[Any]]


class FixtureOnlyFinalCheckoutAdapter:
    """Unwired fixture seam: exactly one syntactic call per explicit operation."""

    def __init__(self, fixture_transport: FixtureTransport) -> None:
        if not callable(fixture_transport) or not getattr(fixture_transport, "__glovo_fixture_only__", False):
            raise FinalCheckoutUnsupported
        self._fixture_transport = fixture_transport

    async def async_submit(self, request: FinalCheckoutRequest) -> FinalCheckoutStatus:
        if not isinstance(request, FinalCheckoutRequest):
            _fail()
        body = request.private_body()
        try:
            # The sole submit invocation. Never add retry, fallback, refresh,
            # status lookup, compensating delete, or a completion mutation here.
            payload = await self._fixture_transport("POST", FINAL_CHECKOUT_PATH, body)
        except asyncio.CancelledError:
            raise FinalCheckoutAmbiguous from None
        except Exception as err:
            status = _safe_status(err)
            if status in {400, 401, 403, 404, 405, 406, 415, 422}:
                raise FinalCheckoutRejected(status) from None
            raise FinalCheckoutAmbiguous from None
        try:
            # A lost/malformed checkout id is ambiguous; it cannot be queried.
            raw_id = payload["checkout"]["checkoutId"] if isinstance(payload, dict) else None
            return _parse_status(payload, expected_checkout_id=_id(raw_id), submit=True)
        except (FinalCheckoutContractError, KeyError, TypeError, ValueError):
            raise FinalCheckoutAmbiguous from None

    async def async_status(self, checkout_id: str) -> FinalCheckoutStatus:
        """One explicit read-only status GET for a known checkout id, never polling."""
        known_id = _id(checkout_id)
        try:
            payload = await self._fixture_transport("GET", FINAL_STATUS_PATH_PREFIX + known_id, None)
        except asyncio.CancelledError:
            raise FinalCheckoutAmbiguous from None
        except Exception:
            raise FinalCheckoutAmbiguous from None
        try:
            return _parse_status(payload, expected_checkout_id=known_id, submit=False)
        except FinalCheckoutContractError:
            raise FinalCheckoutAmbiguous from None

    async def async_complete(self, checkout_id: str) -> None:
        """Completion is intentionally unsupported absent public, complete evidence."""
        _id(checkout_id)
        raise FinalCheckoutUnsupported
