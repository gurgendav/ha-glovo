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
from decimal import Decimal, InvalidOperation
from typing import Any, Final, NoReturn, cast

from .api_session import ApiSessionError, DeliveryLocation, MutationPurpose
from .ordering_contracts import AddressSnapshot, SavedPayment
from .ordering_models import (
    ISO_4217_EXPONENTS,
    ItemDisplaySummary,
    MaskedPaymentSummary,
    Money,
    SavedAddressSummary,
    StoreDisplayName,
)
from .ordering_remote_basket import (
    RemoteBasketResponseProduct,
    RemoteBasketSnapshot,
)

QUOTE_MAX_AGE: Final = 45.0
MAX_TEMPLATE_BYTES: Final = 512_000
MAX_DEPTH: Final = 12
MAX_COMPONENTS: Final = 64
MAX_PRICE_LINES: Final = 40
MAX_STRING: Final = 300
_SOURCE_SCREENS: Final = frozenset({"CART", "REORDER", "UNKNOWN"})
_NOTE_STYLES: Final = frozenset({"PRIME", "ALERT"})
_PRICE_ACTIONS: Final = frozenset({"SHOW_FEES_BREAKDOWN", "SHOW_DELIVERY_BREAKDOWN"})
_FEE_LABEL_STYLES: Final = frozenset(
    {"LOYALTY", "PLAIN", "PROMOTION", "STRIKETHROUGH"}
)
# Exact response id -> (discriminated component type, provider data key) pairs from
# the first-party checkout component record.  Keeping the pair closed prevents a
# schema-valid type from being attached to the wrong neighboring component id.
_WEB_RESPONSE_COMPONENTS: Final = {
    "additionalInfo": ("additionalInfo", "infoPanelData"),
    "allergies": ("textInput", "textInputData"),
    "bagCostDisclaimer": ("staticText", "staticTextData"),
    "cashDisclaimer": ("staticText", "staticTextData"),
    "courierDisclaimer": ("infoPanel", "infoPanelData"),
    "courierTip": ("singleOptionChoice", "singleOptionChoiceData"),
    "courierTipping": ("staticText", "staticTextData"),
    "cutlery": ("binaryChoice", "binaryChoiceData"),
    "deliveryAddress": ("addressPicker", "addressPickerData"),
    "deliveryOptions": ("staticText", "staticTextData"),
    "edenredSuggestion": ("infoPanel", "infoPanelData"),
    "foodIntermediatorDisclaimer": ("staticText", "staticTextData"),
    "legalDisclaimer": ("staticText", "staticTextData"),
    "legalVerificationBanner": ("infoPanel", "infoPanelData"),
    "mcdBagCostDisclaimer": ("staticText", "staticTextData"),
    "mealVoucherSelection": (
        "mealVoucherSelectionInput",
        "mealVoucherSelectionInputData",
    ),
    "omnibusSummaryDisclaimer": ("staticText", "staticTextData"),
    "orderContent": ("orderContent", "orderContentData"),
    "paymentMethod": ("paymentMethodPicker", "paymentMethodPickerData"),
    "paymentMethodBanner": ("infoPanel", "infoPanelData"),
    "paymentMethodHeader": ("staticText", "staticTextData"),
    "persistentTipCheckBox": ("binaryChoice", "binaryChoiceData"),
    "phone": ("phoneInput", "phoneInputData"),
    "pickupAddress": ("addressPicker", "addressPickerData"),
    "placeOrder": ("placeOrder", "buttonData"),
    "priceBreakdown": ("priceBreakdown", "priceBreakdownData"),
    "primeUpsell": ("primeUpsell", "primeUpsellData"),
    "productList": ("productList", "productListData"),
    "promocode": ("promoInput", "promoInputData"),
    "recipientDetails": ("recipientDetails", "recipientDetailsData"),
    "savingsInfo": ("savingsInfo", "savingsInfoData"),
    "schedulingTime": ("timeSelector", "timeSelectorData"),
    "weightedProductInfoBanner": ("infoPanel", "infoPanelData"),
}
_COMPONENT_BASE_FIELDS: Final = frozenset(
    {"id", "type", "placement", "triggersRefresh"}
)
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

    category = "provider_rejection"

    def __init__(self, status: int | None) -> None:
        self.status = status if status in {400, 401, 403, 404, 405, 406, 409, 410, 415, 422, 429} else None
        super().__init__("checkout template was rejected")


