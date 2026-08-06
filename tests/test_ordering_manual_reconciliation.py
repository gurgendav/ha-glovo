"""Deterministic core safety matrix for manual ordering reconciliation."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import inspect
import json
import socket
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]
ORDERING_MODULES = (
    "ordering_models",
    "ordering_basket",
    "ordering_quote",
    "ordering_catalog",
    "ordering_journal",
    "ordering_state",
    "ordering_adapter",
    "ordering_manager",
)


@pytest.fixture(autouse=True)
def no_outbound_socket(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fail every focused test on an attempted network escape."""
    attempted: list[str] = []

    def blocked(*args: Any, **kwargs: Any) -> Any:
        attempted.append(repr((args, kwargs)))
        pytest.fail("ordering core attempted an outbound socket/network call")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    yield
    assert attempted == []


@pytest.fixture()
def ordering() -> Iterator[dict[str, ModuleType]]:
    """Load pure ordering modules without importing Home Assistant."""
    package_name = "glovo_manual_reconciliation_under_test"
    package = ModuleType(package_name)
    package.__path__ = []  # type: ignore[attr-defined]
    sys.modules[package_name] = package
    loaded: dict[str, ModuleType] = {}
    root = ROOT / "custom_components" / "glovo"
    try:
        for name in ORDERING_MODULES:
            module_name = f"{package_name}.{name}"
            spec = importlib.util.spec_from_file_location(module_name, root / f"{name}.py")
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            loaded[name] = module
        yield loaded
    finally:
        for name in tuple(sys.modules):
            if name == package_name or name.startswith(f"{package_name}."):
                sys.modules.pop(name, None)


