"""Privacy-safe immutable models for the mock ordering foundation."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# Deliberately explicit: accepting any three letters silently formats unknown
# currencies with the wrong exponent. Extend this reviewed table as fixtures do.
ISO_4217_EXPONENTS = {
    "AMD": 2,
    "AUD": 2,
    "CAD": 2,
    "CHF": 2,
    "CNY": 2,
    "EUR": 2,
    "GBP": 2,
    "GEL": 2,
    "JPY": 0,
    "KWD": 3,
    "PLN": 2,
    "RUB": 2,
    "UAH": 2,
    "USD": 2,
}
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")
_MASKED_ADDRESS_RE = re.compile(r"^[A-Za-z][A-Za-z -]{0,39} ••••$")
_PHYSICAL_ADDRESS_WORD_RE = re.compile(
    r"\b(?:apartment|apt|avenue|ave|boulevard|blvd|building|court|ct|drive|dr|"
    r"floor|highway|house|hwy|lane|ln|postal|road|rd|square|sq|street|st|unit|zip)\b",
    re.IGNORECASE,
)
_SAFE_DIAGNOSTIC_KEYS = frozenset(
    {
        "basketversion",
        "checkoutsessionid",
        "checkoutversion",
        "sessionid",
        "templateid",
        "versionid",
    }
)
_PRIVATE_KEY_NAMES = frozenset(
    {
        "addressid",
        "coordinates",
        "customerid",
        "cvv",
        "latitude",
        "longitude",
        "oauth",
        "pan",
        "paymentid",
    }
)
_SECRET_KEY_NAMES = frozenset(
    {
        "auth",
        "authentication",
        "bearer",
        "cookie",
        "credential",
        "credentials",
        "oauth",
        "setcookie",
        "token",
    }
)
_SECRET_KEY_FRAGMENTS = (
    "accesstoken",
    "apikey",
    "authorization",
    "authheader",
    "bearer",
    "credential",
    "password",
    "passphrase",
    "refreshtoken",
    "sessionsecret",
    "setcookie",
)
_REDACTED = "[redacted]"


def _strict_int(value: object, name: str, *, minimum: int = 0) -> int:
    """Return an integer while rejecting bools and coercion."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < minimum:
        raise ValueError(f"{name} is out of range")
    return value


def _safe_key(value: object, name: str) -> str:
    if not isinstance(value, str) or not _KEY_RE.fullmatch(value):
        raise ValueError(f"{name} must be an opaque local key")
    return value


def _safe_label(value: object, name: str, *, maximum: int = 100) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be text")
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        raise ValueError(f"{name} is invalid")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{name} is invalid")
    return normalized


def _redacts_diagnostic_key(value: object) -> bool:
    """Match common private/credential key spellings without hiding safe model IDs."""
    normalized = re.sub(r"[^a-z0-9]", "", str(value).casefold())
    if normalized in _SAFE_DIAGNOSTIC_KEYS:
        return False
    if normalized in _PRIVATE_KEY_NAMES or normalized in _SECRET_KEY_NAMES:
        return True
    if normalized.endswith(("cookie", "secret", "token")):
        return True
    return any(fragment in normalized for fragment in _SECRET_KEY_FRAGMENTS)


@dataclass(frozen=True, slots=True)
class Money:
    """An amount represented only in integer minor units and ISO currency."""

    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount_minor", _strict_int(self.amount_minor, "amount"))
        if not isinstance(self.currency, str) or self.currency not in ISO_4217_EXPONENTS:
            raise ValueError("currency is not a supported ISO-4217 code")

    @property
    def action_amount(self) -> str:
        """Format the exact authoritative minor-unit amount without floating point."""
        exponent = ISO_4217_EXPONENTS[self.currency]
        sign = "-" if self.amount_minor < 0 else ""
        digits = str(abs(self.amount_minor))
        if exponent:
            digits = digits.zfill(exponent + 1)
            major, minor = digits[:-exponent], digits[-exponent:]
            return f"{sign}{int(major):,}.{minor} {self.currency}"
        return f"{sign}{int(digits):,} {self.currency}"


@dataclass(frozen=True, slots=True, repr=False)
class SavedAddressSummary:
    """Opaque fixture address selection with no physical address or remote identifier."""

    selection_key: str
    masked_label: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "selection_key", _safe_key(self.selection_key, "address key"))
        label = _safe_label(self.masked_label, "address label", maximum=45)
        alias = label.removesuffix(" ••••")
        if not _MASKED_ADDRESS_RE.fullmatch(label) or _PHYSICAL_ADDRESS_WORD_RE.search(alias):
            raise ValueError("address label must be a short opaque alias ending in ••••")
        object.__setattr__(self, "masked_label", label)

    def __repr__(self) -> str:
        return "SavedAddressSummary(<masked>)"

    def public_dict(self) -> dict[str, str]:
        return {"key": self.selection_key, "label": self.masked_label}


