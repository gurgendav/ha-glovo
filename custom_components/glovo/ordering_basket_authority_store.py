"""Privacy-minimal durable evidence about observed provider basket state.

This store is evidence only.  In particular, a loaded PRESENT_OBSERVED image is
not runtime authority to mutate a basket or obtain a quote; provider evidence
must be freshly re-adopted by the composing runtime after every reload.
"""

from __future__ import annotations

import asyncio
import copy
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

BASKET_AUTHORITY_STORE_VERSION = 1
_STORAGE_KEYS = frozenset(
    {
        "version",
        "generation",
        "knowledge",
        "account_digest",
        "store_digest",
        "intent_digest",
        "snapshot_digest",
        "observed_at",
    }
)


class BasketKnowledge(StrEnum):
    """Closed durable classification of basket evidence, never authority."""

    UNKNOWN = "UNKNOWN"
    ABSENT_OBSERVED = "ABSENT_OBSERVED"
    PRESENT_OBSERVED = "PRESENT_OBSERVED"
    CONFLICT = "CONFLICT"


class BasketAuthorityFault(RuntimeError):
    """Redaction-safe basket evidence validation or persistence failure."""


class BasketAuthorityStorage(Protocol):
    """Minimal private storage boundary used by the durable evidence store."""

    async def async_load(self) -> Any: ...

    async def async_save(self, data: dict[str, Any]) -> None: ...


class MemoryBasketAuthorityStorage:
    """Copy-isolated deterministic storage for tests and local composition."""

    def __init__(self, data: Any = None) -> None:
        self.data = copy.deepcopy(data)

    async def async_load(self) -> Any:
        return copy.deepcopy(self.data)

    async def async_save(self, data: dict[str, Any]) -> None:
        self.data = copy.deepcopy(data)


class HomeAssistantBasketAuthorityStorage:
    """A separate private v1 Home Assistant Store with no legacy fallback."""

    def __init__(self, hass: Any, entry_key: str) -> None:
        from homeassistant.helpers.storage import Store

        self._store = Store(
            hass,
            BASKET_AUTHORITY_STORE_VERSION,
            f"glovo.ordering_basket_authority_v1.{entry_key}",
        )

    async def async_load(self) -> Any:
        return await self._store.async_load()

    async def async_save(self, data: dict[str, Any]) -> None:
        await self._store.async_save(data)