class Clock:
    def __init__(self, value: float = 1_800_000_100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class Challenges:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> str:
        self.count += 1
        return f"manual-challenge-{self.count:04d}-xxxxxxxxxxxxxxxx"


class Attempts:
    def __init__(self, *values: str) -> None:
        self.values = list(values) or ["generated"]

    def __call__(self) -> str:
        return self.values.pop(0)


class DelegatingStorage:
    def __init__(self, base: Any) -> None:
        self.base = base

    @property
    def data(self) -> Any:
        return self.base.data

    @property
    def legacy_data(self) -> Any:
        return getattr(self.base, "legacy_data", None)

    async def async_load(self) -> Any:
        return await self.base.async_load()

    async def async_load_legacy(self) -> Any:
        return await self.base.async_load_legacy()

    async def async_save(self, data: dict[str, Any]) -> None:
        await self.base.async_save(data)


class FailMatchingSave(DelegatingStorage):
    def __init__(self, base: Any, predicate: Any) -> None:
        super().__init__(base)
        self.predicate = predicate
        self.failed = False

    async def async_save(self, data: dict[str, Any]) -> None:
        if not self.failed and self.predicate(data):
            self.failed = True
            raise OSError("injected deterministic save failure")
        await super().async_save(data)


class BarrierJournalStorage(DelegatingStorage):
    def __init__(self, base: Any, *, fail_manual: bool) -> None:
        super().__init__(base)
        self.fail_manual = fail_manual
        self.pending = asyncio.Event()
        self.release = asyncio.Event()
        self.seen = False

    async def async_save(self, data: dict[str, Any]) -> None:
        manual = any(
            record["state"] == "MANUAL_CHECK_REQUIRED" for record in data["records"]
        )
        if manual and not self.seen:
            self.seen = True
            self.pending.set()
            await self.release.wait()
            if self.fail_manual:
                raise OSError("injected manual journal failure")
        await super().async_save(data)


class BarrierStateStorage(DelegatingStorage):
    def __init__(self, base: Any, *, block_manual: bool, fail_manual: bool) -> None:
        super().__init__(base)
        self.block_manual = block_manual
        self.fail_manual = fail_manual
        self.pending = asyncio.Event()
        self.release = asyncio.Event()
        self.seen = False

    async def async_save(self, data: dict[str, Any]) -> None:
        manual_only = data.get("manual_check_required") is True and not data.get(
            "integrity_fault"
        )
        if manual_only and not self.seen:
            self.seen = True
            self.pending.set()
            if self.block_manual:
                await self.release.wait()
            if self.fail_manual:
                raise OSError("injected manual latch failure")
        await super().async_save(data)


class BlockingAdapter:
    def __init__(self) -> None:
        self.invoked = asyncio.Event()
        self.execution_count = 0

    async def async_execute(self, *, mode: str, fingerprint: str) -> Any:
        assert mode == "live"
        assert len(fingerprint) == 64
        self.execution_count += 1
        self.invoked.set()
        await asyncio.Future()
        raise AssertionError("unreachable")


class TimeoutAdapter:
    def __init__(self) -> None:
        self.execution_count = 0

    async def async_execute(self, *, mode: str, fingerprint: str) -> Any:
        assert mode == "live"
        assert len(fingerprint) == 64
        self.execution_count += 1
        raise TimeoutError("synthetic timeout")


def _admin(ordering: dict[str, ModuleType], user_id: str = "admin-a") -> Any:
    return ordering["ordering_manager"].OrderingUser(user_id, True)


def _record(
    state: str = "MANUAL_CHECK_REQUIRED",
    *,
    attempt_id: str = "attempt-live-one",
    execution_mode: str | None = None,
) -> dict[str, Any]:
    mode = execution_mode or (
        "mock" if state == "LEGACY_MOCK_NO_REMOTE_EFFECT" else "live"
    )
    value: dict[str, Any] = {
        "attempt_id": attempt_id,
        "state": state,
        "created_at": 1_800_000_000.0,
        "updated_at": 1_800_000_010.0,
        "generation": 4,
        "amount_minor": 624000,
        "currency": "AMD",
        "execution_mode": mode,
        "request_fingerprint": "f" * 64,
        "provider_session_hash": "a" * 64 if mode == "live" else None,
        "checkout_id": None,
        "dispatch_started_at": None,
        "failure_class": None,
        "resolution": None,
        "evidence_source": None,
        "record_revision": 3,
        "store_display_name": "Fixture Kitchen",
        "item_count": 1,
        "item_summary": "1 fixture item",
        "masked_payment_label": "Test card •••• 4242",
        "masked_address_alias": "Saved destination ••••",
        "last_reviewed_at": None,
    }
    if state in {"DISPATCHING", "VERIFYING"}:
        value["dispatch_started_at"] = 1_800_000_005.0
    elif state == "MANUAL_CHECK_REQUIRED":
        value.update(
            dispatch_started_at=1_800_000_005.0,
            failure_class="timeout",
        )
    elif state in {"CONFIRMED_SUCCEEDED", "CONFIRMED_FAILED"}:
        value["dispatch_started_at"] = 1_800_000_005.0
        if mode == "live":
            value.update(
                failure_class="timeout",
                resolution=(
                    "found_succeeded"
                    if state == "CONFIRMED_SUCCEEDED"
                    else "found_failed_or_cancelled"
                ),
                evidence_source="admin_review",
                last_reviewed_at=1_800_000_009.0,
            )
        else:
            value.update(
                resolution=(
                    "synthetic_success"
                    if state == "CONFIRMED_SUCCEEDED"
                    else "synthetic_failure"
                ),
                evidence_source="mock_adapter",
            )
    elif state == "LEGACY_MOCK_NO_REMOTE_EFFECT":
        value.update(
            resolution="legacy_mock_no_remote_effect",
            evidence_source="legacy_migration",
            last_reviewed_at=1_800_000_009.0,
        )
    return value


def _state(
    record: dict[str, Any] | None = None,
    *,
    generation: int = 4,
    integrity: bool = False,
    resolution: str | None = None,
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "version": 2,
        "generation": generation,
        "manual_check_required": record is not None,
        "integrity_fault": integrity,
    }
    if record is not None:
        raw["manual_binding"] = {
            "attempt_id": record["attempt_id"],
            "record_revision": record["record_revision"],
            "generation": generation,
            "resolution": resolution,
        }
    return raw


async def _build(
    ordering: dict[str, ModuleType],
    *,
    journal_storage: Any | None = None,
    state_storage: Any | None = None,
    adapter: Any | None = None,
    clock: Clock | None = None,
    execution_mode: str = "mock",
    attempts: Attempts | None = None,
) -> tuple[Any, Any, Any, Any]:
    clock = clock or Clock()
    journal_storage = journal_storage or ordering[
        "ordering_journal"
    ].MemoryJournalStorage()
    state_storage = state_storage or ordering[
        "ordering_state"
    ].MemoryOrderingStateStorage()
    durable = ordering["ordering_state"].DurableOrderingState(state_storage)
    await durable.async_load()
    journal = ordering["ordering_journal"].AttemptJournal(journal_storage, clock=clock)
    adapter = adapter or ordering["ordering_adapter"].MockCheckoutAdapter()
    manager = ordering["ordering_manager"].OrderingManager(
        allow_ordering=True,
        ordering_acknowledged=True,
        live_options=lambda: {
            "allow_ordering": True,
            "ordering_acknowledged": True,
        },
        catalog=ordering["ordering_catalog"].SyntheticCatalogProvider(clock=clock),
        journal=journal,
        checkout_adapter=adapter,
        clock=clock,
        challenge_source=Challenges(),
        attempt_source=attempts or Attempts(),
        durable_state=durable,
        execution_mode=execution_mode,
    )
    await manager.async_initialize()
    return manager, adapter, journal_storage, state_storage


async def _prepare_checkout(ordering: dict[str, ModuleType], manager: Any) -> tuple[Any, int, dict[str, Any]]:
    user = _admin(ordering)
    generation = manager.generation
    await manager.async_add_item(
        user,
        generation,
        store_key="fixture-store",
        product_key="fixture-meal",
        variant_key="standard",
        modifier_keys=("no-change",),
        quantity=1,
    )
    quote = await manager.async_quote(
        user,
        generation,
        address_key="masked-destination",
        payment_key="masked-payment",
    )
    confirmation = await manager.async_prepare_mock_confirmation(
        user, generation, quote["fingerprint"]
    )
    return user, generation, confirmation


async def _prepare_manual(
    manager: Any,
    user: Any,
    record: dict[str, Any],
    resolution: str,
) -> dict[str, Any]:
    return await manager.async_prepare_manual_resolution(
        user,
        attempt_id=record["attempt_id"],
        expected_revision=record["record_revision"],
        expected_state=record["state"],
        resolution=resolution,
    )


def test_A_historical_resolution_never_falls_back_when_live_attempt_recovery_faults(
    ordering: dict[str, ModuleType],
) -> None:
    async def scenario() -> None:
        journal_module = ordering["ordering_journal"]
        state_module = ordering["ordering_state"]
        manager_module = ordering["ordering_manager"]
        historical = _record(
            "CONFIRMED_FAILED", attempt_id="attempt-historical-a"
        )
        journal_base = journal_module.MemoryJournalStorage(
            {"version": 2, "records": [historical]}
        )
        failing_journal = FailMatchingSave(
            journal_base,
            lambda data: any(
                item["attempt_id"] == "attempt-live-b"
                and item["state"] == "MANUAL_CHECK_REQUIRED"
                for item in data["records"]
            ),
        )
        state_base = state_module.MemoryOrderingStateStorage()
        adapter = TimeoutAdapter()
        manager, _, _, _ = await _build(
            ordering,
            journal_storage=failing_journal,
            state_storage=state_base,
            adapter=adapter,
            execution_mode="live",
            attempts=Attempts("live-b"),
        )
        user, generation, confirmation = await _prepare_checkout(ordering, manager)
        with pytest.raises(manager_module.OrderingSecurityFault):
            await manager.async_execute_mock_checkout(
                user,
                generation,
                confirmation["challenge"],
                confirmation["fingerprint"],
            )

        assert adapter.execution_count == 1
        assert manager.integrity_fault is True
        assert state_base.data["integrity_fault"] is True
        assert state_base.data["manual_binding"]["attempt_id"] == "attempt-live-b"
        assert state_base.data["manual_binding"]["record_revision"] == 4
        assert next(
            item for item in journal_base.data["records"] if item["attempt_id"] == "attempt-live-b"
        )["state"] == "DISPATCHING"
        assert (await manager.async_list_manual_checks(user))["attempts"] == []
        with pytest.raises(manager_module.InvalidManualResolution):
            await manager.async_get_manual_check(user, "attempt-historical-a")
        with pytest.raises(manager_module.OrderingSecurityFault):
            await _prepare_manual(manager, user, historical, "found_failed_or_cancelled")
        with pytest.raises(manager_module.OrderingSecurityFault):
            await manager.async_resolve_manual_check(
                user,
                attempt_id=historical["attempt_id"],
                expected_revision=historical["record_revision"],
                expected_state=historical["state"],
                resolution="found_failed_or_cancelled",
                challenge="manual-challenge-does-not-matter",
                acknowledged=True,
            )

        rebuilt, rebuilt_adapter, _, _ = await _build(
            ordering,
            journal_storage=journal_base,
            state_storage=state_base,
            clock=Clock(1_800_000_200.0),
        )
        checks = await rebuilt.async_list_manual_checks(user)
        assert [item["attemptRef"] for item in checks["attempts"]] == ["attempt-live-b"]
        assert rebuilt.integrity_fault is True
        assert rebuilt.enabled is False
        assert rebuilt_adapter.execution_count == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_layer", ["none", "journal", "latch"])
def test_B_live_cancellation_completes_two_layer_recovery_or_durable_integrity_fallback(
    ordering: dict[str, ModuleType], failure_layer: str
) -> None:
    async def scenario() -> None:
        journal_module = ordering["ordering_journal"]
        state_module = ordering["ordering_state"]
        journal_base = journal_module.MemoryJournalStorage()
        journal_barrier = BarrierJournalStorage(
            journal_base, fail_manual=failure_layer == "journal"
        )
        if failure_layer != "journal":
            journal_barrier.release.set()
        state_base = state_module.MemoryOrderingStateStorage()
        state_barrier = BarrierStateStorage(
            state_base,
            block_manual=failure_layer in {"none", "latch"},
            fail_manual=failure_layer == "latch",
        )
        adapter = BlockingAdapter()
        manager, _, _, _ = await _build(
            ordering,
            journal_storage=journal_barrier,
            state_storage=state_barrier,
            adapter=adapter,
            execution_mode="live",
            attempts=Attempts("cancelled-live"),
        )
        user, generation, confirmation = await _prepare_checkout(ordering, manager)
        execution = asyncio.create_task(
            manager.async_execute_mock_checkout(
                user,
                generation,
                confirmation["challenge"],
                confirmation["fingerprint"],
            )
        )
        await adapter.invoked.wait()
        execution.cancel()

        if failure_layer == "journal":
            await journal_barrier.pending.wait()
            execution.cancel()
            assert not execution.done()
            journal_barrier.release.set()
        else:
            await state_barrier.pending.wait()
            execution.cancel()
            assert not execution.done()
            state_barrier.release.set()

        with pytest.raises(asyncio.CancelledError):
            await execution
        assert adapter.execution_count == 1
        assert manager.enabled is False

        states = {
            item["attempt_id"]: item["state"] for item in journal_base.data["records"]
        }
        if failure_layer == "none":
            assert states["attempt-cancelled-live"] == "MANUAL_CHECK_REQUIRED"
            assert state_base.data["manual_check_required"] is True
            assert state_base.data["integrity_fault"] is False
            assert manager.manual_check_required is True
        elif failure_layer == "journal":
            assert states["attempt-cancelled-live"] == "DISPATCHING"
            assert state_base.data["manual_check_required"] is True
            assert state_base.data["integrity_fault"] is True
            assert manager.integrity_fault is True
        else:
            assert states["attempt-cancelled-live"] == "INTEGRITY_FAULT"
            assert state_base.data["manual_check_required"] is False
            assert manager.integrity_fault is True

        rebuilt, rebuilt_adapter, _, _ = await _build(
            ordering,
            journal_storage=journal_base,
            state_storage=state_base,
            clock=Clock(1_800_000_200.0),
        )
        assert rebuilt.enabled is False
        assert rebuilt_adapter.execution_count == 0
        if failure_layer == "none":
            assert rebuilt.manual_check_required is True
            assert rebuilt.integrity_fault is False
        else:
            assert rebuilt.integrity_fault is True

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("state", "mode"),
    [
        ("DRAFT", "mock"),
        ("QUOTED", "live"),
        ("RESERVED", "mock"),
        ("DISPATCHING", "live"),
        ("VERIFYING", "mock"),
        ("MANUAL_CHECK_REQUIRED", "live"),
        ("CONFIRMED_SUCCEEDED", "live"),
        ("CONFIRMED_SUCCEEDED", "mock"),
        ("CONFIRMED_FAILED", "live"),
        ("CONFIRMED_FAILED", "mock"),
        ("LEGACY_MOCK_NO_REMOTE_EFFECT", "mock"),
        ("INTEGRITY_FAULT", "live"),
    ],
)
def test_C_attempt_record_accepts_only_relevant_valid_state_authority_rows(
    ordering: dict[str, ModuleType], state: str, mode: str
) -> None:
    record = ordering["ordering_journal"].AttemptRecord.from_dict(
        _record(state, execution_mode=mode)
    )
    assert record.state.value == state
    assert record.execution_mode == mode