class InvalidQuoteConfirmation(RuntimeError):
    """The exact-total challenge is absent, changed, expired, or consumed."""

    def __init__(self) -> None:
        super().__init__("quote confirmation is invalid")


def _fail(category: str = "schema") -> NoReturn:
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
        "label": value.address_line,
        "details": value.details,
        "latitude": value.latitude,
        "longitude": value.longitude,
        # The current web client sends only postal custom fields here. Saved-address
        # structural fields are not provider custom-field IDs, so never project them.
        "customFields": [],
    }


def _payment_fingerprint(value: SavedPayment) -> str:
    return _canonical_hash(
        {
            "paymentInstrumentId": value.payment_instrument_id,
            "metadataId": value.metadata_id,
            "metadataIdPresent": value.metadata_id_present,
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
    full_address: str = field(default="Saved destination", repr=False)
    item_display: tuple[dict[str, Any], ...] = field(default=(), repr=False)

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
        _text(self.full_address, maximum=300)
        _bounded_payload(list(self.item_display))
        for item in self.item_display:
            row = _object(
                item,
                required={"name", "quantity", "options"},
                allowed={"name", "quantity", "options"},
            )
            _display(row["name"], maximum=120)
            _int(row["quantity"], minimum=1, maximum=1_000)
            for option in _array(row["options"], maximum=100):
                _display(option, maximum=120)

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

    @property
    def delivery_location(self) -> DeliveryLocation:
        """Return the private location context bound to the selected address."""
        return DeliveryLocation(
            self.delivery_address.country_code,
            self.delivery_address.city_code,
            self.delivery_address.latitude,
            self.delivery_address.longitude,
        )

    def private_body(self) -> dict[str, Any]:
        """Build the only approved new-quote shape; never log or persist it."""
        basket = self.basket
        credit_card: dict[str, Any] = {}
        if self.payment.metadata_id_present:
            credit_card["token"] = self.payment.metadata_id
        return {
            "checkout": {
                "sourceScreen": self.source_screen,
                "orderDetails": {
                    "orderType": "STORES",
                    "origin": "CHECKOUT",
                    "cityCode": basket.city_code,
                    "categoryId": basket.store_category_id,
                    "handlingStrategy": {"type": "DELIVERY"},
                    "storeId": basket.store_id,
                    "storeAddressId": basket.store_address_id,
                    "basketId": basket.basket_id,
                    "paymentMethodSupport": {
                        "clientSupports": [],
                        "clientReady": [],
                    },
                },
                "components": {
                    "productList": [item.canonical_dict() for item in basket.products],
                    "deliveryAddress": _address_body(self.delivery_address),
                    "paymentMethod": {
                        "paymentInstrumentId": self.payment.payment_instrument_id,
                        "type": "CreditCard",
                        "creditCard": credit_card,
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
    value_prefix: str | None = None
    value_prefix_style: str | None = None
    note_style: str | None = None

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "value": self.value,
            "type": self.line_type,
            "style": self.style,
            "notes": list(self.notes),
            "valuePrefix": self.value_prefix,
            "valuePrefixStyle": self.value_prefix_style,
            "noteStyle": self.note_style,
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
        if self.value_prefix is not None:
            result["valuePrefix"] = self.value_prefix
        if self.value_prefix_style is not None:
            result["valuePrefixStyle"] = self.value_prefix_style
        if self.note_style is not None:
            result["noteStyle"] = self.note_style
        return result


@dataclass(frozen=True, slots=True, repr=False)
class AuthoritativeQuote:
    """Private parsed provider authority with a local receipt-time safety lease."""

    checkout_session_id: str = field(repr=False)
    version_id: int = field(repr=False)
    template_id: int | None = field(repr=False)
    basket_id: str = field(repr=False)
    basket_version: str = field(repr=False)
    customer_id: str = field(repr=False)
    store_id: int = field(repr=False)
    store_address_id: int = field(repr=False)
    store_category_id: int = field(repr=False)
    city_code: str = field(repr=False)
    handling_strategy: str = field(repr=False)
    exact_products: tuple[RemoteBasketResponseProduct, ...] = field(repr=False)
    address_fingerprint: str = field(repr=False)
    payment_fingerprint: str = field(repr=False)
    capability_fingerprint: str = field(repr=False)
    owner_key: str = field(repr=False)
    generation: int = field(repr=False)
    intent_key: str = field(repr=False)
    delivery_location: DeliveryLocation = field(repr=False)
    total: Money
    price_lines: tuple[ProviderPriceLine, ...]
    eta: str | None
    received_at: float = field(repr=False)
    store_display_name: str
    masked_address: str
    masked_payment: str
    name: str
    submit_projection_bytes: bytes = field(default=b"{}", repr=False)
    full_address: str = field(default="Saved destination", repr=False)
    item_display: tuple[dict[str, Any], ...] = field(default=(), repr=False)

    def exact_submit_projection(self) -> dict[str, Any]:
        """Return a fresh copy of the exact immutable template submit authority."""
        value: object = None
        try:
            value = json.loads(self.submit_projection_bytes)
        except (TypeError, ValueError):
            _fail("mismatch")
        if not isinstance(value, dict):
            _fail("mismatch")
        _bounded_payload(value)
        return cast(dict[str, Any], value)

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
                "deliveryContext": self.delivery_location.transport_context(),
                "submitProjectionHash": hashlib.sha256(self.submit_projection_bytes).hexdigest(),
                "confirmationDisplay": {
                    "store": self.store_display_name,
                    "items": list(self.item_display),
                    "fullAddress": self.full_address,
                    "payment": self.masked_payment,
                },
            }
        )

    def is_fresh(self, now: object) -> bool:
        current = _finite(now)
        age = current - _finite(self.received_at)
        return 0 <= age < QUOTE_MAX_AGE

    def public_confirmation(self) -> dict[str, Any]:
        count = sum(item.quantity.increments for item in self.exact_products)
        return {
            "store": self.store_display_name,
            "itemSummary": f"{count} item(s)",
            "itemCount": count,
            "items": [dict(item) for item in self.item_display],
            "address": self.full_address,
            "payment": self.masked_payment,
            "priceLines": [line.public_dict() for line in self.price_lines],
            "purchaseTotalCents": self.total.amount_minor,
            "currencyCode": self.total.currency,
            "eta": self.eta,
            "expiresAt": self.expires_at,
        }


def _major_to_minor(value: object, currency: str) -> int:
    """Convert provider picker major units to exact local integer minor units."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail()
    if not math.isfinite(float(value)) or currency not in ISO_4217_EXPONENTS:
        _fail()
    try:
        scaled = Decimal(str(value)) * (Decimal(10) ** ISO_4217_EXPONENTS[currency])
        integral = scaled.to_integral_value()
    except (InvalidOperation, ValueError, OverflowError):
        _fail()
    if scaled != integral:
        _fail("mismatch")
    return _int(int(integral), maximum=100_000_000_000)


def _nullable_display(value: object, *, maximum: int = 120) -> str | None:
    return None if value is None else _display(value, maximum=maximum)


def _validate_fee_label(value: object) -> None:
    """Validate the exact provider delivery-fee label object without using it."""

    label = _object(
        value,
        required={"style"},
        allowed={"value", "style"},
    )
    if label.get("value") is not None:
        _text(label["value"], maximum=120, allow_empty=True)
    if label["style"] not in _FEE_LABEL_STYLES:
        _fail("unsupported")


def _parse_price_lines(value: object) -> tuple[ProviderPriceLine, ...]:
    rows = _array(value, minimum=1, maximum=MAX_PRICE_LINES)
    result: list[ProviderPriceLine] = []
    total_count = 0
    required = {
        "title", "value", "type", "isNoteHighlighted", "noteStyle", "showDivider"
    }
    allowed = required | {
        "value",
        "isNoteHighlighted",
        "note",
        "noteStyle",
        "showDivider",
        "valuePrefix",
        "valueStyle",
        "valuePrefixStyle",
        "action",
        "actionResource",
    }
    for raw in rows:
        item = _object(raw, required=required, allowed=allowed)
        line_type = _text(item["type"], maximum=20)
        if line_type not in {"OTHER", "DELIVERY", "TOTAL"}:
            _fail("unsupported")
        total_count += int(line_type == "TOTAL")
        for key in ("isNoteHighlighted", "showDivider"):
            if not isinstance(item[key], bool):
                _fail()
        note_style = item.get("noteStyle")
        if note_style is not None:
            note_style = _text(note_style, maximum=20)
            if note_style not in _NOTE_STYLES:
                _fail("unsupported")
        action = item.get("action")
        if action is not None:
            action = _text(action, maximum=40)
            if action not in _PRICE_ACTIONS:
                _fail("unsupported")
        action_resource = item.get("actionResource")
        if action_resource is not None:
            _text(action_resource, maximum=MAX_STRING)
        note = _nullable_display(item.get("note"))
        result.append(
            ProviderPriceLine(
                title=_display(item["title"], maximum=100),
                value=_nullable_display(item.get("value"), maximum=100),
                line_type=line_type,
                style=_nullable_display(item.get("valueStyle"), maximum=40),
                notes=() if note is None else (note,),
                value_prefix=_nullable_display(item.get("valuePrefix"), maximum=100),
                value_prefix_style=_nullable_display(
                    item.get("valuePrefixStyle"), maximum=40
                ),
                note_style=note_style,
            )
        )
    if total_count != 1:
        _fail("mismatch")
    return tuple(result)


def _parse_components(
    value: object, *, total: Money, request: QuoteRequest
) -> tuple[tuple[ProviderPriceLine, ...], str, str]:
    rows = _array(value, minimum=2, maximum=MAX_COMPONENTS)
    seen: set[str] = set()
    lines: tuple[ProviderPriceLine, ...] | None = None
    eta: str | None = None
    normalized: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict) or not {"id", "type"}.issubset(raw):
            _fail()
        component_id = _text(raw["id"], maximum=60)
        component_type = _text(raw["type"], maximum=60)
        component_schema = _WEB_RESPONSE_COMPONENTS.get(component_id)
        if (
            component_schema is None
            or component_type != component_schema[0]
            or component_id in seen
        ):
            _fail("unsupported")
        seen.add(component_id)
        placement = raw.get("placement")
        if placement is not None:
            allowed_placements = (
                {"floating", "summary"}
                if component_id == "placeOrder"
                else {"main", "summary", "floating"}
            )
            if placement not in allowed_placements:
                _fail("unsupported")
        triggers_refresh = raw.get("triggersRefresh")
        if triggers_refresh is not None and not isinstance(triggers_refresh, bool):
            _fail()
        if component_id == "paymentMethod":
            if component_type != "paymentMethodPicker":
                _fail("unsupported")
            component = _object(
                raw,
                required={"id", "type", "paymentMethodPickerData"},
                allowed=_COMPONENT_BASE_FIELDS | {"paymentMethodPickerData"},
            )
            picker = _object(
                component["paymentMethodPickerData"],
                required={
                    "required", "countryCode", "currencyCode", "priceStatus",
                    "orderTotal", "productsTotal", "value",
                },
                allowed={
                    "required", "countryCode", "currencyCode", "priceStatus",
                    "orderTotal", "productsTotal", "cash", "value",
                },
            )
            if (
                picker["required"] is not True
                or picker["countryCode"] != request.delivery_address.country_code
                or picker["currencyCode"] != total.currency
                or _major_to_minor(picker["orderTotal"], total.currency) != total.amount_minor
                or request.basket.basket_price.minor is None
                or _major_to_minor(picker["productsTotal"], total.currency)
                != request.basket.basket_price.minor
            ):
                _fail("mismatch")
            _text(picker["priceStatus"], maximum=40)
            cash = picker.get("cash")
            if cash is not None:
                cash_data = _object(
                    cash, required={"isAllowed"}, allowed={"isAllowed", "message"}
                )
                if not isinstance(cash_data["isAllowed"], bool):
                    _fail()
                if cash_data.get("message") is not None:
                    _display(cash_data["message"], maximum=120)
            payment_value = _object(
                picker["value"],
                required={"type", "paymentInstrumentId", "creditCard"},
                allowed={"type", "isDefault", "tags", "paymentInstrumentId", "creditCard"},
            )
            if (
                payment_value["type"] != "CreditCard"
                or payment_value["paymentInstrumentId"] != request.payment.payment_instrument_id
            ):
                _fail("mismatch")
            if "isDefault" in payment_value and not isinstance(payment_value["isDefault"], bool):
                _fail()
            if "tags" in payment_value:
                for tag in _array(payment_value["tags"], maximum=16):
                    _text(tag, maximum=60)
            card = _object(
                payment_value["creditCard"],
                required=set(),
                allowed={
                    "lastFourDigits", "provider", "paymentProvider", "defaultCard", "token"
                },
            )
            response_token = card.get("token")
            if response_token is not None:
                if isinstance(response_token, bool) or not isinstance(
                    response_token, (str, int, float)
                ):
                    _fail()
                if isinstance(response_token, (int, float)) and not math.isfinite(
                    float(response_token)
                ):
                    _fail()
            request_token = request.payment.metadata_id
            if request.payment.metadata_id_present:
                if "token" not in card or response_token != request_token:
                    _fail("mismatch")
            elif "token" in card:
                _fail("mismatch")
            suffix = card.get("lastFourDigits")
            if suffix is not None:
                if not isinstance(suffix, str) or re.fullmatch(r"\d{1,4}", suffix) is None:
                    _fail()
                if suffix != request.payment.last_four_digits:
                    _fail("mismatch")
            elif request.payment.last_four_digits is not None:
                _fail("mismatch")
            for key in ("provider", "paymentProvider"):
                if card.get(key) is not None:
                    _text(card[key], maximum=80)
            if "defaultCard" in card and not isinstance(card["defaultCard"], bool):
                _fail()
            normalized.append(
                {
                    "id": component_id,
                    "type": component_type,
                    "orderTotalMinor": total.amount_minor,
                    "productsTotalMinor": request.basket.basket_price.minor,
                    "countryCode": request.delivery_address.country_code,
                    "currencyCode": total.currency,
                    "paymentFingerprint": request.payment_fingerprint,
                }
            )
        elif component_id == "priceBreakdown":
            if component_type != "priceBreakdown":
                _fail("unsupported")
            component = _object(
                raw,
                required={"id", "type", "priceBreakdownData"},
                allowed=_COMPONENT_BASE_FIELDS | {"priceBreakdownData"},
            )
            breakdown = _object(
                component["priceBreakdownData"],
                required={"breakDown"},
                allowed={"breakDown"},
            )
            lines = _parse_price_lines(breakdown["breakDown"])
            normalized.append(
                {"id": component_id, "type": component_type, "lines": [line.canonical_dict() for line in lines]}
            )
        elif component_id == "schedulingTime":
            if component_type != "timeSelector":
                _fail("unsupported")
            component = _object(
                raw,
                required={"id", "type", "timeSelectorData"},
                allowed=_COMPONENT_BASE_FIELDS | {"timeSelectorData"},
            )
            time_data = _object(
                component["timeSelectorData"],
                required={"required", "selectors", "icon", "iconSelected"},
                allowed={"required", "selectors", "value", "icon", "iconSelected"},
            )
            if not isinstance(time_data["required"], bool):
                _fail()
            selected_value_raw = time_data.get("value")
            if selected_value_raw is None:
                # Optional in the provider display schema, but no authoritative ETA
                # can be selected without it for a paid quote.
                _fail("mismatch")
            selected_value = _text(selected_value_raw, maximum=80)
            for key in ("icon", "iconSelected"):
                icon = _object(
                    time_data[key],
                    required={"lightImageId", "darkImageId"},
                    allowed={"lightImageId", "darkImageId"},
                )
                _text(icon["lightImageId"], maximum=120)
                _text(icon["darkImageId"], maximum=120)
            selected_descriptions: list[str] = []
            selectors = _array(time_data["selectors"], minimum=1, maximum=16)
            for selector_raw in selectors:
                selector = _object(
                    selector_raw,
                    required={"label", "description", "disabled"},
                    allowed={
                        "value", "label", "description", "disabled",
                        "mainDeliveryFeeLabel", "secondaryDeliveryFeeLabel", "feeTag",
                        "options",
                    },
                )
                selector_value_raw = selector.get("value")
                selector_value = (
                    None
                    if selector_value_raw is None
                    else _text(selector_value_raw, maximum=80)
                )
                _display(selector["label"], maximum=100)
                description = _display(selector["description"], maximum=100)
                if not isinstance(selector["disabled"], bool):
                    _fail()
                for key in (
                    "mainDeliveryFeeLabel", "secondaryDeliveryFeeLabel", "feeTag"
                ):
                    if key in selector:
                        _validate_fee_label(selector[key])
                if "options" in selector:
                    for option_raw in _array(selector["options"], maximum=32):
                        option = _object(
                            option_raw,
                            required={"label", "timeSlots"},
                            allowed={"label", "timeSlots"},
                        )
                        _display(option["label"], maximum=100)
                        for slot_raw in _array(option["timeSlots"], maximum=64):
                            slot = _object(
                                slot_raw,
                                required={"label", "value"},
                                allowed={"label", "value"},
                            )
                            _display(slot["label"], maximum=100)
                            _text(slot["value"], maximum=100)
                if selector_value == selected_value and selector["disabled"] is False:
                    selected_descriptions.append(description)
            if len(selected_descriptions) != 1:
                _fail("mismatch")
            eta = selected_descriptions[0]
            normalized.append(
                {"id": component_id, "type": component_type, "value": selected_value, "eta": eta}
            )
        else:
            # The first-party validator owns these display-only schemas. We accept
            # only its closed id/type/data-key pairing, never execute actions/URLs,
            # and bind the bounded opaque data into the quote fingerprint.
            data_key = component_schema[1]
            _object(
                raw,
                required={"id", "type", data_key},
                allowed=_COMPONENT_BASE_FIELDS | {data_key},
            )
            normalized.append({"id": component_id, "type": component_type, "hash": _canonical_hash(raw)})
    if (
        "paymentMethod" not in seen
        or "priceBreakdown" not in seen
        or "schedulingTime" not in seen
        or lines is None
        or eta is None
    ):
        _fail("unsupported")
    return lines, eta, _canonical_hash(normalized)


def parse_quote_template(
    payload: object, *, request: QuoteRequest, received_at: object
) -> AuthoritativeQuote:
    """Parse the current HTTP ``checkout`` envelope and bind it to the request."""
    if not isinstance(request, QuoteRequest):
        _fail()
    receipt = _finite(received_at)
    if not math.isfinite(receipt + QUOTE_MAX_AGE):
        _fail("stale")
    _bounded_payload(payload)
    root = _object(payload, required={"checkout"}, allowed={"checkout"})
    checkout = _object(
        root["checkout"],
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
            "isLegalVerificationRequired",
        },
        allowed={
            "checkoutSessionId",
            "versionId",
            "templateId",
            "storeId",
            "storeAddressId",
            "basketId",
            "basketCreationWidgetId",
            "currencyCode",
            "purchaseTotalCents",
            "basketVersion",
            "isLegalVerificationRequired",
        },
    )
    checkout_session_id = _id(details["checkoutSessionId"])
    version_id = _int(details["versionId"], maximum=2_147_483_647)
    template_value = details.get("templateId")
    template_id = (
        None if template_value is None else _int(template_value, minimum=1, maximum=2_147_483_647)
    )
    widget_id = details.get("basketCreationWidgetId")
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
    legal = details["isLegalVerificationRequired"]
    if legal not in {False, None} or isinstance(legal, int) and not isinstance(legal, bool):
        _fail("unsupported")
    price_lines, eta, capability_fingerprint = _parse_components(
        checkout["components"], total=total, request=request
    )
    analytics = _object(
        checkout["analytics"],
        required=set(),
        allowed={"templateReceived", "checkoutLoaded"},
    )
    # Analytics payloads are opaque provider telemetry echoed by the web client.
    # They are already bounded above and are never exposed publicly.
    request_checkout = request.private_body()["checkout"]
    projection_details = dict(request_checkout["orderDetails"])
    projection_details["versionId"] = version_id
    projection_details["basketId"] = basket_id
    if widget_id is not None:
        projection_details["basketCreationWidgetId"] = widget_id
    projection_details["checkoutSessionId"] = checkout_session_id
    submit_projection = {
        "orderDetails": projection_details,
        # Browser submission echoes the private input components, not display
        # components returned by the template.
        "components": request_checkout["components"],
        "analytics": {"templateReceived": analytics.get("templateReceived")},
    }
    submit_projection_bytes = json.dumps(
        submit_projection,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
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
        delivery_location=request.delivery_location,
        total=total,
        price_lines=price_lines,
        eta=eta,
        received_at=receipt,
        store_display_name=request.store_display_name,
        masked_address=request.masked_address,
        masked_payment=request.masked_payment,
        name=_display(checkout["name"], maximum=100),
        submit_projection_bytes=submit_projection_bytes,
        full_address=request.full_address,
        item_display=request.item_display,
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
                delivery_location=request.delivery_location,
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
