"""Authoritative checkout-template parsing and private exact-total authority."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

from .api_session import ApiSessionError, MutationPurpose
from .ordering_contracts import AddressSnapshot, SavedPayment
from .ordering_models import (
    ISO_4217_EXPONENTS,
    ItemDisplaySummary,
    MaskedPaymentSummary,
    Money,
    SavedAddressSummary,
    StoreDisplayName,
)
from .ordering_remote_basket import RemoteBasketProduct, RemoteBasketSnapshot

QUOTE_MAX_AGE: Final = 45.0
MAX_TEMPLATE_BYTES: Final = 512_000
MAX_DEPTH: Final = 12
MAX_COMPONENTS: Final = 16
MAX_PRICE_LINES: Final = 40
MAX_STRING: Final = 300
_SOURCE_SCREENS: Final = frozenset({"STORE_PAGE", "BASKET", "CHECKOUT"})
_COMPONENT_TYPES: Final = frozenset(
    {
        "PAYMENT_METHOD_PICKER",
        "PRICE_BREAKDOWN",
        "DELIVERY_ETA",
        "STORE_CAPABILITIES",
    }
)
_CAPABILITIES: Final = frozenset({"DELIVERY", "CREDIT_CARD", "IMMEDIATE"})
_LINE_TYPES: Final = frozenset({"OTHER", "DELIVERY", "TOTAL"})
_LINE_STYLES: Final = frozenset({"DEFAULT", "EMPHASIS", "MUTED"})
_LOCAL_KEY_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class QuoteContractError(ValueError):
    """Sanitized request/response/template authority failure."""

    def __init__(self, category: str = "schema") -> None:
        self.category = category if category in {"schema", "mismatch", "unsupported", "stale"} else "schema"
        super().__init__("checkout template did not satisfy the approved contract")


class QuoteTemplateAmbiguous(RuntimeError):
    """The single template POST failed after dispatch and is never replayed."""

    def __init__(self) -> None:
        super().__init__("checkout template outcome is ambiguous")


class QuoteTemplateRejected(RuntimeError):
    """The provider deterministically rejected template creation."""

    def __init__(self, status: int | None) -> None:
        self.status = status if status in {400, 401, 403, 404, 405, 406, 409, 410, 415, 422, 429} else None
        super().__init__("checkout template was rejected")


class InvalidQuoteConfirmation(RuntimeError):
    """The exact-total challenge is absent, changed, expired, or consumed."""

    def __init__(self) -> None:
        super().__init__("quote confirmation is invalid")


def _fail(category: str = "schema") -> None:
    raise QuoteContractError(category)


def _object(
    value: object, *, required: set[str] | frozenset[str], allowed: set[str] | frozenset[str]
) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail()
    keys = set(value)
    if not set(required).issubset(keys) or not keys.issubset(set(allowed)):
        _fail()
    return value


def _array(value: object, *, minimum: int = 0, maximum: int) -> list[Any]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
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


def _display(value: object, *, maximum: int = 120) -> str:
    text = _text(value, maximum=maximum)
    try:
        return ItemDisplaySummary(text).value
    except (TypeError, ValueError) as err:
        raise QuoteContractError from err


def _id(value: object) -> str:
    text = _text(value, maximum=128)
    if _LOCAL_KEY_RE.fullmatch(text) is None:
        _fail()
    return text


def _int(value: object, *, minimum: int = 0, maximum: int = 100_000_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        _fail()
    return value


def _finite(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail()
    result = float(value)
    if not math.isfinite(result):
        _fail()
    return result


def _bounded_payload(value: object) -> None:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
    except (TypeError, ValueError, OverflowError):
        _fail()
    if len(encoded.encode()) > MAX_TEMPLATE_BYTES:
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


def _canonical_hash(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError, OverflowError) as err:
        raise QuoteContractError from err
    return hashlib.sha256(encoded).hexdigest()


def _address_body(value: AddressSnapshot) -> dict[str, Any]:
    return {
        "id": value.remote_id,
        "addressLine": value.address_line,
        "details": value.details,
        "latitude": value.latitude,
        "longitude": value.longitude,
        "countryCode": value.country_code,
        "cityCode": value.city_code,
        "cityName": value.city_name,
        "kind": value.kind,
        "tag": value.tag,
        "fields": [
            {"type": item.field_type, "value": item.value} for item in value.fields
        ],
    }


def _payment_fingerprint(value: SavedPayment) -> str:
    return _canonical_hash(
        {
            "paymentInstrumentId": value.payment_instrument_id,
            "metadataId": value.metadata_id,
            "displayName": value.display_name,
            "displayDescription": value.display_description,
            "lastFourDigits": value.last_four_digits,
            "selected": value.selected,
            "eligibleType": "CREDIT_CARD",
        }
    )


@dataclass(frozen=True, slots=True, repr=False)
class QuoteRequest:
    """Private normalized new-template request; refresh identifiers are impossible."""

    owner_key: str = field(repr=False)
    generation: int = field(repr=False)
    intent_key: str = field(repr=False)
    source_screen: str
    basket: RemoteBasketSnapshot = field(repr=False)
    delivery_address: AddressSnapshot = field(repr=False)
    payment: SavedPayment = field(repr=False)
    masked_address: str
    masked_payment: str
    store_display_name: str

    def __post_init__(self) -> None:
        _id(self.owner_key)
        _int(self.generation, maximum=2_147_483_647)
        _id(self.intent_key)
        if self.source_screen not in _SOURCE_SCREENS:
            _fail("unsupported")
        if not isinstance(self.basket, RemoteBasketSnapshot):
            _fail()
        if self.basket.handling_strategy != "DELIVERY" or self.basket.city_code is None:
            _fail("unsupported")
        if not isinstance(self.delivery_address, AddressSnapshot):
            _fail()
        if self.delivery_address.city_code != self.basket.city_code:
            _fail("mismatch")
        if not isinstance(self.payment, SavedPayment) or self.payment.selected is not True:
            _fail("unsupported")
        try:
            masked_address = SavedAddressSummary(
                "quote-address", self.masked_address
            ).masked_label
            masked_payment = MaskedPaymentSummary(
                "quote-payment", self.masked_payment
            ).masked_label
            store_name = StoreDisplayName(self.store_display_name).value
        except (TypeError, ValueError) as err:
            raise QuoteContractError from err
        object.__setattr__(self, "masked_address", masked_address)
        object.__setattr__(self, "masked_payment", masked_payment)
        object.__setattr__(self, "store_display_name", store_name)

    @property
    def store_fingerprint(self) -> str:
        basket = self.basket
        return _canonical_hash(
            {
                "storeId": basket.store_id,
                "storeAddressId": basket.store_address_id,
                "storeCategoryId": basket.store_category_id,
                "cityCode": basket.city_code,
                "handlingStrategy": basket.handling_strategy,
                "storeDisplayName": self.store_display_name,
            }
        )

    @property
    def address_fingerprint(self) -> str:
        return self.delivery_address.canonical_fingerprint

    @property
    def payment_fingerprint(self) -> str:
        return _payment_fingerprint(self.payment)

    def private_body(self) -> dict[str, Any]:
        """Build the only approved new-quote shape; never log or persist it."""
        basket = self.basket
        return {
            "checkout": {
                "orderDetails": {
                    "sourceScreen": self.source_screen,
                    "orderType": "STORES",
                    "cityCode": basket.city_code,
                    "categoryId": basket.store_category_id,
                    "handlingStrategy": {"type": "DELIVERY"},
                    "storeId": basket.store_id,
                    "storeAddressId": basket.store_address_id,
                    "basketId": basket.basket_id,
                    "paymentMethodSupport": {
                        "supported": ["CREDIT_CARD"],
                        "available": ["CREDIT_CARD"],
                    },
                },
                "components": {
                    "productList": [item.canonical_dict() for item in basket.products],
                    "deliveryAddress": _address_body(self.delivery_address),
                    "paymentMethod": {
                        "type": "CREDIT_CARD",
                        "paymentInstrumentId": self.payment.payment_instrument_id,
                        "metadataId": self.payment.metadata_id,
                    },
                },
                "analytics": {"templateReceived": None},
                "basketDetails": {
                    "basketVersion": basket.basket_version,
                    "isPrimeSubscriptionSimulated": (
                        basket.is_prime_subscription_simulated is True
                    ),
                },
            }
        }


@dataclass(frozen=True, slots=True)
class ProviderPriceLine:
    title: str
    value: str | None
    line_type: str
    style: str | None = None
    notes: tuple[str, ...] = ()

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "value": self.value,
            "type": self.line_type,
            "style": self.style,
            "notes": list(self.notes),
        }

    def public_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "title": self.title,
            "value": self.value,
            "type": self.line_type,
        }
        if self.style is not None:
            result["style"] = self.style
        if self.notes:
            result["notes"] = list(self.notes)
        return result


@dataclass(frozen=True, slots=True, repr=False)
class AuthoritativeQuote:
    """Private parsed provider authority with a local receipt-time safety lease."""

    checkout_session_id: str = field(repr=False)
    version_id: int = field(repr=False)
    template_id: int | None = field(repr=False)
    basket_id: str = field(repr=False)
    basket_version: str = field(repr=False)
    customer_id: int = field(repr=False)
    store_id: int = field(repr=False)
    store_address_id: int = field(repr=False)
    store_category_id: int = field(repr=False)
    city_code: str = field(repr=False)
    handling_strategy: str = field(repr=False)
    exact_products: tuple[RemoteBasketProduct, ...] = field(repr=False)
    address_fingerprint: str = field(repr=False)
    payment_fingerprint: str = field(repr=False)
    capability_fingerprint: str = field(repr=False)
    owner_key: str = field(repr=False)
    generation: int = field(repr=False)
    intent_key: str = field(repr=False)
    total: Money
    price_lines: tuple[ProviderPriceLine, ...]
    eta: str | None
    received_at: float = field(repr=False)
    store_display_name: str
    masked_address: str
    masked_payment: str
    name: str

    @property
    def expires_at(self) -> float:
        return self.received_at + QUOTE_MAX_AGE

    @property
    def fingerprint(self) -> str:
        return _canonical_hash(
            {
                "checkout": {
                    "sessionDigest": hashlib.sha256(
                        self.checkout_session_id.encode()
                    ).hexdigest(),
                    "versionId": self.version_id,
                    "templateId": self.template_id,
                },
                "basket": {
                    "id": self.basket_id,
                    "version": self.basket_version,
                    "customerId": self.customer_id,
                    "storeId": self.store_id,
                    "storeAddressId": self.store_address_id,
                    "storeCategoryId": self.store_category_id,
                    "cityCode": self.city_code,
                    "handlingStrategy": self.handling_strategy,
                    "products": [
                        item.canonical_dict() for item in self.exact_products
                    ],
                },
                "addressFingerprint": self.address_fingerprint,
                "paymentEligibilityFingerprint": self.payment_fingerprint,
                "total": {
                    "minor": self.total.amount_minor,
                    "currency": self.total.currency,
                },
                "rawPriceLines": [line.canonical_dict() for line in self.price_lines],
                "eta": self.eta,
                "capabilityFingerprint": self.capability_fingerprint,
                "owner": self.owner_key,
                "generation": self.generation,
                "intent": self.intent_key,
            }
        )

    def is_fresh(self, now: object) -> bool:
        current = _finite(now)
        age = current - _finite(self.received_at)
        return 0 <= age < QUOTE_MAX_AGE

    def public_confirmation(self) -> dict[str, Any]:
        count = sum(item.quantity for item in self.exact_products)
        return {
            "store": self.store_display_name,
            "itemSummary": f"{count} item(s)",
            "itemCount": count,
            "address": self.masked_address,
            "payment": self.masked_payment,
            "priceLines": [line.public_dict() for line in self.price_lines],
            "purchaseTotalCents": self.total.amount_minor,
            "currencyCode": self.total.currency,
            "eta": self.eta,
            "expiresAt": self.expires_at,
        }


def _parse_price_lines(value: object) -> tuple[ProviderPriceLine, ...]:
    rows = _array(value, minimum=1, maximum=MAX_PRICE_LINES)
    result: list[ProviderPriceLine] = []
    total_count = 0
    for raw in rows:
        item = _object(
            raw,
            required={"title", "value", "type"},
            allowed={"title", "value", "type", "style", "notes", "actions"},
        )
        line_type = _text(item["type"], maximum=20)
        if line_type not in _LINE_TYPES:
            _fail("unsupported")
        total_count += int(line_type == "TOTAL")
        raw_value = item["value"]
        display_value = (
            None if raw_value is None else _display(raw_value, maximum=100)
        )
        style = item.get("style")
        if style is not None:
            style = _text(style, maximum=20)
            if style not in _LINE_STYLES:
                _fail("unsupported")
        notes_value = item.get("notes", [])
        notes = tuple(
            _display(note, maximum=120)
            for note in _array(notes_value, maximum=4)
        )
        actions = _array(item.get("actions", []), maximum=0)
        if actions:
            _fail("unsupported")
        result.append(
            ProviderPriceLine(
                title=_display(item["title"], maximum=100),
                value=display_value,
                line_type=line_type,
                style=style,
                notes=notes,
            )
        )
    if total_count != 1:
        _fail("mismatch")
    return tuple(result)


def _parse_components(
    value: object, *, total: Money, eta: str | None
) -> tuple[tuple[ProviderPriceLine, ...], str]:
    rows = _array(value, minimum=2, maximum=MAX_COMPONENTS)
    seen: set[str] = set()
    lines: tuple[ProviderPriceLine, ...] | None = None
    normalized: list[dict[str, Any]] = []
    for raw in rows:
        component = _object(
            raw, required={"type", "data"}, allowed={"type", "data"}
        )
        component_type = _text(component["type"], maximum=40)
        if component_type not in _COMPONENT_TYPES or component_type in seen:
            _fail("unsupported")
        seen.add(component_type)
        data = component["data"]
        if component_type == "PAYMENT_METHOD_PICKER":
            wrapper = _object(
                data,
                required={"paymentMethodPickerData"},
                allowed={"paymentMethodPickerData"},
            )
            picker = _object(
                wrapper["paymentMethodPickerData"],
                required={"orderTotal", "currencyCode"},
                allowed={"orderTotal", "currencyCode"},
            )
            if (
                _int(picker["orderTotal"]) != total.amount_minor
                or picker["currencyCode"] != total.currency
            ):
                _fail("mismatch")
            normalized.append(
                {
                    "type": component_type,
                    "orderTotal": total.amount_minor,
                    "currencyCode": total.currency,
                }
            )
        elif component_type == "PRICE_BREAKDOWN":
            wrapper = _object(
                data,
                required={"priceBreakdownData"},
                allowed={"priceBreakdownData"},
            )
            breakdown = _object(
                wrapper["priceBreakdownData"],
                required={"breakDown"},
                allowed={"breakDown"},
            )
            lines = _parse_price_lines(breakdown["breakDown"])
            normalized.append(
                {"type": component_type, "lines": [line.canonical_dict() for line in lines]}
            )
        elif component_type == "DELIVERY_ETA":
            eta_data = _object(data, required={"eta"}, allowed={"eta"})
            component_eta = eta_data["eta"]
            if component_eta is not None:
                component_eta = _display(component_eta, maximum=100)
            if component_eta != eta:
                _fail("mismatch")
            normalized.append({"type": component_type, "eta": component_eta})
        else:
            capability_data = _object(
                data, required={"capabilities"}, allowed={"capabilities"}
            )
            raw_capabilities = _array(
                capability_data["capabilities"], minimum=1, maximum=len(_CAPABILITIES)
            )
            capabilities = [_text(item, maximum=30) for item in raw_capabilities]
            if (
                len(set(capabilities)) != len(capabilities)
                or any(item not in _CAPABILITIES for item in capabilities)
                or "DELIVERY" not in capabilities
                or "CREDIT_CARD" not in capabilities
                or "IMMEDIATE" not in capabilities
            ):
                _fail("unsupported")
            normalized.append({"type": component_type, "capabilities": capabilities})
    if (
        "PAYMENT_METHOD_PICKER" not in seen
        or "PRICE_BREAKDOWN" not in seen
        or "DELIVERY_ETA" not in seen
        or "STORE_CAPABILITIES" not in seen
        or lines is None
    ):
        _fail("unsupported")
    return lines, _canonical_hash(normalized)


def parse_quote_template(
    payload: object, *, request: QuoteRequest, received_at: object
) -> AuthoritativeQuote:
    """Parse only response.data.checkout and bind it to the exact request."""
    if not isinstance(request, QuoteRequest):
        _fail()
    receipt = _finite(received_at)
    if not math.isfinite(receipt + QUOTE_MAX_AGE):
        _fail("stale")
    _bounded_payload(payload)
    root = _object(payload, required={"response"}, allowed={"response"})
    response = _object(root["response"], required={"data"}, allowed={"data"})
    data = _object(response["data"], required={"checkout"}, allowed={"checkout"})
    checkout = _object(
        data["checkout"],
        required={"name", "orderType", "enabled", "orderDetails", "components", "analytics"},
        allowed={"name", "orderType", "enabled", "orderDetails", "components", "analytics"},
    )
    if checkout["orderType"] != "STORES" or checkout["enabled"] is not True:
        _fail("unsupported")
    details = _object(
        checkout["orderDetails"],
        required={
            "checkoutSessionId",
            "versionId",
            "storeId",
            "storeAddressId",
            "basketId",
            "currencyCode",
            "purchaseTotalCents",
            "basketVersion",
            "eta",
            "legalVerificationRequired",
        },
        allowed={
            "checkoutSessionId",
            "versionId",
            "templateId",
            "storeId",
            "storeAddressId",
            "basketId",
            "basketWidgetId",
            "currencyCode",
            "purchaseTotalCents",
            "basketVersion",
            "eta",
            "legalVerificationRequired",
        },
    )
    checkout_session_id = _id(details["checkoutSessionId"])
    version_id = _int(details["versionId"], maximum=2_147_483_647)
    template_value = details.get("templateId")
    template_id = (
        None if template_value is None else _int(template_value, minimum=1, maximum=2_147_483_647)
    )
    widget_id = details.get("basketWidgetId")
    if widget_id is not None:
        _id(widget_id)
    currency = details["currencyCode"]
    if not isinstance(currency, str) or currency not in ISO_4217_EXPONENTS:
        _fail("unsupported")
    total = Money(_int(details["purchaseTotalCents"]), currency)
    basket = request.basket
    basket_id = _id(details["basketId"])
    basket_version = _id(details["basketVersion"])
    store_id = _int(details["storeId"], minimum=1, maximum=2_147_483_647)
    store_address_id = _int(
        details["storeAddressId"], minimum=1, maximum=2_147_483_647
    )
    if (
        store_id != basket.store_id
        or store_address_id != basket.store_address_id
        or basket_id != basket.basket_id
        or basket_version != basket.basket_version
    ):
        _fail("mismatch")
    legal = details["legalVerificationRequired"]
    if legal not in {False, None} or isinstance(legal, int) and not isinstance(legal, bool):
        _fail("unsupported")
    eta_value = details["eta"]
    eta = None if eta_value is None else _display(eta_value, maximum=100)
    price_lines, capability_fingerprint = _parse_components(
        checkout["components"], total=total, eta=eta
    )
    analytics = _object(
        checkout["analytics"],
        required=set(),
        allowed={"sourceScreen", "templateReceived"},
    )
    if "sourceScreen" in analytics and analytics["sourceScreen"] not in _SOURCE_SCREENS:
        _fail("unsupported")
    if "templateReceived" in analytics and not isinstance(analytics["templateReceived"], bool):
        _fail()
    return AuthoritativeQuote(
        checkout_session_id=checkout_session_id,
        version_id=version_id,
        template_id=template_id,
        basket_id=basket_id,
        basket_version=basket_version,
        customer_id=basket.customer_id,
        store_id=store_id,
        store_address_id=store_address_id,
        store_category_id=basket.store_category_id,
        city_code=basket.city_code or "",
        handling_strategy=basket.handling_strategy,
        exact_products=basket.products,
        address_fingerprint=request.address_fingerprint,
        payment_fingerprint=request.payment_fingerprint,
        capability_fingerprint=capability_fingerprint,
        owner_key=request.owner_key,
        generation=request.generation,
        intent_key=request.intent_key,
        total=total,
        price_lines=price_lines,
        eta=eta,
        received_at=receipt,
        store_display_name=request.store_display_name,
        masked_address=request.masked_address,
        masked_payment=request.masked_payment,
        name=_display(checkout["name"], maximum=100),
    )


class QuoteTemplateClient:
    """Private single-POST template client with no checkout dispatch method."""

    def __init__(
        self,
        session: Any,
        *,
        clock: Callable[[], float] = time.monotonic,
        invalidate_authority: Callable[[], None] | None = None,
    ) -> None:
        self._session = session
        self._clock = clock
        self._invalidate_authority = invalidate_authority or (lambda: None)

    async def async_create(self, request: QuoteRequest) -> AuthoritativeQuote:
        if not isinstance(request, QuoteRequest):
            raise QuoteContractError
        self._invalidate_authority()
        try:
            payload = await self._session.async_mutate(
                MutationPurpose.CREATE_QUOTE_TEMPLATE,
                "POST",
                "/v3/checkouts/order/1/template",
                request.private_body(),
            )
        except ApiSessionError as err:
            if err.category in {"transport", "schema"} or err.status in {500, 502, 503, 504}:
                raise QuoteTemplateAmbiguous from err
            raise QuoteTemplateRejected(err.status) from err
        try:
            return parse_quote_template(
                payload, request=request, received_at=self._clock()
            )
        except QuoteContractError as err:
            raise QuoteTemplateAmbiguous from err


@dataclass(frozen=True, slots=True, repr=False)
class _PreparedConfirmation:
    owner_key: str = field(repr=False)
    challenge: str = field(repr=False)
    fingerprint: str = field(repr=False)


class AuthoritativeConfirmationManager:
    """Ephemeral single-use confirmation store; it has no purchase operation."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        challenge_source: Callable[[], str] | None = None,
    ) -> None:
        self._clock = clock
        self._challenge_source = challenge_source or (
            lambda: secrets.token_urlsafe(32)
        )
        self._quotes: dict[str, AuthoritativeQuote] = {}
        self._confirmations: dict[str, _PreparedConfirmation] = {}

    def invalidate_all(self) -> None:
        self._quotes.clear()
        self._confirmations.clear()

    def invalidate(self, owner_key: str) -> None:
        self._quotes.pop(owner_key, None)
        self._confirmations.pop(owner_key, None)

    def install(self, quote: AuthoritativeQuote) -> dict[str, Any]:
        try:
            fresh = isinstance(quote, AuthoritativeQuote) and quote.is_fresh(
                self._clock()
            )
        except (QuoteContractError, TypeError, ValueError):
            fresh = False
        if not fresh:
            if isinstance(quote, AuthoritativeQuote):
                self.invalidate(quote.owner_key)
            raise InvalidQuoteConfirmation
        self.invalidate(quote.owner_key)
        self._quotes[quote.owner_key] = quote
        return quote.public_confirmation()

    def prepare(self, *, owner_key: str) -> dict[str, Any]:
        quote = self._quotes.get(owner_key)
        try:
            fresh = quote is not None and quote.is_fresh(self._clock())
        except (QuoteContractError, TypeError, ValueError):
            fresh = False
        if quote is None or not fresh:
            self.invalidate(owner_key)
            raise InvalidQuoteConfirmation
        challenge = self._challenge_source()
        if not isinstance(challenge, str) or not 24 <= len(challenge) <= 200:
            self.invalidate(owner_key)
            raise InvalidQuoteConfirmation
        self._confirmations[owner_key] = _PreparedConfirmation(
            owner_key=owner_key,
            challenge=challenge,
            fingerprint=quote.fingerprint,
        )
        return {**quote.public_confirmation(), "challenge": challenge}

    def consume(
        self,
        *,
        owner_key: str,
        challenge: str,
        current: AuthoritativeQuote,
    ) -> AuthoritativeQuote:
        prepared = self._confirmations.pop(owner_key, None)
        installed = self._quotes.get(owner_key)
        try:
            valid = bool(
                prepared is not None
                and installed is not None
                and isinstance(current, AuthoritativeQuote)
                and isinstance(challenge, str)
                and prepared.owner_key == owner_key
                and current.owner_key == owner_key
                and hmac.compare_digest(prepared.challenge, challenge)
                and hmac.compare_digest(prepared.fingerprint, current.fingerprint)
                and hmac.compare_digest(installed.fingerprint, current.fingerprint)
                and current.is_fresh(self._clock())
            )
        except (QuoteContractError, TypeError, ValueError):
            valid = False
        if not valid:
            self.invalidate(owner_key)
            raise InvalidQuoteConfirmation
        # A valid confirmation is consumed too. No refresh is possible between
        # this boundary and a future separately reviewed dispatch implementation.
        self._quotes.pop(owner_key, None)
        return current
