"""User-owned, in-memory local baskets for mock ordering."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .ordering_models import BasketSnapshot, CanonicalItem

DEFAULT_BASKET_TTL_SECONDS = 15 * 60
MAX_DISTINCT_ITEMS = 20
MAX_ITEM_QUANTITY = 20
MAX_TOTAL_QUANTITY = 50


class BasketError(ValueError):
    """Base basket validation error with no basket contents in its message."""


class BasketStoreMismatch(BasketError):
    """Raised when an item belongs to another store."""


class BasketLimitExceeded(BasketError):
    """Raised when a basket bound would be exceeded."""


class BasketNotFound(BasketError):
    """Raised when a required basket is absent or expired."""


@dataclass(slots=True)
class _Basket:
    owner_key: str
    store_key: str
    store_label: str
    items: dict[tuple[str, str, tuple[str, ...]], CanonicalItem]
    revision: int
    generation: int
    expires_at: float


class BasketManager:
    """Generation-bound one-store baskets kept only in process memory."""

    def __init__(
        self,
        *,
        clock: Callable[[], float],
        ttl_seconds: int = DEFAULT_BASKET_TTL_SECONDS,
    ) -> None:
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or not math.isfinite(ttl_seconds)
            or ttl_seconds <= 0
        ):
            raise ValueError("basket TTL must be a positive integer")
        self._clock = clock
        self.ttl_seconds = ttl_seconds
        self._baskets: dict[str, _Basket] = {}

    def _now(self) -> float:
        value = self._clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            self.invalidate_all()
            raise BasketError("basket clock is invalid")
        return float(value)

    def invalidate_all(self) -> None:
        """Immediately discard all ephemeral basket contents."""
        self._baskets.clear()

    def _current(self, owner_key: str, generation: int) -> _Basket | None:
        basket = self._baskets.get(owner_key)
        if basket is None:
            return None
        now = self._now()
        if not math.isfinite(basket.expires_at):
            self._baskets.pop(owner_key, None)
            raise BasketError("basket expiry is invalid")
        if basket.generation != generation or now >= basket.expires_at:
            self._baskets.pop(owner_key, None)
            return None
        return basket

    def add_item(
        self,
        *,
        owner_key: str,
        generation: int,
        store_key: str,
        store_label: str,
        item: CanonicalItem,
    ) -> None:
        """Add one canonical line, enforcing store and quantity bounds."""
        basket = self._current(owner_key, generation)
        if basket is None:
            basket = _Basket(
                owner_key=owner_key,
                store_key=store_key,
                store_label=store_label,
                items={},
                revision=0,
                generation=generation,
                expires_at=self._now() + self.ttl_seconds,
            )
            self._baskets[owner_key] = basket
        elif basket.store_key != store_key:
            raise BasketStoreMismatch("a basket may contain items from one store only")

        current = basket.items.get(item.line_key)
        quantity = item.quantity + (current.quantity if current else 0)
        if quantity > MAX_ITEM_QUANTITY:
            raise BasketLimitExceeded("per-item quantity limit exceeded")
        distinct = len(basket.items) + (0 if current else 1)
        if distinct > MAX_DISTINCT_ITEMS:
            raise BasketLimitExceeded("distinct item limit exceeded")
        total = sum(line.quantity for line in basket.items.values())
        total += item.quantity
        if total > MAX_TOTAL_QUANTITY:
            raise BasketLimitExceeded("total basket quantity limit exceeded")

        basket.items[item.line_key] = item.with_quantity(quantity)
        basket.revision += 1
        basket.expires_at = self._now() + self.ttl_seconds

    def clear(self, owner_key: str) -> None:
        self._baskets.pop(owner_key, None)

    def snapshot(self, owner_key: str, generation: int) -> BasketSnapshot:
        basket = self._current(owner_key, generation)
        if basket is None or not basket.items:
            raise BasketNotFound("basket is empty or expired")
        return BasketSnapshot(
            owner_key=basket.owner_key,
            store_key=basket.store_key,
            store_label=basket.store_label,
            items=tuple(basket.items.values()),
            revision=basket.revision,
            generation=basket.generation,
        )

    def public_dict(self, owner_key: str, generation: int) -> dict[str, Any]:
        basket = self._current(owner_key, generation)
        if basket is None:
            return {"store": None, "items": [], "revision": 0, "expiresAt": None}
        return {
            "store": {"key": basket.store_key, "label": basket.store_label},
            "items": [item.canonical_dict() for item in sorted(basket.items.values(), key=lambda x: x.line_key)],
            "revision": basket.revision,
            "expiresAt": basket.expires_at,
        }
