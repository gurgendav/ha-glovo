"""Focused public-surface regressions for the frozen live-ordering contract."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]
SURFACE = ROOT / "custom_components" / "glovo" / "ordering_surface.py"


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture()
def surface_module() -> ModuleType:
    package = ModuleType("glovo_live_surface_test")
    package.__path__ = [str(SURFACE.parent)]  # type: ignore[attr-defined]
    manager = ModuleType("glovo_live_surface_test.ordering_manager")

    class OrderingManager:  # pragma: no cover - only import shape
        pass

    class OrderingUser:
        def __init__(self, user_id: str, is_admin: bool) -> None:
            self.user_id = user_id
            self.is_admin = is_admin

    manager.OrderingManager = OrderingManager
    manager.OrderingUser = OrderingUser
    sys.modules[package.__name__] = package
    sys.modules[manager.__name__] = manager
    spec = importlib.util.spec_from_file_location(
        "glovo_live_surface_test.ordering_surface", SURFACE
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    for name in tuple(sys.modules):
        if name.startswith("glovo_live_surface_test"):
            sys.modules.pop(name, None)


class Adapter:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}
        self.registrations: list[str] = []
        self.panels = 0
        self.removed = 0

    async def async_register_handler(self, name: str, handler: Any) -> None:
        self.registrations.append(name)
        self.handlers[name] = handler

    async def async_remove_handlers(self) -> None:
        self.handlers.clear()
        self.removed += 1

    async def async_register_panel(self, **kwargs: Any) -> None:
        assert kwargs["require_admin"] is True
        self.panels += 1

    async def async_remove_panel(self) -> None:
        self.panels -= 1


class Manager:
    recovery_required = False

    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled
        self.calls: list[dict[str, Any]] = []
        self.state_calls: list[Any] = []

    async def async_live_dispatch(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"safe": True}

    async def async_state(self, user: Any) -> dict[str, Any]:
        self.state_calls.append(user)
        return {"generation": 9, "enabled": self.enabled}

    async def async_list_manual_checks(self, user: Any) -> dict[str, Any]:
        return {"attempts": []}

    async def async_get_manual_check(self, user: Any, attempt: str) -> dict[str, Any]:
        return {"attemptRef": attempt}

    async def async_prepare_manual_resolution(self, user: Any, **kwargs: Any) -> dict[str, Any]:
        return {"challenge": "local-only"}

    async def async_resolve_manual_check(self, user: Any, **kwargs: Any) -> dict[str, Any]:
        return {"manualCheckRequired": True}


def test_recovery_registers_only_bootstrap_and_recovery_while_gated_off(
    surface_module: ModuleType,
) -> None:
    manager, adapter = Manager(), Adapter()
    surface = surface_module.OrderingSurface(manager, adapter)
    run(surface.async_setup())
    expected = {surface_module.PUBLIC_OPERATION_COMMANDS["state"]} | set(surface_module.RECOVERY_COMMANDS)
    assert set(adapter.handlers) == expected
    assert adapter.panels == 1
    first = list(adapter.registrations)
    run(surface.async_setup())
    assert adapter.registrations == first
    run(surface.async_unload())
    assert adapter.handlers == {} and adapter.removed == 1


def test_enabled_surface_registers_all_frozen_operations(
    surface_module: ModuleType,
) -> None:
    manager, adapter = Manager(enabled=True), Adapter()
    run(surface_module.OrderingSurface(manager, adapter).async_setup())
    assert set(adapter.handlers) == set(surface_module.PUBLIC_OPERATION_COMMANDS.values()) | set(surface_module.RECOVERY_COMMANDS)


def test_owner_is_always_authenticated_user_and_never_a_spoofed_request_value(
    surface_module: ModuleType,
) -> None:
    manager, adapter = Manager(enabled=True), Adapter()
    surface = surface_module.OrderingSurface(manager, adapter)
    run(surface.async_setup())
    user = surface_module.OrderingUser("authenticated-admin", True)
    result = run(adapter.handlers["glovo/ordering/live/addresses"](user, {"id": 7, "type": "glovo/ordering/live/addresses", "generation": 4, "owner": "attacker"}))
    assert result == {"safe": True}
    assert manager.calls == [{"owner_key": "authenticated-admin", "operation": "live/addresses", "request": {"generation": 4, "owner": "attacker"}}]
    # The HA schema rejects this extra field before this handler can run.  The
    # pure surface nevertheless never treats it as an owner identity.


def test_public_operation_names_match_frozen_facade_contract(
    surface_module: ModuleType,
) -> None:
    source = (ROOT / "custom_components" / "glovo" / "ordering_live_api.py").read_text(encoding="utf-8")
    frozen = {
        "state", "live/addresses", "live/stores", "live/store_menu", "live/payment_methods",
        "live/basket", "live/basket_set", "live/basket_clear", "live/basket_reconcile",
        "live/create_quote", "live/prepare_confirmation", "live/execute_checkout", "live/checkout_status",
    }
    assert all(f'"{name}"' in source for name in frozen)
    assert set(surface_module.PUBLIC_OPERATION_COMMANDS) == frozen
    assert all(name == f"glovo/ordering/{operation}" for operation, name in surface_module.PUBLIC_OPERATION_COMMANDS.items())


def test_no_non_websocket_live_primitives_or_private_transport_names() -> None:
    runtime_files = [
        ROOT / "custom_components" / "glovo" / "ordering_surface.py",
        ROOT / "custom_components" / "glovo" / "ordering_ha.py",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in runtime_files)
    for forbidden in ("async_register_service", "async_register_entity", "async_fire", "webhook", "mqtt", "provider_id", "checkoutSessionId", "Authorization"):
        assert forbidden not in combined