@dataclass(frozen=True, slots=True, repr=False)
class MaskedPaymentSummary:
    """Opaque fixture payment selection that requires an explicitly masked label."""

    selection_key: str
    masked_label: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "selection_key", _safe_key(self.selection_key, "payment key"))
        label = _safe_label(self.masked_label, "payment label")
        digits = "".join(character for character in label if character.isdigit())
        if not any(marker in label for marker in ("•", "*", "ending")) or len(digits) > 4:
            raise ValueError("payment label must be masked and contain at most four visible digits")
        object.__setattr__(self, "masked_label", label)

    def __repr__(self) -> str:
        return "MaskedPaymentSummary(<masked>)"

    def public_dict(self) -> dict[str, str]:
        return {"key": self.selection_key, "label": self.masked_label}


@dataclass(frozen=True, slots=True)
class CanonicalOption:
    """Canonical product option selected from the fixture catalog."""

    key: str
    label: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _safe_key(self.key, "option key"))
        object.__setattr__(self, "label", _safe_label(self.label, "option label"))

    def canonical_dict(self) -> dict[str, str]:
        return {"key": self.key, "label": self.label}


@dataclass(frozen=True, slots=True)
class CanonicalItem:
    """Canonical basket item; every variant, modifier, and quantity is explicit."""

    product_key: str
    product_label: str
    variant_key: str
    variant_label: str
    options: tuple[CanonicalOption, ...]
    quantity: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "product_key", _safe_key(self.product_key, "product key"))
        object.__setattr__(self, "product_label", _safe_label(self.product_label, "product label"))
        object.__setattr__(self, "variant_key", _safe_key(self.variant_key, "variant key"))
        object.__setattr__(self, "variant_label", _safe_label(self.variant_label, "variant label"))
        object.__setattr__(self, "quantity", _strict_int(self.quantity, "quantity", minimum=1))
        if not isinstance(self.options, tuple) or not all(
            isinstance(option, CanonicalOption) for option in self.options
        ):
            raise TypeError("options must be a canonical tuple")
        ordered = tuple(sorted(self.options, key=lambda option: option.key))
        if len({option.key for option in ordered}) != len(ordered):
            raise ValueError("option keys must be unique")
        object.__setattr__(self, "options", ordered)

    @property
    def line_key(self) -> tuple[str, str, tuple[str, ...]]:
        return (
            self.product_key,
            self.variant_key,
            tuple(option.key for option in self.options),
        )

    def with_quantity(self, quantity: int) -> CanonicalItem:
        return CanonicalItem(
            product_key=self.product_key,
            product_label=self.product_label,
            variant_key=self.variant_key,
            variant_label=self.variant_label,
            options=self.options,
            quantity=quantity,
        )

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "productKey": self.product_key,
            "productLabel": self.product_label,
            "variantKey": self.variant_key,
            "variantLabel": self.variant_label,
            "options": [option.canonical_dict() for option in self.options],
            "quantity": self.quantity,
        }


@dataclass(frozen=True, slots=True)
class BasketSnapshot:
    """Immutable, generation-bound snapshot used for quoting and fingerprinting."""

    owner_key: str = field(repr=False)
    store_key: str
    store_label: str
    items: tuple[CanonicalItem, ...]
    revision: int
    generation: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "owner_key", _safe_key(self.owner_key, "owner key"))
        object.__setattr__(self, "store_key", _safe_key(self.store_key, "store key"))
        object.__setattr__(self, "store_label", _safe_label(self.store_label, "store label"))
        object.__setattr__(self, "revision", _strict_int(self.revision, "revision"))
        object.__setattr__(self, "generation", _strict_int(self.generation, "generation"))
        if not self.items:
            raise ValueError("basket is empty")
        ordered = tuple(sorted(self.items, key=lambda item: item.line_key))
        object.__setattr__(self, "items", ordered)

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "storeKey": self.store_key,
            "storeLabel": self.store_label,
            "items": [item.canonical_dict() for item in self.items],
            "revision": self.revision,
            "generation": self.generation,
        }


def redact_mapping(value: Any) -> Any:
    """Recursively redact known secret-bearing keys from diagnostic-style mappings."""
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            if _redacts_diagnostic_key(text_key):
                redacted[text_key] = _REDACTED
            else:
                redacted[text_key] = redact_mapping(item)
        return redacted
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_mapping(item) for item in value]
    return value
