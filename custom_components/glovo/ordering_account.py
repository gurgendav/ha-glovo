"""Private account/address/payment GET client with ephemeral local selections."""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

from .ordering_contracts import (
    AddressSnapshot,
    CustomerIdentity,
    SavedPayment,
    address_snapshots_equal,
    build_payment_query,
    parse_customer,
    parse_saved_addresses,
    parse_saved_payments,
)
from .ordering_models import MaskedPaymentSummary, SavedAddressSummary

_ADDRESS_PATH: Final = "/customer_profile/api/v1/address_book/me/addresses"
_PAYMENT_PATH: Final = "/v4/payment_methods"
_CUSTOMER_PATH: Final = "/v3/me"
SELECTION_TTL_SECONDS: Final = 300.0


class InvalidSelection(ValueError):
    """A local selection is missing, expired, stale, or owned by another user."""

    def __init__(self) -> None:
        super().__init__("selection is unavailable")


@dataclass(frozen=True, slots=True, repr=False)
class _Selection:
    owner_key: str = field(repr=False)
    generation: int = field(repr=False)
    expires_at: float = field(repr=False)
    value: AddressSnapshot | SavedPayment = field(repr=False)


class AccountClient:
    """Strict GET-only account client; no provider reference is publicly projected."""

    selection_ttl_seconds = SELECTION_TTL_SECONDS

    def __init__(
        self,
        session: Any,
        *,
        clock: Callable[[], float] = time.time,
        handle_source: Callable[[], str] | None = None,
    ) -> None:
        self._session = session
        self._clock = clock
        self._handle_source = handle_source or (
            lambda: f"choice-{secrets.token_hex(16)}"
        )
        self._addresses: dict[str, _Selection] = {}
        self._payments: dict[str, _Selection] = {}

    @staticmethod
    def _identity(owner_key: str, generation: int) -> tuple[str, int]:
        if not isinstance(owner_key, str) or not owner_key or len(owner_key) > 128:
            raise InvalidSelection
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise InvalidSelection
        return owner_key, generation

    def _new_handle(self, existing: dict[str, _Selection]) -> str:
        for _ in range(4):
            handle = self._handle_source()
            if (
                isinstance(handle, str)
                and 1 <= len(handle) <= 64
                and handle not in existing
            ):
                return handle
        raise InvalidSelection

    async def async_customer(self) -> CustomerIdentity:
        payload = await self._session.async_get("account", _CUSTOMER_PATH)
        return parse_customer(payload)

    async def async_saved_addresses(
        self, *, owner_key: str, generation: int
    ) -> tuple[SavedAddressSummary, ...]:
        owner, current_generation = self._identity(owner_key, generation)
        payload = await self._session.async_get("address", _ADDRESS_PATH)
        snapshots = parse_saved_addresses(payload)
        expires_at = self._clock() + self.selection_ttl_seconds
        result: list[SavedAddressSummary] = []
        labels = {
            "HOUSE": "Saved home ••••",
            "APARTMENT": "Saved home ••••",
            "OFFICE": "Saved office ••••",
            "OTHER": "Saved destination ••••",
        }
        for snapshot in snapshots:
            handle = self._new_handle(self._addresses)
            self._addresses[handle] = _Selection(
                owner, current_generation, expires_at, snapshot
            )
            result.append(SavedAddressSummary(handle, labels[snapshot.kind]))
        self._purge()
        return tuple(result)

    async def async_saved_payments(
        self,
        *,
        owner_key: str,
        generation: int,
        amount_minor: int,
        currency: str,
        checkout_session: str | None = None,
        store_address_id: int | None = None,
        client_supports: tuple[str, ...] = (),
        client_ready: bool | None = None,
    ) -> tuple[MaskedPaymentSummary, ...]:
        owner, current_generation = self._identity(owner_key, generation)
        query = build_payment_query(
            amount_minor=amount_minor,
            currency=currency,
            checkout_session=checkout_session,
            store_address_id=store_address_id,
            client_supports=client_supports,
            client_ready=client_ready,
        )
        payload = await self._session.async_get("payment", _PAYMENT_PATH, query)
        payments = parse_saved_payments(payload)
        expires_at = self._clock() + self.selection_ttl_seconds
        result: list[MaskedPaymentSummary] = []
        for payment in payments:
            handle = self._new_handle(self._payments)
            self._payments[handle] = _Selection(
                owner, current_generation, expires_at, payment
            )
            suffix = f" {payment.last_four_digits}" if payment.last_four_digits else ""
            result.append(
                MaskedPaymentSummary(handle, f"Saved card ••••{suffix}")
            )
        self._purge()
        return tuple(result)

    def _resolve(
        self,
        selections: dict[str, _Selection],
        handle: str,
        *,
        owner_key: str,
        generation: int,
    ) -> AddressSnapshot | SavedPayment:
        owner, current_generation = self._identity(owner_key, generation)
        self._purge()
        selection = selections.get(handle)
        if (
            selection is None
            or selection.owner_key != owner
            or selection.generation != current_generation
            or selection.expires_at <= self._clock()
        ):
            raise InvalidSelection
        return selection.value

    def resolve_address(
        self, handle: str, *, owner_key: str, generation: int
    ) -> AddressSnapshot:
        value = self._resolve(
            self._addresses,
            handle,
            owner_key=owner_key,
            generation=generation,
        )
        if not isinstance(value, AddressSnapshot):
            raise InvalidSelection
        return value

    def resolve_payment(
        self, handle: str, *, owner_key: str, generation: int
    ) -> SavedPayment:
        value = self._resolve(
            self._payments,
            handle,
            owner_key=owner_key,
            generation=generation,
        )
        if not isinstance(value, SavedPayment):
            raise InvalidSelection
        return value

    async def async_revalidate_address(
        self, handle: str, *, owner_key: str, generation: int
    ) -> bool:
        selected = self.resolve_address(
            handle, owner_key=owner_key, generation=generation
        )
        payload = await self._session.async_get("address", _ADDRESS_PATH)
        current = parse_saved_addresses(payload)
        return any(
            item.remote_id == selected.remote_id
            and address_snapshots_equal(item, selected)
            for item in current
        )

    def _purge(self) -> None:
        now = self._clock()
        for selections in (self._addresses, self._payments):
            expired = [
                key for key, selection in selections.items() if selection.expires_at <= now
            ]
            for key in expired:
                selections.pop(key, None)

    def invalidate(self) -> None:
        """Invalidate all ephemeral selections on reauth/unload/rebuild."""
        self._addresses.clear()
        self._payments.clear()
