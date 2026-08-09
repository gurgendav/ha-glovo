"""Private account/address/payment GET client with ephemeral local selections."""

from __future__ import annotations

import logging
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from .ordering_contracts import (
    AddressSnapshot,
    ContractError,
    CustomerIdentity,
    SavedPayment,
    address_snapshots_equal,
    build_payment_query,
    parse_customer,
    parse_saved_addresses,
    parse_saved_payments,
)
from .api_session import ApiSessionError
from .ordering_models import MaskedPaymentSummary, SavedAddressSummary

_ADDRESS_PATH: Final = "/customer_profile/api/v1/address_book/me/addresses"
_PAYMENT_PATH: Final = "/v4/payment_methods"
_CUSTOMER_PATH: Final = "/v3/me"
SELECTION_TTL_SECONDS: Final = 300.0
_ROAD_TYPE_WORDS: Final = frozenset(
    {
        "apartment", "apt", "avenue", "ave", "boulevard", "blvd",
        "building", "court", "ct", "drive", "dr", "floor", "highway",
        "house", "hwy", "lane", "ln", "road", "rd", "square", "sq",
        "street", "st", "unit",
    }
)
_ORDINAL_WORDS: Final = (
    "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
)

_LOGGER = logging.getLogger(__name__)
_ADDRESS_FIELDS: Final = frozenset(
    {
        "id",
        "addressLine",
        "details",
        "latitude",
        "longitude",
        "countryCode",
        "cityCode",
        "cityName",
        "kind",
        "tag",
        "fields",
    }
)


_SCHEMA_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")


def _schema_keys(value: object) -> str:
    """Return only bounded static-looking field names, never payload values."""
    if not isinstance(value, Mapping):
        return "-"
    approved = sorted(
        key
        for key in value
        if isinstance(key, str) and _SCHEMA_KEY.fullmatch(key)
    )[:32]
    return ",".join(approved)


def _address_payload_fingerprint(payload: Any) -> str:
    """Describe only fixed response structure; never values or unknown key names."""
    cursor = payload
    data_depth = 0
    while isinstance(cursor, Mapping) and "data" in cursor and data_depth < 5:
        cursor = cursor["data"]
        data_depth += 1
    addresses = cursor.get("addresses") if isinstance(cursor, Mapping) else None
    first = addresses[0] if isinstance(addresses, list) and addresses else None
    entry = first.get("entry") if isinstance(first, Mapping) else None
    address = entry.get("address") if isinstance(entry, Mapping) else None
    direct = first.get("address") if isinstance(first, Mapping) else None
    candidate = address if isinstance(address, Mapping) else direct
    covered = len(_ADDRESS_FIELDS.intersection(candidate)) if isinstance(candidate, Mapping) else 0
    row_metadata = {"title", "subtitle", "isDefault", "default", "defaultAddress"}
    allowed_row = {"entryType", "entry", "address", *row_metadata}
    address_metadata = {"isDefault", "default", "defaultAddress"}
    raw_fields = candidate.get("fields") if isinstance(candidate, Mapping) else None
    field_rows = raw_fields if isinstance(raw_fields, list) else []
    field_schema = sorted(
        {
            key
            for field in field_rows
            if isinstance(field, Mapping)
            for key in field
            if isinstance(key, str) and _SCHEMA_KEY.fullmatch(key)
        }
    )[:32]
    field_types = sorted(
        {
            str(field["type"])
            for field in field_rows
            if isinstance(field, Mapping)
            and isinstance(field.get("type"), str)
            and _SCHEMA_KEY.fullmatch(field["type"])
        }
    )[:32]
    field_value_types = sorted(
        {
            type(field.get("value")).__name__
            for field in field_rows
            if isinstance(field, Mapping) and "value" in field
        }
    )
    return (
        f"root={type(payload).__name__} data_depth={data_depth} "
        f"leaf={type(cursor).__name__} addresses={type(addresses).__name__} "
        f"count={len(addresses) if isinstance(addresses, list) else -1} "
        f"item={type(first).__name__} "
        f"entryType={isinstance(first, Mapping) and 'entryType' in first} "
        f"entry={isinstance(entry, Mapping)} nested_address={isinstance(address, Mapping)} "
        f"direct_address={isinstance(direct, Mapping)} fields={covered}/{len(_ADDRESS_FIELDS)} "
        f"row_keys={len(first) if isinstance(first, Mapping) else -1} "
        f"row_known={len(set(first).intersection(allowed_row)) if isinstance(first, Mapping) else -1} "
        f"row_metadata={len(set(first).intersection(row_metadata)) if isinstance(first, Mapping) else -1} "
        f"address_keys={len(candidate) if isinstance(candidate, Mapping) else -1} "
        f"address_metadata={len(set(candidate).intersection(address_metadata)) if isinstance(candidate, Mapping) else -1} "
        f"row_schema={_schema_keys(first)} address_schema={_schema_keys(candidate)} "
        f"field_count={len(field_rows)} field_schema={','.join(field_schema)} "
        f"field_types={','.join(field_types)} field_value_types={','.join(field_value_types)}"
    )


