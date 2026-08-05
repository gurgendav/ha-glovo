"""Durable monotonic generation and fail-closed ordering safety latch."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

STATE_VERSION = 1


class OrderingStateFault(RuntimeError):
    """Durable ordering authority cannot be trusted or persisted."""


class OrderingStateStorage(Protocol):
    async def async_load(self) -> Any: ...

    async def async_save(self, data: dict[str, Any]) -> None: ...


class MemoryOrderingStateStorage:
    """Deterministic storage for tests."""

    def __init__(self, data: Any = None) -> None:
        self.data = copy.deepcopy(data)

    async def async_load(self) -> Any:
        return copy.deepcopy(self.data)

    async def async_save(self, data: dict[str, Any]) -> None:
        self.data = copy.deepcopy(data)


class HomeAssistantOrderingStateStorage:
    """Adapter around Home Assistant's atomic JSON Store."""

    def __init__(self, hass: Any, entry_key: str) -> None:
        from homeassistant.helpers.storage import Store

        self._store = Store(hass, STATE_VERSION, f"glovo.ordering_safety.{entry_key}")

    async def async_load(self) -> Any:
        return await self._store.async_load()

    async def async_save(self, data: dict[str, Any]) -> None:
        await self._store.async_save(data)


@dataclass(frozen=True, slots=True)
class OrderingStateSnapshot:
    generation: int
    safety_fault: bool

    @classmethod
    def from_raw(cls, raw: object) -> OrderingStateSnapshot:
        if raw is None:
            return cls(generation=1, safety_fault=False)
        if (
            not isinstance(raw, Mapping)
            or frozenset(raw) != {"version", "generation", "safety_fault"}
            or raw["version"] != STATE_VERSION
        ):
            raise OrderingStateFault("ordering safety state schema mismatch")
        generation = raw["generation"]
        safety_fault = raw["safety_fault"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise OrderingStateFault("ordering generation is invalid")
        if not isinstance(safety_fault, bool):
            raise OrderingStateFault("ordering safety latch is invalid")
        return cls(generation=generation, safety_fault=safety_fault)

    def to_raw(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "generation": self.generation,
            "safety_fault": self.safety_fault,
        }


class DurableOrderingState:
    """Serialized durable authority; memory advances only after a successful save."""

    def __init__(self, storage: OrderingStateStorage) -> None:
        self._storage = storage
        self._lock = asyncio.Lock()
        self._snapshot = OrderingStateSnapshot(1, False)
        self.loaded = False
        self.storage_fault = False

    @property
    def generation(self) -> int:
        return self._snapshot.generation

    @property
    def safety_fault(self) -> bool:
        return self._snapshot.safety_fault or self.storage_fault

    async def async_load(self) -> OrderingStateSnapshot:
        async with self._lock:
            try:
                candidate = OrderingStateSnapshot.from_raw(await self._storage.async_load())
            except OrderingStateFault:
                self.loaded = False
                self.storage_fault = True
                raise
            except Exception as err:
                self.loaded = False
                self.storage_fault = True
                raise OrderingStateFault("ordering safety state could not be read") from err
            self._snapshot = candidate
            self.loaded = True
            self.storage_fault = False
            return candidate

    async def _async_save_candidate(self, candidate: OrderingStateSnapshot) -> None:
        if not self.loaded or self.storage_fault:
            raise OrderingStateFault("ordering safety state is not writable")
        try:
            await self._storage.async_save(candidate.to_raw())
        except Exception as err:
            self.storage_fault = True
            raise OrderingStateFault("ordering safety state could not be saved") from err
        self._snapshot = candidate

    async def async_bump(self, *, latch_fault: bool = False) -> int:
        """Atomically persist one monotonic bump before publishing it in memory."""
        async with self._lock:
            try:
                durable = OrderingStateSnapshot.from_raw(await self._storage.async_load())
            except OrderingStateFault:
                self.storage_fault = True
                raise
            except Exception as err:
                self.storage_fault = True
                raise OrderingStateFault("ordering safety state could not be re-read") from err
            base_generation = max(self._snapshot.generation, durable.generation)
            candidate = OrderingStateSnapshot(
                generation=base_generation + 1,
                safety_fault=(
                    self._snapshot.safety_fault or durable.safety_fault or latch_fault
                ),
            )
            await self._async_save_candidate(candidate)
            return candidate.generation

    async def async_latch_fault(self) -> int:
        return await self.async_bump(latch_fault=True)

    async def async_assert_current(self, expected_generation: int) -> None:
        """Re-read durable authority at the final execution boundary."""
        async with self._lock:
            try:
                durable = OrderingStateSnapshot.from_raw(await self._storage.async_load())
            except OrderingStateFault:
                self.storage_fault = True
                raise
            except Exception as err:
                self.storage_fault = True
                raise OrderingStateFault("ordering safety state could not be re-read") from err
            if (
                durable.generation != expected_generation
                or durable.generation != self._snapshot.generation
                or durable.safety_fault
            ):
                raise OrderingStateFault("durable ordering authority changed")
