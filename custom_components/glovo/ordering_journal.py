"""Durable v2 privacy-minimal journal for checkout attempts and recovery."""

from __future__ import annotations

import asyncio
import copy
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol

from .ordering_models import (
    ISO_4217_EXPONENTS,
    ItemDisplaySummary,
    MaskedPaymentSummary,
    SavedAddressSummary,
    StoreDisplayName,
)

JOURNAL_VERSION = 2
LEGACY_JOURNAL_VERSION = 1
MAX_JOURNAL_RECORDS = 200


class JournalState(StrEnum):
    """Durable attempt states."""

    DRAFT = "DRAFT"
    QUOTED = "QUOTED"
    RESERVED = "RESERVED"
    DISPATCHING = "DISPATCHING"
    VERIFYING = "VERIFYING"
    MANUAL_CHECK_REQUIRED = "MANUAL_CHECK_REQUIRED"
    CONFIRMED_SUCCEEDED = "CONFIRMED_SUCCEEDED"
    CONFIRMED_FAILED = "CONFIRMED_FAILED"
    LEGACY_MOCK_NO_REMOTE_EFFECT = "LEGACY_MOCK_NO_REMOTE_EFFECT"
    INTEGRITY_FAULT = "INTEGRITY_FAULT"
    # Source-compatible aliases. V2 persistence always uses the values above.
    UNCERTAIN = "MANUAL_CHECK_REQUIRED"
    SECURITY_FAULT = "INTEGRITY_FAULT"


class JournalFault(RuntimeError):
    """Base class for redaction-safe journal failures."""


class JournalCorrupt(JournalFault):
    """Durable journal data or a requested transition is untrustworthy."""


class JournalStorageFault(JournalFault):
    """Durable journal storage could not be read or atomically saved."""


class JournalStorage(Protocol):
    async def async_load(self) -> Any: ...

    async def async_save(self, data: dict[str, Any]) -> None: ...


class MemoryJournalStorage:
    """Separate v2/v1 deterministic storage used by tests."""

    def __init__(self, data: Any = None, *, legacy_data: Any = None) -> None:
        if isinstance(data, Mapping) and data.get("version") == LEGACY_JOURNAL_VERSION:
            self.data = None
            self.legacy_data = copy.deepcopy(data)
        else:
            self.data = copy.deepcopy(data)
            self.legacy_data = copy.deepcopy(legacy_data)

    async def async_load(self) -> Any:
        return copy.deepcopy(self.data)

    async def async_load_legacy(self) -> Any:
        return copy.deepcopy(self.legacy_data)

    async def async_save(self, data: dict[str, Any]) -> None:
        self.data = copy.deepcopy(data)


class HomeAssistantJournalStorage:
    """Use a new v2 key while retaining the old v1 store untouched."""

    def __init__(self, hass: Any, entry_key: str) -> None:
        from homeassistant.helpers.storage import Store

        self._store = Store(hass, JOURNAL_VERSION, f"glovo.ordering_journal_v2.{entry_key}")
        self._legacy_store = Store(
            hass, LEGACY_JOURNAL_VERSION, f"glovo.ordering_journal.{entry_key}"
        )

    async def async_load(self) -> Any:
        return await self._store.async_load()

    async def async_load_legacy(self) -> Any:
        return await self._legacy_store.async_load()

    async def async_save(self, data: dict[str, Any]) -> None:
        await self._store.async_save(data)


def _finite_timestamp(value: object, message: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise JournalCorrupt(message)
    return float(value)


def _optional_timestamp(value: object, message: str) -> float | None:
    return None if value is None else _finite_timestamp(value, message)


def _strict_int(value: object, message: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise JournalCorrupt(message)
    return value


def _safe_text(
    value: object,
    message: str,
    *,
    maximum: int,
    optional: bool = False,
) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str):
        raise JournalCorrupt(message)
    normalized = " ".join(value.split())
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)
    ):
        raise JournalCorrupt(message)
    return normalized