@pytest.mark.parametrize(
    "mutation",
    [
        {"execution_mode": "mock"},
        {"dispatch_started_at": None},
        {"failure_class": None},
        {"resolution": "found_succeeded"},
        {"evidence_source": "admin_review"},
        {"state": "MANUAL_CHECK_REQUIRED", "execution_mode": "mock"},
        {"state": "LEGACY_MOCK_NO_REMOTE_EFFECT", "execution_mode": "live"},
        {"state": "CONFIRMED_SUCCEEDED", "resolution": None, "evidence_source": None},
        {"state": "CONFIRMED_SUCCEEDED", "resolution": "found_failed_or_cancelled"},
        {"state": "CONFIRMED_FAILED", "resolution": "found_succeeded"},
        {"state": "CONFIRMED_FAILED", "evidence_source": "mock_adapter"},
        {"updated_at": 1_799_999_999.0},
        {"dispatch_started_at": 1_800_000_011.0},
    ],
)
def test_C_attempt_record_rejects_forbidden_or_contradictory_authority_rows(
    ordering: dict[str, ModuleType], mutation: dict[str, Any]
) -> None:
    raw = _record()
    raw.update(mutation)
    with pytest.raises(ordering["ordering_journal"].JournalCorrupt):
        ordering["ordering_journal"].AttemptRecord.from_dict(raw)


def test_C_transition_matrix_and_invalid_initialization_fail_closed(
    ordering: dict[str, ModuleType],
) -> None:
    async def scenario() -> None:
        journal_module = ordering["ordering_journal"]
        state_module = ordering["ordering_state"]
        manager_module = ordering["ordering_manager"]
        expected = {
            "DRAFT": {"QUOTED", "INTEGRITY_FAULT"},
            "QUOTED": {"RESERVED", "DISPATCHING", "INTEGRITY_FAULT"},
            "RESERVED": {"DISPATCHING", "INTEGRITY_FAULT"},
            "DISPATCHING": {
                "VERIFYING",
                "CONFIRMED_SUCCEEDED",
                "CONFIRMED_FAILED",
                "MANUAL_CHECK_REQUIRED",
                "LEGACY_MOCK_NO_REMOTE_EFFECT",
                "INTEGRITY_FAULT",
            },
            "VERIFYING": {
                "CONFIRMED_SUCCEEDED",
                "CONFIRMED_FAILED",
                "MANUAL_CHECK_REQUIRED",
                "LEGACY_MOCK_NO_REMOTE_EFFECT",
                "INTEGRITY_FAULT",
            },
            "MANUAL_CHECK_REQUIRED": {
                "CONFIRMED_SUCCEEDED",
                "CONFIRMED_FAILED",
                "INTEGRITY_FAULT",
            },
        }
        actual = {
            source.value: {target.value for target in targets}
            for source, targets in journal_module._ALLOWED_TRANSITIONS.items()
        }
        assert actual == expected

        terminal_storage = journal_module.MemoryJournalStorage(
            {"version": 2, "records": [_record("CONFIRMED_SUCCEEDED")]}
        )
        terminal = journal_module.AttemptJournal(terminal_storage, clock=Clock())
        await terminal.async_load()
        for target in journal_module.JournalState:
            with pytest.raises(journal_module.JournalCorrupt):
                await terminal.async_transition("attempt-live-one", target)

        invalid = _record()
        invalid["failure_class"] = None
        state_storage = state_module.MemoryOrderingStateStorage()
        manager, _, _, _ = await _build(
            ordering,
            journal_storage=journal_module.MemoryJournalStorage(
                {"version": 2, "records": [invalid]}
            ),
            state_storage=state_storage,
        )
        assert manager.integrity_fault is True
        assert manager.enabled is False
        assert state_storage.data["integrity_fault"] is True
        with pytest.raises(manager_module.OrderingSecurityFault):
            await manager.async_catalog(_admin(ordering), manager.generation)

    asyncio.run(scenario())