def _strict_generation(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BasketAuthorityFault("basket evidence generation is invalid")
    return value


def _strict_timestamp(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise BasketAuthorityFault("basket evidence timestamp is invalid")
    return float(value)


def _strict_digest(value: object, *, nullable: bool) -> str | None:
    if value is None and nullable:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise BasketAuthorityFault("basket evidence digest is invalid")
    return value


@dataclass(frozen=True, slots=True)
class BasketAuthoritySnapshot:
    """Immutable, privacy-minimal durable observation.

    Digest values are opaque domain-separated SHA-256 outputs supplied by the
    caller.  This type deliberately cannot reconstruct or expose source data.
    """

    version: int
    generation: int
    knowledge: BasketKnowledge
    account_digest: str | None
    store_digest: str | None
    intent_digest: str | None
    snapshot_digest: str | None
    observed_at: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version != BASKET_AUTHORITY_STORE_VERSION
        ):
            raise BasketAuthorityFault("basket evidence version is invalid")
        _strict_generation(self.generation)
        if not isinstance(self.knowledge, BasketKnowledge):
            raise BasketAuthorityFault("basket evidence knowledge is invalid")
        _strict_timestamp(self.observed_at)

        digests = (
            self.account_digest,
            self.store_digest,
            self.intent_digest,
            self.snapshot_digest,
        )
        for digest in digests:
            _strict_digest(digest, nullable=True)

        first_three_present = all(digest is not None for digest in digests[:3])
        if self.knowledge is BasketKnowledge.UNKNOWN:
            valid = all(digest is None for digest in digests)
        elif self.knowledge in {
            BasketKnowledge.ABSENT_OBSERVED,
            BasketKnowledge.CONFLICT,
        }:
            valid = first_three_present and self.snapshot_digest is None
        else:
            valid = first_three_present and self.snapshot_digest is not None
        if not valid:
            raise BasketAuthorityFault("basket evidence fields are contradictory")

    @classmethod
    def from_raw(cls, raw: object) -> BasketAuthoritySnapshot:
        """Parse one exact v1 JSON image, rejecting every ambiguity."""

        if not isinstance(raw, Mapping) or frozenset(raw) != _STORAGE_KEYS:
            raise BasketAuthorityFault("basket evidence schema mismatch")
        version = raw["version"]
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version != BASKET_AUTHORITY_STORE_VERSION
        ):
            raise BasketAuthorityFault("basket evidence version is invalid")
        knowledge_value = raw["knowledge"]
        if type(knowledge_value) is not str:
            raise BasketAuthorityFault("basket evidence knowledge is invalid")
        try:
            knowledge = BasketKnowledge(knowledge_value)
        except ValueError as err:
            raise BasketAuthorityFault("basket evidence knowledge is invalid") from err

        return cls(
            version=version,
            generation=_strict_generation(raw["generation"]),
            knowledge=knowledge,
            account_digest=_strict_digest(raw["account_digest"], nullable=True),
            store_digest=_strict_digest(raw["store_digest"], nullable=True),
            intent_digest=_strict_digest(raw["intent_digest"], nullable=True),
            snapshot_digest=_strict_digest(raw["snapshot_digest"], nullable=True),
            observed_at=_strict_timestamp(raw["observed_at"]),
        )

    def to_raw(self) -> dict[str, Any]:
        """Return the complete exact v1 JSON image and nothing else."""

        # Revalidate even if a caller used object-level escape hatches to tamper
        # with a frozen instance before attempting persistence.
        validated = BasketAuthoritySnapshot(
            version=self.version,
            generation=self.generation,
            knowledge=self.knowledge,
            account_digest=self.account_digest,
            store_digest=self.store_digest,
            intent_digest=self.intent_digest,
            snapshot_digest=self.snapshot_digest,
            observed_at=self.observed_at,
        )
        return {
            "version": validated.version,
            "generation": validated.generation,
            "knowledge": validated.knowledge.value,
            "account_digest": validated.account_digest,
            "store_digest": validated.store_digest,
            "intent_digest": validated.intent_digest,
            "snapshot_digest": validated.snapshot_digest,
            "observed_at": float(validated.observed_at),
        }


