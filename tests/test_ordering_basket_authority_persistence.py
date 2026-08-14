"""Runtime composition tests for privacy-minimal durable basket evidence."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections.abc import Iterator
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from test_ordering_basket_authority import (
    Harness,
    ProviderRejected,
    modules as _modules_fixture,
    run,
)


@pytest.fixture()
def modules() -> Iterator[dict[str, ModuleType]]:
    """Reuse the strict isolated module loader without importing its fixture name."""

    loader = getattr(_modules_fixture, "__wrapped__")
    yield from loader()


class RecordingStorage:
    """Copy-isolated save ledger with deterministic next-save failures."""

    def __init__(self, data: Any = None) -> None:
        self.data = copy.deepcopy(data)
        self.saves: list[dict[str, Any]] = []
        self.next_failure: BaseException | None = None

    async def async_load(self) -> Any:
        return copy.deepcopy(self.data)

    async def async_save(self, data: dict[str, Any]) -> None:
        self.saves.append(copy.deepcopy(data))
        failure = self.next_failure
        self.next_failure = None
        if failure is not None:
            raise failure
        self.data = copy.deepcopy(data)


def _raw(kind: str, generation: int = 3) -> dict[str, Any]:
    digests: dict[str, Any] = {
        "account_digest": "a" * 64,
        "store_digest": "b" * 64,
        "intent_digest": "c" * 64,
        "snapshot_digest": "d" * 64,
    }
    if kind == "UNKNOWN":
        digests = {key: None for key in digests}
    elif kind in {"ABSENT_OBSERVED", "CONFLICT"}:
        digests["snapshot_digest"] = None
    return {
        "version": 1,
        "generation": generation,
        "knowledge": kind,
        **digests,
        "observed_at": 50.0,
    }


async def _attach(
    harness: Harness, storage: RecordingStorage | None = None
) -> tuple[Any, RecordingStorage]:
    store_module = harness.modules["ordering_basket_authority_store"]
    backend = storage or RecordingStorage()
    durable = store_module.DurableBasketAuthority(backend, clock=harness.clock)
    await durable.async_load()
    await durable.async_record_unknown(generation=7)
    harness.facade._basket_evidence = durable
    return durable, backend


@pytest.mark.parametrize(
    "old_kind",
    [None, "UNKNOWN", "ABSENT_OBSERVED", "PRESENT_OBSERVED", "CONFLICT"],
)
def test_every_restart_image_is_overwritten_unknown_before_flow_activation(
    modules: dict[str, ModuleType], old_kind: str | None
) -> None:
    async def scenario() -> None:
        harness = Harness(modules)
        store_module = modules["ordering_basket_authority_store"]
        backend = RecordingStorage(None if old_kind is None else _raw(old_kind))
        durable = store_module.DurableBasketAuthority(backend, clock=harness.clock)
        harness.facade._basket_evidence = durable
        prep = modules["ordering_prep_authority"].PreparationMutationAuthority(
            modules["ordering_prep_authority"].MemoryPreparationStorage(),
            clock=harness.clock,
        )
        manager = SimpleNamespace(generation=7, enabled=True)
        flow = modules["ordering_live_flow"].OrderingLiveFlow(
            facade=harness.facade,
            preparation_authority=prep,
            basket_authority=durable,
            live_options=lambda: {
                "allow_ordering": True,
                "ordering_acknowledged": True,
            },
            manager=manager,
        )

        await flow.async_initialize()

        assert flow.live_ordering_available is True
        assert durable.snapshot.knowledge.value == "UNKNOWN"
        assert durable.snapshot.generation == 7
        assert backend.data == durable.snapshot.to_raw()
        assert backend.saves == [backend.data]
        assert await harness.facade.async_dispatch(
            owner="admin-a", operation="live/basket", request={"generation": 7}
        ) == {"status": "unknown"}

    run(scenario())


def test_discovery_persists_exact_domain_hashes_before_each_publication(
    modules: dict[str, ModuleType],
) -> None:
    async def scenario() -> None:
        harness = Harness(modules)
        durable, storage = await _attach(harness)
        packages = modules["ordering_packages"]
        expected_account = packages.account_digest(harness.customer)
        expected_store = packages.store_digest(harness.store)
        expected_intent = harness.facade._expectation_hash(
            "basket_intent", harness.intent()
        )

        absent = await harness.facade.async_dispatch(
            owner="admin-a",
            operation="live/basket_adopt",
            request=harness.adopt_request("admin-a"),
        )
        assert absent["status"] == "absent_verified"
        assert storage.data == {
            "version": 1,
            "generation": 7,
            "knowledge": "ABSENT_OBSERVED",
            "account_digest": expected_account,
            "store_digest": expected_store,
            "intent_digest": expected_intent,
            "snapshot_digest": None,
            "observed_at": 100.0,
        }

        snapshot = harness.snapshot()
        harness.next_discovery = harness.discovery_module.RemoteBasketDiscoveryResult(
            harness.discovery_module.RemoteBasketDiscoveryStatus.ADOPTED, snapshot
        )
        adopted = await harness.facade.async_dispatch(
            owner="admin-b",
            operation="live/basket_adopt",
            request=harness.adopt_request("admin-b"),
        )
        assert adopted["status"] == "adopted"
        expected_snapshot = hashlib.sha256(
            b"glovo.ordering.basket_authority.provider_snapshot.v1\0"
            + snapshot.provider_projection_bytes
        ).hexdigest()
        assert storage.data == {
            "version": 1,
            "generation": 7,
            "knowledge": "PRESENT_OBSERVED",
            "account_digest": expected_account,
            "store_digest": expected_store,
            "intent_digest": expected_intent,
            "snapshot_digest": expected_snapshot,
            "observed_at": 100.0,
        }

        harness.next_discovery = harness.discovery_module.RemoteBasketDiscoveryResult(
            harness.discovery_module.RemoteBasketDiscoveryStatus.CONFLICT
        )
        conflict = await harness.facade.async_dispatch(
            owner="admin-a",
            operation="live/basket_adopt",
            request=harness.adopt_request("admin-a"),
        )
        assert conflict == {"status": "conflict"}
        assert storage.data["knowledge"] == "CONFLICT"
        assert storage.data["account_digest"] == expected_account
        assert storage.data["store_digest"] == expected_store
        assert storage.data["intent_digest"] == expected_intent
        assert storage.data["snapshot_digest"] is None
        assert durable.transient_write_fault is False

        encoded = json.dumps(storage.saves, sort_keys=True)
        for private in (
            "admin-a",
            "admin-b",
            str(harness.customer.customer_id),
            snapshot.basket_id,
            snapshot.basket_version,
            snapshot.products[0].basket_product_id,
            harness.store.slug,
        ):
            assert private not in encoded

    run(scenario())


def test_save_failure_before_adoption_stays_unknown_and_only_explicit_adoption_recovers(
    modules: dict[str, ModuleType],
) -> None:
    async def scenario() -> None:
        harness = Harness(modules)
        durable, storage = await _attach(harness)
        storage.next_failure = OSError("private storage detail")

        with pytest.raises(harness.api.PublicContractError):
            await harness.facade.async_dispatch(
                owner="admin-a",
                operation="live/basket_adopt",
                request=harness.adopt_request("admin-a"),
            )
        assert durable.transient_write_fault is True
        assert harness.facade._basket_authority.mode is harness.api._BasketAuthorityMode.UNKNOWN
        assert (harness.create_calls, harness.replace_calls, harness.delete_calls) == (0, 0, 0)
        with pytest.raises(harness.api.PublicContractError):
            await harness.facade.async_dispatch(
                owner="admin-a",
                operation="live/basket_set",
                request=harness.request("admin-a", 2, 0),
            )
        assert harness.create_calls == 0

        recovered = await harness.facade.async_dispatch(
            owner="admin-a",
            operation="live/basket_adopt",
            request=harness.adopt_request("admin-a"),
        )
        assert recovered["status"] == "absent_verified"
        assert durable.transient_write_fault is False
        assert storage.data["knowledge"] == "ABSENT_OBSERVED"
        assert len(harness.discovery_calls) == 1

    run(scenario())


@pytest.mark.parametrize("operation", ["create", "replace", "delete"])
def test_post_success_save_failure_executes_once_blocks_retry_and_needs_adoption(
    modules: dict[str, ModuleType], operation: str
) -> None:
    async def scenario() -> None:
        harness = Harness(modules)
        durable, storage = await _attach(harness)
        if operation == "create":
            await harness.facade.async_dispatch(
                owner="admin-a",
                operation="live/basket_adopt",
                request=harness.adopt_request("admin-a"),
            )
            public_operation = "live/basket_set"
            request = harness.request("admin-a", 2, 0)
        else:
            harness.next_discovery = harness.discovery_module.RemoteBasketDiscoveryResult(
                harness.discovery_module.RemoteBasketDiscoveryStatus.ADOPTED,
                harness.snapshot(),
            )
            await harness.facade.async_dispatch(
                owner="admin-a",
                operation="live/basket_adopt",
                request=harness.adopt_request("admin-a"),
            )
            public_operation = (
                "live/basket_set" if operation == "replace" else "live/basket_clear"
            )
            request = (
                harness.request("admin-a", 3, 1)
                if operation == "replace"
                else {"generation": 7, "expectedRevision": 1}
            )
        storage.next_failure = OSError("private storage detail")

        with pytest.raises(harness.api.PublicContractError):
            await harness.facade.async_dispatch(
                owner="admin-a", operation=public_operation, request=request
            )
        calls = (harness.create_calls, harness.replace_calls, harness.delete_calls)
        expected_calls = {
            "create": (1, 0, 0),
            "replace": (0, 1, 0),
            "delete": (0, 0, 1),
        }[operation]
        assert calls == expected_calls
        assert durable.transient_write_fault is True
        assert harness.facade._basket_authority.mode is harness.api._BasketAuthorityMode.UNKNOWN

        with pytest.raises(harness.api.PublicContractError):
            await harness.facade.async_dispatch(
                owner="admin-b", operation=public_operation, request=request
            )
        assert (harness.create_calls, harness.replace_calls, harness.delete_calls) == calls

        quantity = 3 if operation == "replace" else 2
        harness.next_discovery = harness.discovery_module.RemoteBasketDiscoveryResult(
            harness.discovery_module.RemoteBasketDiscoveryStatus.ADOPTED,
            harness.snapshot(quantity, version="basket-v2"),
        )
        recovered = await harness.facade.async_dispatch(
            owner="admin-a",
            operation="live/basket_adopt",
            request=harness.adopt_request("admin-a", quantity),
        )
        assert recovered["status"] == "adopted"
        assert durable.transient_write_fault is False
        assert storage.data["knowledge"] == "PRESENT_OBSERVED"
        assert (harness.create_calls, harness.replace_calls, harness.delete_calls) == calls

    run(scenario())


def test_cancelled_present_save_finalizes_unknown_without_repeating_create(
    modules: dict[str, ModuleType],
) -> None:
    async def scenario() -> None:
        harness = Harness(modules)
        durable, storage = await _attach(harness)
        await harness.facade.async_dispatch(
            owner="admin-a",
            operation="live/basket_adopt",
            request=harness.adopt_request("admin-a"),
        )
        storage.next_failure = asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await harness.facade.async_dispatch(
                owner="admin-a",
                operation="live/basket_set",
                request=harness.request("admin-a", 2, 0),
            )
        assert harness.create_calls == 1
        assert durable.snapshot.knowledge.value == "UNKNOWN"
        assert storage.data["knowledge"] == "UNKNOWN"
        assert harness.facade._basket_authority.mode is harness.api._BasketAuthorityMode.UNKNOWN
        with pytest.raises(harness.api.PublicContractError):
            await harness.facade.async_dispatch(
                owner="admin-a",
                operation="live/basket_set",
                request=harness.request("admin-a", 2, 0),
            )
        assert harness.create_calls == 1

    run(scenario())


@pytest.mark.parametrize("operation", ["replace", "delete"])
def test_deterministic_rejection_preserves_durable_and_in_memory_present(
    modules: dict[str, ModuleType], operation: str
) -> None:
    async def scenario() -> None:
        harness = Harness(modules)
        durable, storage = await _attach(harness)
        harness.next_discovery = harness.discovery_module.RemoteBasketDiscoveryResult(
            harness.discovery_module.RemoteBasketDiscoveryStatus.ADOPTED,
            harness.snapshot(),
        )
        await harness.facade.async_dispatch(
            owner="admin-a",
            operation="live/basket_adopt",
            request=harness.adopt_request("admin-a"),
        )
        present = copy.deepcopy(storage.data)
        if operation == "replace":
            harness.next_replace = ProviderRejected()
            public_operation = "live/basket_set"
            request = harness.request("admin-a", 3, 1)
        else:
            harness.next_delete = ProviderRejected()
            public_operation = "live/basket_clear"
            request = {"generation": 7, "expectedRevision": 1}

        with pytest.raises(harness.api.PreparationMutationRejected):
            await harness.facade.async_dispatch(
                owner="admin-a", operation=public_operation, request=request
            )
        assert storage.data == present
        assert durable.snapshot.knowledge.value == "PRESENT_OBSERVED"
        assert harness.facade._basket_authority.mode is harness.api._BasketAuthorityMode.ADOPTED

    run(scenario())


def test_ambiguous_replace_and_generation_invalidation_persist_unknown(
    modules: dict[str, ModuleType],
) -> None:
    async def scenario() -> None:
        harness = Harness(modules)
        durable, storage = await _attach(harness)
        harness.next_discovery = harness.discovery_module.RemoteBasketDiscoveryResult(
            harness.discovery_module.RemoteBasketDiscoveryStatus.ADOPTED,
            harness.snapshot(),
        )
        await harness.facade.async_dispatch(
            owner="admin-a",
            operation="live/basket_adopt",
            request=harness.adopt_request("admin-a"),
        )
        harness.next_replace = RuntimeError("ambiguous private transport")
        with pytest.raises(harness.api.PublicContractError):
            await harness.facade.async_dispatch(
                owner="admin-a",
                operation="live/basket_set",
                request=harness.request("admin-a", 3, 1),
            )
        assert storage.data["knowledge"] == "UNKNOWN"
        assert harness.replace_calls == 1

        prep = modules["ordering_prep_authority"].PreparationMutationAuthority(
            modules["ordering_prep_authority"].MemoryPreparationStorage(),
            clock=harness.clock,
        )
        flow = modules["ordering_live_flow"].OrderingLiveFlow(
            facade=harness.facade,
            preparation_authority=prep,
            basket_authority=durable,
            live_options=lambda: {
                "allow_ordering": True,
                "ordering_acknowledged": True,
            },
            manager=SimpleNamespace(generation=7, enabled=True),
        )
        await flow.async_initialize()
        await flow.async_invalidate(8)
        assert storage.data["generation"] == 8
        assert storage.data["knowledge"] == "UNKNOWN"
        assert flow.live_ordering_available is False

    run(scenario())
