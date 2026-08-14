"""Strict privacy and durability tests for basket authority evidence storage."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]
MODULE_PATH = (
    ROOT / "custom_components" / "glovo" / "ordering_basket_authority_store.py"
)
DIGESTS = {
    "account": "a" * 64,
    "store": "b" * 64,
    "intent": "c" * 64,
    "snapshot": "d" * 64,
}


@pytest.fixture()
def authority_store() -> Iterator[ModuleType]:
    name = "ordering_basket_authority_store_under_test"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(name, None)


def raw_for(knowledge: str = "PRESENT_OBSERVED") -> dict[str, Any]:
    raw: dict[str, Any] = {
        "version": 1,
        "generation": 7,
        "knowledge": knowledge,
        "account_digest": DIGESTS["account"],
        "store_digest": DIGESTS["store"],
        "intent_digest": DIGESTS["intent"],
        "snapshot_digest": DIGESTS["snapshot"],
        "observed_at": 1_723_456_789.25,
    }
    if knowledge == "UNKNOWN":
        for key in (
            "account_digest",
            "store_digest",
            "intent_digest",
            "snapshot_digest",
        ):
            raw[key] = None
    elif knowledge in {"ABSENT_OBSERVED", "CONFLICT"}:
        raw["snapshot_digest"] = None
    return raw


@pytest.mark.parametrize(
    "knowledge",
    ["UNKNOWN", "ABSENT_OBSERVED", "PRESENT_OBSERVED", "CONFLICT"],
)
def test_every_closed_state_strictly_round_trips(
    authority_store: ModuleType, knowledge: str
) -> None:
    raw = raw_for(knowledge)
    snapshot = authority_store.BasketAuthoritySnapshot.from_raw(raw)

    assert snapshot.version == authority_store.BASKET_AUTHORITY_STORE_VERSION == 1
    assert snapshot.knowledge is authority_store.BasketKnowledge(knowledge)
    assert snapshot.to_raw() == raw
    with pytest.raises((AttributeError, TypeError)):
        snapshot.generation = 8


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: [],
        lambda raw: {key: value for key, value in raw.items() if key != "observed_at"},
        lambda raw: {**raw, "provider_payload": {}},
        lambda raw: {**raw, "version": 2},
        lambda raw: {**raw, "version": "1"},
        lambda raw: {**raw, "generation": -1},
        lambda raw: {**raw, "generation": 1.0},
        lambda raw: {**raw, "knowledge": "PRESENT"},
        lambda raw: {**raw, "knowledge": None},
        lambda raw: {**raw, "account_digest": "A" * 64},
        lambda raw: {**raw, "store_digest": "b" * 63},
        lambda raw: {**raw, "intent_digest": "g" * 64},
        lambda raw: {**raw, "snapshot_digest": 12},
        lambda raw: {**raw, "observed_at": "1723456789"},
        lambda raw: {**raw, "snapshot_digest": None},
        lambda raw: {**raw_for("UNKNOWN"), "account_digest": "a" * 64},
        lambda raw: {**raw_for("ABSENT_OBSERVED"), "intent_digest": None},
        lambda raw: {
            **raw_for("ABSENT_OBSERVED"),
            "snapshot_digest": "d" * 64,
        },
        lambda raw: {**raw_for("CONFLICT"), "store_digest": None},
        lambda raw: {**raw_for("CONFLICT"), "snapshot_digest": "d" * 64},
    ],
)
def test_malformed_valid_json_matrix_fails_closed(
    authority_store: ModuleType, mutate: Any
) -> None:
    malformed = mutate(raw_for())
    # The test matrix itself remains JSON-compatible; failures are schema failures.
    json.dumps(malformed, allow_nan=False)

    with pytest.raises(authority_store.BasketAuthorityFault):
        authority_store.BasketAuthoritySnapshot.from_raw(malformed)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("version", True),
        ("generation", True),
        ("observed_at", True),
        ("observed_at", math.nan),
        ("observed_at", math.inf),
        ("observed_at", -math.inf),
        ("observed_at", -0.01),
    ],
)
def test_bool_nonfinite_and_negative_time_fail_closed(
    authority_store: ModuleType, field: str, bad_value: Any
) -> None:
    raw = {**raw_for(), field: bad_value}
    with pytest.raises(authority_store.BasketAuthorityFault):
        authority_store.BasketAuthoritySnapshot.from_raw(raw)


@pytest.mark.parametrize(
    ("knowledge", "expected_digests"),
    [
        ("UNKNOWN", (None, None, None, None)),
        (
            "ABSENT_OBSERVED",
            (DIGESTS["account"], DIGESTS["store"], DIGESTS["intent"], None),
        ),
        (
            "PRESENT_OBSERVED",
            (
                DIGESTS["account"],
                DIGESTS["store"],
                DIGESTS["intent"],
                DIGESTS["snapshot"],
            ),
        ),
        (
            "CONFLICT",
            (DIGESTS["account"], DIGESTS["store"], DIGESTS["intent"], None),
        ),
    ],
)
def test_record_methods_save_before_publishing_each_state(
    authority_store: ModuleType,
    knowledge: str,
    expected_digests: tuple[str | None, ...],
) -> None:
    async def scenario() -> None:
        storage = authority_store.MemoryBasketAuthorityStorage()
        durable = authority_store.DurableBasketAuthority(
            storage, clock=lambda: 1_800_000_000.5
        )
        await durable.async_load()
        kwargs = {
            "generation": 9,
            "account_digest": DIGESTS["account"],
            "store_digest": DIGESTS["store"],
            "intent_digest": DIGESTS["intent"],
        }
        if knowledge == "UNKNOWN":
            snapshot = await durable.async_record_unknown(generation=9)
        elif knowledge == "ABSENT_OBSERVED":
            snapshot = await durable.async_record_absent(**kwargs)
        elif knowledge == "PRESENT_OBSERVED":
            snapshot = await durable.async_record_present(
                **kwargs, snapshot_digest=DIGESTS["snapshot"]
            )
        else:
            snapshot = await durable.async_record_conflict(**kwargs)

        assert snapshot is durable.snapshot
        assert snapshot.knowledge.value == knowledge
        assert snapshot.observed_at == 1_800_000_000.5
        assert (
            snapshot.account_digest,
            snapshot.store_digest,
            snapshot.intent_digest,
            snapshot.snapshot_digest,
        ) == expected_digests
        assert storage.data == snapshot.to_raw()
        assert durable.transient_write_fault is False

    asyncio.run(scenario())


def test_first_load_is_unknown_without_creating_authority(
    authority_store: ModuleType,
) -> None:
    async def scenario() -> None:
        storage = authority_store.MemoryBasketAuthorityStorage()
        durable = authority_store.DurableBasketAuthority(
            storage, clock=lambda: 1_700_000_000.0
        )

        snapshot = await durable.async_load()

        assert snapshot == durable.snapshot
        assert snapshot.to_raw() == raw_for("UNKNOWN") | {
            "generation": 0,
            "observed_at": 1_700_000_000.0,
        }
        assert storage.data is None
        assert durable.loaded is True
        assert durable.integrity_fault is False

    asyncio.run(scenario())


def test_second_runtime_loads_evidence_but_has_no_mutation_authority_api(
    authority_store: ModuleType,
) -> None:
    async def scenario() -> None:
        storage = authority_store.MemoryBasketAuthorityStorage(raw_for())
        first = authority_store.DurableBasketAuthority(storage)
        second = authority_store.DurableBasketAuthority(storage)

        assert (await first.async_load()).knowledge.value == "PRESENT_OBSERVED"
        loaded = await second.async_load()

        assert loaded.knowledge.value == "PRESENT_OBSERVED"
        assert loaded == second.snapshot
        assert not hasattr(loaded, "authorizes_mutation")
        assert not hasattr(second, "async_authorize")
        assert not hasattr(second, "async_quote")

    asyncio.run(scenario())


def test_malformed_existing_state_latches_permanent_integrity_fault_redacted(
    authority_store: ModuleType,
) -> None:
    async def scenario() -> None:
        secret = "customer-991-basket-live-token"
        storage = authority_store.MemoryBasketAuthorityStorage(
            {**raw_for(), "provider_payload": secret}
        )
        durable = authority_store.DurableBasketAuthority(storage)

        with pytest.raises(authority_store.BasketAuthorityFault) as raised:
            await durable.async_load()
        assert secret not in str(raised.value)
        assert durable.integrity_fault is True
        assert durable.loaded is False

        storage.data = raw_for("UNKNOWN")
        with pytest.raises(authority_store.BasketAuthorityFault):
            await durable.async_load()
        assert durable.integrity_fault is True
        assert durable.loaded is False

    asyncio.run(scenario())


def test_load_io_failure_latches_permanent_integrity_fault_and_is_redacted(
    authority_store: ModuleType,
) -> None:
    async def scenario() -> None:
        secret = "raw-provider-error-body-with-token"

        class FailingLoad:
            async def async_load(self) -> Any:
                raise OSError(secret)

            async def async_save(self, data: dict[str, Any]) -> None:
                raise AssertionError(data)

        durable = authority_store.DurableBasketAuthority(FailingLoad())
        with pytest.raises(authority_store.BasketAuthorityFault) as raised:
            await durable.async_load()

        assert secret not in str(raised.value)
        assert raised.value.__cause__ is not None
        assert durable.integrity_fault is True
        assert durable.loaded is False

    asyncio.run(scenario())


def test_save_failure_keeps_memory_behind_storage_and_sets_transient_fault(
    authority_store: ModuleType,
) -> None:
    async def scenario() -> None:
        secret = "provider-error-body"

        class FailingSave(authority_store.MemoryBasketAuthorityStorage):
            fail = False

            async def async_save(self, data: dict[str, Any]) -> None:
                if self.fail:
                    raise OSError(secret)
                await super().async_save(data)

        storage = FailingSave(raw_for("ABSENT_OBSERVED"))
        durable = authority_store.DurableBasketAuthority(storage)
        original = await durable.async_load()
        storage.fail = True

        with pytest.raises(authority_store.BasketAuthorityFault) as raised:
            await durable.async_record_present(
                generation=8,
                account_digest=DIGESTS["account"],
                store_digest=DIGESTS["store"],
                intent_digest=DIGESTS["intent"],
                snapshot_digest=DIGESTS["snapshot"],
            )

        assert secret not in str(raised.value)
        assert durable.snapshot is original
        assert storage.data == original.to_raw()
        assert durable.transient_write_fault is True
        assert durable.integrity_fault is False
        assert durable.loaded is True

    asyncio.run(scenario())


def test_cancelled_save_keeps_memory_unchanged_and_sets_transient_fault(
    authority_store: ModuleType,
) -> None:
    async def scenario() -> None:
        class BlockingSave(authority_store.MemoryBasketAuthorityStorage):
            def __init__(self, data: Any) -> None:
                super().__init__(data)
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def async_save(self, data: dict[str, Any]) -> None:
                self.started.set()
                await self.release.wait()
                await super().async_save(data)

        storage = BlockingSave(raw_for("ABSENT_OBSERVED"))
        durable = authority_store.DurableBasketAuthority(storage)
        original = await durable.async_load()
        task = asyncio.create_task(
            durable.async_record_unknown(generation=original.generation)
        )
        await storage.started.wait()

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert durable.snapshot is original
        assert storage.data == original.to_raw()
        assert durable.transient_write_fault is True
        assert durable.integrity_fault is False

    asyncio.run(scenario())


def test_concurrent_transitions_serialize_in_save_order_without_sleep(
    authority_store: ModuleType,
) -> None:
    async def scenario() -> None:
        class OrderedBarrierStorage(authority_store.MemoryBasketAuthorityStorage):
            def __init__(self) -> None:
                super().__init__()
                self.first_started = asyncio.Event()
                self.release_first = asyncio.Event()
                self.saved_knowledge: list[str] = []

            async def async_save(self, data: dict[str, Any]) -> None:
                if not self.saved_knowledge:
                    self.first_started.set()
                    await self.release_first.wait()
                self.saved_knowledge.append(data["knowledge"])
                await super().async_save(data)

        storage = OrderedBarrierStorage()
        durable = authority_store.DurableBasketAuthority(storage)
        await durable.async_load()
        first = asyncio.create_task(
            durable.async_record_absent(
                generation=1,
                account_digest=DIGESTS["account"],
                store_digest=DIGESTS["store"],
                intent_digest=DIGESTS["intent"],
            )
        )
        await storage.first_started.wait()
        second_entered = asyncio.Event()

        async def record_second() -> Any:
            second_entered.set()
            return await durable.async_record_conflict(
                generation=2,
                account_digest=DIGESTS["account"],
                store_digest=DIGESTS["store"],
                intent_digest=DIGESTS["intent"],
            )

        second = asyncio.create_task(record_second())
        await second_entered.wait()
        assert storage.saved_knowledge == []
        assert durable.snapshot.knowledge.value == "UNKNOWN"

        storage.release_first.set()
        first_snapshot, second_snapshot = await asyncio.gather(first, second)

        assert first_snapshot.knowledge.value == "ABSENT_OBSERVED"
        assert second_snapshot.knowledge.value == "CONFLICT"
        assert storage.saved_knowledge == ["ABSENT_OBSERVED", "CONFLICT"]
        assert durable.snapshot is second_snapshot
        assert storage.data == second_snapshot.to_raw()

    asyncio.run(scenario())


def test_success_after_transient_failure_clears_write_fault(
    authority_store: ModuleType,
) -> None:
    async def scenario() -> None:
        class FailOnce(authority_store.MemoryBasketAuthorityStorage):
            failures = 1

            async def async_save(self, data: dict[str, Any]) -> None:
                if self.failures:
                    self.failures -= 1
                    raise OSError
                await super().async_save(data)

        storage = FailOnce()
        durable = authority_store.DurableBasketAuthority(storage)
        await durable.async_load()
        with pytest.raises(authority_store.BasketAuthorityFault):
            await durable.async_record_unknown(generation=1)
        assert durable.transient_write_fault is True

        saved = await durable.async_record_unknown(generation=2)
        assert durable.transient_write_fault is False
        assert durable.snapshot is saved
        assert storage.data == saved.to_raw()

    asyncio.run(scenario())


def test_serialized_image_contains_only_closed_privacy_minimal_fields(
    authority_store: ModuleType,
) -> None:
    snapshot = authority_store.BasketAuthoritySnapshot.from_raw(raw_for())
    serialized = json.dumps(snapshot.to_raw(), sort_keys=True)

    assert set(snapshot.to_raw()) == {
        "version",
        "generation",
        "knowledge",
        "account_digest",
        "store_digest",
        "intent_digest",
        "snapshot_digest",
        "observed_at",
    }
    assert all(digest in serialized for digest in DIGESTS.values())
    for forbidden in (
        "provider_id",
        "customer_id",
        "basket_id",
        "store_id",
        "address_id",
        "handle",
        "label",
        "latitude",
        "longitude",
        "coordinates",
        "product",
        "price",
        "payment",
        "token",
        "payload",
        "error_body",
    ):
        assert forbidden not in serialized.lower()


def test_home_assistant_storage_uses_private_v1_key_and_envelope(
    authority_store: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict[str, Any] = {}

    class FakeStore:
        def __init__(self, hass: Any, version: int, key: str) -> None:
            calls.update(hass=hass, version=version, key=key)

        async def async_load(self) -> Any:
            calls["loaded"] = True
            return None

        async def async_save(self, data: dict[str, Any]) -> None:
            calls["saved"] = data

    homeassistant = ModuleType("homeassistant")
    helpers = ModuleType("homeassistant.helpers")
    storage_module = ModuleType("homeassistant.helpers.storage")
    storage_module.Store = FakeStore  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.storage", storage_module)

    hass = object()
    storage = authority_store.HomeAssistantBasketAuthorityStorage(hass, "entry-private")
    assert calls == {
        "hass": hass,
        "version": authority_store.BASKET_AUTHORITY_STORE_VERSION,
        "key": "glovo.ordering_basket_authority_v1.entry-private",
    }
    assert asyncio.run(storage.async_load()) is None
    raw = raw_for("UNKNOWN")
    asyncio.run(storage.async_save(raw))
    assert calls["loaded"] is True
    assert calls["saved"] == raw