class DurableBasketAuthority:
    """Serialized durable basket evidence with write-before-publish semantics."""

    def __init__(
        self,
        storage: BasketAuthorityStorage,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._storage = storage
        self._clock = clock
        self._lock = asyncio.Lock()
        self._snapshot = BasketAuthoritySnapshot(
            BASKET_AUTHORITY_STORE_VERSION,
            0,
            BasketKnowledge.UNKNOWN,
            None,
            None,
            None,
            None,
            0.0,
        )
        self.loaded = False
        self.integrity_fault = False
        self.transient_write_fault = False

    @property
    def snapshot(self) -> BasketAuthoritySnapshot:
        """Return current evidence; it confers no provider call authority."""

        return self._snapshot

    def _unknown_candidate(self, generation: int) -> BasketAuthoritySnapshot:
        return BasketAuthoritySnapshot(
            BASKET_AUTHORITY_STORE_VERSION,
            generation,
            BasketKnowledge.UNKNOWN,
            None,
            None,
            None,
            None,
            _strict_timestamp(self._clock()),
        )

    async def async_load(self) -> BasketAuthoritySnapshot:
        """Load durable evidence, permanently latching malformed/read failures."""

        async with self._lock:
            if self.integrity_fault:
                raise BasketAuthorityFault(
                    "basket evidence integrity fault is permanent"
                )
            try:
                raw = await self._storage.async_load()
                candidate = (
                    self._unknown_candidate(0)
                    if raw is None
                    else BasketAuthoritySnapshot.from_raw(raw)
                )
            except asyncio.CancelledError:
                raise
            except BasketAuthorityFault as err:
                self.integrity_fault = True
                self.loaded = False
                raise BasketAuthorityFault(
                    "basket authority evidence is invalid"
                ) from err
            except Exception as err:
                self.integrity_fault = True
                self.loaded = False
                raise BasketAuthorityFault(
                    "basket authority evidence could not be read"
                ) from err

            self._snapshot = candidate
            self.loaded = True
            self.transient_write_fault = False
            return candidate

    async def _async_save_candidate(
        self, candidate: BasketAuthoritySnapshot
    ) -> BasketAuthoritySnapshot:
        if not self.loaded or self.integrity_fault:
            raise BasketAuthorityFault("basket authority evidence is not writable")
        try:
            await self._storage.async_save(candidate.to_raw())
        except asyncio.CancelledError:
            self.transient_write_fault = True
            raise
        except Exception as err:
            self.transient_write_fault = True
            raise BasketAuthorityFault(
                "basket authority evidence could not be saved"
            ) from err
        self._snapshot = candidate
        self.transient_write_fault = False
        return candidate

    async def async_record_unknown(self, *, generation: int) -> BasketAuthoritySnapshot:
        """Invalidate runtime knowledge after reload or an uncertain mutation."""

        async with self._lock:
            return await self._async_save_candidate(
                self._unknown_candidate(_strict_generation(generation))
            )

    def _observed_candidate(
        self,
        *,
        generation: int,
        knowledge: BasketKnowledge,
        account_digest: str,
        store_digest: str,
        intent_digest: str,
        snapshot_digest: str | None,
    ) -> BasketAuthoritySnapshot:
        return BasketAuthoritySnapshot(
            BASKET_AUTHORITY_STORE_VERSION,
            _strict_generation(generation),
            knowledge,
            _strict_digest(account_digest, nullable=False),
            _strict_digest(store_digest, nullable=False),
            _strict_digest(intent_digest, nullable=False),
            _strict_digest(
                snapshot_digest,
                nullable=knowledge is not BasketKnowledge.PRESENT_OBSERVED,
            ),
            _strict_timestamp(self._clock()),
        )

    async def async_record_absent(
        self,
        *,
        generation: int,
        account_digest: str,
        store_digest: str,
        intent_digest: str,
    ) -> BasketAuthoritySnapshot:
        """Persist a fresh provider observation that the exact basket is absent."""

        async with self._lock:
            candidate = self._observed_candidate(
                generation=generation,
                knowledge=BasketKnowledge.ABSENT_OBSERVED,
                account_digest=account_digest,
                store_digest=store_digest,
                intent_digest=intent_digest,
                snapshot_digest=None,
            )
            return await self._async_save_candidate(candidate)

    async def async_record_present(
        self,
        *,
        generation: int,
        account_digest: str,
        store_digest: str,
        intent_digest: str,
        snapshot_digest: str,
    ) -> BasketAuthoritySnapshot:
        """Persist hashes of one freshly observed provider basket snapshot."""

        async with self._lock:
            candidate = self._observed_candidate(
                generation=generation,
                knowledge=BasketKnowledge.PRESENT_OBSERVED,
                account_digest=account_digest,
                store_digest=store_digest,
                intent_digest=intent_digest,
                snapshot_digest=snapshot_digest,
            )
            return await self._async_save_candidate(candidate)

    async def async_record_conflict(
        self,
        *,
        generation: int,
        account_digest: str,
        store_digest: str,
        intent_digest: str,
    ) -> BasketAuthoritySnapshot:
        """Persist a closed conflict without retaining conflicting raw data."""

        async with self._lock:
            candidate = self._observed_candidate(
                generation=generation,
                knowledge=BasketKnowledge.CONFLICT,
                account_digest=account_digest,
                store_digest=store_digest,
                intent_digest=intent_digest,
                snapshot_digest=None,
            )
            return await self._async_save_candidate(candidate)
