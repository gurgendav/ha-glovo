"""Focused no-network regression tests for durable preparatory authority."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]


class Clock:
    def __init__(self) -> None:
        self.value = 1_900_000_000.0

    def __call__(self) -> float:
        self.value += 1.0
        return self.value


def _modules() -> dict[str, ModuleType]:
    package_name = "glovo_prep_authority_under_test"
    package = ModuleType(package_name)
    package.__path__ = []  # type: ignore[attr-defined]
    sys.modules[package_name] = package
    result: dict[str, ModuleType] = {}
    for name in (
        "ordering_models",
        "ordering_state",
        "ordering_journal",
        "ordering_prep_authority",
    ):
        spec = importlib.util.spec_from_file_location(
            f"{package_name}.{name}", ROOT / "custom_components/glovo" / f"{name}.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        result[name] = module
    return result


async def _build() -> tuple[dict[str, ModuleType], Any, Any, Any]:
    modules = _modules()
    state_storage = modules["ordering_state"].MemoryOrderingStateStorage()
    state = modules["ordering_state"].DurableOrderingState(state_storage)
    await state.async_load()
    storage = modules["ordering_prep_authority"].MemoryPreparationStorage()
    authority = modules["ordering_prep_authority"].PreparationMutationAuthority(
        storage, clock=Clock(), durable_state=state
    )
    await authority.async_load()
    return modules, authority, storage, state


class BarrierStorage:
    def __init__(self, base: Any) -> None:
        self.base, self.pending, self.release, self.blocked = base, asyncio.Event(), asyncio.Event(), False

    async def async_load(self) -> Any:
        return await self.base.async_load()

    async def async_save(self, data: dict[str, Any]) -> None:
        if not self.blocked and any(r["state"] == "RECONCILIATION_REQUIRED" for r in data["records"]):
            self.blocked = True
            self.pending.set()
            await self.release.wait()
        await self.base.async_save(data)


def test_restart_no_replay_exact_get_and_mismatch_stays_blocked() -> None:
    async def scenario() -> None:
        modules, authority, storage, state = await _build()
        prep = modules["ordering_prep_authority"]
        record = await authority.async_acquire(
            attempt_id="prep-one", generation=1, purpose=prep.PreparationPurpose.BASKET_REPLACE,
            expectation_hash="a" * 64, expected_category="expected_present",
        )
        with pytest.raises(prep.PreparationReplayDenied):
            await authority.async_acquire(
                attempt_id="prep-two", generation=1, purpose=prep.PreparationPurpose.BASKET_REPLACE,
                expectation_hash="a" * 64, expected_category="expected_present",
            )
        restarted = prep.PreparationMutationAuthority(storage, clock=Clock(), durable_state=state)
        current = (await restarted.async_load())[0]
        assert current.state is prep.PreparationState.RECONCILIATION_REQUIRED
        assert state.preparation_binding is not None
        mismatch = await restarted.async_reconcile_once(
            attempt_id=record.attempt_id, expected_revision=current.record_revision,
            observed_expectation_hash="b" * 64, observed_category="expected_present",
        )
        assert mismatch.state is prep.PreparationState.RECONCILIATION_REQUIRED and mismatch.reconciliation_get_used
        with pytest.raises(prep.PreparationReplayDenied):
            await restarted.async_reconcile_once(
                attempt_id=record.attempt_id, expected_revision=mismatch.record_revision,
                observed_expectation_hash="a" * 64, observed_category="expected_present",
            )
    asyncio.run(scenario())


def test_exact_match_and_closed_provider_evidence() -> None:
    async def scenario() -> None:
        modules, authority, _, _ = await _build()
        prep = modules["ordering_prep_authority"]
        dispatching = await authority.async_acquire(
            attempt_id="prep-evidence", generation=1, purpose=prep.PreparationPurpose.QUOTE_TEMPLATE_CREATE,
            expectation_hash="c" * 64, expected_category="expected_template",
        )
        with pytest.raises(prep.PreparationAuthorityFault):
            await authority.async_record_outcome(
                attempt_id=dispatching.attempt_id, expected_revision=dispatching.record_revision,
                outcome_category="operator_says_ok",
            )
        unresolved = await authority.async_record_outcome(
            attempt_id=dispatching.attempt_id, expected_revision=dispatching.record_revision,
            outcome_category="ambiguous",
        )
        matched = await authority.async_reconcile_once(
            attempt_id=unresolved.attempt_id, expected_revision=unresolved.record_revision,
            observed_expectation_hash="c" * 64, observed_category="expected_template",
        )
        assert matched.state is prep.PreparationState.RECONCILED
    asyncio.run(scenario())


def test_repeated_cancellation_finishes_two_layers_before_returning() -> None:
    async def scenario() -> None:
        modules, authority, storage, state = await _build()
        prep = modules["ordering_prep_authority"]
        barrier = BarrierStorage(storage)
        authority = prep.PreparationMutationAuthority(barrier, clock=Clock(), durable_state=state)
        await authority.async_load()
        dispatching = await authority.async_acquire(
            attempt_id="prep-cancel", generation=1, purpose=prep.PreparationPurpose.BASKET_DELETE,
            expectation_hash="e" * 64, expected_category="expected_absent",
        )
        task = asyncio.create_task(authority.async_record_outcome(
            attempt_id=dispatching.attempt_id, expected_revision=dispatching.record_revision,
            outcome_category="ambiguous",
        ))
        await barrier.pending.wait()
        task.cancel()
        task.cancel()
        barrier.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert storage.data["records"][0]["state"] == "RECONCILIATION_REQUIRED"
        assert state.preparation_binding is not None
    asyncio.run(scenario())


def test_manual_resolution_cannot_clear_preparation_block_or_bypass_provider_evidence() -> None:
    async def scenario() -> None:
        modules, _, _, state = await _build()
        journal_module = modules["ordering_journal"]

        await state.async_latch_manual_check(attempt_id="attempt-manual", record_revision=1)
        await state.async_latch_preparation_reconciliation(
            attempt_id="prep-bound", record_revision=3,
            purpose="basket_create", expectation_hash="f" * 64,
        )
        await state.async_update_manual_binding(
            attempt_id="attempt-manual", record_revision=2, resolution="found_succeeded"
        )
        await state.async_clear_manual_check(
            attempt_id="attempt-manual", record_revision=2,
            resolution="found_succeeded", generation=state.generation,
        )
        assert state.manual_check_required is False
        assert state.preparation_binding is not None and state.safety_fault

        journal = journal_module.AttemptJournal(
            journal_module.MemoryJournalStorage(), clock=Clock()
        )
        await journal.async_load()
        created = await journal.async_create(
            "attempt-provider", 1, "a" * 64, execution_mode="live",
            provider_session_hash="b" * 64,
        )
        with pytest.raises(journal_module.JournalCorrupt):
            await journal.async_record_provider_terminal(
                created.attempt_id, expected_revision=created.record_revision,
                succeeded=True, provider_evidence_hash="c" * 64,
            )
        await journal.async_transition(created.attempt_id, journal_module.JournalState.QUOTED)
        dispatching = await journal.async_transition(
            created.attempt_id, journal_module.JournalState.DISPATCHING
        )
        with pytest.raises(journal_module.JournalCorrupt):
            await journal.async_record_provider_terminal(
                dispatching.attempt_id, expected_revision=dispatching.record_revision,
                succeeded=True, provider_evidence_hash="not-a-hash",
            )
        terminal = await journal.async_record_provider_terminal(
            dispatching.attempt_id, expected_revision=dispatching.record_revision,
            succeeded=True, provider_evidence_hash="c" * 64,
        )
        assert terminal.state is journal_module.JournalState.CONFIRMED_SUCCEEDED
        assert terminal.provider_evidence_hash == "c" * 64
        assert terminal.evidence_source == "provider_response"

    asyncio.run(scenario())