def test_C_every_relevant_allowed_and_forbidden_transition_is_exercised(
    ordering: dict[str, ModuleType],
) -> None:
    async def scenario() -> None:
        journal_module = ordering["ordering_journal"]
        async def journal_at(source: str, attempt_id: str, mode: str) -> Any:
            storage = journal_module.MemoryJournalStorage()
            journal = journal_module.AttemptJournal(storage, clock=Clock())
            await journal.async_load()
            await journal.async_create(
                attempt_id,
                4,
                "f" * 64,
                execution_mode=mode,
                provider_session_hash="a" * 64 if mode == "live" else None,
            )
            if source != "DRAFT":
                await journal.async_transition(
                    attempt_id, journal_module.JournalState.QUOTED
                )
            if source == "RESERVED":
                await journal.async_transition(
                    attempt_id, journal_module.JournalState.RESERVED
                )
            if source in {
                "DISPATCHING",
                "VERIFYING",
                "MANUAL_CHECK_REQUIRED",
            }:
                await journal.async_transition(
                    attempt_id, journal_module.JournalState.DISPATCHING
                )
            if source == "VERIFYING":
                await journal.async_transition(
                    attempt_id, journal_module.JournalState.VERIFYING
                )
            if source == "MANUAL_CHECK_REQUIRED":
                await journal.async_transition(
                    attempt_id,
                    journal_module.JournalState.MANUAL_CHECK_REQUIRED,
                    failure_class="timeout",
                )
            return journal

        allowed_cases = (
            ("DRAFT", "QUOTED", "live"),
            ("DRAFT", "INTEGRITY_FAULT", "live"),
            ("QUOTED", "RESERVED", "live"),
            ("QUOTED", "DISPATCHING", "live"),
            ("QUOTED", "INTEGRITY_FAULT", "live"),
            ("RESERVED", "DISPATCHING", "live"),
            ("RESERVED", "INTEGRITY_FAULT", "live"),
            ("DISPATCHING", "VERIFYING", "live"),
            ("DISPATCHING", "CONFIRMED_SUCCEEDED", "mock"),
            ("DISPATCHING", "CONFIRMED_FAILED", "mock"),
            ("DISPATCHING", "MANUAL_CHECK_REQUIRED", "live"),
            ("DISPATCHING", "LEGACY_MOCK_NO_REMOTE_EFFECT", "mock"),
            ("DISPATCHING", "INTEGRITY_FAULT", "live"),
            ("VERIFYING", "CONFIRMED_SUCCEEDED", "mock"),
            ("VERIFYING", "CONFIRMED_FAILED", "mock"),
            ("VERIFYING", "MANUAL_CHECK_REQUIRED", "live"),
            ("VERIFYING", "LEGACY_MOCK_NO_REMOTE_EFFECT", "mock"),
            ("VERIFYING", "INTEGRITY_FAULT", "live"),
            ("MANUAL_CHECK_REQUIRED", "CONFIRMED_SUCCEEDED", "live"),
            ("MANUAL_CHECK_REQUIRED", "CONFIRMED_FAILED", "live"),
            ("MANUAL_CHECK_REQUIRED", "INTEGRITY_FAULT", "live"),
        )
        for index, (source, target, mode) in enumerate(allowed_cases):
            attempt_id = f"attempt-allowed-{index:02d}"
            journal = await journal_at(source, attempt_id, mode)
            current_revision = journal.get(attempt_id).record_revision
            if source == "MANUAL_CHECK_REQUIRED" and target.startswith("CONFIRMED_"):
                resolution = (
                    "found_succeeded"
                    if target == "CONFIRMED_SUCCEEDED"
                    else "found_failed_or_cancelled"
                )
                changed = await journal.async_resolve(
                    attempt_id,
                    expected_revision=current_revision,
                    resolution=resolution,
                )
            else:
                kwargs: dict[str, Any] = {}
                if target == "MANUAL_CHECK_REQUIRED":
                    kwargs["failure_class"] = "timeout"
                elif target == "CONFIRMED_SUCCEEDED":
                    kwargs["resolution"] = "synthetic_success"
                    kwargs["evidence_source"] = "mock_adapter"
                elif target == "CONFIRMED_FAILED":
                    kwargs["resolution"] = "synthetic_failure"
                    kwargs["evidence_source"] = "mock_adapter"
                elif target == "LEGACY_MOCK_NO_REMOTE_EFFECT":
                    kwargs["resolution"] = "legacy_mock_no_remote_effect"
                    kwargs["evidence_source"] = "mock_adapter"
                changed = await journal.async_transition(
                    attempt_id, journal_module.JournalState(target), **kwargs
                )
            assert changed.state.value == target

        nonterminal = (
            "DRAFT",
            "QUOTED",
            "RESERVED",
            "DISPATCHING",
            "VERIFYING",
            "MANUAL_CHECK_REQUIRED",
        )
        for index, source in enumerate(nonterminal):
            allowed = {
                target.value
                for target in journal_module._ALLOWED_TRANSITIONS[
                    journal_module.JournalState(source)
                ]
            }
            for target in journal_module.JournalState:
                if target.value in allowed:
                    continue
                attempt_id = f"attempt-forbidden-{index:02d}-{target.name.casefold()}"
                journal = await journal_at(source, attempt_id, "live")
                storage = journal._storage
                with pytest.raises(journal_module.JournalCorrupt):
                    await journal.async_transition(attempt_id, target)
                assert storage.data["records"][0]["state"] == source

    asyncio.run(scenario())


_ADVERSARIAL_DISPLAY_VALUES = (
    "123 Synthetic Test Street",
    "provider_id=prov_123",
    "payment id pay_123",
    "address id addr_123",
    "session id sess_123",
    "order id ord_123",
    "Bearer synthetic-not-a-secret",
    "access_token=synthetic-not-a-secret",
    "Cookie sid=synthetic-not-a-secret",
    "Traceback File error.py line 9",
    '{"order_id":"ord_123"}',
    "response body: synthetic-private-example",
    "safe\x00unsafe",
)


