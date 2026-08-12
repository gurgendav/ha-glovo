"""G4 regression tests for the transport-free durable live-flow seam."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]
GLOVO = ROOT / "custom_components" / "glovo"
MODULES = (
    "ordering_models", "ordering_contracts", "api_session", "ordering_remote_basket",
    "ordering_live_quote", "ordering_account", "ordering_live_catalog",
    "ordering_live_selection", "ordering_packages", "ordering_prep_authority", "ordering_live_api",
    "ordering_live_flow",
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture()
def modules() -> dict[str, ModuleType]:
    package_name = "glovo_g4_live_flow"
    package = ModuleType(package_name)
    package.__path__ = [str(GLOVO)]  # type: ignore[attr-defined]
    sys.modules[package_name] = package
    loaded: dict[str, ModuleType] = {}
    try:
        for name in MODULES:
            spec = importlib.util.spec_from_file_location(
                f"{package_name}.{name}", GLOVO / f"{name}.py"
            )
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            loaded[name] = module
        yield loaded
    finally:
        for name in tuple(sys.modules):
            if name == package_name or name.startswith(f"{package_name}."):
                sys.modules.pop(name, None)


class Clock:
    def __call__(self) -> float:
        return 10.0


class FakeFacade:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def async_dispatch(self, *, owner: str, operation: str, request: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((owner, operation, request))
        return {"safe": True}


def test_live_flow_defaults_off_and_never_claims_a_production_final_adapter(
    modules: dict[str, ModuleType],
) -> None:
    async def scenario() -> None:
        options: dict[str, object] = {
            "allow_ordering": False,
            "ordering_acknowledged": False,
            "allow_live_checkout": True,
            "live_checkout_acknowledged": True,
        }
        prep = modules["ordering_prep_authority"]
        authority = prep.PreparationMutationAuthority(
            prep.MemoryPreparationStorage(), clock=Clock()
        )
        facade = FakeFacade()
        flow = modules["ordering_live_flow"].OrderingLiveFlow(
            facade=facade, preparation_authority=authority, live_options=lambda: options
        )
        await flow.async_initialize()
        assert flow.live_checkout_available is False
        with pytest.raises(modules["ordering_live_flow"].LiveFlowUnavailable):
            await flow.async_live_dispatch("admin-one", "live/addresses", {"generation": 1})
        options.update(allow_ordering=True, ordering_acknowledged=True)
        result = await flow.async_live_dispatch("admin-one", "live/addresses", {"generation": 1})
        assert result == {"safe": True}
        assert facade.calls == [("admin-one", "live/addresses", {"generation": 1})]
        state = flow.public_state(1)
        assert state["liveCheckoutAvailable"] is False
        encoded = json.dumps(state)
        for private in ("provider", "token", "session", "checkout", "attempt"):
            assert private not in encoded

    run(scenario())


def test_package_save_stage_survives_privacy_redaction(
    modules: dict[str, ModuleType],
) -> None:
    async def scenario() -> None:
        options = {"allow_ordering": True, "ordering_acknowledged": True}
        prep = modules["ordering_prep_authority"]
        authority = prep.PreparationMutationAuthority(
            prep.MemoryPreparationStorage(), clock=Clock()
        )

        class FailingFacade:
            async def async_dispatch(self, **_kwargs: Any) -> dict[str, Any]:
                raise modules["ordering_live_api"].PackageSaveStageError("selection")

        flow = modules["ordering_live_flow"].OrderingLiveFlow(
            facade=FailingFacade(),
            preparation_authority=authority,
            live_options=lambda: options,
        )
        await flow.async_initialize()
        with pytest.raises(modules["ordering_live_flow"].LiveFlowUnavailable) as err:
            await flow.async_live_dispatch(
                "admin-one",
                "library/package_save",
                {
                    "generation": 1,
                    "expectedStoreRevision": 0,
                    "packageRef": "",
                    "expectedRevision": 0,
                    "name": "Safe display name",
                    "aliases": [],
                    "storeHandle": "store-local",
                    "products": [
                        {"productHandle": "product-local", "quantity": 1, "options": []}
                    ],
                },
            )
        assert err.value.stage == "selection"
        assert str(err.value) == "package save is unavailable"

    run(scenario())


def test_authority_requires_durable_prepared_and_dispatching_before_single_call(
    modules: dict[str, ModuleType],
) -> None:
    async def scenario() -> None:
        prep = modules["ordering_prep_authority"]
        storage = prep.MemoryPreparationStorage()
        authority = prep.PreparationMutationAuthority(storage, clock=Clock())
        await authority.async_load()
        record = await authority.async_acquire(
            attempt_id="prep-test-one",
            generation=1,
            purpose=prep.PreparationPurpose.BASKET_CREATE,
            expectation_hash="a" * 64,
            expected_category="expected_present",
        )
        assert record.state is prep.PreparationState.DISPATCHING
        assert storage.data["records"][0]["state"] == "DISPATCHING"
        with pytest.raises(prep.PreparationReplayDenied):
            await authority.async_acquire(
                attempt_id="prep-test-two",
                generation=1,
                purpose=prep.PreparationPurpose.BASKET_CREATE,
                expectation_hash="a" * 64,
                expected_category="expected_present",
            )

    run(scenario())
