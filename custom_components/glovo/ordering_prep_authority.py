"""Durable, transport-free authority for one preparatory provider mutation.

This module deliberately owns no HTTP client.  A caller may invoke a provider
transport only after :meth:`async_acquire` has returned a DISPATCHING record.
Every ambiguous outcome is recorded as a non-replayable reconciliation block.
"""

from __future__ import annotations

import asyncio
import copy
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol

from .ordering_state import DurableOrderingState, OrderingStateFault

PREP_AUTHORITY_VERSION = 1
MAX_PREP_ATTEMPTS = 100


class PreparationPurpose(StrEnum):
    """Closed set of reviewed preparatory remote mutation purposes."""

    BASKET_CREATE = "basket_create"
    BASKET_REPLACE = "basket_replace"
    BASKET_DELETE = "basket_delete"
    QUOTE_TEMPLATE_CREATE = "quote_template_create"


class PreparationState(StrEnum):
    PREPARED = "PREPARED"
    DISPATCHING = "DISPATCHING"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    RECONCILED = "RECONCILED"
    PROVIDER_SUCCEEDED = "PROVIDER_SUCCEEDED"
    PROVIDER_FAILED = "PROVIDER_FAILED"
    INTEGRITY_FAULT = "INTEGRITY_FAULT"


class PreparationAuthorityFault(RuntimeError):
    """A redaction-safe authority or persistence failure."""


class PreparationReplayDenied(PreparationAuthorityFault):
    """An existing intent/attempt may not be remotely submitted again."""


class PreparationStorage(Protocol):
    async def async_load(self) -> Any: ...

    async def async_save(self, data: dict[str, Any]) -> None: ...


class MemoryPreparationStorage:
    """Deterministic private v1 storage for tests."""

    def __init__(self, data: Any = None) -> None:
        self.data = copy.deepcopy(data)

    async def async_load(self) -> Any:
        return copy.deepcopy(self.data)

    async def async_save(self, data: dict[str, Any]) -> None:
        self.data = copy.deepcopy(data)


class HomeAssistantPreparationStorage:
    """New private key; it never reads or rewrites earlier order stores."""

    def __init__(self, hass: Any, entry_key: str) -> None:
        from homeassistant.helpers.storage import Store

        self._store = Store(
            hass,
            PREP_AUTHORITY_VERSION,
            f"glovo.ordering_prep_authority_v1.{entry_key}",
        )

    async def async_load(self) -> Any:
        return await self._store.async_load()

    async def async_save(self, data: dict[str, Any]) -> None:
        await self._store.async_save(data)


