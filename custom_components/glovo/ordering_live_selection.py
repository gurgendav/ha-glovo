"""Private live catalog selections and exact handle-to-basket compilation.

Provider identifiers remain only in this module's private bindings.  Every value
accepted from the facade is a short local handle scoped to one owner/generation
and a finite monotonic lease.
"""
from __future__ import annotations

import math
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from .ordering_contracts import CatalogMenu, CatalogOption, CatalogProduct, ExactMoney, LiveStore
from .ordering_remote_basket import (
    MAX_PRODUCT_QUANTITY,
    MAX_PRODUCTS,
    MAX_TOTAL_QUANTITY,
    BasketContractError,
    BasketIntent,
    RemoteBasketProduct,
    RemoteCustomization,
)

LIVE_SELECTION_TTL_SECONDS: Final = 300.0
MAX_HANDLE_LENGTH: Final = 64


class LiveSelectionError(ValueError):
    """Sanitized local selection/compiler failure."""

    def __init__(self) -> None:
        super().__init__("live selection is unavailable or invalid")


@dataclass(frozen=True, slots=True, repr=False)
class _Bound:
    owner: str = field(repr=False)
    generation: int = field(repr=False)
    expires_at: float = field(repr=False)
    value: object = field(repr=False)
    parent: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class SelectedProduct:
    """Strict public-shaped local choice; it contains no provider identity."""

    product_handle: str
    quantity: int
    option_groups: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def __post_init__(self) -> None:
        _handle(self.product_handle)
        _quantity(self.quantity)
        if not isinstance(self.option_groups, tuple) or len(self.option_groups) > 24:
            raise LiveSelectionError
        groups: set[str] = set()
        for row in self.option_groups:
            if not isinstance(row, tuple) or len(row) != 2:
                raise LiveSelectionError
            group, options = row
            _handle(group)
            if group in groups or not isinstance(options, tuple) or len(options) > 64:
                raise LiveSelectionError
            groups.add(group)
            if len(set(options)) != len(options):
                raise LiveSelectionError
            for option in options:
                _handle(option)