@pytest.mark.parametrize(
    ("field", "attack"),
    [
        (field, attack)
        for field in (
            "store_display_name",
            "item_summary",
            "masked_payment_label",
            "masked_address_alias",
        )
        for attack in _ADVERSARIAL_DISPLAY_VALUES
    ],
)
def test_D_privacy_adversarial_display_metadata_is_rejected_on_write_and_load(
    ordering: dict[str, ModuleType], field: str, attack: str
) -> None:
    async def scenario() -> None:
        journal_module = ordering["ordering_journal"]
        raw = _record()
        raw[field] = (
            f"{attack} ••••"
            if field in {"masked_payment_label", "masked_address_alias"}
            else attack
        )
        with pytest.raises(journal_module.JournalCorrupt):
            journal_module.AttemptRecord.from_dict(raw)

        storage = journal_module.MemoryJournalStorage()
        journal = journal_module.AttemptJournal(storage, clock=Clock())
        await journal.async_load()
        kwargs = {
            "store_display_name": "Fixture Kitchen",
            "item_summary": "1 fixture item",
            "masked_payment_label": "Test card •••• 4242",
            "masked_address_alias": "Saved destination ••••",
        }
        kwargs[field] = raw[field]
        with pytest.raises(journal_module.JournalCorrupt):
            await journal.async_create(
                "attempt-private-write",
                1,
                "f" * 64,
                **kwargs,
            )
        assert storage.data is None or storage.data["records"] == []

    asyncio.run(scenario())


def test_D_serialized_journal_and_state_contain_only_allowlisted_privacy_schema(
    ordering: dict[str, ModuleType],
) -> None:
    journal_module = ordering["ordering_journal"]
    state_module = ordering["ordering_state"]
    journal_data = {"version": 2, "records": [_record()]}
    state_data = _state(_record())
    journal_module.AttemptRecord.from_dict(journal_data["records"][0])
    state_module.OrderingStateSnapshot.from_raw(state_data)
    serialized = json.dumps({"journal": journal_data, "state": state_data}).casefold()
    prohibited_content = (
        "main street",
        "prov_123",
        "pay_123",
        "addr_123",
        "sess_123",
        "ord_123",
        "bearer ",
        "access_token",
        "cookie sid",
        "traceback",
        "response body",
    )
    assert not [value for value in prohibited_content if value in serialized]
    prohibited_keys = {
        "authorization",
        "token",
        "cookie",
        "address_id",
        "payment_id",
        "provider_id",
        "session_id",
        "order_id",
        "raw_exception",
        "traceback",
        "body",
    }

    def keys(value: Any) -> set[str]:
        if isinstance(value, dict):
            return set(value).union(*(keys(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(item) for item in value))
        return set()

    assert keys({"journal": journal_data, "state": state_data}).isdisjoint(prohibited_keys)


def _legacy_record(state: str, attempt_id: str) -> dict[str, Any]:
    return {
        "attempt_id": attempt_id,
        "state": state,
        "created_at": 1_800_000_000.0,
        "updated_at": 1_800_000_001.0,
        "generation": 2,
        "quote_fingerprint": "b" * 64,
        "outcome": None,
    }


def test_E_core_migration_restart_storage_keys_and_zero_replay(
    ordering: dict[str, ModuleType],
) -> None:
    async def scenario() -> None:
        journal_module = ordering["ordering_journal"]
        state_module = ordering["ordering_state"]
        legacy = {
            "version": 1,
            "records": [
                _legacy_record("DISPATCHING", "attempt-v1-dispatching"),
                _legacy_record("VERIFYING", "attempt-v1-verifying"),
                _legacy_record("UNCERTAIN", "attempt-v1-uncertain"),
                _legacy_record("SECURITY_FAULT", "attempt-v1-fault"),
            ],
        }
        storage = journal_module.MemoryJournalStorage(legacy)
        journal = journal_module.AttemptJournal(storage, clock=Clock())
        records = await journal.async_load()
        assert [record.state.value for record in records[:3]] == [
            "LEGACY_MOCK_NO_REMOTE_EFFECT"
        ] * 3
        assert records[3].state.value == "INTEGRITY_FAULT"
        assert storage.legacy_data == legacy
        assert storage.data["version"] == 2

        journal_source = inspect.getsource(journal_module.HomeAssistantJournalStorage)
        state_source = inspect.getsource(state_module.HomeAssistantOrderingStateStorage)
        assert "glovo.ordering_journal_v2." in journal_source
        assert "glovo.ordering_journal." in journal_source
        assert "glovo.ordering_safety_v2." in state_source
        assert "glovo.ordering_safety." in state_source

        for inflight in ("DISPATCHING", "VERIFYING"):
            live = _record(inflight, attempt_id=f"attempt-v2-{inflight.casefold()}")
            journal_base = journal_module.MemoryJournalStorage(
                {"version": 2, "records": [live]}
            )
            state_base = state_module.MemoryOrderingStateStorage()
            manager, adapter, _, _ = await _build(
                ordering,
                journal_storage=journal_base,
                state_storage=state_base,
                clock=Clock(1_800_000_200.0),
            )
            assert manager.manual_check_required is True
            assert manager.journal.records[0].state.value == "MANUAL_CHECK_REQUIRED"
            assert manager.journal.records[0].failure_class == "restart_inflight"
            assert state_base.data["manual_binding"]["attempt_id"] == live["attempt_id"]
            assert adapter.execution_count == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fault", ["partial", "contradictory", "migration_save", "restart_save"]
)
def test_E_partial_or_failed_migration_and_restart_normalization_fail_closed(
    ordering: dict[str, ModuleType], fault: str
) -> None:
    async def scenario() -> None:
        journal_module = ordering["ordering_journal"]
        state_module = ordering["ordering_state"]
        if fault == "partial":
            journal_storage: Any = journal_module.MemoryJournalStorage(
                {
                    "version": 1,
                    "records": [_legacy_record("DISPATCHING", "attempt-partial")],
                }
            )
            state_storage: Any = state_module.MemoryOrderingStateStorage()
        elif fault == "contradictory":
            contradictory = _legacy_record("UNCERTAIN", "attempt-contradictory")
            contradictory["outcome"] = "claimed_remote_success"
            journal_storage = journal_module.MemoryJournalStorage(
                {"version": 1, "records": [contradictory]}
            )
            state_storage = state_module.MemoryOrderingStateStorage(
                _state(None, generation=2)
            )
        elif fault == "migration_save":
            base = journal_module.MemoryJournalStorage(
                {
                    "version": 1,
                    "records": [_legacy_record("DISPATCHING", "attempt-migration")],
                }
            )
            journal_storage = FailMatchingSave(base, lambda data: data["version"] == 2)
            state_storage = state_module.MemoryOrderingStateStorage(
                _state(None, generation=2)
            )
        else:
            live = _record("DISPATCHING", attempt_id="attempt-restart")
            base = journal_module.MemoryJournalStorage(
                {"version": 2, "records": [live]}
            )
            journal_storage = FailMatchingSave(
                base,
                lambda data: data["records"][0]["state"]
                == "MANUAL_CHECK_REQUIRED",
            )
            state_storage = state_module.MemoryOrderingStateStorage(
                _state(None, generation=4)
            )
        manager, adapter, _, _ = await _build(
            ordering,
            journal_storage=journal_storage,
            state_storage=state_storage,
            clock=Clock(1_800_000_200.0),
        )
        assert manager.integrity_fault is True
        assert manager.enabled is False
        assert adapter.execution_count == 0
        assert state_storage.data["integrity_fault"] is True

    asyncio.run(scenario())