def _hash(value: object, message: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise PreparationAuthorityFault(message)
    return value


def _attempt_id(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("prep-") or len(value) > 100:
        raise PreparationAuthorityFault("preparation attempt identity is invalid")
    return value


def _category(value: object) -> str:
    # This is intentionally a tiny privacy-safe classification, never a URL/body.
    if value not in {"expected_present", "expected_absent", "expected_template"}:
        raise PreparationAuthorityFault("preparation state category is invalid")
    return str(value)


def _timestamp(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PreparationAuthorityFault("preparation timestamp is invalid")
    return float(value)


@dataclass(frozen=True, slots=True)
class PreparationAttempt:
    """Private durable identity and hashed expectation for one remote mutation."""

    attempt_id: str
    generation: int
    purpose: PreparationPurpose
    expectation_hash: str
    expected_category: str
    state: PreparationState
    record_revision: int
    created_at: float
    updated_at: float
    dispatch_started_at: float | None
    reconciliation_get_used: bool
    outcome_category: str | None
    provider_evidence_hash: str | None
    private_checkout_reference: str | None

    def to_raw(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "generation": self.generation,
            "purpose": self.purpose.value,
            "expectation_hash": self.expectation_hash,
            "expected_category": self.expected_category,
            "state": self.state.value,
            "record_revision": self.record_revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "dispatch_started_at": self.dispatch_started_at,
            "reconciliation_get_used": self.reconciliation_get_used,
            "outcome_category": self.outcome_category,
            "provider_evidence_hash": self.provider_evidence_hash,
            "private_checkout_reference": self.private_checkout_reference,
        }

    @classmethod
    def from_raw(cls, raw: object) -> PreparationAttempt:
        keys = {
            "attempt_id", "generation", "purpose", "expectation_hash", "expected_category",
            "state", "record_revision", "created_at", "updated_at", "dispatch_started_at",
            "reconciliation_get_used", "outcome_category", "provider_evidence_hash",
            "private_checkout_reference",
        }
        if not isinstance(raw, Mapping) or frozenset(raw) != frozenset(keys):
            raise PreparationAuthorityFault("preparation record schema mismatch")
        try:
            purpose = PreparationPurpose(raw["purpose"])
            state = PreparationState(raw["state"])
        except (TypeError, ValueError) as err:
            raise PreparationAuthorityFault("preparation purpose or state is invalid") from err
        generation = raw["generation"]
        revision = raw["record_revision"]
        if (
            isinstance(generation, bool) or not isinstance(generation, int) or generation < 1
            or isinstance(revision, bool) or not isinstance(revision, int) or revision < 1
            or not isinstance(raw["reconciliation_get_used"], bool)
        ):
            raise PreparationAuthorityFault("preparation generation or revision is invalid")
        reference = raw["private_checkout_reference"]
        if reference is not None and (
            not isinstance(reference, str) or not reference or len(reference) > 100
        ):
            raise PreparationAuthorityFault("private checkout reference is invalid")
        outcome = raw["outcome_category"]
        if outcome is not None and outcome not in {"ambiguous", "provider_success", "provider_failure"}:
            raise PreparationAuthorityFault("preparation outcome category is invalid")
        evidence = raw["provider_evidence_hash"]
        record = cls(
            attempt_id=_attempt_id(raw["attempt_id"]),
            generation=generation,
            purpose=purpose,
            expectation_hash=_hash(raw["expectation_hash"], "preparation expectation hash is invalid"),
            expected_category=_category(raw["expected_category"]),
            state=state,
            record_revision=revision,
            created_at=_timestamp(raw["created_at"]),
            updated_at=_timestamp(raw["updated_at"]),
            dispatch_started_at=(None if raw["dispatch_started_at"] is None else _timestamp(raw["dispatch_started_at"])),
            reconciliation_get_used=raw["reconciliation_get_used"],
            outcome_category=outcome,
            provider_evidence_hash=(None if evidence is None else _hash(evidence, "provider evidence hash is invalid")),
            private_checkout_reference=reference,
        )
        _validate(record)
        return record


def _validate(record: PreparationAttempt) -> None:
    if record.updated_at < record.created_at:
        raise PreparationAuthorityFault("preparation timestamps are contradictory")
    pre_dispatch = record.state is PreparationState.PREPARED
    in_flight = record.state is PreparationState.DISPATCHING
    terminal = record.state in {
        PreparationState.RECONCILED,
        PreparationState.PROVIDER_SUCCEEDED,
        PreparationState.PROVIDER_FAILED,
    }
    if pre_dispatch and (
        record.dispatch_started_at is not None or record.outcome_category is not None
        or record.provider_evidence_hash is not None or record.private_checkout_reference is not None
        or record.reconciliation_get_used
    ):
        raise PreparationAuthorityFault("prepared preparation metadata is contradictory")
    if in_flight and (
        record.dispatch_started_at is None or record.outcome_category is not None
        or record.provider_evidence_hash is not None or record.private_checkout_reference is not None
        or record.reconciliation_get_used
    ):
        raise PreparationAuthorityFault("dispatching preparation metadata is contradictory")
    if record.state is PreparationState.RECONCILIATION_REQUIRED and (
        record.dispatch_started_at is None or record.outcome_category != "ambiguous"
        or record.provider_evidence_hash is not None or record.private_checkout_reference is not None
    ):
        raise PreparationAuthorityFault("reconciliation preparation metadata is contradictory")
    if record.state is PreparationState.RECONCILED and (
        record.dispatch_started_at is None or not record.reconciliation_get_used
        or record.outcome_category != "ambiguous" or record.provider_evidence_hash is not None
        or record.private_checkout_reference is not None
    ):
        raise PreparationAuthorityFault("reconciled preparation metadata is contradictory")
    if record.state in {PreparationState.PROVIDER_SUCCEEDED, PreparationState.PROVIDER_FAILED} and (
        record.dispatch_started_at is None
        or record.outcome_category not in {"provider_success", "provider_failure"}
        or record.provider_evidence_hash is None
        or (record.state is PreparationState.PROVIDER_SUCCEEDED) != (record.outcome_category == "provider_success")
    ):
        raise PreparationAuthorityFault("provider terminal preparation evidence is contradictory")
    if record.state is PreparationState.INTEGRITY_FAULT and terminal:
        raise PreparationAuthorityFault("integrity preparation metadata is contradictory")


class PreparationMutationAuthority:
    """Serialized, no-network authority with a single exact-GET reconciliation."""

    def __init__(
        self,
        storage: PreparationStorage,
        *,
        clock: Callable[[], float],
        durable_state: DurableOrderingState | None = None,
    ) -> None:
        self._storage = storage
        self._clock = clock
        self._durable_state = durable_state
        self._lock = asyncio.Lock()
        self._records: list[PreparationAttempt] = []
        self.loaded = False
        self.integrity_fault = False

    @property
    def records(self) -> tuple[PreparationAttempt, ...]:
        return tuple(self._records)

    @property
    def unresolved(self) -> tuple[PreparationAttempt, ...]:
        return tuple(
            record for record in self._records
            if record.state in {PreparationState.DISPATCHING, PreparationState.RECONCILIATION_REQUIRED}
        )

    def _now(self) -> float:
        return _timestamp(self._clock())

    def _payload(self, records: list[PreparationAttempt]) -> dict[str, Any]:
        return {"version": PREP_AUTHORITY_VERSION, "records": [record.to_raw() for record in records]}

    async def _save(self, records: list[PreparationAttempt]) -> None:
        try:
            await self._storage.async_save(self._payload(records))
        except Exception as err:
            raise PreparationAuthorityFault("preparation authority could not be saved") from err

    async def _cancel_resistant(self, awaitable: Any) -> tuple[Any, bool]:
        """Finish a required persistence layer despite repeated cancellation."""
        task = asyncio.ensure_future(awaitable)
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        return task.result(), cancelled

    async def _latch_reconciliation(self, record: PreparationAttempt) -> None:
        if self._durable_state is None:
            return
        try:
            await self._cancel_resistant(
                self._durable_state.async_latch_preparation_reconciliation(
                    attempt_id=record.attempt_id,
                    record_revision=record.record_revision,
                    purpose=record.purpose.value,
                    expectation_hash=record.expectation_hash,
                )
            )
        except BaseException:
            # A second independent durable layer must never be skipped.  If it
            # cannot bind the block, latch permanent integrity before returning.
            try:
                await self._cancel_resistant(self._durable_state.async_latch_fault())
            except BaseException:
                self.integrity_fault = True
            raise

    async def async_load(self) -> tuple[PreparationAttempt, ...]:
        async with self._lock:
            try:
                raw = await self._storage.async_load()
                if raw is None:
                    self._records = []
                    self.loaded = True
                    return ()
                if (
                    not isinstance(raw, Mapping) or frozenset(raw) != {"version", "records"}
                    or raw["version"] != PREP_AUTHORITY_VERSION or not isinstance(raw["records"], list)
                    or len(raw["records"]) > MAX_PREP_ATTEMPTS
                ):
                    raise PreparationAuthorityFault("preparation authority schema mismatch")
                records = [PreparationAttempt.from_raw(item) for item in raw["records"]]
                if len({item.attempt_id for item in records}) != len(records):
                    raise PreparationAuthorityFault("preparation attempt identities are not unique")
                if len([item for item in records if item.state in {PreparationState.DISPATCHING, PreparationState.RECONCILIATION_REQUIRED}]) > 1:
                    raise PreparationAuthorityFault("multiple unresolved preparation attempts")
                changed = False
                recovered: PreparationAttempt | None = None
                normalized: list[PreparationAttempt] = []
                for item in records:
                    if item.state is PreparationState.DISPATCHING:
                        item = replace(
                            item, state=PreparationState.RECONCILIATION_REQUIRED,
                            updated_at=self._now(), outcome_category="ambiguous",
                            record_revision=item.record_revision + 1,
                        )
                        changed = True
                    if item.state is PreparationState.RECONCILIATION_REQUIRED:
                        recovered = item
                    normalized.append(PreparationAttempt.from_raw(item.to_raw()))
                if changed:
                    await self._save(normalized)
                self._records = normalized
                self.loaded = True
                if recovered is not None:
                    await self._latch_reconciliation(recovered)
                return self.records
            except (PreparationAuthorityFault, OrderingStateFault):
                self.integrity_fault = True
                self.loaded = False
                raise
            except Exception as err:
                self.integrity_fault = True
                self.loaded = False
                raise PreparationAuthorityFault("preparation authority could not be read") from err

    async def async_acquire(
        self, *, attempt_id: str, generation: int, purpose: PreparationPurpose,
        expectation_hash: str, expected_category: str,
    ) -> PreparationAttempt:
        """Persist PREPARED then DISPATCHING and return the sole call authority."""
        async with self._lock:
            if not self.loaded or self.integrity_fault:
                raise PreparationAuthorityFault("preparation authority is not writable")
            if not isinstance(purpose, PreparationPurpose):
                raise PreparationAuthorityFault("preparation purpose is invalid")
            if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
                raise PreparationAuthorityFault("preparation generation is invalid")
            expectation_hash = _hash(expectation_hash, "preparation expectation hash is invalid")
            expected_category = _category(expected_category)
            attempt_id = _attempt_id(attempt_id)
            if self.unresolved or any(
                item.attempt_id == attempt_id
                or (item.generation, item.purpose, item.expectation_hash) == (generation, purpose, expectation_hash)
                for item in self._records
            ):
                raise PreparationReplayDenied("preparation attempt is already authoritative")
            now = self._now()
            prepared = PreparationAttempt(
                attempt_id, generation, purpose, expectation_hash, expected_category,
                PreparationState.PREPARED, 1, now, now, None, False, None, None, None,
            )
            await self._save([*self._records, prepared])
            dispatching = replace(
                prepared, state=PreparationState.DISPATCHING, updated_at=self._now(),
                dispatch_started_at=self._now(), record_revision=2,
            )
            await self._save([*self._records, dispatching])
            self._records = [*self._records, dispatching]
            return dispatching

    async def async_record_outcome(
        self, *, attempt_id: str, expected_revision: int, outcome_category: str,
        provider_evidence_hash: str | None = None, private_checkout_reference: str | None = None,
    ) -> PreparationAttempt:
        """Record a closed provider result; ambiguity enters reconciliation, never replay."""
        async with self._lock:
            current = next((item for item in self._records if item.attempt_id == attempt_id), None)
            if current is None or current.record_revision != expected_revision or current.state is not PreparationState.DISPATCHING:
                raise PreparationReplayDenied("preparation outcome identity changed")
            if outcome_category not in {"ambiguous", "provider_success", "provider_failure"}:
                raise PreparationAuthorityFault("preparation outcome category is invalid")
            if outcome_category == "ambiguous":
                if provider_evidence_hash is not None or private_checkout_reference is not None:
                    raise PreparationAuthorityFault("ambiguous preparation evidence is invalid")
                state = PreparationState.RECONCILIATION_REQUIRED
            else:
                provider_evidence_hash = _hash(provider_evidence_hash, "provider evidence hash is required")
                if private_checkout_reference is not None and (not isinstance(private_checkout_reference, str) or not private_checkout_reference or len(private_checkout_reference) > 100):
                    raise PreparationAuthorityFault("private checkout reference is invalid")
                state = PreparationState.PROVIDER_SUCCEEDED if outcome_category == "provider_success" else PreparationState.PROVIDER_FAILED
            updated = PreparationAttempt.from_raw(replace(
                current, state=state, updated_at=self._now(), outcome_category=outcome_category,
                provider_evidence_hash=provider_evidence_hash, private_checkout_reference=private_checkout_reference,
                record_revision=current.record_revision + 1,
            ).to_raw())
            candidate = [updated if item.attempt_id == attempt_id else item for item in self._records]
            try:
                _, cancelled = await self._cancel_resistant(self._save(candidate))
            except BaseException:
                self.integrity_fault = True
                if self._durable_state is not None:
                    try:
                        await self._cancel_resistant(self._durable_state.async_latch_fault())
                    except BaseException:
                        pass
                raise
            self._records = candidate
            if state is PreparationState.RECONCILIATION_REQUIRED:
                await self._latch_reconciliation(updated)
            if cancelled:
                raise asyncio.CancelledError
            return updated

    async def async_reconcile_once(
        self, *, attempt_id: str, expected_revision: int, observed_expectation_hash: str,
        observed_category: str,
    ) -> PreparationAttempt:
        """Consume the only caller-performed GET comparison; no network occurs here."""
        async with self._lock:
            current = next((item for item in self._records if item.attempt_id == attempt_id), None)
            if (
                current is None or current.state is not PreparationState.RECONCILIATION_REQUIRED
                or current.record_revision != expected_revision or current.reconciliation_get_used
            ):
                raise PreparationReplayDenied("preparation reconciliation is unavailable")
            observed_expectation_hash = _hash(observed_expectation_hash, "observed expectation hash is invalid")
            observed_category = _category(observed_category)
            exact = (
                observed_expectation_hash == current.expectation_hash
                and observed_category == current.expected_category
            )
            updated = PreparationAttempt.from_raw(replace(
                current, state=(PreparationState.RECONCILED if exact else PreparationState.RECONCILIATION_REQUIRED),
                updated_at=self._now(), reconciliation_get_used=True,
                record_revision=current.record_revision + 1,
            ).to_raw())
            candidate = [updated if item.attempt_id == attempt_id else item for item in self._records]
            await self._cancel_resistant(self._save(candidate))
            self._records = candidate
            if exact and self._durable_state is not None:
                await self._cancel_resistant(
                    self._durable_state.async_clear_preparation_reconciliation(
                        attempt_id=updated.attempt_id,
                        purpose=updated.purpose.value,
                        expectation_hash=updated.expectation_hash,
                    )
                )
            if not exact:
                await self._latch_reconciliation(updated)
            return updated
