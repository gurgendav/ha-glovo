"""Durable, privacy-minimal journal for mock checkout attempts."""

from __future__ import annotations

import asyncio
import copy
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol

JOURNAL_VERSION = 1
MAX_JOURNAL_RECORDS = 200


class JournalState(StrEnum):
    """Allowed attempt states; safe code never performs a remote dispatch."""

    DRAFT = "DRAFT"
    QUOTED = "QUOTED"
    RESERVED = "RESERVED"
    DISPATCHING = "DISPATCHING"
    VERIFYING = "VERIFYING"
    CONFIRMED_SUCCEEDED = "CONFIRMED_SUCCEEDED"
    CONFIRMED_FAILED = "CONFIRMED_FAILED"
    UNCERTAIN = "UNCERTAIN"
    SECURITY_FAULT = "SECURITY_FAULT"


class JournalFault(RuntimeError):
    """Base class for journal conditions that require fail-closed behavior."""


class JournalCorrupt(JournalFault):
    """Raised when durable journal data is structurally untrustworthy."""


class JournalStorageFault(JournalFault):
    """Raised when durable journal storage cannot be read or atomically saved."""


class JournalStorage(Protocol):
    async def async_load(self) -> Any: ...

    async def async_save(self, data: dict[str, Any]) -> None: ...


class MemoryJournalStorage:
    """Deterministic storage used by tests; no disk or network side effects."""

    def __init__(self, data: Any = None) -> None:
        self.data = copy.deepcopy(data)

    async def async_load(self) -> Any:
        return copy.deepcopy(self.data)

    async def async_save(self, data: dict[str, Any]) -> None:
        self.data = copy.deepcopy(data)


class HomeAssistantJournalStorage:
    """Small adapter around ``homeassistant.helpers.storage.Store``."""

    def __init__(self, hass: Any, entry_key: str) -> None:
        from homeassistant.helpers.storage import Store

        self._store = Store(hass, JOURNAL_VERSION, f"glovo.ordering_journal.{entry_key}")

    async def async_load(self) -> Any:
        return await self._store.async_load()

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


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """Privacy-minimal durable attempt metadata (never basket or challenge data)."""

    attempt_id: str
    state: JournalState
    created_at: float
    updated_at: float
    generation: int
    quote_fingerprint: str
    outcome: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "generation": self.generation,
            "quote_fingerprint": self.quote_fingerprint,
            "outcome": self.outcome,
        }

    @classmethod
    def from_dict(cls, value: object) -> AttemptRecord:
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
            raise JournalCorrupt("journal record schema mismatch")
        attempt_id = value["attempt_id"]
        fingerprint = value["quote_fingerprint"]
        generation = value["generation"]
        outcome = value["outcome"]
        if (
            not isinstance(attempt_id, str)
            or not attempt_id.startswith("attempt-")
            or len(attempt_id) > 100
        ):
            raise JournalCorrupt("journal attempt key is invalid")
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise JournalCorrupt("journal fingerprint is invalid")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise JournalCorrupt("journal generation is invalid")
        if outcome is not None and (not isinstance(outcome, str) or len(outcome) > 50):
            raise JournalCorrupt("journal outcome is invalid")
        try:
            state = JournalState(value["state"])
        except (TypeError, ValueError) as err:
            raise JournalCorrupt("journal state is invalid") from err
        return cls(
            attempt_id=attempt_id,
            state=state,
            created_at=_finite_timestamp(value["created_at"], "journal timestamp is invalid"),
            updated_at=_finite_timestamp(value["updated_at"], "journal timestamp is invalid"),
            generation=generation,
            quote_fingerprint=fingerprint,
            outcome=outcome,
        )


_ALLOWED_TRANSITIONS = {
    JournalState.DRAFT: frozenset({JournalState.QUOTED, JournalState.SECURITY_FAULT}),
    JournalState.QUOTED: frozenset(
        {JournalState.RESERVED, JournalState.DISPATCHING, JournalState.SECURITY_FAULT}
    ),
    JournalState.RESERVED: frozenset(
        {JournalState.DISPATCHING, JournalState.UNCERTAIN, JournalState.SECURITY_FAULT}
    ),
    JournalState.DISPATCHING: frozenset(
        {
            JournalState.VERIFYING,
            JournalState.CONFIRMED_SUCCEEDED,
            JournalState.CONFIRMED_FAILED,
            JournalState.UNCERTAIN,
            JournalState.SECURITY_FAULT,
        }
    ),
    JournalState.VERIFYING: frozenset(
        {
            JournalState.CONFIRMED_SUCCEEDED,
            JournalState.CONFIRMED_FAILED,
            JournalState.UNCERTAIN,
            JournalState.SECURITY_FAULT,
        }
    ),
}
_TERMINAL_STATES = frozenset(
    {
        JournalState.CONFIRMED_SUCCEEDED,
        JournalState.CONFIRMED_FAILED,
        JournalState.UNCERTAIN,
        JournalState.SECURITY_FAULT,
    }
)


class AttemptJournal:
    """Serialized durable journal that never publishes an unpersisted mutation."""

    def __init__(self, storage: JournalStorage, *, clock: Callable[[], float]) -> None:
        self._storage = storage
        self._clock = clock
        self._lock = asyncio.Lock()
        self._records: list[AttemptRecord] = []
        self.corrupt = False
        self.loaded = False

    @property
    def records(self) -> tuple[AttemptRecord, ...]:
        return tuple(self._records)

    @property
    def unresolved_safety_fault(self) -> bool:
        return any(
            item.state in {JournalState.UNCERTAIN, JournalState.SECURITY_FAULT}
            for item in self._records
        )

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
            except Exception as err:
                self.corrupt = True
                self.loaded = False
                self._records = []
                raise JournalStorageFault("journal storage could not be read") from err
            try:
                if raw is None:
                    self._records = []
                    self.loaded = True
                    return ()
                if (
                    not isinstance(raw, Mapping)
                    or frozenset(raw) != {"version", "records"}
                    or raw["version"] != JOURNAL_VERSION
                    or not isinstance(raw["records"], list)
                    or len(raw["records"]) > MAX_JOURNAL_RECORDS
                ):
                    raise JournalCorrupt("journal container schema mismatch")
                records = [AttemptRecord.from_dict(item) for item in raw["records"]]
                if len({item.attempt_id for item in records}) != len(records):
                    raise JournalCorrupt("journal attempt keys are not unique")
                recovered = False
                normalized: list[AttemptRecord] = []
                for record in records:
                    if record.state in (JournalState.DISPATCHING, JournalState.VERIFYING):
                        record = replace(
                            record,
                            state=JournalState.UNCERTAIN,
                            updated_at=self._now(),
                            outcome="restart_recovery",
                        )
                        recovered = True
                    normalized.append(record)
                self.loaded = True
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
        protected = [item for item in records if item.state is JournalState.UNCERTAIN]
        if len(protected) >= MAX_JOURNAL_RECORDS:
            raise JournalCorrupt("journal is full of unresolved attempts")
        removable = [item for item in records if item.state is not JournalState.UNCERTAIN]
        return protected + removable[-(MAX_JOURNAL_RECORDS - len(protected)) :]

    async def async_create(
        self, attempt_id: str, generation: int, quote_fingerprint: str
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
                    "quote_fingerprint": quote_fingerprint,
                    "outcome": None,
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
            updated = AttemptRecord.from_dict(
                replace(current, state=state, updated_at=self._now(), outcome=outcome).to_dict()
            )
            candidate = list(self._records)
            candidate[index] = updated
            await self._async_save_records_unlocked(candidate)
            self._records = candidate
            return updated