def _hash(value: object, message: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise JournalCorrupt(message)
    return value


_CHECKOUT_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")


def _checkout_reference(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _CHECKOUT_REFERENCE_RE.fullmatch(value):
        raise JournalCorrupt("journal checkout reference is invalid")
    return value


_RECORD_KEYS = frozenset(
    {
        "attempt_id",
        "state",
        "created_at",
        "updated_at",
        "generation",
        "amount_minor",
        "currency",
        "execution_mode",
        "request_fingerprint",
        "provider_session_hash",
        "checkout_id",
        "dispatch_started_at",
        "failure_class",
        "resolution",
        "evidence_source",
        "record_revision",
        "store_display_name",
        "item_count",
        "item_summary",
        "masked_payment_label",
        "masked_address_alias",
        "last_reviewed_at",
    }
)
_ALLOWED_EXECUTION_MODES = frozenset({"mock", "live"})
_ALLOWED_FAILURE_CLASSES = frozenset(
    {
        "cancelled",
        "connection_reset",
        "http_5xx",
        "malformed_response",
        "persistence_failure",
        "restart_inflight",
        "timeout",
        "unknown_ambiguous",
    }
)
_ALLOWED_RESOLUTIONS = frozenset(
    {
        "found_succeeded",
        "found_failed_or_cancelled",
        "legacy_mock_no_remote_effect",
        "synthetic_success",
        "synthetic_failure",
    }
)
_ALLOWED_EVIDENCE = frozenset({"admin_review", "legacy_migration", "mock_adapter"})


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """V2 durable attempt metadata; only privacy-safe display fields are stored."""

    attempt_id: str
    state: JournalState
    created_at: float
    updated_at: float
    generation: int
    amount_minor: int
    currency: str
    execution_mode: str
    request_fingerprint: str
    provider_session_hash: str | None
    checkout_id: str | None
    dispatch_started_at: float | None
    failure_class: str | None
    resolution: str | None
    evidence_source: str | None
    record_revision: int
    store_display_name: str
    item_count: int
    item_summary: str
    masked_payment_label: str
    masked_address_alias: str
    last_reviewed_at: float | None

    @property
    def quote_fingerprint(self) -> str:
        """Compatibility name for the v1 mock-only manager."""
        return self.request_fingerprint

    @property
    def outcome(self) -> str | None:
        """Compatibility view for old tests/callers."""
        return self.resolution or self.failure_class

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "generation": self.generation,
            "amount_minor": self.amount_minor,
            "currency": self.currency,
            "execution_mode": self.execution_mode,
            "request_fingerprint": self.request_fingerprint,
            "provider_session_hash": self.provider_session_hash,
            "checkout_id": self.checkout_id,
            "dispatch_started_at": self.dispatch_started_at,
            "failure_class": self.failure_class,
            "resolution": self.resolution,
            "evidence_source": self.evidence_source,
            "record_revision": self.record_revision,
            "store_display_name": self.store_display_name,
            "item_count": self.item_count,
            "item_summary": self.item_summary,
            "masked_payment_label": self.masked_payment_label,
            "masked_address_alias": self.masked_address_alias,
            "last_reviewed_at": self.last_reviewed_at,
        }

    @classmethod
    def from_dict(cls, value: object) -> AttemptRecord:
        if not isinstance(value, Mapping) or frozenset(value) != _RECORD_KEYS:
            raise JournalCorrupt("journal record schema mismatch")
        attempt_id = value["attempt_id"]
        if (
            not isinstance(attempt_id, str)
            or not attempt_id.startswith("attempt-")
            or len(attempt_id) > 100
        ):
            raise JournalCorrupt("journal attempt key is invalid")
        try:
            state = JournalState(value["state"])
        except (TypeError, ValueError) as err:
            raise JournalCorrupt("journal state is invalid") from err
        currency = value["currency"]
        if not isinstance(currency, str) or currency not in ISO_4217_EXPONENTS:
            raise JournalCorrupt("journal currency is invalid")
        execution_mode = value["execution_mode"]
        if execution_mode not in _ALLOWED_EXECUTION_MODES:
            raise JournalCorrupt("journal execution mode is invalid")
        failure_class = value["failure_class"]
        if failure_class is not None and failure_class not in _ALLOWED_FAILURE_CLASSES:
            raise JournalCorrupt("journal failure class is invalid")
        resolution = value["resolution"]
        if resolution is not None and resolution not in _ALLOWED_RESOLUTIONS:
            raise JournalCorrupt("journal resolution is invalid")
        evidence_source = value["evidence_source"]
        if evidence_source is not None and evidence_source not in _ALLOWED_EVIDENCE:
            raise JournalCorrupt("journal evidence source is invalid")
        checkout_id = _checkout_reference(value["checkout_id"])
        try:
            store_display_name = StoreDisplayName(value["store_display_name"]).value
            item_summary = ItemDisplaySummary(value["item_summary"]).value
            masked_payment_label = MaskedPaymentSummary(
                "journal-payment", value["masked_payment_label"]
            ).masked_label
            masked_address_alias = SavedAddressSummary(
                "journal-address", value["masked_address_alias"]
            ).masked_label
        except (TypeError, ValueError) as err:
            raise JournalCorrupt("journal display metadata is invalid") from err
        record = cls(
            attempt_id=attempt_id,
            state=state,
            created_at=_finite_timestamp(value["created_at"], "journal timestamp is invalid"),
            updated_at=_finite_timestamp(value["updated_at"], "journal timestamp is invalid"),
            generation=_strict_int(value["generation"], "journal generation is invalid", minimum=1),
            amount_minor=_strict_int(value["amount_minor"], "journal amount is invalid"),
            currency=currency,
            execution_mode=execution_mode,
            request_fingerprint=_hash(
                value["request_fingerprint"], "journal fingerprint is invalid"
            ),  # type: ignore[arg-type]
            provider_session_hash=_hash(
                value["provider_session_hash"],
                "journal provider session hash is invalid",
                optional=True,
            ),
            checkout_id=checkout_id,
            dispatch_started_at=_optional_timestamp(
                value["dispatch_started_at"], "journal dispatch timestamp is invalid"
            ),
            failure_class=failure_class,
            resolution=resolution,
            evidence_source=evidence_source,
            record_revision=_strict_int(
                value["record_revision"], "journal revision is invalid", minimum=1
            ),
            store_display_name=store_display_name,
            item_count=_strict_int(value["item_count"], "journal item count is invalid"),
            item_summary=item_summary,
            masked_payment_label=masked_payment_label,
            masked_address_alias=masked_address_alias,
            last_reviewed_at=_optional_timestamp(
                value["last_reviewed_at"], "journal review timestamp is invalid"
            ),
        )
        _validate_record_invariants(record)
        return record

    def recovery_dict(self) -> dict[str, Any]:
        """Return the strict privacy allowlist for admin recovery."""
        return {
            "attemptRef": self.attempt_id,
            "state": self.state.value,
            "recordRevision": self.record_revision,
            "submittedAt": self.dispatch_started_at or self.created_at,
            "storeDisplayName": self.store_display_name,
            "amountMinor": self.amount_minor,
            "currency": self.currency,
            "itemCount": self.item_count,
            "itemSummary": self.item_summary,
            "maskedPaymentLabel": self.masked_payment_label,
            "maskedAddressAlias": self.masked_address_alias,
            "hasCheckoutId": self.checkout_id is not None,
            "ambiguityReason": self.failure_class,
            "lastReviewedAt": self.last_reviewed_at,
        }


def _validate_record_invariants(record: AttemptRecord) -> None:
    """Enforce the complete state/mode/authority matrix on load and write."""
    if record.updated_at < record.created_at:
        raise JournalCorrupt("journal timestamps are contradictory")
    if record.dispatch_started_at is not None and not (
        record.created_at <= record.dispatch_started_at <= record.updated_at
    ):
        raise JournalCorrupt("journal dispatch timestamp is contradictory")

    empty_authority = (
        record.failure_class is None
        and record.resolution is None
        and record.evidence_source is None
        and record.last_reviewed_at is None
    )
    if record.state in {
        JournalState.DRAFT,
        JournalState.QUOTED,
        JournalState.RESERVED,
    }:
        if record.dispatch_started_at is not None or not empty_authority:
            raise JournalCorrupt("pre-dispatch journal metadata is contradictory")
        return
    if record.state in {JournalState.DISPATCHING, JournalState.VERIFYING}:
        if record.dispatch_started_at is None or not empty_authority:
            raise JournalCorrupt("in-flight journal metadata is contradictory")
        return
    if record.state is JournalState.MANUAL_CHECK_REQUIRED:
        if (
            record.execution_mode != "live"
            or record.dispatch_started_at is None
            or record.failure_class is None
            or record.resolution is not None
            or record.evidence_source is not None
        ):
            raise JournalCorrupt("manual check journal metadata is contradictory")
        return
    if record.state is JournalState.CONFIRMED_SUCCEEDED:
        expected = {
            ("live", "found_succeeded", "admin_review"),
            ("mock", "synthetic_success", "mock_adapter"),
        }
    elif record.state is JournalState.CONFIRMED_FAILED:
        expected = {
            ("live", "found_failed_or_cancelled", "admin_review"),
            ("mock", "synthetic_failure", "mock_adapter"),
        }
    else:
        expected = set()
    if expected:
        if (
            (record.execution_mode, record.resolution, record.evidence_source)
            not in expected
            or (record.execution_mode == "live" and record.failure_class is None)
            or (record.execution_mode == "live" and record.last_reviewed_at is None)
            or (record.execution_mode == "mock" and record.failure_class is not None)
        ):
            raise JournalCorrupt("confirmed journal metadata is contradictory")
        return
    if record.state is JournalState.LEGACY_MOCK_NO_REMOTE_EFFECT:
        if (
            record.execution_mode != "mock"
            or record.resolution != "legacy_mock_no_remote_effect"
            or record.evidence_source not in {"legacy_migration", "mock_adapter"}
            or record.failure_class is not None
        ):
            raise JournalCorrupt("legacy terminal journal metadata is contradictory")
        return
    if record.state is JournalState.INTEGRITY_FAULT and (
        record.resolution is not None
        or record.evidence_source is not None
        or record.failure_class is not None
    ):
        raise JournalCorrupt("integrity journal metadata is contradictory")


_ALLOWED_TRANSITIONS = {
    JournalState.DRAFT: frozenset({JournalState.QUOTED, JournalState.INTEGRITY_FAULT}),
    JournalState.QUOTED: frozenset(
        {JournalState.RESERVED, JournalState.DISPATCHING, JournalState.INTEGRITY_FAULT}
    ),
    JournalState.RESERVED: frozenset(
        {JournalState.DISPATCHING, JournalState.INTEGRITY_FAULT}
    ),
    JournalState.DISPATCHING: frozenset(
        {
            JournalState.VERIFYING,
            JournalState.CONFIRMED_SUCCEEDED,
            JournalState.CONFIRMED_FAILED,
            JournalState.MANUAL_CHECK_REQUIRED,
            JournalState.LEGACY_MOCK_NO_REMOTE_EFFECT,
            JournalState.INTEGRITY_FAULT,
        }
    ),
    JournalState.VERIFYING: frozenset(
        {
            JournalState.CONFIRMED_SUCCEEDED,
            JournalState.CONFIRMED_FAILED,
            JournalState.MANUAL_CHECK_REQUIRED,
            JournalState.LEGACY_MOCK_NO_REMOTE_EFFECT,
            JournalState.INTEGRITY_FAULT,
        }
    ),
    JournalState.MANUAL_CHECK_REQUIRED: frozenset(
        {
            JournalState.CONFIRMED_SUCCEEDED,
            JournalState.CONFIRMED_FAILED,
            JournalState.INTEGRITY_FAULT,
        }
    ),
}
_TERMINAL_STATES = frozenset(
    {
        JournalState.CONFIRMED_SUCCEEDED,
        JournalState.CONFIRMED_FAILED,
        JournalState.LEGACY_MOCK_NO_REMOTE_EFFECT,
        JournalState.INTEGRITY_FAULT,
    }
)


def _migrate_v1_record(value: object) -> AttemptRecord:
    required = frozenset(
        {
            "attempt_id",
            "state",
            "created_at",
            "updated_at",
            "generation",
            "quote_fingerprint",
            "outcome",
        }
    )
    if not isinstance(value, Mapping) or frozenset(value) != required:
        raise JournalCorrupt("legacy journal record schema mismatch")
    state = value["state"]
    outcome = value["outcome"]
    if state in {
        "DRAFT",
        "QUOTED",
        "RESERVED",
        "DISPATCHING",
        "VERIFYING",
        "UNCERTAIN",
    } and outcome is not None:
        raise JournalCorrupt("legacy journal outcome contradicts nonterminal state")
    if state == "SECURITY_FAULT":
        migrated_state = JournalState.INTEGRITY_FAULT
        resolution = None
        failure_class = None
        evidence = None
    elif state == "CONFIRMED_SUCCEEDED":
        migrated_state = JournalState.CONFIRMED_SUCCEEDED
        resolution = "synthetic_success"
        failure_class = None
        evidence = "mock_adapter"
    elif state == "CONFIRMED_FAILED":
        migrated_state = JournalState.CONFIRMED_FAILED
        resolution = "synthetic_failure"
        failure_class = None
        evidence = "mock_adapter"
    elif state in {"DRAFT", "QUOTED", "RESERVED", "DISPATCHING", "VERIFYING", "UNCERTAIN"}:
        migrated_state = JournalState.LEGACY_MOCK_NO_REMOTE_EFFECT
        resolution = "legacy_mock_no_remote_effect"
        failure_class = None
        evidence = "legacy_migration"
    else:
        raise JournalCorrupt("legacy journal state is invalid")
    return AttemptRecord.from_dict(
        {
            "attempt_id": value["attempt_id"],
            "state": migrated_state.value,
            "created_at": value["created_at"],
            "updated_at": value["updated_at"],
            "generation": value["generation"],
            "amount_minor": 0,
            "currency": "AMD",
            "execution_mode": "mock",
            "request_fingerprint": value["quote_fingerprint"],
            "provider_session_hash": None,
            "checkout_id": None,
            "dispatch_started_at": None,
            "failure_class": failure_class,
            "resolution": resolution,
            "evidence_source": evidence,
            "record_revision": 1,
            "store_display_name": "Legacy mock fixture",
            "item_count": 0,
            "item_summary": "Legacy mock attempt",
            "masked_payment_label": "Legacy mock ••••",
            "masked_address_alias": "Legacy mock ••••",
            "last_reviewed_at": value["updated_at"],
        }
    )


class AttemptJournal:
    """Serialized v2 journal that publishes only successfully persisted mutations."""

    def __init__(self, storage: JournalStorage, *, clock: Callable[[], float]) -> None:
        self._storage = storage
        self._clock = clock
        self._lock = asyncio.Lock()
        self._records: list[AttemptRecord] = []
        self.corrupt = False
        self.loaded = False
        self.load_source = "unloaded"

    @property
    def records(self) -> tuple[AttemptRecord, ...]:
        return tuple(self._records)

    @property
    def unresolved_manual_checks(self) -> tuple[AttemptRecord, ...]:
        return tuple(
            item for item in self._records if item.state is JournalState.MANUAL_CHECK_REQUIRED
        )

    @property
    def integrity_fault(self) -> bool:
        return self.corrupt or any(
            item.state is JournalState.INTEGRITY_FAULT for item in self._records
        )

    @property
    def unresolved_safety_fault(self) -> bool:
        """Compatibility aggregate; callers should distinguish both properties."""
        return bool(self.unresolved_manual_checks) or self.integrity_fault

    def get(self, attempt_id: str) -> AttemptRecord:
        try:
            return next(item for item in self._records if item.attempt_id == attempt_id)
        except StopIteration as err:
            raise JournalCorrupt("attempt key is absent") from err

    def _now(self) -> float:
        return _finite_timestamp(self._clock(), "journal clock is invalid")

    @staticmethod
    def _payload(records: list[AttemptRecord]) -> dict[str, Any]:
        return {"version": JOURNAL_VERSION, "records": [item.to_dict() for item in records]}

    async def _async_save_records_unlocked(self, records: list[AttemptRecord]) -> None:
        if self.corrupt or not self.loaded:
            raise JournalCorrupt("journal is not writable")
        try:
            await self._storage.async_save(self._payload(records))
        except Exception as err:
            raise JournalStorageFault("journal storage could not be saved") from err

    async def async_load(self) -> tuple[AttemptRecord, ...]:
        async with self._lock:
            try:
                raw = await self._storage.async_load()
                source = "v2"
                if raw is None:
                    legacy_loader = getattr(self._storage, "async_load_legacy", None)
                    raw = await legacy_loader() if legacy_loader is not None else None
                    source = "v1" if raw is not None else "new"
                elif isinstance(raw, Mapping) and raw.get("version") == LEGACY_JOURNAL_VERSION:
                    source = "v1"
            except Exception as err:
                self.corrupt = True
                self.loaded = False
                self._records = []
                raise JournalStorageFault("journal storage could not be read") from err
            try:
                if raw is None:
                    self._records = []
                    self.loaded = True
                    self.load_source = "new"
                    return ()
                if (
                    not isinstance(raw, Mapping)
                    or frozenset(raw) != {"version", "records"}
                    or not isinstance(raw["records"], list)
                    or len(raw["records"]) > MAX_JOURNAL_RECORDS
                ):
                    raise JournalCorrupt("journal container schema mismatch")
                if source == "v1":
                    if raw["version"] != LEGACY_JOURNAL_VERSION:
                        raise JournalCorrupt("legacy journal version mismatch")
                    records = [_migrate_v1_record(item) for item in raw["records"]]
                else:
                    if raw["version"] != JOURNAL_VERSION:
                        raise JournalCorrupt("journal version mismatch")
                    records = [AttemptRecord.from_dict(item) for item in raw["records"]]
                if len({item.attempt_id for item in records}) != len(records):
                    raise JournalCorrupt("journal attempt keys are not unique")

                recovered = source == "v1"
                normalized: list[AttemptRecord] = []
                for record in records:
                    if record.state in {JournalState.DISPATCHING, JournalState.VERIFYING}:
                        now = self._now()
                        if record.execution_mode == "live":
                            record = replace(
                                record,
                                state=JournalState.MANUAL_CHECK_REQUIRED,
                                updated_at=now,
                                failure_class="restart_inflight",
                                record_revision=record.record_revision + 1,
                            )
                        else:
                            record = replace(
                                record,
                                state=JournalState.LEGACY_MOCK_NO_REMOTE_EFFECT,
                                updated_at=now,
                                resolution="legacy_mock_no_remote_effect",
                                evidence_source="legacy_migration",
                                record_revision=record.record_revision + 1,
                            )
                        recovered = True
                    normalized.append(AttemptRecord.from_dict(record.to_dict()))
                self.loaded = True
                self.load_source = source
                if recovered:
                    await self._async_save_records_unlocked(normalized)
                self._records = normalized
                return self.records
            except JournalFault:
                self.corrupt = True
                self.loaded = False
                self._records = []
                raise

    @staticmethod
    def _bounded(records: list[AttemptRecord]) -> list[AttemptRecord]:
        if len(records) <= MAX_JOURNAL_RECORDS:
            return records
        protected = [
            item
            for item in records
            if item.state in {JournalState.MANUAL_CHECK_REQUIRED, JournalState.INTEGRITY_FAULT}
        ]
        if len(protected) >= MAX_JOURNAL_RECORDS:
            raise JournalCorrupt("journal is full of protected attempts")
        removable = [item for item in records if item not in protected]
        return protected + removable[-(MAX_JOURNAL_RECORDS - len(protected)) :]

    async def async_create(
        self,
        attempt_id: str,
        generation: int,
        request_fingerprint: str,
        *,
        amount_minor: int = 0,
        currency: str = "AMD",
        execution_mode: str = "mock",
        provider_session_hash: str | None = None,
        checkout_id: str | None = None,
        store_display_name: str = "Mock fixture store",
        item_count: int = 0,
        item_summary: str = "Mock fixture attempt",
        masked_payment_label: str = "Mock payment ••••",
        masked_address_alias: str = "Mock destination ••••",
    ) -> AttemptRecord:
        async with self._lock:
            if any(item.attempt_id == attempt_id for item in self._records):
                raise JournalCorrupt("attempt key collision")
            now = self._now()
            record = AttemptRecord.from_dict(
                {
                    "attempt_id": attempt_id,
                    "state": JournalState.DRAFT.value,
                    "created_at": now,
                    "updated_at": now,
                    "generation": generation,
                    "amount_minor": amount_minor,
                    "currency": currency,
                    "execution_mode": execution_mode,
                    "request_fingerprint": request_fingerprint,
                    "provider_session_hash": provider_session_hash,
                    "checkout_id": checkout_id,
                    "dispatch_started_at": None,
                    "failure_class": None,
                    "resolution": None,
                    "evidence_source": None,
                    "record_revision": 1,
                    "store_display_name": store_display_name,
                    "item_count": item_count,
                    "item_summary": item_summary,
                    "masked_payment_label": masked_payment_label,
                    "masked_address_alias": masked_address_alias,
                    "last_reviewed_at": None,
                }
            )
            candidate = self._bounded([*self._records, record])
            await self._async_save_records_unlocked(candidate)
            self._records = candidate
            return record

    async def async_transition(
        self,
        attempt_id: str,
        state: JournalState,
        *,
        outcome: str | None = None,
        failure_class: str | None = None,
        resolution: str | None = None,
        evidence_source: str | None = None,
    ) -> AttemptRecord:
        async with self._lock:
            try:
                index = next(
                    index for index, item in enumerate(self._records) if item.attempt_id == attempt_id
                )
            except StopIteration as err:
                raise JournalCorrupt("attempt key is absent") from err
            current = self._records[index]
            if not isinstance(state, JournalState):
                raise JournalCorrupt("journal state is invalid")
            if current.state in _TERMINAL_STATES or state not in _ALLOWED_TRANSITIONS.get(
                current.state, frozenset()
            ):
                raise JournalCorrupt("illegal journal state transition")
            if outcome is not None:
                if state is JournalState.MANUAL_CHECK_REQUIRED:
                    failure_class = (
                        outcome if outcome in _ALLOWED_FAILURE_CLASSES else "persistence_failure"
                    )
                elif state in {JournalState.CONFIRMED_SUCCEEDED, JournalState.CONFIRMED_FAILED}:
                    resolution = outcome
                    evidence_source = evidence_source or "mock_adapter"
            now = self._now()
            updated = AttemptRecord.from_dict(
                replace(
                    current,
                    state=state,
                    updated_at=now,
                    dispatch_started_at=(
                        now
                        if state is JournalState.DISPATCHING
                        and current.dispatch_started_at is None
                        else current.dispatch_started_at
                    ),
                    failure_class=failure_class,
                    resolution=resolution,
                    evidence_source=evidence_source,
                    record_revision=current.record_revision + 1,
                ).to_dict()
            )
            candidate = list(self._records)
            candidate[index] = updated
            await self._async_save_records_unlocked(candidate)
            self._records = candidate
            return updated

    async def async_review_unknown(
        self, attempt_id: str, *, expected_revision: int
    ) -> AttemptRecord:
        async with self._lock:
            current = self.get(attempt_id)
            if (
                current.state is not JournalState.MANUAL_CHECK_REQUIRED
                or current.record_revision != expected_revision
            ):
                raise JournalCorrupt("manual check revision or state changed")
            index = self._records.index(current)
            now = self._now()
            updated = AttemptRecord.from_dict(
                replace(
                    current,
                    updated_at=now,
                    last_reviewed_at=now,
                    record_revision=current.record_revision + 1,
                ).to_dict()
            )
            candidate = list(self._records)
            candidate[index] = updated
            await self._async_save_records_unlocked(candidate)
            self._records = candidate
            return updated

    async def async_resolve(
        self,
        attempt_id: str,
        *,
        expected_revision: int,
        resolution: str,
    ) -> AttemptRecord:
        if resolution not in {"found_succeeded", "found_failed_or_cancelled"}:
            raise JournalCorrupt("manual resolution is invalid")
        state = (
            JournalState.CONFIRMED_SUCCEEDED
            if resolution == "found_succeeded"
            else JournalState.CONFIRMED_FAILED
        )
        async with self._lock:
            current = self.get(attempt_id)
            if (
                current.state is not JournalState.MANUAL_CHECK_REQUIRED
                or current.record_revision != expected_revision
            ):
                raise JournalCorrupt("manual check revision or state changed")
            index = self._records.index(current)
            now = self._now()
            updated = AttemptRecord.from_dict(
                replace(
                    current,
                    state=state,
                    updated_at=now,
                    resolution=resolution,
                    evidence_source="admin_review",
                    last_reviewed_at=now,
                    record_revision=current.record_revision + 1,
                ).to_dict()
            )
            candidate = list(self._records)
            candidate[index] = updated
            await self._async_save_records_unlocked(candidate)
            self._records = candidate
            return updated
