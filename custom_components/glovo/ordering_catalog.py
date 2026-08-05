"""Read-only synthetic catalog and authoritative fixture quote provider."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from .ordering_models import (
    BasketSnapshot,
    CanonicalItem,
    CanonicalOption,
    MaskedPaymentSummary,
    SavedAddressSummary,
)
from .ordering_quote import CheckoutFixture

PROVIDER_NAME: Final = "synthetic_fixture"
_CURRENCY: Final = "AMD"


class CatalogError(ValueError):
    """Raised for unknown or malformed fixture catalog selections."""


@dataclass(frozen=True, slots=True)
class CatalogModifier:
    key: str
    label: str
    price_minor: int

    def public_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "priceCents": self.price_minor}


@dataclass(frozen=True, slots=True)
class CatalogVariant:
    key: str
    label: str
    price_minor: int

    def public_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "priceCents": self.price_minor}


@dataclass(frozen=True, slots=True)
class CatalogProduct:
    key: str
    label: str
    variants: tuple[CatalogVariant, ...]
    modifiers: tuple[CatalogModifier, ...]

    def public_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "variants": [variant.public_dict() for variant in self.variants],
            "modifiers": [modifier.public_dict() for modifier in self.modifiers],
        }


@dataclass(frozen=True, slots=True)
class CatalogStore:
    key: str
    label: str
    products: tuple[CatalogProduct, ...]

    def public_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "products": [product.public_dict() for product in self.products],
        }


_PRODUCT = CatalogProduct(
    key="fixture-meal",
    label="Synthetic meal",
    variants=(
        CatalogVariant(key="standard", label="Standard", price_minor=550000),
        CatalogVariant(key="small", label="Small", price_minor=450000),
    ),
    modifiers=(
        CatalogModifier(key="no-change", label="No change", price_minor=0),
        CatalogModifier(key="fixture-extra", label="Synthetic extra", price_minor=50000),
    ),
)
_STORES = (
    CatalogStore(key="fixture-store", label="Synthetic Kitchen", products=(_PRODUCT,)),
    CatalogStore(
        key="different-fixture-store",
        label="Alternate Synthetic Kitchen",
        products=(_PRODUCT,),
    ),
)
_ADDRESSES = (
    SavedAddressSummary("masked-destination", "Saved destination ••••"),
    SavedAddressSummary("masked-destination-alt", "Alternate destination ••••"),
)
_PAYMENTS = (
    MaskedPaymentSummary("masked-payment", "Test card •••• 4242"),
    MaskedPaymentSummary("masked-payment-alt", "Backup test card •••• 4444"),
)


class SyntheticCatalogProvider:
    """In-process fixtures only; this class has no network or mutation dependency."""

    provider_name = PROVIDER_NAME

    def __init__(self, *, clock: Callable[[], float]) -> None:
        self._clock = clock

    def public_catalog(self) -> dict[str, Any]:
        return {
            "provider": self.provider_name,
            "currencyCode": _CURRENCY,
            "stores": [store.public_dict() for store in _STORES],
            "addresses": [address.public_dict() for address in _ADDRESSES],
            "payments": [payment.public_dict() for payment in _PAYMENTS],
        }

    def _store(self, key: str) -> CatalogStore:
        try:
            return next(store for store in _STORES if store.key == key)
        except StopIteration as err:
            raise CatalogError("unknown fixture store") from err

    def resolve_item(
        self,
        *,
        store_key: str,
        product_key: str,
        variant_key: str,
        modifier_keys: Sequence[str],
        quantity: int,
    ) -> tuple[str, CanonicalItem]:
        store = self._store(store_key)
        try:
            product = next(product for product in store.products if product.key == product_key)
            variant = next(variant for variant in product.variants if variant.key == variant_key)
        except StopIteration as err:
            raise CatalogError("unknown fixture catalog selection") from err
        if isinstance(modifier_keys, (str, bytes)) or len(set(modifier_keys)) != len(modifier_keys):
            raise CatalogError("fixture modifier selection is invalid")
        modifiers: list[CatalogModifier] = []
        for modifier_key in modifier_keys:
            try:
                modifiers.append(
                    next(modifier for modifier in product.modifiers if modifier.key == modifier_key)
                )
            except StopIteration as err:
                raise CatalogError("unknown fixture modifier") from err
        options = tuple(CanonicalOption(item.key, item.label) for item in modifiers)
        return store.label, CanonicalItem(
            product_key=product.key,
            product_label=product.label,
            variant_key=variant.key,
            variant_label=variant.label,
            options=options,
            quantity=quantity,
        )

    def address(self, key: str) -> SavedAddressSummary:
        try:
            return next(item for item in _ADDRESSES if item.selection_key == key)
        except StopIteration as err:
            raise CatalogError("unknown masked destination") from err

    def payment(self, key: str) -> MaskedPaymentSummary:
        try:
            return next(item for item in _PAYMENTS if item.selection_key == key)
        except StopIteration as err:
            raise CatalogError("unknown masked payment") from err

    def create_quote(self, basket: BasketSnapshot) -> CheckoutFixture:
        """Return a deterministic authoritative fixture with complete price lines."""
        items_total = 0
        for line in basket.items:
            store = self._store(basket.store_key)
            try:
                product = next(item for item in store.products if item.key == line.product_key)
                variant = next(item for item in product.variants if item.key == line.variant_key)
            except StopIteration as err:
                raise CatalogError("basket no longer matches fixture catalog") from err
            modifier_prices = {item.key: item.price_minor for item in product.modifiers}
            try:
                unit_price = variant.price_minor + sum(
                    modifier_prices[option.key] for option in line.options
                )
            except KeyError as err:
                raise CatalogError("basket modifier no longer matches fixture catalog") from err
            items_total += unit_price * line.quantity

        payload = {
            "checkoutSessionId": f"fixture-session-v{basket.revision}",
            "versionId": "fixture-checkout-v1",
            "templateId": "fixture-template-v1",
            "basketVersion": basket.revision,
            "purchaseTotalCents": items_total + 60000 + 24000 - 10000,
            "currencyCode": _CURRENCY,
            "priceLines": [
                {"kind": "items", "label": "Items", "amountCents": items_total},
                {"kind": "delivery_fee", "label": "Delivery fee", "amountCents": 60000},
                {"kind": "service_fee", "label": "Service fee", "amountCents": 24000},
                {"kind": "discount", "label": "Fixture discount", "amountCents": -10000},
            ],
            "eta": "25–35 min",
            "expiresAt": self._clock() + 120,
        }
        return CheckoutFixture.from_mapping(payload, now=self._clock())
