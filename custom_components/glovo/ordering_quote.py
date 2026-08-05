"""Strict fixture quote parsing and canonical confirmation fingerprints."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .ordering_models import (
    BasketSnapshot,
    MaskedPaymentSummary,
    Money,
    SavedAddressSummary,
)

_REQUIRED_FIXTURE_KEYS = frozenset(
    {
        "checkoutSessionId",
        "versionId",
        "templateId",
        "basketVersion",
        "purchaseTotalCents",
        "currencyCode",
        "priceLines",
        "eta",
        "expiresAt",
    }
)
_REQUIRED_PRICE_LINE_KEYS = frozenset({"kind", "label", "amountCents"})


class InvalidCheckoutFixture(ValueError):
    """Raised when an authoritative checkout fixture is stale or malformed."""


def _text(value: object, name: str, *, maximum: int = 100) -> str:
    if not isinstance(value, str):
        raise InvalidCheckoutFixture(f"{name} must be text")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum or any(ord(char) < 32 for char in normalized):
        raise InvalidCheckoutFixture(f"{name} is invalid")
    return normalized


def _integer(value: object, name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidCheckoutFixture(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise InvalidCheckoutFixture(f"{name} is out of range")
    return value


def _finite_time(value: object, message: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise InvalidCheckoutFixture(message)
    return float(value)


@dataclass(frozen=True, slots=True)
class PriceLine:
    """An arbitrary complete authoritative price line in signed minor units."""

    kind: str
    label: str
    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _text(self.kind, "price line kind", maximum=50))
        object.__setattr__(self, "label", _text(self.label, "price line label"))
        object.__setattr__(self, "amount_minor", _integer(self.amount_minor, "price line amount"))
        try:
            Money(0, self.currency)
        except (TypeError, ValueError) as err:
            raise InvalidCheckoutFixture("price line currency is invalid") from err

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "amountCents": self.amount_minor,
            "currencyCode": self.currency,
        }

    def public_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "amountCents": self.amount_minor,
            "currencyCode": self.currency,
        }


@dataclass(frozen=True, slots=True)
class CheckoutFixture:
    """Authoritative, explicitly expiring checkout fixture contract."""

    checkout_session_id: str
    version_id: str
    template_id: str
    basket_version: int
    purchase_total: Money
    price_lines: tuple[PriceLine, ...]
    eta: str
    expires_at: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "checkout_session_id", _text(self.checkout_session_id, "checkout session")
        )
        object.__setattr__(self, "version_id", _text(self.version_id, "version"))
        object.__setattr__(self, "template_id", _text(self.template_id, "template"))
        object.__setattr__(
            self,
            "basket_version",
            _integer(self.basket_version, "basket version", minimum=0),
        )
        object.__setattr__(self, "eta", _text(self.eta, "ETA"))
        if not isinstance(self.purchase_total, Money):
            raise InvalidCheckoutFixture("purchase total is invalid")
        if (
            not isinstance(self.price_lines, tuple)
            or not self.price_lines
            or not all(isinstance(line, PriceLine) for line in self.price_lines)
            or any(
                line.currency != self.purchase_total.currency for line in self.price_lines
            )
            or sum(line.amount_minor for line in self.price_lines)
            != self.purchase_total.amount_minor
        ):
            raise InvalidCheckoutFixture("price lines are inconsistent")
        object.__setattr__(
            self,
            "expires_at",
            _finite_time(self.expires_at, "expiry must be a finite timestamp"),
        )

    @classmethod
    def from_mapping(cls, payload: object, *, now: float) -> CheckoutFixture:
        """Parse a fixture using an exact schema and fail closed on drift."""
        now = _finite_time(now, "current time must be finite")
        if not isinstance(payload, Mapping) or frozenset(payload) != _REQUIRED_FIXTURE_KEYS:
            raise InvalidCheckoutFixture("checkout fixture schema mismatch")
        currency = payload["currencyCode"]
        try:
            total = Money(
                _integer(payload["purchaseTotalCents"], "purchase total", minimum=0),
                currency,  # type: ignore[arg-type]
            )
        except (TypeError, ValueError) as err:
            raise InvalidCheckoutFixture("purchase total is invalid") from err
        raw_lines = payload["priceLines"]
        if not isinstance(raw_lines, list) or not raw_lines or len(raw_lines) > 100:
            raise InvalidCheckoutFixture("price lines are invalid")
        lines: list[PriceLine] = []
        for raw_line in raw_lines:
            if not isinstance(raw_line, Mapping) or frozenset(raw_line) != _REQUIRED_PRICE_LINE_KEYS:
                raise InvalidCheckoutFixture("price line schema mismatch")
            lines.append(
                PriceLine(
                    kind=raw_line["kind"],  # type: ignore[arg-type]
                    label=raw_line["label"],  # type: ignore[arg-type]
                    amount_minor=_integer(raw_line["amountCents"], "price line amount"),
                    currency=total.currency,
                )
            )
        if sum(line.amount_minor for line in lines) != total.amount_minor:
            raise InvalidCheckoutFixture("price lines do not equal purchase total")
        expires_at = payload["expiresAt"]
        expires = _finite_time(expires_at, "expiry must be a timestamp")
        if expires <= now or expires > now + 15 * 60:
            raise InvalidCheckoutFixture("checkout fixture is expired or excessively long-lived")
        return cls(
            checkout_session_id=_text(payload["checkoutSessionId"], "checkout session"),
            version_id=_text(payload["versionId"], "version"),
            template_id=_text(payload["templateId"], "template"),
            basket_version=_integer(payload["basketVersion"], "basket version", minimum=0),
            purchase_total=total,
            price_lines=tuple(lines),
            eta=_text(payload["eta"], "ETA"),
            expires_at=expires,
        )

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "checkoutSessionId": self.checkout_session_id,
            "versionId": self.version_id,
            "templateId": self.template_id,
            "basketVersion": self.basket_version,
            "purchaseTotalCents": self.purchase_total.amount_minor,
            "currencyCode": self.purchase_total.currency,
            "priceLines": [line.canonical_dict() for line in self.price_lines],
            "eta": self.eta,
            "expiresAt": self.expires_at,
        }

    def public_dict(self) -> dict[str, Any]:
        return {
            "checkoutSessionId": self.checkout_session_id,
            "versionId": self.version_id,
            "templateId": self.template_id,
            "basketVersion": self.basket_version,
            "purchaseTotalCents": self.purchase_total.amount_minor,
            "currencyCode": self.purchase_total.currency,
            "priceLines": [line.public_dict() for line in self.price_lines],
            "eta": self.eta,
            "expiresAt": self.expires_at,
            "actionText": f"Run mock checkout — {self.purchase_total.action_amount}",
        }


def canonical_fingerprint(
    basket: BasketSnapshot,
    address: SavedAddressSummary,
    payment: MaskedPaymentSummary,
    fixture: CheckoutFixture,
) -> str:
    """Bind every checkout-relevant field in deterministic canonical JSON."""
    payload = {
        "basket": basket.canonical_dict(),
        "address": {
            "selectionKey": address.selection_key,
            "maskedLabel": address.masked_label,
        },
        "payment": {
            "selectionKey": payment.selection_key,
            "maskedLabel": payment.masked_label,
        },
        "checkout": fixture.canonical_dict(),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