def test_E_unresolved_manual_and_integrity_records_survive_compaction(
    ordering: dict[str, ModuleType],
) -> None:
    async def scenario() -> None:
        journal_module = ordering["ordering_journal"]
        records = [
            _record(
                "LEGACY_MOCK_NO_REMOTE_EFFECT",
                attempt_id=f"attempt-terminal-{index:03d}",
                execution_mode="mock",
            )
            for index in range(198)
        ]
        manual = _record(attempt_id="attempt-protected-manual")
        integrity = _record("INTEGRITY_FAULT", attempt_id="attempt-protected-integrity")
        storage = journal_module.MemoryJournalStorage(
            {"version": 2, "records": [*records, manual, integrity]}
        )
        journal = journal_module.AttemptJournal(storage, clock=Clock())
        await journal.async_load()
        await journal.async_create("attempt-newest", 4, "c" * 64)
        ids = {record.attempt_id for record in journal.records}
        assert len(ids) == journal_module.MAX_JOURNAL_RECORDS
        assert {"attempt-protected-manual", "attempt-protected-integrity"} <= ids

    asyncio.run(scenario())


@pytest.mark.parametrize("resolution", ["found_succeeded", "found_failed_or_cancelled"])
def test_F_manual_block_surface_and_terminal_resolution_invalidate_old_authority(
    ordering: dict[str, ModuleType], resolution: str
) -> None:
    async def scenario() -> None:
        journal_module = ordering["ordering_journal"]
        state_module = ordering["ordering_state"]
        manager_module = ordering["ordering_manager"]
        record = _record()
        manager, adapter, _, _ = await _build(
            ordering,
            journal_storage=journal_module.MemoryJournalStorage(
                {"version": 2, "records": [record]}
            ),
            state_storage=state_module.MemoryOrderingStateStorage(_state(record)),
        )
        user = _admin(ordering)
        generation = manager.generation
        blocked_calls = [
            manager.async_catalog(user, generation),
            manager.async_get_basket(user, generation),
            manager.async_add_item(
                user,
                generation,
                store_key="fixture-store",
                product_key="fixture-meal",
                variant_key="standard",
                modifier_keys=(),
                quantity=1,
            ),
            manager.async_clear_basket(user, generation),
            manager.async_quote(
                user,
                generation,
                address_key="masked-destination",
                payment_key="masked-payment",
            ),
            manager.async_prepare_mock_confirmation(user, generation, "f" * 64),
            manager.async_execute_mock_checkout(user, generation, "x" * 24, "f" * 64),
        ]
        for call in blocked_calls:
            with pytest.raises(manager_module.OrderingManualCheckRequired):
                await call
        state = await manager.async_state(user)
        assert state["manualCheckRequired"] is True
        assert (await manager.async_list_manual_checks(user))["attempts"][0][
            "attemptRef"
        ] == record["attempt_id"]
        assert (await manager.async_get_manual_check(user, record["attempt_id"]))[
            "attemptRef"
        ] == record["attempt_id"]

        challenge = await _prepare_manual(manager, user, record, resolution)
        result = await manager.async_resolve_manual_check(
            user,
            attempt_id=record["attempt_id"],
            expected_revision=record["record_revision"],
            expected_state=record["state"],
            resolution=resolution,
            challenge=challenge["challenge"],
            acknowledged=True,
        )
        assert result == {"resolved": True, "manualCheckRequired": False}
        assert manager.generation == generation + 1
        assert adapter.execution_count == 0
        with pytest.raises(manager_module.StaleOrderingGeneration):
            await manager.async_get_basket(user, generation)
        with pytest.raises(manager_module.StaleOrderingGeneration):
            await manager.async_prepare_mock_confirmation(user, generation, "f" * 64)
        with pytest.raises(manager_module.StaleOrderingGeneration):
            await manager.async_execute_mock_checkout(user, generation, "x" * 24, "f" * 64)

    asyncio.run(scenario())


