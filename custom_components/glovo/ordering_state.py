"""Durable v2 generation, resolvable manual latch, and permanent integrity latch."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol

STATE_VERSION = 2
LEGACY_STATE_VERSION = 1


class OrderingStateFault(RuntimeError):
    """Durable ordering authority cannot be trusted or persisted."""


class OrderingStateStorage(Protocol):
    async def async_load(self) -> Any: ...

    async def async_save(self, data: dict[str, Any]) -> None: ...


_MANUAL_RESOLUTIONS = frozenset(
    {"found_succeeded", "found_failed_or_cancelled"}
)


@dataclass(frozen=True, slots=True)
class ManualCheckBinding:
    """Exact durable identity of the one clearable reconciliation attempt."""

    attempt_id: str
    record_revision: int
    generation: int
    resolution: str | None = None

    @classmethod
    def from_raw(cls, raw: object) -> ManualCheckBinding:
        if (
            not isinstance(raw, Mapping)
            or frozenset(raw)
            != {"attempt_id", "record_revision", "generation", "resolution"}
        ):
            raise OrderingStateFault("manual check binding schema mismatch")
        attempt_id = raw["attempt_id"]
        revision = raw["record_revision"]
        generation = raw["generation"]
        resolution = raw["resolution"]
        if (
            not isinstance(attempt_id, str)
            or not attempt_id.startswith("attempt-")
            or len(attempt_id) > 100
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
            or (resolution is not None and resolution not in _MANUAL_RESOLUTIONS)
        ):
            raise OrderingStateFault("manual check binding is invalid")
        return cls(attempt_id, revision, generation, resolution)

    def to_raw(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "record_revision": self.record_revision,
            "generation": self.generation,
            "resolution": self.resolution,
        }


class MemoryOrderingStateStorage:
    """Separate v2/v1 deterministic storage for tests."""

    def __init__(self, data: Any = None, *, legacy_data: Any = None) -> None:
        if isinstance(data, Mapping) and data.get("version") == LEGACY_STATE_VERSION:
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


class HomeAssistantOrderingStateStorage:
    """Use a new v2 key while retaining the old v1 store untouched."""

    def __init__(self, hass: Any, entry_key: str) -> None:
        from homeassistant.helpers.storage import Store

        self._store = Store(hass, STATE_VERSION, f"glovo.ordering_safety_v2.{entry_key}")
        self._legacy_store = Store(
            hass, LEGACY_STATE_VERSION, f"glovo.ordering_safety.{entry_key}"
        )

    async def async_load(self) -> Any:
        return await self._store.async_load()

    async def async_load_legacy(self) -> Any:
        return await self._legacy_store.async_load()

    async def async_save(self, data: dict[str, Any]) -> None:
        await self._store.async_save(data)


@dataclass(frozen=True, slots=True)
class OrderingStateSnapshot:
    generation: int
    manual_check_required: bool
    integrity_fault: bool
    manual_binding: ManualCheckBinding | None = None

    @property
    def safety_fault(self) -> bool:
        """Compatibility aggregate for callers predating v2."""
        return self.manual_check_required or self.integrity_fault

    @classmethod
    def from_raw(cls, raw: object) -> OrderingStateSnapshot:
        if raw is None:
            return cls(generation=1, manual_check_required=False, integrity_fault=False)
        base_keys = {"version", "generation", "manual_check_required", "integrity_fault"}
        if not isinstance(raw, Mapping) or frozenset(raw) not in {
            frozenset(base_keys),
            frozenset({*base_keys, "manual_binding"}),
        } or raw["version"] != STATE_VERSION:
            raise OrderingStateFault("ordering safety state schema mismatch")
        generation = raw["generation"]
        manual = raw["manual_check_required"]
        integrity = raw["integrity_fault"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise OrderingStateFault("ordering generation is invalid")
        if not isinstance(manual, bool) or not isinstance(integrity, bool):
            raise OrderingStateFault("ordering safety latches are invalid")
        binding = (
            ManualCheckBinding.from_raw(raw["manual_binding"])
            if "manual_binding" in raw and raw["manual_binding"] is not None
            else None
        )
        if manual != (binding is not None):
            raise OrderingStateFault("manual check latch is not uniquely bound")
        if binding is not None and binding.generation > generation:
            raise OrderingStateFault("manual check binding generation is invalid")
        return cls(generation, manual, integrity, binding)

    @classmethod
    def from_legacy(cls, raw: object) -> OrderingStateSnapshot:
        if (
            not isinstance(raw, Mapping)
            or frozenset(raw) != {"version", "generation", "safety_fault"}
            or raw["version"] != LEGACY_STATE_VERSION
        ):
            raise OrderingStateFault("legacy ordering safety state schema mismatch")
        generation = raw["generation"]
        safety_fault = raw["safety_fault"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise OrderingStateFault("legacy ordering generation is invalid")
        if not isinstance(safety_fault, bool):
            raise OrderingStateFault("legacy ordering safety latch is invalid")
        # V1 uncertainty was not safely distinguishable from corruption. Preserve it
        # as permanent integrity fault rather than making it UI-clearable.
        return cls(generation, False, safety_fault, None)

    def to_raw(self) -> dict[str, Any]:
        raw = {
            "version": STATE_VERSION,
            "generation": self.generation,
            "manual_check_required": self.manual_check_required,
            "integrity_fault": self.integrity_fault,
        }
        if self.manual_binding is not None:
            raw["manual_binding"] = self.manual_binding.to_raw()
        return raw


class DurableOrderingState:
    """Serialized durable authority; memory advances only after a successful save."""

    def __init__(self, storage: OrderingStateStorage) -> None:
        self._storage = storage
        self._lock = asyncio.Lock()
        self._snapshot = OrderingStateSnapshot(1, False, False, None)
        self.loaded = False
        self.storage_fault = False
        self.transient_write_fault = False
        self.load_source = "unloaded"

    @property
    def generation(self) -> int:
        return self._snapshot.generation

    @property
    def manual_check_required(self) -> bool:
        return self._snapshot.manual_check_required

    @property
    def integrity_fault(self) -> bool:
        return self._snapshot.integrity_fault or self.storage_fault

    @property
    def manual_binding(self) -> ManualCheckBinding | None:
        return self._snapshot.manual_binding

    @property
    def safety_fault(self) -> bool:
        return self.manual_check_required or self.integrity_fault or self.transient_write_fault

    async def _async_raw_with_source(self) -> tuple[Any, str]:
        raw = await self._storage.async_load()
        source = "v2"
        if raw is None:
            legacy_loader = getattr(self._storage, "async_load_legacy", None)
            raw = await legacy_loader() if legacy_loader is not None else None
            source = "v1" if raw is not None else "new"
        elif isinstance(raw, Mapping) and raw.get("version") == LEGACY_STATE_VERSION:
            source = "v1"
        return raw, source

    async def async_load(self) -> OrderingStateSnapshot:
        async with self._lock:
            try:
                raw, source = await self._async_raw_with_source()
                if source == "v1":
                    candidate = OrderingStateSnapshot.from_legacy(raw)
                else:
                    candidate = OrderingStateSnapshot.from_raw(raw)
                if source in {"v1", "new"}:
                    await self._storage.async_save(candidate.to_raw())
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
            self.transient_write_fault = False
            self.load_source = source
            return candidate

    async def _async_read_v2(self) -> OrderingStateSnapshot:
        raw = await self._storage.async_load()
        if raw is None:
            raise OrderingStateFault("ordering v2 safety state disappeared")
        return OrderingStateSnapshot.from_raw(raw)

    async def _async_save_candidate(
        self, candidate: OrderingStateSnapshot, *, permanent_on_failure: bool = True
    ) -> None:
        if not self.loaded or self.storage_fault:
            raise OrderingStateFault("ordering safety state is not writable")
        try:
            await self._storage.async_save(candidate.to_raw())
        except Exception as err:
            if permanent_on_failure:
                self.storage_fault = True
            else:
                self.transient_write_fault = True
            raise OrderingStateFault("ordering safety state could not be saved") from err
        self._snapshot = candidate
        self.transient_write_fault = False

    async def async_bump(
        self,
        *,
        latch_fault: bool = False,
        manual_binding: ManualCheckBinding | None = None,
    ) -> int:
        """Persist one monotonic bump while retaining every existing latch."""
        async with self._lock:
            try:
                durable = await self._async_read_v2()
            except OrderingStateFault:
                self.storage_fault = True
                raise
            except Exception as err:
                self.storage_fault = True
                raise OrderingStateFault("ordering safety state could not be re-read") from err
            base_generation = max(self._snapshot.generation, durable.generation)
            if (
                self._snapshot.manual_binding is not None
                and durable.manual_binding is not None
                and self._snapshot.manual_binding != durable.manual_binding
            ):
                self.storage_fault = True
                raise OrderingStateFault("manual check binding changed unexpectedly")
            binding = manual_binding or durable.manual_binding or self._snapshot.manual_binding
            next_generation = base_generation + 1
            if binding is not None:
                binding = replace(binding, generation=next_generation)
            candidate = OrderingStateSnapshot(
                generation=next_generation,
                manual_check_required=binding is not None,
                integrity_fault=(
                    self._snapshot.integrity_fault or durable.integrity_fault or latch_fault
                ),
                manual_binding=binding,
            )
            await self._async_save_candidate(candidate)
            return candidate.generation

    async def async_latch_fault(self) -> int:
        return await self.async_bump(latch_fault=True)

    async def async_latch_manual_check(
        self, *, attempt_id: str, record_revision: int
    ) -> int:
        binding = ManualCheckBinding.from_raw(
            {
                "attempt_id": attempt_id,
                "record_revision": record_revision,
                "generation": self.generation,
                "resolution": None,
            }
        )
        return await self.async_bump(manual_binding=binding)

    async def async_update_manual_binding(
        self,
        *,
        attempt_id: str,
        record_revision: int,
        resolution: str | None,
    ) -> None:
        """Persist an exact journal revision before any latch clear can proceed."""
        async with self._lock:
            durable = await self._async_read_v2()
            if (
                not durable.manual_check_required
                or durable.manual_binding is None
                or durable.manual_binding.attempt_id != attempt_id
            ):
                self.storage_fault = True
                raise OrderingStateFault("manual check binding changed unexpectedly")
            binding = ManualCheckBinding.from_raw(
                {
                    "attempt_id": attempt_id,
                    "record_revision": record_revision,
                    "generation": durable.generation,
                    "resolution": resolution,
                }
            )
            candidate = OrderingStateSnapshot(
                durable.generation,
                True,
                durable.integrity_fault,
                binding,
            )
            await self._async_save_candidate(candidate)

    async def async_clear_manual_check(
        self,
        *,
        attempt_id: str,
        record_revision: int,
        resolution: str,
        generation: int,
    ) -> None:
        """Clear only the resolvable latch; integrity can never be cleared here."""
        async with self._lock:
            try:
                durable = await self._async_read_v2()
            except Exception as err:
                self.storage_fault = True
                raise OrderingStateFault("ordering safety state could not be re-read") from err
            if durable.integrity_fault or self._snapshot.integrity_fault:
                raise OrderingStateFault("integrity fault cannot be cleared")
            expected = ManualCheckBinding.from_raw(
                {
                    "attempt_id": attempt_id,
                    "record_revision": record_revision,
                    "generation": generation,
                    "resolution": resolution,
                }
            )
            if (
                durable.manual_binding != expected
                or self._snapshot.manual_binding != expected
                or durable.generation != generation
            ):
                raise OrderingStateFault("manual check clear binding mismatch")
            candidate = OrderingStateSnapshot(
                max(self._snapshot.generation, durable.generation), False, False, None
            )
            # A transient failure leaves the durable manual latch set and permits a
            # same-resolution recovery call to finish the clear later.
            await self._async_save_candidate(candidate, permanent_on_failure=False)

    async def async_assert_current(self, expected_generation: int) -> None:
        """Re-read durable authority at the final execution boundary."""
        async with self._lock:
            try:
                durable = await self._async_read_v2()
            except OrderingStateFault:
                self.storage_fault = True
                raise
            except Exception as err:
                self.storage_fault = True
                raise OrderingStateFault("ordering safety state could not be re-read") from err
            if (
                durable.generation != expected_generation
                or durable.generation != self._snapshot.generation
                or durable.manual_check_required
                or durable.integrity_fault
            ):
                raise OrderingStateFault("durable ordering authority changed")