def _finite(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LiveSelectionError
    value = float(value)
    if not math.isfinite(value):
        raise LiveSelectionError
    return value


def _owner(owner: object) -> str:
    if not isinstance(owner, str) or not owner or len(owner) > MAX_HANDLE_LENGTH:
        raise LiveSelectionError
    return owner


def _generation(generation: object) -> int:
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        raise LiveSelectionError
    return generation


def _handle(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_HANDLE_LENGTH:
        raise LiveSelectionError
    return value


def _quantity(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_PRODUCT_QUANTITY:
        raise LiveSelectionError
    return value


def parse_selected_products(value: object) -> tuple[SelectedProduct, ...]:
    """Parse the frozen public selection shape without accepting arbitrary JSON."""
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_PRODUCTS:
        raise LiveSelectionError
    result: list[SelectedProduct] = []
    for row in value:
        if not isinstance(row, Mapping) or set(row) != {"productHandle", "quantity", "options"}:
            raise LiveSelectionError
        options = row["options"]
        if not isinstance(options, list):
            raise LiveSelectionError
        parsed_groups: list[tuple[str, tuple[str, ...]]] = []
        for group in options:
            if not isinstance(group, Mapping) or set(group) != {"groupHandle", "optionHandles"}:
                raise LiveSelectionError
            choices = group["optionHandles"]
            if not isinstance(choices, list):
                raise LiveSelectionError
            parsed_groups.append((group["groupHandle"], tuple(choices)))
        result.append(SelectedProduct(row["productHandle"], row["quantity"], tuple(parsed_groups)))
    if sum(item.quantity for item in result) > MAX_TOTAL_QUANTITY:
        raise LiveSelectionError
    if len({item.product_handle for item in result}) != len(result):
        # The first release deliberately has no line-merge semantics.
        raise LiveSelectionError
    return tuple(result)


class LiveSelectionRegistry:
    """Issues private, expiring catalog handles and compiles exact remote intents."""

    selection_ttl_seconds = LIVE_SELECTION_TTL_SECONDS

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        handle_source: Callable[[], str] | None = None,
    ) -> None:
        self._clock = clock
        self._handle_source = handle_source or (lambda: f"live-{secrets.token_hex(16)}")
        self._values: dict[str, _Bound] = {}

    def _now(self) -> float:
        return _finite(self._clock())

    def _new_handle(self) -> str:
        for _ in range(8):
            handle = self._handle_source()
            try:
                _handle(handle)
            except LiveSelectionError:
                continue
            if handle not in self._values:
                return handle
        raise LiveSelectionError

    def _bind(self, *, owner: str, generation: int, value: object, parent: str | None = None) -> str:
        handle = self._new_handle()
        self._values[handle] = _Bound(owner, generation, self._now() + self.selection_ttl_seconds, value, parent)
        return handle

    def _resolve(self, handle: object, *, owner: object, generation: object, expected: type, parent: str | None = None) -> Any:
        handle = _handle(handle)
        owner = _owner(owner)
        generation = _generation(generation)
        self.purge()
        bound = self._values.get(handle)
        if (
            bound is None
            or bound.owner != owner
            or bound.generation != generation
            or bound.expires_at <= self._now()
            or not isinstance(bound.value, expected)
            or (parent is not None and bound.parent != parent)
        ):
            raise LiveSelectionError
        return bound.value

    def issue_store(
        self,
        store: LiveStore,
        *,
        owner: str,
        generation: int,
        address_handle: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(store, LiveStore):
            raise LiveSelectionError
        owner = _owner(owner)
        generation = _generation(generation)
        if address_handle is not None:
            address_handle = _handle(address_handle)
        handle = self._bind(
            owner=owner,
            generation=generation,
            value=store,
            parent=address_handle,
        )
        # Fees are display-only catalog facts; they are not payment authority.
        return {
            "storeHandle": handle,
            "label": store.name,
            "category": store.category,
            "deliveryFeeMinor": store.delivery_fee.amount_minor,
            "serviceFeeMinor": store.service_fee.amount_minor,
            "currency": store.delivery_fee.currency,
        }

    def issue_menu(self, store_handle: str, menu: CatalogMenu, *, owner: str, generation: int) -> dict[str, Any]:
        store = self._resolve(store_handle, owner=owner, generation=generation, expected=LiveStore)
        if not isinstance(menu, CatalogMenu) or menu.store_address_id != store.address_id:
            raise LiveSelectionError
        result: list[dict[str, Any]] = []
        for product in menu.products:
            product_handle = self._bind(owner=owner, generation=generation, value=product, parent=store_handle)
            groups: list[dict[str, Any]] = []
            for group in product.option_groups:
                group_handle = self._bind(owner=owner, generation=generation, value=group, parent=product_handle)
                options: list[dict[str, Any]] = []
                for option in group.options:
                    option_handle = self._bind(owner=owner, generation=generation, value=option, parent=group_handle)
                    options.append({"optionHandle": option_handle, "label": option.label, "priceMinor": option.price.amount_minor, "currency": option.price.currency, "selected": option.selected})
                groups.append({"groupHandle": group_handle, "label": group.label, "min": group.minimum, "max": group.maximum, "multipleSelection": group.multiple_selection, "options": options})
            result.append({"productHandle": product_handle, "label": product.name, "priceMinor": product.price.amount_minor, "currency": product.price.currency, "optionGroups": groups})
        return {"storeHandle": store_handle, "products": result}

    def compile_intent(
        self,
        *,
        owner: str,
        generation: int,
        customer_id: int,
        store_handle: str,
        selections: Sequence[SelectedProduct],
    ) -> BasketIntent:
        store = self._resolve(store_handle, owner=owner, generation=generation, expected=LiveStore)
        if not isinstance(selections, tuple) or not 1 <= len(selections) <= MAX_PRODUCTS:
            raise LiveSelectionError
        if not all(isinstance(item, SelectedProduct) for item in selections):
            raise LiveSelectionError
        if sum(item.quantity for item in selections) > MAX_TOTAL_QUANTITY:
            raise LiveSelectionError
        remote: list[RemoteBasketProduct] = []
        for selected in selections:
            product = self._resolve(selected.product_handle, owner=owner, generation=generation, expected=CatalogProduct, parent=store_handle)
            customizations = self._compile_customizations(owner, generation, selected, product)
            remote.append(RemoteBasketProduct(product.product_id, product.external_id, None, product.store_product_id, None, selected.quantity, None, customizations))
        if len({item.identity for item in remote}) != len(remote):
            raise LiveSelectionError
        try:
            return BasketIntent(customer_id, store.store_id, store.address_id, store.category_id, "DELIVERY", tuple(remote))
        except BasketContractError as exc:
            raise LiveSelectionError from exc

    def _compile_customizations(self, owner: str, generation: int, selected: SelectedProduct, product: CatalogProduct) -> tuple[RemoteCustomization, ...]:
        provided = dict(selected.option_groups)
        if len(provided) != len(selected.option_groups):
            raise LiveSelectionError
        result: list[RemoteCustomization] = []
        for group in product.option_groups:
            # A group is only selectable by its child handle.  Defaults are trusted
            # only after the same min/max and fixed-price checks below.
            group_handles = [key for key, bound in self._values.items() if bound.value is group and bound.parent == selected.product_handle]
            if len(group_handles) != 1:
                raise LiveSelectionError
            group_handle = group_handles[0]
            chosen_handles = provided.pop(group_handle, None)
            chosen = tuple(option for option in group.options if option.selected) if chosen_handles is None else tuple(self._resolve(handle, owner=owner, generation=generation, expected=CatalogOption, parent=group_handle) for handle in chosen_handles)
            if len(chosen) != len(set(chosen)) or any(option not in group.options for option in chosen):
                raise LiveSelectionError
            if not group.minimum <= len(chosen) <= group.maximum:
                raise LiveSelectionError
            if not group.multiple_selection and len(chosen) > 1:
                raise LiveSelectionError
            for option in chosen:
                if not isinstance(option.price, ExactMoney) or option.price.currency != product.price.currency or option.price.amount_minor < 0:
                    raise LiveSelectionError
                result.append(RemoteCustomization(group.key, group.label, group.position, option.key, option.label, 1))
        if provided:
            raise LiveSelectionError
        return tuple(sorted(result, key=lambda item: (item.group_position, item.group_id, item.attribute_id)))

    def product_currency(self, handle: str, *, owner: str, generation: int, store_handle: str) -> str:
        product = self._resolve(handle, owner=owner, generation=generation, expected=CatalogProduct, parent=store_handle)
        return product.price.currency

    def resolve_store(
        self,
        handle: str,
        *,
        owner: str,
        generation: int,
        address_handle: str | None = None,
    ) -> LiveStore:
        if address_handle is not None:
            address_handle = _handle(address_handle)
        return self._resolve(
            handle,
            owner=owner,
            generation=generation,
            expected=LiveStore,
            parent=address_handle,
        )

    def purge(self) -> None:
        now = self._now()
        for handle in [key for key, bound in self._values.items() if bound.expires_at <= now]:
            self._values.pop(handle, None)

    def invalidate(self) -> None:
        self._values.clear()