@pytest.mark.parametrize("resolution", ["found_succeeded", "found_failed_or_cancelled"])
def test_F_terminal_resolution_never_revives_pre_manual_basket_quote_or_confirmation(
    ordering: dict[str, ModuleType], resolution: str
) -> None:
    async def scenario() -> None:
        manager_module = ordering["ordering_manager"]
        manager, adapter, _, _ = await _build(ordering)
        user, old_generation, confirmation = await _prepare_checkout(ordering, manager)
        old_fingerprint = confirmation["fingerprint"]
        record = await manager.journal.async_create(
            "attempt-live-authority-cutoff",
            old_generation,
            old_fingerprint,
            amount_minor=624000,
            currency="AMD",
            execution_mode="live",
            provider_session_hash="a" * 64,
            store_display_name="Fixture Kitchen",
            item_count=1,
            item_summary="1 fixture item",
            masked_payment_label="Test card •••• 4242",
            masked_address_alias="Saved destination ••••",
        )
        await manager.journal.async_transition(record.attempt_id, ordering["ordering_journal"].JournalState.QUOTED)
        await manager.journal.async_transition(
            record.attempt_id, ordering["ordering_journal"].JournalState.DISPATCHING
        )
        record = await manager.journal.async_transition(
            record.attempt_id,
            ordering["ordering_journal"].JournalState.MANUAL_CHECK_REQUIRED,
            failure_class="timeout",
        )
        await manager._async_latch_manual_check(
            attempt_id=record.attempt_id,
            record_revision=record.record_revision,
        )
        assert manager.generation == old_generation + 1
        prepared = await manager.async_prepare_manual_resolution(
            user,
            attempt_id=record.attempt_id,
            expected_revision=record.record_revision,
            expected_state=record.state.value,
            resolution=resolution,
        )
        await manager.async_resolve_manual_check(
            user,
            attempt_id=record.attempt_id,
            expected_revision=record.record_revision,
            expected_state=record.state.value,
            resolution=resolution,
            challenge=prepared["challenge"],
            acknowledged=True,
        )
        assert manager.generation == old_generation + 2
        stale_calls = (
            manager.async_get_basket(user, old_generation),
            manager.async_quote(
                user,
                old_generation,
                address_key="masked-destination",
                payment_key="masked-payment",
            ),
            manager.async_prepare_mock_confirmation(
                user, old_generation, old_fingerprint
            ),
            manager.async_execute_mock_checkout(
                user,
                old_generation,
                confirmation["challenge"],
                old_fingerprint,
            ),
        )
        for call in stale_calls:
            with pytest.raises(manager_module.StaleOrderingGeneration):
                await call
        assert adapter.execution_count == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "invalid_case",
    [
        "non_admin",
        "ack_false",
        "ack_truthy_int",
        "ack_truthy_text",
        "expired",
        "owner",
        "attempt",
        "revision",
        "state",
        "resolution",
        "generation",
    ],
)
def test_F_manual_resolution_challenge_rejects_every_binding_mismatch(
    ordering: dict[str, ModuleType], invalid_case: str
) -> None:
    async def scenario() -> None:
        journal_module = ordering["ordering_journal"]
        state_module = ordering["ordering_state"]
        manager_module = ordering["ordering_manager"]
        record = _record()
        clock = Clock()
        manager, _, _, _ = await _build(
            ordering,
            journal_storage=journal_module.MemoryJournalStorage(
                {"version": 2, "records": [record]}
            ),
            state_storage=state_module.MemoryOrderingStateStorage(_state(record)),
            clock=clock,
        )
        user = _admin(ordering)
        challenge = await _prepare_manual(
            manager, user, record, "found_failed_or_cancelled"
        )
        kwargs: dict[str, Any] = {
            "attempt_id": record["attempt_id"],
            "expected_revision": record["record_revision"],
            "expected_state": record["state"],
            "resolution": "found_failed_or_cancelled",
            "challenge": challenge["challenge"],
            "acknowledged": True,
        }
        caller = user
        if invalid_case == "non_admin":
            caller = manager_module.OrderingUser("ordinary", False)
        elif invalid_case == "ack_false":
            kwargs["acknowledged"] = False
        elif invalid_case == "ack_truthy_int":
            kwargs["acknowledged"] = 1
        elif invalid_case == "ack_truthy_text":
            kwargs["acknowledged"] = "true"
        elif invalid_case == "expired":
            clock.advance(60)
        elif invalid_case == "owner":
            caller = _admin(ordering, "admin-b")
        elif invalid_case == "attempt":
            kwargs["attempt_id"] = "attempt-other"
        elif invalid_case == "revision":
            kwargs["expected_revision"] += 1
        elif invalid_case == "state":
            kwargs["expected_state"] = "CONFIRMED_FAILED"
        elif invalid_case == "resolution":
            kwargs["resolution"] = "found_succeeded"
        elif invalid_case == "generation":
            prepared = manager._manual_challenges[challenge["challenge"]]
            manager._manual_challenges[challenge["challenge"]] = replace(
                prepared, generation=prepared.generation + 1
            )
        error = (
            manager_module.OrderingAdminRequired
            if invalid_case == "non_admin"
            else manager_module.InvalidManualResolution
        )
        with pytest.raises(error):
            await manager.async_resolve_manual_check(caller, **kwargs)
        assert manager.manual_check_required is True
        assert manager.journal.get(record["attempt_id"]).state.value == record["state"]

    asyncio.run(scenario())


def test_F_still_unknown_updates_exact_binding_and_multiple_live_attempts_fault(
    ordering: dict[str, ModuleType],
) -> None:
    async def scenario() -> None:
        journal_module = ordering["ordering_journal"]
        state_module = ordering["ordering_state"]
        record = _record()
        state_storage = state_module.MemoryOrderingStateStorage(_state(record))
        manager, _, _, _ = await _build(
            ordering,
            journal_storage=journal_module.MemoryJournalStorage(
                {"version": 2, "records": [record]}
            ),
            state_storage=state_storage,
        )
        user = _admin(ordering)
        prepared = await _prepare_manual(manager, user, record, "still_unknown")
        result = await manager.async_resolve_manual_check(
            user,
            attempt_id=record["attempt_id"],
            expected_revision=record["record_revision"],
            expected_state=record["state"],
            resolution="still_unknown",
            challenge=prepared["challenge"],
            acknowledged=True,
        )
        updated = manager.journal.get(record["attempt_id"])
        assert result["resolved"] is False
        assert updated.record_revision == record["record_revision"] + 1
        assert state_storage.data["manual_binding"] == {
            "attempt_id": record["attempt_id"],
            "record_revision": updated.record_revision,
            "generation": manager.generation,
            "resolution": None,
        }

        second = _record(attempt_id="attempt-live-two")
        bad, _, _, _ = await _build(
            ordering,
            journal_storage=journal_module.MemoryJournalStorage(
                {"version": 2, "records": [record, second]}
            ),
            state_storage=state_module.MemoryOrderingStateStorage(_state(record)),
        )
        assert bad.integrity_fault is True

    asyncio.run(scenario())


class LoggedStorage(DelegatingStorage):
    def __init__(self, base: Any, label: str, log: list[tuple[str, Any]]) -> None:
        super().__init__(base)
        self.label = label
        self.log = log

    async def async_save(self, data: dict[str, Any]) -> None:
        self.log.append((self.label, copy.deepcopy(data)))
        await super().async_save(data)


class FailClearOnce(DelegatingStorage):
    def __init__(self, base: Any) -> None:
        super().__init__(base)
        self.failed = False

    async def async_save(self, data: dict[str, Any]) -> None:
        if not self.failed and data.get("manual_check_required") is False:
            self.failed = True
            raise OSError("injected latch clear failure")
        await super().async_save(data)