def _safe_saved_address_alias(snapshot: AddressSnapshot) -> str | None:
    """Derive a non-exact alias from provider display text, then title."""
    address_line_source = (
        snapshot.address_line if snapshot.display_title is not None else None
    )
    field_sources = (
        tuple(
            field.value
            for field in snapshot.fields
            if field.field_type in {"STREET_NAME", "BUILDING_NAME"}
        )
        if snapshot.display_title is not None
        else ()
    )
    for source in (
        snapshot.display_subtitle,
        address_line_source,
        *field_sources,
    ):
        if not source:
            continue
        first_segment = source.split(",", 1)[0]
        words: list[str] = []
        for token in first_segment.split():
            if any(char.isdigit() for char in token):
                continue
            cleaned = "".join(
                char for char in token if char.isalpha() or char in "-'’"
            ).strip("-'’")
            if not cleaned or cleaned.casefold() in _ROAD_TYPE_WORDS:
                continue
            next_candidate = " ".join((*words, cleaned))
            if len(next_candidate) > 40:
                break
            words.append(cleaned)
        candidate = " ".join(words)
        if candidate:
            return candidate
    if snapshot.display_title and snapshot.display_title.casefold() not in {
        "saved apartment",
        "saved home",
        "saved office",
        "saved other",
    }:
        return snapshot.display_title
    return None


class InvalidSelection(ValueError):
    """A local selection is missing, expired, stale, or owned by another user."""

    def __init__(self) -> None:
        super().__init__("selection is unavailable")


@dataclass(frozen=True, slots=True, repr=False)
class _PaymentContext:
    amount_minor: int = field(repr=False)
    currency: str = field(repr=False)
    checkout_session: str | None = field(repr=False)
    store_address_id: int | None = field(repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class _Selection:
    owner_key: str = field(repr=False)
    generation: int = field(repr=False)
    expires_at: float = field(repr=False)
    value: AddressSnapshot | SavedPayment = field(repr=False)
    payment_context: _PaymentContext | None = field(default=None, repr=False)


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
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
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
        try:
            snapshots = parse_saved_addresses(payload)
        except ContractError as err:
            _LOGGER.warning(
                "Glovo saved-address response failed schema validation at %s: %s",
                err.category,
                _address_payload_fingerprint(payload),
            )
            raise ApiSessionError(
                category=err.category,
                endpoint_family="address",
            ) from None
        expires_at = self._clock() + self.selection_ttl_seconds
        result: list[SavedAddressSummary] = []
        labels = {
            "HOUSE": "Saved home ••••",
            "APARTMENT": "Saved home ••••",
            "OFFICE": "Saved office ••••",
            "OTHER": "Saved destination ••••",
        }
        ordinal_bases = {
            "HOUSE": "Home",
            "APARTMENT": "Home",
            "OFFICE": "Office",
            "OTHER": "Destination",
        }
        ordinal_counts: dict[str, int] = {}
        for snapshot in snapshots:
            handle = self._new_handle(self._addresses)
            self._addresses[handle] = _Selection(
                owner, current_generation, expires_at, snapshot
            )
            label = labels[snapshot.kind]
            alias = _safe_saved_address_alias(snapshot)
            if alias:
                try:
                    label = SavedAddressSummary(handle, f"{alias} ••••").masked_label
                except ValueError:
                    alias = None
            if not alias and snapshot.display_title is not None:
                base = ordinal_bases[snapshot.kind]
                ordinal = ordinal_counts.get(base, 0) + 1
                ordinal_counts[base] = ordinal
                label = f"{base} {_ORDINAL_WORDS[ordinal - 1]} ••••"
            result.append(SavedAddressSummary(handle, label))
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
        selected = tuple(item for item in payments if item.selected is True)
        if len(selected) > 1:
            raise InvalidSelection
        expires_at = self._clock() + self.selection_ttl_seconds
        context = _PaymentContext(amount_minor, currency, checkout_session, store_address_id)
        result: list[MaskedPaymentSummary] = []
        for payment in selected:
            handle = self._new_handle(self._payments)
            self._payments[handle] = _Selection(
                owner, current_generation, expires_at, payment, context
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

    async def async_revalidate_payment(
        self,
        handle: str,
        *,
        owner_key: str,
        generation: int,
        amount_minor: int,
        currency: str,
        checkout_session: str,
        store_address_id: int,
    ) -> bool:
        """Re-fetch the exact checkout-scoped method and require the same selected card."""
        owner, current_generation = self._identity(owner_key, generation)
        selected = self._resolve(
            self._payments, handle, owner_key=owner, generation=current_generation
        )
        if not isinstance(selected, SavedPayment) or selected.selected is not True:
            return False
        record = self._payments.get(handle)
        try:
            query = build_payment_query(
                amount_minor=amount_minor,
                currency=currency,
                checkout_session=checkout_session,
                store_address_id=store_address_id,
                client_supports=("CREDIT_CARD",),
                client_ready=True,
            )
        except Exception:
            return False
        payload = await self._session.async_get("payment", _PAYMENT_PATH, query)
        current = parse_saved_payments(payload)
        matches = [
            item
            for item in current
            if item.selected is True
            and item.payment_instrument_id == selected.payment_instrument_id
            and item.metadata_id == selected.metadata_id
            and item.last_four_digits == selected.last_four_digits
        ]
        return len(matches) == 1 and record is not None and record.owner_key == owner

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