def test_F_generation_precedes_resolution_and_failed_journal_keeps_block(
    ordering: dict[str, ModuleType],
) -> None:
    async def scenario() -> None:
        journal_module = ordering["ordering_journal"]
        state_module = ordering["ordering_state"]
        manager_module = ordering["ordering_manager"]
        record = _record()
        log: list[tuple[str, Any]] = []
        journal_base = journal_module.MemoryJournalStorage(
            {"version": 2, "records": [record]}
        )
        state_base = state_module.MemoryOrderingStateStorage(_state(record))
        manager, _, _, _ = await _build(
            ordering,
            journal_storage=LoggedStorage(journal_base, "journal", log),
            state_storage=LoggedStorage(state_base, "state", log),
        )
        prepared = await _prepare_manual(
            manager, _admin(ordering), record, "found_failed_or_cancelled"
        )
        await manager.async_resolve_manual_check(
            _admin(ordering),
            attempt_id=record["attempt_id"],
            expected_revision=record["record_revision"],
            expected_state=record["state"],
            resolution="found_failed_or_cancelled",
            challenge=prepared["challenge"],
            acknowledged=True,
        )
        assert [label for label, _ in log] == ["state", "journal", "state", "state"]
        assert log[0][1]["generation"] == 5
        assert log[1][1]["records"][0]["state"] == "CONFIRMED_FAILED"

        journal_base = journal_module.MemoryJournalStorage(
            {"version": 2, "records": [record]}
        )
        state_base = state_module.MemoryOrderingStateStorage(_state(record))
        failing = FailMatchingSave(
            journal_base,
            lambda data: data["records"][0]["state"] == "CONFIRMED_FAILED",
        )
        blocked, _, _, _ = await _build(
            ordering,
            journal_storage=failing,
            state_storage=state_base,
        )
        prepared = await _prepare_manual(
            blocked, _admin(ordering), record, "found_failed_or_cancelled"
        )
        with pytest.raises(manager_module.OrderingRecoveryWriteFailed):
            await blocked.async_resolve_manual_check(
                _admin(ordering),
                attempt_id=record["attempt_id"],
                expected_revision=record["record_revision"],
                expected_state=record["state"],
                resolution="found_failed_or_cancelled",
                challenge=prepared["challenge"],
                acknowledged=True,
            )
        assert state_base.data["generation"] == 5
        assert state_base.data["manual_check_required"] is True
        assert journal_base.data["records"][0]["state"] == "MANUAL_CHECK_REQUIRED"
        assert blocked.manual_check_required is True

    asyncio.run(scenario())


def test_F_failed_latch_clear_has_exact_idempotent_completion_and_conflict_rejection(
    ordering: dict[str, ModuleType],
) -> None:
    async def scenario() -> None:
        journal_module = ordering["ordering_journal"]
        state_module = ordering["ordering_state"]
        manager_module = ordering["ordering_manager"]
        record = _record()
        journal_base = journal_module.MemoryJournalStorage(
            {"version": 2, "records": [record]}
        )
        state_base = state_module.MemoryOrderingStateStorage(_state(record))
        state_storage = FailClearOnce(state_base)
        manager, _, _, _ = await _build(
            ordering,
            journal_storage=journal_base,
            state_storage=state_storage,
        )
        user = _admin(ordering)
        prepared = await _prepare_manual(
            manager, user, record, "found_failed_or_cancelled"
        )
        with pytest.raises(manager_module.OrderingRecoveryWriteFailed):
            await manager.async_resolve_manual_check(
                user,
                attempt_id=record["attempt_id"],
                expected_revision=record["record_revision"],
                expected_state=record["state"],
                resolution="found_failed_or_cancelled",
                challenge=prepared["challenge"],
                acknowledged=True,
            )
        terminal = manager.journal.get(record["attempt_id"])
        assert terminal.state.value == "CONFIRMED_FAILED"
        assert state_base.data["manual_binding"]["resolution"] == "found_failed_or_cancelled"
        assert state_base.data["manual_check_required"] is True

        with pytest.raises(manager_module.InvalidManualResolution):
            await manager.async_prepare_manual_resolution(
                user,
                attempt_id=terminal.attempt_id,
                expected_revision=terminal.record_revision,
                expected_state=terminal.state.value,
                resolution="found_succeeded",
            )
        retry = await manager.async_prepare_manual_resolution(
            user,
            attempt_id=terminal.attempt_id,
            expected_revision=terminal.record_revision,
            expected_state=terminal.state.value,
            resolution="found_failed_or_cancelled",
        )
        result = await manager.async_resolve_manual_check(
            user,
            attempt_id=terminal.attempt_id,
            expected_revision=terminal.record_revision,
            expected_state=terminal.state.value,
            resolution="found_failed_or_cancelled",
            challenge=retry["challenge"],
            acknowledged=True,
        )
        assert result["resolved"] is True
        assert state_base.data["manual_check_required"] is False
        with pytest.raises(manager_module.InvalidManualResolution):
            await manager.async_resolve_manual_check(
                user,
                attempt_id=terminal.attempt_id,
                expected_revision=terminal.record_revision,
                expected_state=terminal.state.value,
                resolution="found_failed_or_cancelled",
                challenge=retry["challenge"],
                acknowledged=True,
            )

    asyncio.run(scenario())


def test_F_concurrent_resolutions_have_exactly_one_durable_winner(
    ordering: dict[str, ModuleType],
) -> None:
    async def scenario() -> None:
        journal_module = ordering["ordering_journal"]
        state_module = ordering["ordering_state"]
        record = _record()
        state_storage = state_module.MemoryOrderingStateStorage(_state(record))
        manager, _, _, _ = await _build(
            ordering,
            journal_storage=journal_module.MemoryJournalStorage(
                {"version": 2, "records": [record]}
            ),
            state_storage=state_storage,
        )
        users = (_admin(ordering, "admin-a"), _admin(ordering, "admin-b"))
        resolutions = ("found_succeeded", "found_failed_or_cancelled")
        prepared = [
            await _prepare_manual(manager, user, record, resolution)
            for user, resolution in zip(users, resolutions, strict=True)
        ]
        outcomes = await asyncio.gather(
            *(
                manager.async_resolve_manual_check(
                    user,
                    attempt_id=record["attempt_id"],
                    expected_revision=record["record_revision"],
                    expected_state=record["state"],
                    resolution=resolution,
                    challenge=challenge["challenge"],
                    acknowledged=True,
                )
                for user, resolution, challenge in zip(
                    users, resolutions, prepared, strict=True
                )
            ),
            return_exceptions=True,
        )
        winners = [outcome for outcome in outcomes if isinstance(outcome, dict)]
        losers = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
        assert len(winners) == 1
        assert len(losers) == 1
        terminal = manager.journal.get(record["attempt_id"])
        assert terminal.resolution in resolutions
        assert state_storage.data["manual_check_required"] is False
        assert manager.manual_check_required is False

    asyncio.run(scenario())


def test_G_core_api_inventory_has_no_retry_fault_clear_or_automatic_resubmission(
    ordering: dict[str, ModuleType],
) -> None:
    manager_class = ordering["ordering_manager"].OrderingManager
    public_methods = {
        name
        for name, value in inspect.getmembers(manager_class, inspect.iscoroutinefunction)
        if name.startswith("async_")
    }
    assert not {name for name in public_methods if "retry" in name}
    assert "async_clear_integrity_fault" not in public_methods
    assert "async_clear_security_fault" not in public_methods
    source = inspect.getsource(manager_class)
    assert source.count("self._checkout_adapter.async_execute(") == 1
    assert "async_retry_attempt" not in source
    assert "retryAttempt" not in source
    assert socket.create_connection.__name__ == "blocked"
