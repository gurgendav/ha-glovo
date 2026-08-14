"""Behavioral Home Assistant boundary tests using realistic API/service stubs."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import socket
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import voluptuous as vol

ROOT = Path(__file__).parents[1]
GLOVO_ROOT = ROOT / "custom_components" / "glovo"


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def _module(name: str, *, package: bool = False) -> ModuleType:
    module = ModuleType(name)
    if package:
        module.__path__ = []  # type: ignore[attr-defined]
    return module


def _load(name: str, path: Path, *, package: bool = False) -> ModuleType:
    kwargs = {"submodule_search_locations": [str(path.parent)]} if package else {}
    spec = importlib.util.spec_from_file_location(name, path, **kwargs)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def no_outbound_socket(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fail every HA lifecycle test on an unexpected network escape."""
    attempted: list[str] = []

    def blocked(*args: Any, **kwargs: Any) -> Any:
        attempted.append(repr((args, kwargs)))
        pytest.fail("ordering HA lifecycle attempted an outbound socket/network call")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    yield
    assert attempted == []


@pytest.fixture()
def ha_runtime(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    prefix = "glovo_ha_under_test"
    package = _module(prefix, package=True)
    package.__path__ = [str(GLOVO_ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, prefix, package)

    homeassistant = _module("homeassistant", package=True)
    components = _module("homeassistant.components", package=True)
    core = _module("homeassistant.core")
    config_entries = _module("homeassistant.config_entries")
    helpers = _module("homeassistant.helpers", package=True)
    selector = _module("homeassistant.helpers.selector")
    storage_module = _module("homeassistant.helpers.storage")
    http_module = _module("homeassistant.components.http")
    frontend = _module("homeassistant.components.frontend")
    panel_custom = _module("homeassistant.components.panel_custom")
    websocket_api = _module("homeassistant.components.websocket_api")

    class HomeAssistant:
        pass

    def callback(value: Any) -> Any:
        return value

    core.HomeAssistant = HomeAssistant
    core.callback = callback

    class ConfigFlow:
        VERSION = 1
        MINOR_VERSION = 1

        def __init_subclass__(cls, **kwargs: Any) -> None:
            kwargs.pop("domain", None)
            super().__init_subclass__()

        def async_update_reload_and_abort(self, entry: Any, **kwargs: Any) -> dict[str, Any]:
            return {"type": "abort", "reason": "reauth_successful", "entry": entry, **kwargs}

        def async_show_form(self, **kwargs: Any) -> dict[str, Any]:
            return {"type": "form", **kwargs}

    class OptionsFlow:
        def async_create_entry(self, **kwargs: Any) -> dict[str, Any]:
            return {"type": "create_entry", **kwargs}

        def async_show_form(self, **kwargs: Any) -> dict[str, Any]:
            return {"type": "form", **kwargs}

    config_entries.ConfigFlow = ConfigFlow
    config_entries.OptionsFlow = OptionsFlow
    config_entries.ConfigFlowResult = dict

    class _Selector:
        def __init__(self, config: Any = None) -> None:
            self.config = config

        def __call__(self, value: Any) -> Any:
            return value

    class TextSelectorType:
        PASSWORD = "password"

    class NumberSelectorMode:
        BOX = "box"

    selector.TextSelector = _Selector
    selector.TextSelectorConfig = lambda **kwargs: kwargs
    selector.TextSelectorType = TextSelectorType
    selector.NumberSelector = _Selector
    selector.NumberSelectorConfig = lambda **kwargs: kwargs
    selector.NumberSelectorMode = NumberSelectorMode
    selector.BooleanSelector = _Selector
    helpers.selector = selector

    class Store:
        values: dict[str, Any] = {}

        def __init__(self, hass: Any, version: int, key: str) -> None:
            self.key = key

        async def async_load(self) -> Any:
            return self.values.get(self.key)

        async def async_save(self, data: dict[str, Any]) -> None:
            self.values[self.key] = data

    storage_module.Store = Store

    @dataclass
    class StaticPathConfig:
        url_path: str
        path: str
        cache_headers: bool

    http_module.StaticPathConfig = StaticPathConfig

    removed_panels: list[str] = []
    panel_calls: list[dict[str, Any]] = []
    commands: list[Any] = []

    def async_remove_panel(hass: Any, path: str) -> None:
        removed_panels.append(path)

    async def async_register_panel(hass: Any, **kwargs: Any) -> None:
        panel_calls.append(kwargs)

    def websocket_command(schema: Any):
        validator = vol.Schema({vol.Required("id"): int, **schema})

        def decorate(function: Any) -> Any:
            @wraps(function)
            async def validated(hass: Any, connection: Any, message: Any) -> None:
                try:
                    clean_message = validator(message)
                except vol.Invalid:
                    connection.send_error(
                        message.get("id", 0) if isinstance(message, dict) else 0,
                        "invalid_format",
                        "Invalid message format",
                    )
                    return
                await function(hass, connection, clean_message)

            validated.ws_schema = validator
            validated.ws_type = next(
                value
                for key, value in schema.items()
                if getattr(key, "schema", None) == "type"
            )
            return validated

        return decorate

    def async_response(function: Any) -> Any:
        return function

    def require_admin(function: Any) -> Any:
        @wraps(function)
        async def admin_only(hass: Any, connection: Any, message: Any) -> None:
            if connection.user.is_admin is not True:
                connection.send_error(
                    message["id"], "admin_required", "Administrator access is required"
                )
                return
            await function(hass, connection, message)

        return admin_only

    def async_register_command(hass: Any, command: Any) -> None:
        commands.append(command)

    frontend.async_remove_panel = async_remove_panel
    panel_custom.async_register_panel = async_register_panel
    websocket_api.websocket_command = websocket_command
    websocket_api.require_admin = require_admin
    websocket_api.async_response = async_response
    websocket_api.async_register_command = async_register_command
    websocket_api.ActiveConnection = object
    components.frontend = frontend
    components.panel_custom = panel_custom
    components.websocket_api = websocket_api

    for name, module in {
        "homeassistant": homeassistant,
        "homeassistant.components": components,
        "homeassistant.components.frontend": frontend,
        "homeassistant.components.panel_custom": panel_custom,
        "homeassistant.components.websocket_api": websocket_api,
        "homeassistant.components.http": http_module,
        "homeassistant.core": core,
        "homeassistant.config_entries": config_entries,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.selector": selector,
        "homeassistant.helpers.storage": storage_module,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    const = _module(f"{prefix}.const")
    const.DOMAIN = "glovo"
    const.CONF_ALLOW_ORDERING = "allow_ordering"
    const.CONF_ORDERING_ACKNOWLEDGED = "ordering_acknowledged"
    const.CONF_ALLOW_LIVE_CHECKOUT = "allow_live_checkout"
    const.CONF_LIVE_CHECKOUT_ACKNOWLEDGED = "live_checkout_acknowledged"
    const.CONF_REFRESH_TOKEN = "refresh_token"
    const.CONF_SCAN_INTERVAL = "scan_interval"
    const.CONF_TOKEN = "token"
    const.DEFAULT_SCAN_INTERVAL = 15
    const.MIN_SCAN_INTERVAL = 5
    const.MAX_SCAN_INTERVAL = 3600
    const.PLATFORMS = ["sensor", "binary_sensor", "device_tracker"]
    monkeypatch.setitem(sys.modules, const.__name__, const)

    coordinator_module = _module(f"{prefix}.coordinator")

    class GlovoDataUpdateCoordinator:
        def __init__(self, hass: Any, entry: Any, api_session: Any = None) -> None:
            self.hass = hass
            self.entry = entry
            self.api_session = api_session
            self.refreshed = False

        async def async_config_entry_first_refresh(self) -> None:
            self.refreshed = True

    coordinator_module.GlovoDataUpdateCoordinator = GlovoDataUpdateCoordinator
    coordinator_module.GlovoConfigEntry = Any
    monkeypatch.setitem(sys.modules, coordinator_module.__name__, coordinator_module)

    glovo_api = _module(f"{prefix}.glovo")

    class GlovoApiError(Exception):
        status = 401

    glovo_api.GlovoApiError = GlovoApiError
    glovo_api.build_token_json = lambda token: f"token-json:{token}"
    glovo_api.ensure_access_token = lambda token: ("access-fixture", token)
    glovo_api.single_attempt_authed_get = lambda method, access, path, query: {}
    setattr(
        glovo_api,
        "single_attempt_authed_location_get",
        lambda method, access, path, query, context: {},
    )
    glovo_api.single_attempt_authed_phase_mutation = (
        lambda method, access, path, query, body, context: {}
    )
    monkeypatch.setitem(sys.modules, glovo_api.__name__, glovo_api)

    for module_name in (
        "ordering_models",
        "ordering_contracts",
        "api_session",
        "ordering_account",
        "ordering_live_catalog",
        "ordering_remote_basket",
        "ordering_remote_basket_discovery",
        "ordering_live_quote",
        "ordering_live_checkout",
        "ordering_live_selection",
        "ordering_packages",
        "ordering_basket",
        "ordering_quote",
        "ordering_catalog",
        "ordering_journal",
        "ordering_state",
        "ordering_prep_authority",
        "ordering_basket_authority_store",
        "ordering_live_api",
        "ordering_live_flow",
        "ordering_runtime",
        "ordering_adapter",
        "ordering_manager",
        "ordering_surface",
        "ordering_ha",
    ):
        _load(f"{prefix}.{module_name}", GLOVO_ROOT / f"{module_name}.py")

    integration = _load(prefix, GLOVO_ROOT / "__init__.py", package=True)
    flow = _load(f"{prefix}.config_flow", GLOVO_ROOT / "config_flow.py")

    class FakeHttp:
        def __init__(self) -> None:
            self.static_paths: list[Any] = []

        async def async_register_static_paths(self, paths: list[Any]) -> None:
            self.static_paths.extend(paths)

    class FakeConfigEntries:
        def __init__(self) -> None:
            self.forwarded: list[Any] = []
            self.unloaded: list[Any] = []
            self.reloaded: list[str] = []
            self.updated: list[dict[str, Any]] = []

        async def async_forward_entry_setups(self, entry: Any, platforms: Any) -> None:
            self.forwarded.append((entry, platforms))

        async def async_unload_platforms(self, entry: Any, platforms: Any) -> bool:
            self.unloaded.append((entry, platforms))
            return True

        async def async_reload(self, entry_id: str) -> None:
            self.reloaded.append(entry_id)

        def async_update_entry(self, entry: Any, **kwargs: Any) -> None:
            self.updated.append(kwargs)
            if "data" in kwargs:
                entry.data = kwargs["data"]
            if "options" in kwargs:
                entry.options = kwargs["options"]
            if "version" in kwargs:
                entry.version = kwargs["version"]
            if "minor_version" in kwargs:
                entry.minor_version = kwargs["minor_version"]

    class FakeHass:
        def __init__(self) -> None:
            self.data: dict[str, Any] = {}
            self.http = FakeHttp()
            self.config_entries = FakeConfigEntries()

        async def async_add_executor_job(self, function: Any, *args: Any) -> Any:
            return function(*args)

    class FakeEntry:
        def __init__(self, options: dict[str, Any]) -> None:
            self.entry_id = "entry-one"
            self.options = options
            self.data = {"token": "old-token"}
            self.runtime_data: Any = None
            self.version = 1
            self.minor_version = 1
            self.listeners: list[Any] = []
            self.unload_callbacks: list[Any] = []

        def add_update_listener(self, listener: Any) -> Any:
            self.listeners.append(listener)
            return listener

        def async_on_unload(self, callback_value: Any) -> None:
            self.unload_callbacks.append(callback_value)

    class Connection:
        def __init__(self, *, admin: bool = True) -> None:
            self.user = SimpleNamespace(id="user-one", is_admin=admin)
            self.results: list[Any] = []
            self.errors: list[Any] = []

        def send_result(self, message_id: int, result: Any) -> None:
            self.results.append((message_id, result))

        def send_error(self, message_id: int, code: str, message: str) -> None:
            self.errors.append((message_id, code, message))

    return SimpleNamespace(
        prefix=prefix,
        integration=integration,
        flow=flow,
        Store=Store,
        FakeHass=FakeHass,
        FakeEntry=FakeEntry,
        Connection=Connection,
        panel_calls=panel_calls,
        removed_panels=removed_panels,
        commands=commands,
    )


def _recovery_commands(runtime: SimpleNamespace) -> set[str]:
    surface = sys.modules[f"{runtime.prefix}.ordering_surface"]
    return {
        surface.PUBLIC_OPERATION_COMMANDS["state"],
        surface.PUBLIC_OPERATION_COMMANDS["live/checkout_status"],
    } | set(surface.RECOVERY_COMMANDS)


def _enabled_commands(runtime: SimpleNamespace) -> set[str]:
    surface = sys.modules[f"{runtime.prefix}.ordering_surface"]
    return (
        set(surface.PUBLIC_OPERATION_COMMANDS.values())
        - {surface.PUBLIC_OPERATION_COMMANDS["live/execute_checkout"]}
    ) | set(surface.RECOVERY_COMMANDS)


def _paid_commands(runtime: SimpleNamespace) -> set[str]:
    surface = sys.modules[f"{runtime.prefix}.ordering_surface"]
    return set(surface.PUBLIC_OPERATION_COMMANDS.values()) | set(
        surface.RECOVERY_COMMANDS
    )


def _command_map(runtime: SimpleNamespace) -> dict[str, Any]:
    return {command.ws_type: command for command in runtime.commands}


def _call_ws(
    runtime: SimpleNamespace,
    hass: Any,
    command_type: str,
    message: dict[str, Any],
    *,
    admin: bool = True,
    user_id: str = "admin-one",
) -> Any:
    connection = runtime.Connection(admin=admin)
    connection.user.id = user_id
    run(_command_map(runtime)[command_type](hass, connection, {"id": 1, **message}))
    return connection


def _manual_record(
    *, integrity: bool = False, checkout_id: str | None = "checkout-private-synthetic"
) -> tuple[dict[str, Any], dict[str, Any]]:
    record = {
        "attempt_id": "attempt-live-ha",
        "state": "MANUAL_CHECK_REQUIRED",
        "created_at": 1_700_000_000.0,
        "updated_at": 1_700_000_010.0,
        "generation": 4,
        "amount_minor": 624000,
        "currency": "AMD",
        "execution_mode": "live",
        "request_fingerprint": "f" * 64,
        "provider_session_hash": "a" * 64,
        "basket_reference_hash": hashlib.sha256(
            b"basket-private-synthetic"
        ).hexdigest(),
        "provider_evidence_hash": None,
        "checkout_id": checkout_id,
        "dispatch_started_at": 1_700_000_005.0,
        "failure_class": "timeout",
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
    state = {
        "version": 2,
        "generation": 4,
        "manual_check_required": True,
        "integrity_fault": integrity,
        "manual_binding": {
            "attempt_id": record["attempt_id"],
            "record_revision": record["record_revision"],
            "generation": 4,
            "resolution": None,
        },
    }
    return record, state


def _seed_manual_recovery(
    runtime: SimpleNamespace,
    *,
    integrity: bool = False,
    checkout_id: str | None = "checkout-private-synthetic",
) -> None:
    record, state = _manual_record(integrity=integrity, checkout_id=checkout_id)
    runtime.Store.values["glovo.ordering_journal_v2.entry-one"] = {
        "version": 2,
        "records": [record],
    }
    runtime.Store.values["glovo.ordering_safety_v2.entry-one"] = state


def _preparation_record(
    *, state: str = "RECONCILIATION_REQUIRED", revision: int = 3
) -> dict[str, Any]:
    return {
        "attempt_id": "prep-live-ha",
        "generation": 4,
        "purpose": "basket_create",
        "expectation_hash": "d" * 64,
        "expected_category": "expected_present",
        "state": state,
        "record_revision": revision,
        "created_at": 1_700_000_000.0,
        "updated_at": 1_700_000_010.0,
        "dispatch_started_at": 1_700_000_005.0,
        "reconciliation_get_used": False,
        "outcome_category": (
            "provider_failure" if state == "PROVIDER_FAILED" else "ambiguous"
        ),
        "provider_evidence_hash": "e" * 64 if state == "PROVIDER_FAILED" else None,
        "private_checkout_reference": None,
    }


def _seed_preparation_recovery(
    runtime: SimpleNamespace, *, dispatching: bool = False
) -> None:
    state = "DISPATCHING" if dispatching else "RECONCILIATION_REQUIRED"
    revision = 2 if dispatching else 3
    record = _preparation_record(state=state, revision=revision)
    if dispatching:
        record["outcome_category"] = None
    runtime.Store.values["glovo.ordering_prep_authority_v1.entry-one"] = {
        "version": 1,
        "records": [record],
    }
    runtime.Store.values["glovo.ordering_safety_v2.entry-one"] = {
        "version": 2,
        "generation": 4,
        "manual_check_required": False,
        "integrity_fault": False,
        "preparation_binding": {
            "attempt_id": record["attempt_id"],
            "record_revision": revision,
            "generation": 4,
            "purpose": record["purpose"],
            "expectation_hash": record["expectation_hash"],
        },
    }


def _preparation_recovery_commands() -> set[str]:
    return {
        "glovo/ordering/state",
        "glovo/ordering/preparation_check",
        "glovo/ordering/prepare_preparation_resolution",
        "glovo/ordering/resolve_preparation_check",
    }


def _checkout_status_payload(status: str, **changes: Any) -> dict[str, Any]:
    order = None
    if status == "COMPLETED":
        order = {
            "id": "order-private-synthetic",
            "basketId": "basket-private-synthetic",
            "total": 624000,
            "currencyCode": "AMD",
        }
    checkout = {
        "checkoutId": "checkout-private-synthetic",
        "status": status,
        "action": None,
        "postAuthAction": None,
        "willDoMinimalAmountAuthAndVoid": False,
        "errors": [],
        "order": order,
        "payments": [],
    }
    checkout.update(changes)
    return {"checkout": checkout}


def _seed_integrity_fault(runtime: SimpleNamespace) -> None:
    runtime.Store.values["glovo.ordering_journal_v2.entry-one"] = {
        "version": 2,
        "records": [],
    }
    runtime.Store.values["glovo.ordering_safety_v2.entry-one"] = {
        "version": 2,
        "generation": 4,
        "manual_check_required": False,
        "integrity_fault": True,
    }


def test_deployed_legacy_mock_mixed_and_latched_states_repair_without_network(
    ha_runtime: SimpleNamespace,
) -> None:
    legacy = {"version": 1, "records": []}
    ha_runtime.Store.values["glovo.ordering_journal.entry-one"] = legacy
    ha_runtime.Store.values["glovo.ordering_safety_v2.entry-one"] = {
        "version": 2,
        "generation": 70,
        "manual_check_required": False,
        "integrity_fault": False,
    }
    options = {
        "allow_ordering": False,
        "ordering_acknowledged": False,
        "allow_live_checkout": False,
        "live_checkout_acknowledged": False,
    }
    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry(options)
    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
    manager_module = sys.modules[f"{ha_runtime.prefix}.ordering_manager"]
    user = manager_module.OrderingUser("admin-repair", True)
    assert run(entry.runtime_data.ordering_manager.async_state(user)) == {
        "enabled": False,
        "generation": 71,
        "mockOnly": True,
        "liveOrderingAvailable": False,
        "manualCheckRequired": False,
        "preparationRecoveryRequired": False,
        "integrityFault": False,
        "orderingBlocked": False,
        "liveCheckoutAvailable": False,
    }
    assert entry.runtime_data.ordering_surface is None
    assert ha_runtime.panel_calls == []
    assert ha_runtime.Store.values["glovo.ordering_journal.entry-one"] == legacy

    # Exact next boot after 9925411 persisted the conservative integrity latch.
    ha_runtime.Store.values["glovo.ordering_safety_v2.entry-one"] = {
        "version": 2,
        "generation": 71,
        "manual_check_required": False,
        "integrity_fault": True,
    }
    restarted_hass = ha_runtime.FakeHass()
    restarted_entry = ha_runtime.FakeEntry(options)
    assert run(
        ha_runtime.integration.async_setup_entry(restarted_hass, restarted_entry)
    ) is True
    repaired = run(restarted_entry.runtime_data.ordering_manager.async_state(user))
    assert repaired["generation"] == 72
    assert repaired["integrityFault"] is False
    assert repaired["manualCheckRequired"] is False
    assert repaired["enabled"] is False
    assert repaired["liveOrderingAvailable"] is False
    assert repaired["liveCheckoutAvailable"] is False


def test_entry_lifecycle_live_gate_panel_and_retained_websocket_shell(
    ha_runtime: SimpleNamespace,
) -> None:
    runtime = ha_runtime
    hass = runtime.FakeHass()
    entry = runtime.FakeEntry(
        {"scan_interval": 15, "allow_ordering": True, "ordering_acknowledged": True}
    )
    assert run(runtime.integration.async_setup_entry(hass, entry)) is True
    assert entry.runtime_data.ordering_manager.enabled is True
    generation = entry.runtime_data.ordering_manager.generation
    assert len(runtime.panel_calls) == 1
    panel = runtime.panel_calls[0]
    assert panel["require_admin"] is True
    assert panel["webcomponent_name"].startswith("glovo-ordering-panel-")
    asset_version = panel["webcomponent_name"].removeprefix(
        "glovo-ordering-panel-"
    )
    assert len(asset_version) == 12
    assert panel["module_url"] == (
        "/glovo_ordering/glovo-ordering-panel.js"
        f"?v={asset_version}&component={panel['webcomponent_name']}"
    )
    assert len(hass.http.static_paths) == 1
    assert hass.http.static_paths[0].cache_headers is False
    assert len(runtime.commands) == len(_enabled_commands(runtime))

    state_shell = runtime.commands[0]
    admin_connection = runtime.Connection(admin=True)
    run(state_shell(hass, admin_connection, {"id": 1, "type": "glovo/ordering/state"}))
    assert admin_connection.results[0][1]["mockOnly"] is False
    assert admin_connection.results[0][1]["liveOrderingAvailable"] is True
    assert admin_connection.results[0][1]["liveCheckoutAvailable"] is False

    non_admin = runtime.Connection(admin=False)
    run(state_shell(hass, non_admin, {"id": 2, "type": "glovo/ordering/state"}))
    assert non_admin.errors[0][1] == "admin_required"

    adopt_shell = _command_map(runtime)["glovo/ordering/live/basket_adopt"]
    denied_adopt = runtime.Connection(admin=False)
    run(
        adopt_shell(
            hass,
            denied_adopt,
            {
                "id": 20,
                "type": "glovo/ordering/live/basket_adopt",
                "generation": generation,
                "addressHandle": "address-local",
                "storeHandle": "store-local",
                "products": [
                    {"productHandle": "product-local", "quantity": 1, "options": []}
                ],
            },
        )
    )
    assert denied_adopt.errors[0][1] == "admin_required"
    for invalid_payload in (
        {
            "generation": generation,
            "addressKey": "address-local",
            "storeHandle": "store-local",
            "products": [],
        },
        {
            "generation": generation,
            "addressHandle": "address-local",
            "storeHandle": "store-local",
            "products": [],
            "providerId": "provider-private-value",
        },
    ):
        rejected = runtime.Connection(admin=True)
        run(
            adopt_shell(
                hass,
                rejected,
                {
                    "id": 21,
                    "type": "glovo/ordering/live/basket_adopt",
                    **invalid_payload,
                },
            )
        )
        assert rejected.errors == [(21, "invalid_format", "Invalid message format")]
        assert "provider-private-value" not in repr(rejected.errors)

    # Options mutate before the update listener/reload: the retained active handler
    # reads entry.options live and rejects immediately.
    entry.options = {
        "scan_interval": 15,
        "allow_ordering": True,
        "ordering_acknowledged": False,
    }
    raced = runtime.Connection(admin=True)
    run(state_shell(hass, raced, {"id": 3, "type": "glovo/ordering/state"}))
    assert raced.results[0][1]["enabled"] is False
    assert raced.results[0][1]["generation"] == generation

    run(entry.listeners[0](hass, entry))
    assert hass.config_entries.reloaded == [entry.entry_id]
    assert "glovo-ordering" in runtime.removed_panels
    assert entry.runtime_data.ordering_manager.generation > generation

    # HA retains command shells by design, but removing callables makes them fail closed.
    retained = runtime.Connection(admin=True)
    run(state_shell(hass, retained, {"id": 4, "type": "glovo/ordering/state"}))
    assert retained.errors[0][1] == "ordering_disabled"

    old_generation = entry.runtime_data.ordering_manager.generation
    entry.options = {
        "scan_interval": 15,
        "allow_ordering": True,
        "ordering_acknowledged": True,
    }
    assert run(runtime.integration.async_setup_entry(hass, entry)) is True
    assert entry.runtime_data.ordering_manager.generation == old_generation
    assert entry.runtime_data.ordering_manager.enabled is True
    assert len(runtime.commands) == len(_enabled_commands(runtime))
    assert run(runtime.integration.async_unload_entry(hass, entry)) is True
    assert entry.runtime_data.ordering_manager.enabled is False
    assert entry.runtime_data.ordering_manager.generation > old_generation


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"allow_ordering": True},
        {"allow_ordering": True, "ordering_acknowledged": False},
        {"allow_ordering": 1, "ordering_acknowledged": True},
    ],
)
def test_disabled_or_unacknowledged_entry_keeps_tracking_without_panel(
    ha_runtime: SimpleNamespace, options: dict[str, Any]
) -> None:
    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry(options)
    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
    assert entry.runtime_data.refreshed is True
    assert entry.runtime_data.ordering_manager.enabled is False
    assert entry.runtime_data.ordering_surface is None
    assert hass.config_entries.forwarded
    assert not ha_runtime.panel_calls


def test_production_composition_keeps_mutation_transport_and_facade_absent_when_gated_off(
    ha_runtime: SimpleNamespace,
) -> None:
    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry({"allow_ordering": False, "ordering_acknowledged": True})
    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
    runtime = entry.runtime_data
    assert runtime.api_session._mutation_transport is None
    assert runtime.ordering_runtime.flow is None
    assert runtime.ordering_manager.live_ordering_available is False


@pytest.mark.parametrize("dispatching", [False, True])
def test_home7_unresolved_preparation_has_reachable_fail_closed_recovery_surface(
    ha_runtime: SimpleNamespace, dispatching: bool
) -> None:
    _seed_preparation_recovery(ha_runtime, dispatching=dispatching)
    options = {
        "allow_ordering": True,
        "ordering_acknowledged": True,
        "allow_live_checkout": False,
        "live_checkout_acknowledged": False,
    }
    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry(options)
    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True

    assert set(_command_map(ha_runtime)) == _preparation_recovery_commands()
    assert len(ha_runtime.panel_calls) == 1
    assert entry.runtime_data.api_session._mutation_transport is None
    assert entry.runtime_data.ordering_runtime.flow is None
    public = _call_ws(
        ha_runtime, hass, "glovo/ordering/state", {"type": "glovo/ordering/state"}
    ).results[0][1]
    assert public["enabled"] is False
    assert public["orderingBlocked"] is True
    assert public["manualCheckRequired"] is False
    assert public["preparationRecoveryRequired"] is True
    assert public["liveOrderingAvailable"] is False
    assert public["liveCheckoutAvailable"] is False
    assert "glovo/ordering/live/basket_set" not in _command_map(ha_runtime)
    assert "glovo/ordering/live/basket_clear" not in _command_map(ha_runtime)
    assert "glovo/ordering/live/execute_checkout" not in _command_map(ha_runtime)

    retained = _command_map(ha_runtime)["glovo/ordering/preparation_check"]
    assert run(ha_runtime.integration.async_unload_entry(hass, entry)) is True
    closed = ha_runtime.Connection(admin=True)
    run(
        retained(
            hass,
            closed,
            {"id": 8, "type": "glovo/ordering/preparation_check"},
        )
    )
    assert closed.errors[0][1] == "ordering_disabled"

    restarted_hass = ha_runtime.FakeHass()
    restarted_entry = ha_runtime.FakeEntry(options)
    assert run(
        ha_runtime.integration.async_setup_entry(restarted_hass, restarted_entry)
    ) is True
    assert set(_command_map(ha_runtime)) == _preparation_recovery_commands()
    restored = _call_ws(
        ha_runtime,
        restarted_hass,
        "glovo/ordering/preparation_check",
        {"type": "glovo/ordering/preparation_check"},
    )
    assert restored.results[0][1]["state"] == "RECONCILIATION_REQUIRED"
    assert restored.results[0][1]["purposeCategory"] == "basket"
    assert "attemptRef" not in restored.results[0][1]
    assert "expectation" not in json.dumps(restored.results[0][1]).lower()


@pytest.mark.parametrize(
    ("resolution", "terminal_state"),
    [
        ("found_succeeded", "OPERATOR_ATTESTED_SUCCEEDED"),
        ("found_failed_or_cancelled", "OPERATOR_ATTESTED_FAILED"),
    ],
)
def test_home7_preparation_attestation_is_admin_challenge_bound_one_shot_and_durable(
    ha_runtime: SimpleNamespace, resolution: str, terminal_state: str
) -> None:
    _seed_preparation_recovery(ha_runtime)
    options = {
        "allow_ordering": True,
        "ordering_acknowledged": True,
        "allow_live_checkout": False,
        "live_checkout_acknowledged": False,
    }
    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry(options)
    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
    generation = entry.runtime_data.ordering_manager.generation
    check = _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/preparation_check",
        {"type": "glovo/ordering/preparation_check"},
    ).results[0][1]
    prepare = {
        "type": "glovo/ordering/prepare_preparation_resolution",
        "expectedGeneration": check["generation"],
        "expectedRecordRevision": check["recordRevision"],
        "expectedState": check["state"],
        "resolution": resolution,
    }
    denied = _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/prepare_preparation_resolution",
        prepare,
        admin=False,
    )
    assert denied.errors[0][1] == "admin_required"
    for spoof in (
        {"expectedGeneration": check["generation"] - 1},
        {"expectedRecordRevision": check["recordRevision"] - 1},
        {"expectedState": "OPERATOR_ATTESTED_UNKNOWN"},
        {"resolution": "provider_succeeded"},
        {"unexpected": "private"},
    ):
        rejected = _call_ws(
            ha_runtime,
            hass,
            "glovo/ordering/prepare_preparation_resolution",
            {**prepare, **spoof},
        )
        assert rejected.errors

    prepared = _call_ws(
        ha_runtime, hass, "glovo/ordering/prepare_preparation_resolution", prepare
    ).results[0][1]
    resolve = {
        "type": "glovo/ordering/resolve_preparation_check",
        **{key: value for key, value in prepare.items() if key != "type"},
        "challenge": prepared["challenge"],
        "acknowledged": True,
    }
    wrong_owner = _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/resolve_preparation_check",
        resolve,
        user_id="admin-two",
    )
    assert wrong_owner.errors[0][1] == "invalid_preparation_resolution"

    prepared = _call_ws(
        ha_runtime, hass, "glovo/ordering/prepare_preparation_resolution", prepare
    ).results[0][1]
    resolve["challenge"] = prepared["challenge"]
    result = _call_ws(
        ha_runtime, hass, "glovo/ordering/resolve_preparation_check", resolve
    )
    assert result.results[0][1] == {
        "resolved": True,
        "preparationRecoveryRequired": False,
        "reloadRequired": True,
    }
    assert entry.runtime_data.ordering_manager.generation > generation
    assert entry.runtime_data.api_session._mutation_transport is None
    assert ha_runtime.Store.values["glovo.ordering_prep_authority_v1.entry-one"][
        "records"
    ][0]["state"] == terminal_state
    assert "preparation_binding" not in ha_runtime.Store.values[
        "glovo.ordering_safety_v2.entry-one"
    ]
    replay = _call_ws(
        ha_runtime, hass, "glovo/ordering/resolve_preparation_check", resolve
    )
    assert replay.errors[0][1] == "invalid_preparation_resolution"

    encoded = json.dumps(
        [check, prepared, result.results[0][1]], sort_keys=True
    ).lower()
    for forbidden in (
        "prep-live-ha",
        "expectation",
        "provider",
        "address",
        "coordinate",
        "product",
        "price",
        "payload",
        "d" * 64,
    ):
        assert forbidden not in encoded

    restarted_hass = ha_runtime.FakeHass()
    restarted_entry = ha_runtime.FakeEntry(options)
    assert run(
        ha_runtime.integration.async_setup_entry(restarted_hass, restarted_entry)
    ) is True
    restarted_state = _call_ws(
        ha_runtime,
        restarted_hass,
        "glovo/ordering/state",
        {"type": "glovo/ordering/state"},
    ).results[0][1]
    assert restarted_state["preparationRecoveryRequired"] is False
    assert restarted_state["orderingBlocked"] is False
    assert "glovo/ordering/live/basket_set" in _command_map(ha_runtime)
    assert "glovo/ordering/live/execute_checkout" not in _command_map(ha_runtime)


def test_home7_still_unknown_attestation_is_durable_and_remains_blocked(
    ha_runtime: SimpleNamespace,
) -> None:
    _seed_preparation_recovery(ha_runtime)
    options = {
        "allow_ordering": True,
        "ordering_acknowledged": True,
        "allow_live_checkout": False,
        "live_checkout_acknowledged": False,
    }
    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry(options)
    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
    check = _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/preparation_check",
        {"type": "glovo/ordering/preparation_check"},
    ).results[0][1]
    prepare = {
        "type": "glovo/ordering/prepare_preparation_resolution",
        "expectedGeneration": check["generation"],
        "expectedRecordRevision": check["recordRevision"],
        "expectedState": check["state"],
        "resolution": "still_unknown",
    }
    challenge = _call_ws(
        ha_runtime, hass, "glovo/ordering/prepare_preparation_resolution", prepare
    ).results[0][1]["challenge"]
    result = _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/resolve_preparation_check",
        {
            "type": "glovo/ordering/resolve_preparation_check",
            **{key: value for key, value in prepare.items() if key != "type"},
            "challenge": challenge,
            "acknowledged": True,
        },
    ).results[0][1]
    assert result == {
        "resolved": False,
        "preparationRecoveryRequired": True,
        "reloadRequired": False,
    }
    assert ha_runtime.Store.values["glovo.ordering_prep_authority_v1.entry-one"][
        "records"
    ][0]["state"] == "OPERATOR_ATTESTED_UNKNOWN"
    assert "preparation_binding" in ha_runtime.Store.values[
        "glovo.ordering_safety_v2.entry-one"
    ]
    blocked = _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/prepare_preparation_resolution",
        {
            **prepare,
            "expectedGeneration": entry.runtime_data.ordering_manager.generation,
            "expectedRecordRevision": check["recordRevision"] + 1,
            "expectedState": "OPERATOR_ATTESTED_UNKNOWN",
        },
    )
    assert blocked.errors[0][1] == "invalid_format"

    restarted_hass = ha_runtime.FakeHass()
    restarted_entry = ha_runtime.FakeEntry(options)
    assert run(
        ha_runtime.integration.async_setup_entry(restarted_hass, restarted_entry)
    ) is True
    assert set(_command_map(ha_runtime)) == _preparation_recovery_commands()
    restored = _call_ws(
        ha_runtime,
        restarted_hass,
        "glovo/ordering/state",
        {"type": "glovo/ordering/state"},
    ).results[0][1]
    assert restored["preparationRecoveryRequired"] is True
    assert restored["orderingBlocked"] is True


def test_home7_simultaneous_manual_and_preparation_recovery_preserves_both_surfaces(
    ha_runtime: SimpleNamespace,
) -> None:
    _seed_preparation_recovery(ha_runtime)
    preparation_binding = ha_runtime.Store.values[
        "glovo.ordering_safety_v2.entry-one"
    ]["preparation_binding"]
    _seed_manual_recovery(ha_runtime)
    ha_runtime.Store.values["glovo.ordering_safety_v2.entry-one"][
        "preparation_binding"
    ] = preparation_binding
    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry(
        {
            "allow_ordering": True,
            "ordering_acknowledged": True,
            "allow_live_checkout": True,
            "live_checkout_acknowledged": True,
        }
    )
    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
    commands = set(_command_map(ha_runtime))
    assert commands == _preparation_recovery_commands() | _recovery_commands(ha_runtime)
    assert "glovo/ordering/live/execute_checkout" not in commands
    assert "glovo/ordering/live/basket_set" not in commands
    assert _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/manual_checks",
        {"type": "glovo/ordering/manual_checks"},
    ).results
    assert _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/preparation_check",
        {"type": "glovo/ordering/preparation_check"},
    ).results


def test_home7_terminal_provider_failure_does_not_create_false_preparation_recovery(
    ha_runtime: SimpleNamespace,
) -> None:
    ha_runtime.Store.values["glovo.ordering_prep_authority_v1.entry-one"] = {
        "version": 1,
        "records": [_preparation_record(state="PROVIDER_FAILED", revision=3)],
    }
    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry(
        {
            "allow_ordering": True,
            "ordering_acknowledged": True,
            "allow_live_checkout": False,
            "live_checkout_acknowledged": False,
        }
    )
    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
    state = _call_ws(
        ha_runtime, hass, "glovo/ordering/state", {"type": "glovo/ordering/state"}
    ).results[0][1]
    assert state["preparationRecoveryRequired"] is False
    assert state["orderingBlocked"] is False
    assert entry.runtime_data.ordering_manager.live_ordering_available is True
    assert "glovo/ordering/live/basket_set" in _command_map(ha_runtime)


def test_checkout_gate_alone_cannot_construct_production_preparation_facade(
    ha_runtime: SimpleNamespace,
) -> None:
    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry(
        {
            "allow_ordering": False,
            "ordering_acknowledged": False,
            "allow_live_checkout": True,
            "live_checkout_acknowledged": True,
        }
    )
    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
    runtime = entry.runtime_data
    assert runtime.api_session._mutation_transport is None
    assert runtime.ordering_runtime.flow is None
    assert runtime.ordering_runtime.live_checkout_available is False


def test_clean_preparation_authority_wires_exact_one_attempt_transport_and_no_final_adapter(
    ha_runtime: SimpleNamespace,
) -> None:
    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry(
        {"allow_ordering": True, "ordering_acknowledged": True}
    )
    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
    runtime = entry.runtime_data
    glovo_api = sys.modules[f"{ha_runtime.prefix}.glovo"]
    assert runtime.api_session._mutation_transport is glovo_api.single_attempt_authed_phase_mutation
    assert runtime.ordering_runtime.flow is not None
    assert runtime.ordering_manager.live_ordering_available is True
    assert runtime.ordering_runtime.live_checkout_available is False
    state = _call_ws(
        ha_runtime, hass, "glovo/ordering/state", {"type": "glovo/ordering/state"}
    ).results[0][1]
    assert state["mockOnly"] is False
    assert state["liveOrderingAvailable"] is True
    assert state["liveCheckoutAvailable"] is False


def test_home8_paid_gates_compose_exact_final_adapter_and_register_admin_submit(
    ha_runtime: SimpleNamespace,
) -> None:
    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry(
        {
            "allow_ordering": True,
            "ordering_acknowledged": True,
            "allow_live_checkout": True,
            "live_checkout_acknowledged": True,
        }
    )
    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
    runtime = entry.runtime_data
    checkout = sys.modules[f"{ha_runtime.prefix}.ordering_live_checkout"]
    assert isinstance(
        runtime.ordering_runtime.flow._final_adapter,
        checkout.ProductionFinalCheckoutAdapter,
    )
    assert callable(runtime.ordering_runtime.flow._final_request_factory)
    assert runtime.ordering_manager.live_checkout_available is True
    assert set(_command_map(ha_runtime)) == _paid_commands(ha_runtime)
    state = _call_ws(
        ha_runtime, hass, "glovo/ordering/state", {"type": "glovo/ordering/state"}
    ).results[0][1]
    assert state["liveCheckoutAvailable"] is True


def test_home8_preparation_only_constructs_zero_final_checkout_capability(
    ha_runtime: SimpleNamespace,
) -> None:
    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry(
        {
            "allow_ordering": True,
            "ordering_acknowledged": True,
            "allow_live_checkout": False,
            "live_checkout_acknowledged": False,
        }
    )
    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
    flow = entry.runtime_data.ordering_runtime.flow
    assert flow is not None
    assert flow._final_adapter is None
    assert flow._final_request_factory is None
    assert "glovo/ordering/live/execute_checkout" not in _command_map(ha_runtime)


def test_deterministic_preparation_rejection_keeps_closed_diagnostic_and_no_latch(
    ha_runtime: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    live_api = sys.modules[f"{ha_runtime.prefix}.ordering_live_api"]
    remote = sys.modules[f"{ha_runtime.prefix}.ordering_remote_basket"]
    api = sys.modules[f"{ha_runtime.prefix}.api_session"]
    outcomes: list[dict[str, Any]] = []

    class Authority:
        loaded = True
        unresolved: tuple[Any, ...] = ()

        async def async_acquire(self, **kwargs: Any) -> Any:
            return SimpleNamespace(attempt_id=kwargs["attempt_id"], record_revision=1)

        async def async_record_outcome(self, **kwargs: Any) -> None:
            outcomes.append(kwargs)

    facade = object.__new__(live_api.LiveOrderingFacade)
    facade._preparation_authority = Authority()
    facade._preparation_attempt_source = lambda: "prep-" + "a" * 32

    async def reject() -> None:
        raise remote.RemoteBasketRejected(api.MutationPurpose.CREATE_BASKET, 422)

    async def scenario() -> None:
        with live_api._basket_sync_stage("provider_mutation"):
            await facade._async_preparation_mutation(
                operation="live/basket_set",
                generation=1,
                purpose_name="basket_create",
                expected_category="expected_present",
                expected={"closed": True},
                invoke=reject,
            )

    with caplog.at_level("WARNING"):
        with pytest.raises(live_api.PreparationMutationRejected) as raised:
            run(scenario())
    assert raised.value.status == 422
    assert raised.value.category == "provider_rejection"
    assert str(raised.value) == "live ordering request is unavailable or invalid"
    assert outcomes[-1]["outcome_category"] == "provider_failure"
    assert outcomes[-1]["provider_evidence_hash"]
    assert Authority.unresolved == ()
    log = caplog.text
    assert "stage=provider_mutation" in log
    assert "class=PreparationMutationRejected" in log
    assert "category=provider_rejection" in log
    assert "status=422" in log
    for forbidden in (
        "prep-",
        "/v1/",
        "customer",
        "latitude",
        "longitude",
        "payload",
        "Bearer",
    ):
        assert forbidden not in log


@pytest.mark.parametrize(
    ("provider_result", "expected_state", "expected_status"),
    [
        ("glovo-422", "PROVIDER_FAILED", 422),
        ("glovo-418", "RECONCILIATION_REQUIRED", None),
        ("glovo-500", "RECONCILIATION_REQUIRED", None),
        ("transport", "RECONCILIATION_REQUIRED", None),
        ("malformed", "RECONCILIATION_REQUIRED", None),
    ],
)
def test_production_shaped_preparation_outcomes_are_conservative_private_and_one_call(
    ha_runtime: SimpleNamespace,
    caplog: pytest.LogCaptureFixture,
    provider_result: str,
    expected_state: str,
    expected_status: int | None,
) -> None:
    from test_ordering_remote_basket_quotes import intent_payload

    prefix = ha_runtime.prefix
    live_api = sys.modules[f"{prefix}.ordering_live_api"]
    remote = sys.modules[f"{prefix}.ordering_remote_basket"]
    api = sys.modules[f"{prefix}.api_session"]
    prep = sys.modules[f"{prefix}.ordering_prep_authority"]
    state_module = sys.modules[f"{prefix}.ordering_state"]
    real_glovo = _load(
        f"{prefix}.real_glovo_error_{provider_result.replace('-', '_')}",
        GLOVO_ROOT / "glovo.py",
    )
    private = "private-response-body https://private.invalid/customer/42"
    calls = 0

    async def mutate(*args: Any) -> Any:
        nonlocal calls
        calls += 1
        if provider_result.startswith("glovo-"):
            raise real_glovo.GlovoApiError(int(provider_result.removeprefix("glovo-")), private)
        if provider_result == "transport":
            raise RuntimeError(private)
        return {"malformedPrivate": private}

    session = api.SerializedApiSession(
        token_source=lambda: "stored-token",
        persist_token=lambda value: None,
        ensure_token=lambda value: ("access-ok", value),
        transport=lambda *args: {},
        mutation_transport=mutate,
    )
    client = remote.RemoteBasketClient(session)
    state_storage = state_module.MemoryOrderingStateStorage()
    durable_state = state_module.DurableOrderingState(state_storage)
    prep_storage = prep.MemoryPreparationStorage()
    authority = prep.PreparationMutationAuthority(
        prep_storage, clock=lambda: 500.0, durable_state=durable_state
    )
    facade = object.__new__(live_api.LiveOrderingFacade)
    facade._preparation_authority = authority
    facade._preparation_attempt_source = lambda: "prep-" + "b" * 32
    intent = remote.parse_basket_intent(intent_payload())
    location = api.DeliveryLocation("AM", "YRV", 40.177, 44.513)

    async def scenario() -> None:
        await durable_state.async_load()
        await authority.async_load()
        with live_api._basket_sync_stage("provider_mutation"):
            await facade._async_preparation_mutation(
                operation="live/basket_set",
                generation=1,
                purpose_name="basket_create",
                expected_category="expected_present",
                expected=intent,
                invoke=lambda: client.async_create(intent, location),
            )

    error_type = (
        live_api.PreparationMutationRejected
        if expected_state == "PROVIDER_FAILED"
        else live_api.PublicContractError
    )
    with caplog.at_level("WARNING"):
        with pytest.raises(error_type) as raised:
            run(scenario())

    assert calls == 1
    assert type(raised.value) is error_type
    assert getattr(raised.value, "status", None) == expected_status
    record = authority.records[-1]
    assert record.state.value == expected_state
    assert prep_storage.data["records"][-1]["state"] == expected_state
    if expected_state == "PROVIDER_FAILED":
        assert record.outcome_category == "provider_failure"
        assert record.provider_evidence_hash == hashlib.sha256(
            json.dumps(
                {
                    "category": "provider_rejection",
                    "class": "RemoteBasketRejected",
                    "purpose": "basket_create",
                    "status": 422,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        assert durable_state.preparation_binding is None
    else:
        assert record.outcome_category == "ambiguous"
        assert record.provider_evidence_hash is None
        assert durable_state.preparation_binding is not None
        assert state_storage.data["preparation_binding"]["attempt_id"] == record.attempt_id
    exposed = "\n".join(
        (
            repr(raised.value),
            str(raised.value),
            caplog.text,
            repr(prep_storage.data),
            repr(state_storage.data),
        )
    )
    assert private not in exposed
    assert "private.invalid" not in exposed


@pytest.mark.parametrize("error_kind", ["class_name", "rejected_without_status"])
def test_unproven_exception_cannot_spoof_preparation_outcome(
    ha_runtime: SimpleNamespace, error_kind: str
) -> None:
    live_api = sys.modules[f"{ha_runtime.prefix}.ordering_live_api"]
    remote = sys.modules[f"{ha_runtime.prefix}.ordering_remote_basket"]
    api = sys.modules[f"{ha_runtime.prefix}.api_session"]
    outcomes: list[dict[str, Any]] = []

    class Authority:
        loaded = True
        unresolved: tuple[Any, ...] = ()

        async def async_acquire(self, **kwargs: Any) -> Any:
            return SimpleNamespace(attempt_id=kwargs["attempt_id"], record_revision=1)

        async def async_record_outcome(self, **kwargs: Any) -> None:
            outcomes.append(kwargs)

    class SomethingAmbiguous(RuntimeError):
        pass

    facade = object.__new__(live_api.LiveOrderingFacade)
    facade._preparation_authority = Authority()
    facade._preparation_attempt_source = lambda: "prep-" + "c" * 32
    error = (
        SomethingAmbiguous("private")
        if error_kind == "class_name"
        else remote.RemoteBasketRejected(api.MutationPurpose.CREATE_BASKET, None)
    )

    async def scenario() -> None:
        await facade._async_preparation_mutation(
            operation="live/basket_set",
            generation=1,
            purpose_name="basket_create",
            expected_category="expected_present",
            expected={"closed": True},
            invoke=lambda: (_ for _ in ()).throw(error),
        )

    with pytest.raises(live_api.PublicContractError):
        run(scenario())
    assert outcomes == [
        {
            "attempt_id": "prep-" + "c" * 32,
            "expected_revision": 1,
            "outcome_category": "ambiguous",
        }
    ]


def test_cancelled_preparation_dispatch_durably_latches_before_cancellation_surfaces(
    ha_runtime: SimpleNamespace,
) -> None:
    prefix = ha_runtime.prefix
    live_api = sys.modules[f"{prefix}.ordering_live_api"]
    prep = sys.modules[f"{prefix}.ordering_prep_authority"]
    state_module = sys.modules[f"{prefix}.ordering_state"]
    durable_state = state_module.DurableOrderingState(
        state_module.MemoryOrderingStateStorage()
    )
    authority = prep.PreparationMutationAuthority(
        prep.MemoryPreparationStorage(), clock=lambda: 500.0, durable_state=durable_state
    )
    facade = object.__new__(live_api.LiveOrderingFacade)
    facade._preparation_authority = authority
    facade._preparation_attempt_source = lambda: "prep-" + "d" * 32

    async def cancelled() -> None:
        raise asyncio.CancelledError

    async def scenario() -> None:
        await durable_state.async_load()
        await authority.async_load()
        await facade._async_preparation_mutation(
            operation="live/basket_clear",
            generation=1,
            purpose_name="basket_delete",
            expected_category="expected_absent",
            expected={"closed": True},
            invoke=cancelled,
        )

    with pytest.raises(asyncio.CancelledError):
        run(scenario())
    assert authority.records[-1].state.value == "RECONCILIATION_REQUIRED"
    assert durable_state.preparation_binding is not None


@pytest.mark.parametrize("provider_succeeded", [False, True])
def test_preparation_outcome_persistence_fault_is_not_erased_after_dispatch(
    ha_runtime: SimpleNamespace, provider_succeeded: bool
) -> None:
    prefix = ha_runtime.prefix
    live_api = sys.modules[f"{prefix}.ordering_live_api"]
    prep = sys.modules[f"{prefix}.ordering_prep_authority"]
    state_module = sys.modules[f"{prefix}.ordering_state"]

    class FailingOutcomeStorage(prep.MemoryPreparationStorage):
        fail = False

        async def async_save(self, data: dict[str, Any]) -> None:
            if self.fail:
                raise RuntimeError("private storage detail")
            await super().async_save(data)

    storage = FailingOutcomeStorage()
    durable_state = state_module.DurableOrderingState(
        state_module.MemoryOrderingStateStorage()
    )
    authority = prep.PreparationMutationAuthority(
        storage, clock=lambda: 500.0, durable_state=durable_state
    )
    facade = object.__new__(live_api.LiveOrderingFacade)
    facade._preparation_authority = authority
    facade._preparation_attempt_source = lambda: "prep-" + "e" * 32

    async def invoke() -> object:
        storage.fail = True
        if not provider_succeeded:
            raise RuntimeError("private transport detail")
        return object()

    async def scenario() -> None:
        await durable_state.async_load()
        await authority.async_load()
        await facade._async_preparation_mutation(
            operation="live/basket_set",
            generation=1,
            purpose_name="basket_create",
            expected_category="expected_present",
            expected={"closed": True},
            invoke=invoke,
        )

    with pytest.raises(prep.PreparationAuthorityFault):
        run(scenario())
    assert authority.integrity_fault is True
    assert durable_state.integrity_fault is True
    assert authority.records[-1].state.value == "DISPATCHING"


def test_basket_context_is_repr_private_and_address_mismatch_stops_before_delete_dispatch(
    ha_runtime: SimpleNamespace,
) -> None:
    live_api = sys.modules[f"{ha_runtime.prefix}.ordering_live_api"]
    api = sys.modules[f"{ha_runtime.prefix}.api_session"]
    location = api.DeliveryLocation("AM", "YRV", 40.177, 44.513)
    state = live_api._BasketState(
        generation=1,
        revision=1,
        store_handle="store-local",
        address_handle="address-local",
        currency="AMD",
        store_label="Fixture Kitchen",
        lines=(),
        snapshot=object(),
        store=object(),
        address_fingerprint="a" * 64,
        delivery_location=location,
    )
    assert "40.177" not in repr(state)
    assert "44.513" not in repr(state)

    class Account:
        @staticmethod
        def resolve_address(*args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(
                canonical_fingerprint="b" * 64,
                country_code="AM",
                city_code="YRV",
                latitude=40.178,
                longitude=44.514,
            )

    class Baskets:
        calls = 0

        async def async_delete(self, *args: Any, **kwargs: Any) -> None:
            self.calls += 1

    facade = object.__new__(live_api.LiveOrderingFacade)
    facade._account = Account()
    facade._baskets = Baskets()
    facade._basket = {"admin": state}
    facade._quote = {}
    facade._generation = 1
    facade._confirmations = SimpleNamespace(invalidate=lambda owner: None)

    with pytest.raises(live_api.PublicContractError):
        run(
            facade.async_dispatch(
                owner="admin",
                operation="live/basket_clear",
                request={"generation": 1, "expectedRevision": 1},
            )
        )
    assert facade._baskets.calls == 0


def test_admin_fixture_transport_reaches_production_preparation_path_without_final_submit(
    ha_runtime: SimpleNamespace,
) -> None:
    """Exercise canonical HA/WS composition with a method/path/body ledger only."""
    from test_ordering_live_get_clients import (
        address_payload,
        menu_payload,
        payment_payload,
        store_payload,
    )
    from test_ordering_remote_basket_quotes import basket_payload, quote_response

    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry(
        {"allow_ordering": True, "ordering_acknowledged": True}
    )
    ledger: list[tuple[str, str, dict[str, str], Any]] = []
    location_contexts: list[dict[str, str]] = []
    read_responses = {
        "/customer_profile/api/v1/address_book/me/addresses": [
            address_payload(), address_payload()
        ],
        "/v3/stores/fixture-kitchen": [
            store_payload(),
            store_payload(),
            store_payload(),
            store_payload(),
            store_payload(),
            store_payload(),
        ],
        "/v4/stores/71/addresses/81/content/main": [menu_payload()],
        "/v3/me": [{"id": 42}, {"id": 42}],
        "/v1/authenticated/customers/42/baskets": [[]],
        "/v4/payment_methods": [payment_payload(), payment_payload()],
    }
    mutation_responses = [basket_payload(), quote_response()]
    glovo_api = sys.modules[f"{ha_runtime.prefix}.glovo"]

    def get(method: str, access: str, path: str, query: dict[str, str]) -> Any:
        assert method == "GET" and access == "access-fixture"
        ledger.append((method, path, copy.deepcopy(query), None))
        return copy.deepcopy(read_responses[path].pop(0))

    def location_get(
        method: str,
        access: str,
        path: str,
        query: dict[str, str],
        context: dict[str, str],
    ) -> Any:
        location_contexts.append(copy.deepcopy(context))
        return get(method, access, path, query)

    def mutate(
        method: str,
        access: str,
        path: str,
        query: dict[str, str],
        body: Any,
        context: dict[str, str],
    ) -> Any:
        assert access == "access-fixture"
        raw = ha_runtime.Store.values["glovo.ordering_prep_authority_v1.entry-one"]
        assert raw["records"][-1]["state"] == "DISPATCHING"
        location_contexts.append(copy.deepcopy(context))
        ledger.append((method, path, copy.deepcopy(query), copy.deepcopy(body)))
        response = copy.deepcopy(mutation_responses.pop(0))
        if path.endswith("/baskets"):
            # The provider response must echo the exact menu-derived customization
            # selected in the submitted intent; the standalone basket fixture uses
            # a different synthetic option group for its parser-only tests.
            response["products"][0]["customizations"] = copy.deepcopy(
                body["products"][0]["customizations"]
            )
        return response

    setattr(glovo_api, "single_attempt_authed_get", get)
    setattr(glovo_api, "single_attempt_authed_location_get", location_get)
    glovo_api.single_attempt_authed_phase_mutation = mutate
    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
    state = _call_ws(
        ha_runtime, hass, "glovo/ordering/state", {"type": "glovo/ordering/state"}
    ).results[0][1]
    generation = state["generation"]
    addresses = _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/live/addresses",
        {"type": "glovo/ordering/live/addresses", "generation": generation},
    ).results[0][1]["addresses"]
    stores = _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/live/stores",
        {
            "type": "glovo/ordering/live/stores",
            "generation": generation,
            "storeSlug": "fixture-kitchen",
            "addressHandle": addresses[0]["key"],
        },
    ).results[0][1]["stores"]
    menu = _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/live/store_menu",
        {
            "type": "glovo/ordering/live/store_menu",
            "generation": generation,
            "storeHandle": stores[0]["storeHandle"],
            "addressHandle": addresses[0]["key"],
        },
    ).results[0][1]
    product = menu["products"][0]
    assert _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/live/basket",
        {"type": "glovo/ordering/live/basket", "generation": generation},
    ).results[0][1] == {"status": "unknown"}
    selection = [
        {
            "productHandle": product["productHandle"],
            "quantity": 2,
            "options": [],
        }
    ]
    adoption_response = _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/live/basket_adopt",
        {
            "type": "glovo/ordering/live/basket_adopt",
            "generation": generation,
            "storeHandle": stores[0]["storeHandle"],
            "addressHandle": addresses[0]["key"],
            "products": selection,
        },
    )
    assert adoption_response.errors == [], ledger
    assert adoption_response.results[0][1]["status"] == "absent_verified"
    assert not [item for item in ledger if item[0] != "GET"]
    basket_response = _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/live/basket_set",
        {
            "type": "glovo/ordering/live/basket_set",
            "generation": generation,
            "expectedRevision": 0,
            "storeHandle": stores[0]["storeHandle"],
            "addressHandle": addresses[0]["key"],
            "products": [
                {
                    "productHandle": product["productHandle"],
                    "quantity": 2,
                    "options": [],
                }
            ],
        },
    )
    assert basket_response.errors == [], ledger
    basket = basket_response.results[0][1]
    payments = _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/live/payment_methods",
        {"type": "glovo/ordering/live/payment_methods", "generation": generation},
    ).results[0][1]["paymentMethods"]
    quote = _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/live/create_quote",
        {
            "type": "glovo/ordering/live/create_quote",
            "generation": generation,
            "addressHandle": addresses[0]["key"],
            "paymentHandle": payments[0]["key"],
        },
    ).results[0][1]
    basket_after_quote = _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/live/basket",
        {"type": "glovo/ordering/live/basket", "generation": generation},
    ).results[0][1]
    prepared = _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/live/prepare_confirmation",
        {"type": "glovo/ordering/live/prepare_confirmation", "generation": generation},
    ).results[0][1]
    assert "glovo/ordering/live/execute_checkout" not in _command_map(ha_runtime)
    assert basket["revision"] == 1
    assert basket["status"] == "adopted"
    assert quote["purchaseTotalCents"] == 560000
    assert basket_after_quote == basket
    assert prepared["challenge"]
    assert [(method, path) for method, path, _, _ in ledger] == [
        ("GET", "/customer_profile/api/v1/address_book/me/addresses"),
        ("GET", "/v3/stores/fixture-kitchen"),
        ("GET", "/v4/stores/71/addresses/81/content/main"),
        ("GET", "/v3/me"),
        ("GET", "/v3/stores/fixture-kitchen"),
        ("GET", "/v1/authenticated/customers/42/baskets"),
        ("GET", "/v3/me"),
        ("GET", "/v3/stores/fixture-kitchen"),
        ("POST", "/v1/authenticated/customers/42/baskets"),
        ("GET", "/v3/stores/fixture-kitchen"),
        ("GET", "/v4/payment_methods"),
        ("GET", "/v3/stores/fixture-kitchen"),
        ("POST", "/v3/checkouts/order/1/template"),
        ("GET", "/customer_profile/api/v1/address_book/me/addresses"),
        ("GET", "/v4/payment_methods"),
        ("GET", "/v3/stores/fixture-kitchen"),
    ]
    expected_location = {
        "countryCode": "AM",
        "cityCode": "YRV",
        "latitude": "40.177",
        "longitude": "44.513",
    }
    assert location_contexts == [expected_location] * 10
    assert ledger[8][2] == {} and ledger[12][2] == {}
    runtime = entry.runtime_data
    assert runtime.ordering_runtime.live_checkout_available is False


def test_bad_preparation_authority_storage_fails_closed_without_mutation_session_or_facade(
    ha_runtime: SimpleNamespace,
) -> None:
    ha_runtime.Store.values["glovo.ordering_prep_authority_v1.entry-one"] = {"bad": True}
    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry(
        {"allow_ordering": True, "ordering_acknowledged": True}
    )
    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
    runtime = entry.runtime_data
    assert runtime.api_session._mutation_transport is None
    assert runtime.ordering_runtime.flow is None
    assert runtime.ordering_manager.live_ordering_available is False


def test_token_data_update_does_not_reload_tracking_entities(
    ha_runtime: SimpleNamespace,
) -> None:
    """A rotated API token must not briefly unload every tracking entity."""
    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry(
        {"scan_interval": 15, "allow_ordering": True, "ordering_acknowledged": True}
    )
    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
    manager = entry.runtime_data.ordering_manager
    generation = manager.generation

    # The coordinator persists a rotated token through async_update_entry, which
    # invokes this listener even though no integration option changed.
    entry.data = {"token": "rotated-token"}
    run(entry.listeners[0](hass, entry))

    assert hass.config_entries.reloaded == []
    assert manager.enabled is True
    assert manager.generation == generation
    assert "glovo-ordering" not in ha_runtime.removed_panels

    # Real option changes still need a reload so the polling interval and gated
    # runtime are rebuilt from the new options.
    entry.options = {
        "scan_interval": 30,
        "allow_ordering": True,
        "ordering_acknowledged": True,
    }
    run(entry.listeners[0](hass, entry))
    assert hass.config_entries.reloaded == [entry.entry_id]


def test_shared_session_token_rotation_persists_without_reload_or_generation_bump(
    ha_runtime: SimpleNamespace,
) -> None:
    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry(
        {"scan_interval": 15, "allow_ordering": True, "ordering_acknowledged": True}
    )
    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
    coordinator = entry.runtime_data
    generation = coordinator.ordering_manager.generation
    session = coordinator.api_session
    assert coordinator._account_client._session is session
    assert coordinator._catalog_client._session is session

    session._ensure_token = lambda token: ("access-rotated", "rotated-token-json")
    session._transport = lambda method, access, path, query: {"ok": True}
    assert run(session.async_get("account", "/v3/me")) == {"ok": True}
    assert entry.data["token"] == "rotated-token-json"
    run(entry.listeners[0](hass, entry))

    assert hass.config_entries.reloaded == []
    assert coordinator.ordering_manager.generation == generation
    assert coordinator.ordering_manager.enabled is True


def test_migration_forces_fresh_opt_in_and_keeps_runtime_panel_disabled(
    ha_runtime: SimpleNamespace,
) -> None:
    for minor_version, options in (
        (1, {}),
        (4, {"allow_ordering": "yes", "ordering_acknowledged": True}),
        (5, {"allow_ordering": True}),
        (
            5,
            {
                "scan_interval": 37,
                "unrelated_home_option": "preserved",
                "allow_ordering": True,
                "ordering_acknowledged": True,
                "allow_live_checkout": True,
                "live_checkout_acknowledged": True,
            },
        ),
    ):
        hass = ha_runtime.FakeHass()
        entry = ha_runtime.FakeEntry(options)
        entry.minor_version = minor_version
        assert run(ha_runtime.integration.async_migrate_entry(hass, entry)) is True
        assert entry.options["allow_ordering"] is False
        assert entry.options["ordering_acknowledged"] is False
        assert entry.options["allow_live_checkout"] is False
        assert entry.options["live_checkout_acknowledged"] is False
        assert entry.minor_version == 6
        if "scan_interval" in options:
            assert entry.options["scan_interval"] == 37
            assert entry.options["unrelated_home_option"] == "preserved"

        assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
        assert entry.runtime_data.refreshed is True
        assert entry.runtime_data.ordering_manager.enabled is False
        assert entry.runtime_data.ordering_surface is None
        assert hass.config_entries.forwarded

    assert not ha_runtime.panel_calls


@pytest.mark.parametrize(
    ("gates", "expected"),
    [
        ((True, True, True, True), (True, True, True, True)),
        ((False, True, True, True), (False, False, False, False)),
        ((True, False, True, True), (True, False, False, False)),
        ((True, True, False, True), (True, True, False, False)),
    ],
)
def test_current_v6_migration_preserves_only_normalized_boolean_gate_state(
    ha_runtime: SimpleNamespace,
    gates: tuple[bool, bool, bool, bool],
    expected: tuple[bool, bool, bool, bool],
) -> None:
    keys = (
        "allow_ordering",
        "ordering_acknowledged",
        "allow_live_checkout",
        "live_checkout_acknowledged",
    )
    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry(
        {"scan_interval": 23, "unrelated_home_option": "preserved"}
        | dict(zip(keys, gates, strict=True))
    )
    entry.minor_version = 6

    assert run(ha_runtime.integration.async_migrate_entry(hass, entry)) is True

    assert tuple(entry.options[key] for key in keys) == expected
    assert entry.options["scan_interval"] == 23
    assert entry.options["unrelated_home_option"] == "preserved"
    assert entry.minor_version == 6


def test_reauth_refresh_resets_both_ordering_options_false(
    ha_runtime: SimpleNamespace,
) -> None:
    entry = ha_runtime.FakeEntry(
        {"scan_interval": 15, "allow_ordering": True, "ordering_acknowledged": True}
    )
    flow = ha_runtime.flow.GlovoConfigFlow()
    flow.hass = ha_runtime.FakeHass()
    flow._get_reauth_entry = lambda: entry
    result = run(flow.async_step_reauth_confirm({"refresh_token": "new-refresh"}))
    assert result["data"]["token"] == "token-json:new-refresh"
    assert result["options"]["allow_ordering"] is False
    assert result["options"]["ordering_acknowledged"] is False
    assert result["options"]["allow_live_checkout"] is False
    assert result["options"]["live_checkout_acknowledged"] is False


def test_options_flow_has_separate_default_off_preparation_and_spending_switches(
    ha_runtime: SimpleNamespace,
) -> None:
    entry = ha_runtime.FakeEntry(
        {
            "scan_interval": 15,
            "allow_ordering": False,
            "ordering_acknowledged": False,
            "allow_live_checkout": True,
            "live_checkout_acknowledged": True,
        }
    )
    flow = ha_runtime.flow.GlovoOptionsFlow()
    flow.hass = ha_runtime.FakeHass()
    flow.config_entry = entry
    accepted = run(
        flow.async_step_init(
            {
                "scan_interval": 15,
                "allow_ordering": True,
                "ordering_acknowledged": True,
                "allow_live_checkout": True,
                "live_checkout_acknowledged": True,
            }
        )
    )
    assert accepted["type"] == "create_entry"
    assert accepted["data"]["allow_ordering"] is True
    assert accepted["data"]["ordering_acknowledged"] is True
    assert accepted["data"]["allow_live_checkout"] is True
    assert accepted["data"]["live_checkout_acknowledged"] is True


def test_panel_registration_failure_preserves_websocket_api_and_tracking(
    ha_runtime: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    ordering_ha = sys.modules[f"{ha_runtime.prefix}.ordering_ha"]

    async def fail_panel(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("injected panel failure")

    monkeypatch.setattr(ordering_ha.panel_custom, "async_register_panel", fail_panel)
    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry(
        {"allow_ordering": True, "ordering_acknowledged": True}
    )
    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
    assert entry.runtime_data.refreshed is True
    assert entry.runtime_data.ordering_manager.enabled is True
    assert entry.runtime_data.ordering_surface is not None
    assert "glovo-ordering" in ha_runtime.removed_panels
    assert len(ha_runtime.commands) == len(_enabled_commands(ha_runtime))
    state = ha_runtime.Connection(admin=True)
    run(
        ha_runtime.commands[0](
            hass, state, {"id": 7, "type": "glovo/ordering/state"}
        )
    )
    assert state.results[0][1]["enabled"] is True
    assert hass.config_entries.forwarded


def test_A_recovery_websocket_commands_are_admin_only_strict_and_sanitized(
    ha_runtime: SimpleNamespace,
) -> None:
    _seed_manual_recovery(ha_runtime)
    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry(
        {"allow_ordering": False, "ordering_acknowledged": False}
    )
    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
    commands = _command_map(ha_runtime)
    assert set(commands) == _recovery_commands(ha_runtime)

    valid = {
        "glovo/ordering/state": {"type": "glovo/ordering/state"},
        "glovo/ordering/manual_checks": {"type": "glovo/ordering/manual_checks"},
        "glovo/ordering/manual_check": {
            "type": "glovo/ordering/manual_check",
            "attemptRef": "attempt-live-ha",
        },
        "glovo/ordering/prepare_manual_resolution": {
            "type": "glovo/ordering/prepare_manual_resolution",
            "attemptRef": "attempt-live-ha",
            "expectedRecordRevision": 3,
            "expectedState": "MANUAL_CHECK_REQUIRED",
            "resolution": "still_unknown",
        },
        "glovo/ordering/resolve_manual_check": {
            "type": "glovo/ordering/resolve_manual_check",
            "attemptRef": "attempt-live-ha",
            "expectedRecordRevision": 3,
            "expectedState": "MANUAL_CHECK_REQUIRED",
            "resolution": "still_unknown",
            "challenge": "synthetic-unused-challenge-value",
            "acknowledged": True,
        },
    }
    for command_type, message in valid.items():
        denied = _call_ws(
            ha_runtime, hass, command_type, message, admin=False, user_id="non-admin"
        )
        assert denied.errors == [
            (1, "admin_required", "Administrator access is required")
        ]

        unknown = _call_ws(
            ha_runtime, hass, command_type, {**message, "unexpected": "private-value"}
        )
        assert unknown.errors == [(1, "invalid_format", "Invalid message format")]

    missing_ack = dict(valid["glovo/ordering/resolve_manual_check"])
    missing_ack.pop("acknowledged")
    assert _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/resolve_manual_check",
        missing_ack,
    ).errors[0][1] == "invalid_format"

    for acknowledged, expected_code in (
        (False, "invalid_format"),
        (0, "invalid_format"),
        (1, "invalid_format"),
        ("true", "invalid_format"),
    ):
        message = {
            **valid["glovo/ordering/resolve_manual_check"],
            "acknowledged": acknowledged,
        }
        assert _call_ws(
            ha_runtime, hass, "glovo/ordering/resolve_manual_check", message
        ).errors[0][1] == expected_code

    bool_revision = {
        **valid["glovo/ordering/prepare_manual_resolution"],
        "expectedRecordRevision": True,
    }
    assert _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/prepare_manual_resolution",
        bool_revision,
    ).errors[0][1] == "invalid_format"
    invalid_outcome = {
        **valid["glovo/ordering/prepare_manual_resolution"],
        "resolution": "retry",
    }
    assert _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/prepare_manual_resolution",
        invalid_outcome,
    ).errors[0][1] == "invalid_format"


def test_A_recovery_challenges_reject_stale_bindings_owner_ttl_and_reuse(
    ha_runtime: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Clock:
        value = 1_800_000_000.0

        def __call__(self) -> float:
            return self.value

    clock = Clock()
    monkeypatch.setattr(ha_runtime.integration.time, "time", clock)
    _seed_manual_recovery(ha_runtime)
    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry(
        {"allow_ordering": False, "ordering_acknowledged": False}
    )
    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
    prepare = {
        "type": "glovo/ordering/prepare_manual_resolution",
        "attemptRef": "attempt-live-ha",
        "expectedRecordRevision": 3,
        "expectedState": "MANUAL_CHECK_REQUIRED",
        "resolution": "still_unknown",
    }

    for mutation in (
        {"expectedRecordRevision": 2},
        {"expectedState": "CONFIRMED_FAILED"},
    ):
        stale = _call_ws(
            ha_runtime,
            hass,
            "glovo/ordering/prepare_manual_resolution",
            {**prepare, **mutation},
        )
        assert stale.errors[0][1] == "invalid_manual_resolution"

    def prepared_challenge() -> str:
        response = _call_ws(
            ha_runtime, hass, "glovo/ordering/prepare_manual_resolution", prepare
        )
        assert not response.errors
        return response.results[0][1]["challenge"]

    def resolve(challenge: str, *, user_id: str = "admin-one") -> Any:
        return _call_ws(
            ha_runtime,
            hass,
            "glovo/ordering/resolve_manual_check",
            {
                "type": "glovo/ordering/resolve_manual_check",
                "attemptRef": "attempt-live-ha",
                "expectedRecordRevision": 3,
                "expectedState": "MANUAL_CHECK_REQUIRED",
                "resolution": "still_unknown",
                "challenge": challenge,
                "acknowledged": True,
            },
            user_id=user_id,
        )

    assert resolve(prepared_challenge(), user_id="admin-two").errors[0][1] == (
        "invalid_manual_resolution"
    )

    challenge = prepared_challenge()
    clock.value += 301
    assert resolve(challenge).errors[0][1] == "invalid_manual_resolution"

    challenge = prepared_challenge()
    run(entry.runtime_data.ordering_manager.async_set_enabled(False))
    assert resolve(challenge).errors[0][1] == "invalid_manual_resolution"

    challenge = prepared_challenge()
    success = resolve(challenge)
    assert success.results[0][1]["manualCheckRequired"] is True
    assert resolve(challenge).errors[0][1] == "invalid_manual_resolution"


def test_B_recovery_handlers_restore_after_disabled_option_reload(
    ha_runtime: SimpleNamespace,
) -> None:
    _seed_manual_recovery(ha_runtime)
    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry(
        {"allow_ordering": True, "ordering_acknowledged": True}
    )
    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
    assert set(_command_map(ha_runtime)) == _recovery_commands(ha_runtime)
    retained_state = _command_map(ha_runtime)["glovo/ordering/state"]
    assert _call_ws(
        ha_runtime, hass, "glovo/ordering/state", {"type": "glovo/ordering/state"}
    ).results[0][1]["manualCheckRequired"] is True

    entry.options = {"allow_ordering": False, "ordering_acknowledged": False}
    run(entry.listeners[0](hass, entry))
    assert hass.config_entries.reloaded == [entry.entry_id]
    disabled = ha_runtime.Connection(admin=True)
    run(retained_state(hass, disabled, {"id": 2, "type": "glovo/ordering/state"}))
    assert disabled.errors[0][1] == "ordering_disabled"

    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
    restored = _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/manual_checks",
        {"type": "glovo/ordering/manual_checks"},
    )
    assert restored.results[0][1]["attempts"][0]["attemptRef"] == "attempt-live-ha"
    assert set(_command_map(ha_runtime)) == _recovery_commands(ha_runtime)


def test_C_unload_deactivates_retained_shell_and_restart_restores_durable_recovery(
    ha_runtime: SimpleNamespace,
) -> None:
    _seed_manual_recovery(ha_runtime)
    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry(
        {"allow_ordering": False, "ordering_acknowledged": False}
    )
    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
    retained = _command_map(ha_runtime)["glovo/ordering/manual_checks"]
    assert run(ha_runtime.integration.async_unload_entry(hass, entry)) is True
    after_unload = ha_runtime.Connection(admin=True)
    run(
        retained(
            hass,
            after_unload,
            {"id": 3, "type": "glovo/ordering/manual_checks"},
        )
    )
    assert after_unload.errors[0][1] == "ordering_disabled"

    restarted_hass = ha_runtime.FakeHass()
    restarted_entry = ha_runtime.FakeEntry(
        {"allow_ordering": True, "ordering_acknowledged": True}
    )
    assert run(
        ha_runtime.integration.async_setup_entry(restarted_hass, restarted_entry)
    ) is True
    assert restarted_entry.runtime_data.ordering_manager.manual_check_required is True
    assert restarted_entry.runtime_data.ordering_manager.enabled is False
    restored = _call_ws(
        ha_runtime,
        restarted_hass,
        "glovo/ordering/state",
        {"type": "glovo/ordering/state"},
    )
    assert restored.results[0][1]["orderingBlocked"] is True
    assert set(_command_map(ha_runtime)) == _recovery_commands(ha_runtime)


@pytest.mark.parametrize("failure", ["static", "panel"])
def test_D_optional_frontend_failure_preserves_recovery_api_and_durable_block(
    ha_runtime: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    _seed_manual_recovery(ha_runtime)
    hass = ha_runtime.FakeHass()

    async def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError(f"injected {failure} failure")

    if failure == "static":
        monkeypatch.setattr(hass.http, "async_register_static_paths", fail)
    else:
        ordering_ha = sys.modules[f"{ha_runtime.prefix}.ordering_ha"]
        monkeypatch.setattr(ordering_ha.panel_custom, "async_register_panel", fail)
    entry = ha_runtime.FakeEntry(
        {"allow_ordering": False, "ordering_acknowledged": False}
    )
    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
    state = _call_ws(
        ha_runtime, hass, "glovo/ordering/state", {"type": "glovo/ordering/state"}
    )
    assert state.results[0][1]["manualCheckRequired"] is True
    assert ha_runtime.Store.values["glovo.ordering_safety_v2.entry-one"][
        "manual_check_required"
    ] is True
    assert hass.config_entries.forwarded


def test_D_required_recovery_handler_partial_registration_fails_setup_closed(
    ha_runtime: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_manual_recovery(ha_runtime)
    ordering_ha = sys.modules[f"{ha_runtime.prefix}.ordering_ha"]
    original = ordering_ha.websocket_api.async_register_command
    registrations = 0

    def fail_second(hass: Any, command: Any) -> None:
        nonlocal registrations
        registrations += 1
        if registrations == 2:
            raise RuntimeError("injected required handler registration failure")
        original(hass, command)

    monkeypatch.setattr(
        ordering_ha.websocket_api, "async_register_command", fail_second
    )
    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry(
        {"allow_ordering": False, "ordering_acknowledged": False}
    )
    with pytest.raises(
        RuntimeError, match="injected required handler registration failure"
    ):
        run(ha_runtime.integration.async_setup_entry(hass, entry))
    assert hass.config_entries.forwarded == []
    assert hass.data["glovo"]["ordering_websocket_handlers"] == {}
    retained = ha_runtime.commands[0]
    connection = ha_runtime.Connection(admin=True)
    run(retained(hass, connection, {"id": 4, "type": "glovo/ordering/state"}))
    assert connection.errors[0][1] == "ordering_disabled"
    assert ha_runtime.Store.values["glovo.ordering_safety_v2.entry-one"][
        "manual_check_required"
    ] is True


def test_E_integrity_fault_is_privacy_safe_permanent_and_not_clearable(
    ha_runtime: SimpleNamespace,
) -> None:
    _seed_integrity_fault(ha_runtime)
    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry(
        {"allow_ordering": False, "ordering_acknowledged": False}
    )
    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
    assert set(_command_map(ha_runtime)) == _recovery_commands(ha_runtime)
    assert not {
        command
        for command in _command_map(ha_runtime)
        if "retry" in command or "clear" in command
    }
    state = _call_ws(
        ha_runtime, hass, "glovo/ordering/state", {"type": "glovo/ordering/state"}
    )
    public_state = dict(state.results[0][1])
    runtime_epoch = public_state.pop("runtimeEpoch")
    assert isinstance(runtime_epoch, str) and len(runtime_epoch) >= 16
    assert public_state == {
        "enabled": False,
        "generation": 4,
        "mockOnly": True,
        "liveOrderingAvailable": False,
        "liveCheckoutAvailable": False,
        "manualCheckRequired": False,
        "preparationRecoveryRequired": False,
        "integrityFault": True,
        "orderingBlocked": True,
    }
    checks = _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/manual_checks",
        {"type": "glovo/ordering/manual_checks"},
    )
    assert checks.results[0][1] == {"attempts": []}
    rejected = _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/prepare_manual_resolution",
        {
            "type": "glovo/ordering/prepare_manual_resolution",
            "attemptRef": "attempt-live-ha",
            "expectedRecordRevision": 3,
            "expectedState": "MANUAL_CHECK_REQUIRED",
            "resolution": "still_unknown",
        },
    )
    assert rejected.errors == [
        (1, "ordering_integrity_fault", "Ordering is blocked by an integrity fault")
    ]
    assert entry.runtime_data.ordering_manager.integrity_fault is True


def test_F_recovery_websocket_payloads_and_errors_use_privacy_allowlist(
    ha_runtime: SimpleNamespace,
) -> None:
    _seed_manual_recovery(ha_runtime)
    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry(
        {"allow_ordering": False, "ordering_acknowledged": False}
    )
    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
    payloads = []
    for command_type, message in (
        ("glovo/ordering/state", {"type": "glovo/ordering/state"}),
        (
            "glovo/ordering/manual_checks",
            {"type": "glovo/ordering/manual_checks"},
        ),
        (
            "glovo/ordering/manual_check",
            {
                "type": "glovo/ordering/manual_check",
                "attemptRef": "attempt-live-ha",
            },
        ),
    ):
        response = _call_ws(ha_runtime, hass, command_type, message)
        payloads.append(response.results[0][1])

    prepare_message = {
        "type": "glovo/ordering/prepare_manual_resolution",
        "attemptRef": "attempt-live-ha",
        "expectedRecordRevision": 3,
        "expectedState": "MANUAL_CHECK_REQUIRED",
        "resolution": "still_unknown",
    }
    prepared = _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/prepare_manual_resolution",
        prepare_message,
    )
    prepared_payload = prepared.results[0][1]
    payloads.append(prepared_payload)
    resolved = _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/resolve_manual_check",
        {
            "type": "glovo/ordering/resolve_manual_check",
            "attemptRef": "attempt-live-ha",
            "expectedRecordRevision": 3,
            "expectedState": "MANUAL_CHECK_REQUIRED",
            "resolution": "still_unknown",
            "challenge": prepared_payload["challenge"],
            "acknowledged": True,
        },
    )
    payloads.append(resolved.results[0][1])

    attempt_keys = {
        "attemptRef",
        "state",
        "recordRevision",
        "submittedAt",
        "storeDisplayName",
        "amountMinor",
        "currency",
        "itemCount",
        "itemSummary",
        "maskedPaymentLabel",
        "maskedAddressAlias",
        "hasCheckoutId",
        "ambiguityReason",
        "lastReviewedAt",
    }
    list_attempt = payloads[1]["attempts"][0]
    get_attempt = payloads[2]
    resolved_attempt = payloads[-1]["attempt"]
    assert set(list_attempt) == attempt_keys
    assert set(get_attempt) == attempt_keys
    assert set(resolved_attempt) == attempt_keys
    assert list_attempt["hasCheckoutId"] is True

    encoded = json.dumps(payloads, sort_keys=True).lower()
    for forbidden in (
        "provider_session",
        "payment_key",
        "address_key",
        "selection_key",
        "checkout-private-synthetic",
        "request_fingerprint",
        "idempotency",
        "latitude",
        "longitude",
        "cookie",
        "bearer",
        "raw_body",
        "f" * 64,
        "a" * 64,
    ):
        assert forbidden not in encoded

    async def raw_failure(user: Any, message: Any) -> dict[str, Any]:
        raise ValueError(
            "Bearer synthetic-not-a-secret Cookie raw_body synthetic provider parser"
        )

    hass.data["glovo"]["ordering_websocket_handlers"][
        "glovo/ordering/state"
    ] = raw_failure
    sanitized = _call_ws(
        ha_runtime, hass, "glovo/ordering/state", {"type": "glovo/ordering/state"}
    )
    assert sanitized.errors == [
        (1, "invalid_ordering_request", "Ordering request is unavailable or invalid")
    ]


def test_status_reconciliation_without_learned_id_performs_zero_gets_and_stays_manual(
    ha_runtime: SimpleNamespace,
) -> None:
    _seed_manual_recovery(ha_runtime, checkout_id=None)
    calls: list[tuple[str, str]] = []
    glovo_api = sys.modules[f"{ha_runtime.prefix}.glovo"]
    setattr(
        glovo_api,
        "single_attempt_authed_get",
        lambda method, _token, path, _query: calls.append((method, path)),
    )
    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry({})
    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
    result = _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/live/checkout_status",
        {"type": "glovo/ordering/live/checkout_status", "generation": 4},
    )
    assert result.errors == []
    assert result.results[0][1] == {
        "status": "no_checkout_id",
        "manualCheckRequired": True,
    }
    assert calls == []
    assert entry.runtime_data.ordering_manager.manual_check_required is True


@pytest.mark.parametrize(
    ("provider_status", "public_status", "journal_state"),
    [
        ("COMPLETED", "succeeded", "CONFIRMED_SUCCEEDED"),
        ("FAILED", "failed", "CONFIRMED_FAILED"),
        ("CANCELLED", "failed", "CONFIRMED_FAILED"),
    ],
)
def test_home7_learned_checkout_id_does_not_compose_provider_status_get(
    ha_runtime: SimpleNamespace,
    provider_status: str,
    public_status: str,
    journal_state: str,
) -> None:
    _seed_manual_recovery(ha_runtime)
    calls: list[tuple[str, str, dict[str, str]]] = []
    glovo_api = sys.modules[f"{ha_runtime.prefix}.glovo"]

    def get(method: str, _token: str, path: str, query: dict[str, str]) -> Any:
        calls.append((method, path, copy.deepcopy(query)))
        return _checkout_status_payload(provider_status)

    setattr(glovo_api, "single_attempt_authed_get", get)
    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry({})
    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
    result = _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/live/checkout_status",
        {"type": "glovo/ordering/live/checkout_status", "generation": 4},
    )
    assert result.errors == []
    assert result.results[0][1] == {
        "status": "unavailable",
        "manualCheckRequired": True,
    }
    assert calls == []
    record = ha_runtime.Store.values["glovo.ordering_journal_v2.entry-one"]["records"][0]
    safety = ha_runtime.Store.values["glovo.ordering_safety_v2.entry-one"]
    assert record["state"] == "MANUAL_CHECK_REQUIRED"
    assert record["resolution"] is None
    assert record["evidence_source"] is None
    assert record["provider_evidence_hash"] is None
    assert safety == {
        "version": 2,
        "generation": 4,
        "manual_check_required": True,
        "integrity_fault": False,
        "manual_binding": {
            "attempt_id": "attempt-live-ha",
            "record_revision": 3,
            "generation": 4,
            "resolution": None,
        },
    }
    assert entry.runtime_data.ordering_manager.manual_check_required is True


@pytest.mark.parametrize(
    "payload",
    [
        _checkout_status_payload("AUTH_REQUIRED", action="AUTH"),
        _checkout_status_payload("NO_AUTH_PENDING", action="NO_AUTH_POLL"),
        _checkout_status_payload("NO_AUTH_PENDING", action="PROCESS_PAYMENT"),
        {"malformed": True},
        _checkout_status_payload(
            "COMPLETED",
            order={
                "id": "order-private-synthetic",
                "basketId": "basket-mismatch",
                "total": 624000,
                "currencyCode": "AMD",
            },
        ),
    ],
    ids=["auth", "pending", "process-payment", "malformed", "identity-mismatch"],
)
def test_home7_status_payloads_are_not_requested_and_manual_block_remains(
    ha_runtime: SimpleNamespace, payload: dict[str, Any]
) -> None:
    _seed_manual_recovery(ha_runtime)
    calls: list[str] = []
    glovo_api = sys.modules[f"{ha_runtime.prefix}.glovo"]

    def get(_method: str, _token: str, path: str, _query: dict[str, str]) -> Any:
        calls.append(path)
        return copy.deepcopy(payload)

    setattr(glovo_api, "single_attempt_authed_get", get)
    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry({})
    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
    result = _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/live/checkout_status",
        {"type": "glovo/ordering/live/checkout_status", "generation": 4},
    )
    assert result.results[0][1] == {
        "status": "unavailable",
        "manualCheckRequired": True,
    }
    assert calls == []
    assert ha_runtime.Store.values["glovo.ordering_journal_v2.entry-one"]["records"][0][
        "state"
    ] == "MANUAL_CHECK_REQUIRED"
    assert entry.runtime_data.ordering_manager.manual_check_required is True


def test_home7_status_transport_is_not_called_and_manual_block_remains(
    ha_runtime: SimpleNamespace,
) -> None:
    _seed_manual_recovery(ha_runtime)
    calls: list[str] = []
    glovo_api = sys.modules[f"{ha_runtime.prefix}.glovo"]

    def get(_method: str, _token: str, path: str, _query: dict[str, str]) -> Any:
        calls.append(path)
        raise ConnectionResetError("synthetic read failure")

    setattr(glovo_api, "single_attempt_authed_get", get)
    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry({})
    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
    result = _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/live/checkout_status",
        {"type": "glovo/ordering/live/checkout_status", "generation": 4},
    )
    assert result.results[0][1]["manualCheckRequired"] is True
    assert result.results[0][1]["status"] == "unavailable"
    assert calls == []


@pytest.mark.parametrize("failure_layer", ["journal", "safety"])
def test_home7_uncomposed_status_cannot_reach_terminal_persistence(
    ha_runtime: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    failure_layer: str,
) -> None:
    _seed_manual_recovery(ha_runtime)
    glovo_api = sys.modules[f"{ha_runtime.prefix}.glovo"]
    setattr(
        glovo_api,
        "single_attempt_authed_get",
        lambda _method, _token, _path, _query: _checkout_status_payload("COMPLETED"),
    )
    original_save = ha_runtime.Store.async_save

    async def failing_save(storage: Any, data: dict[str, Any]) -> None:
        if failure_layer == "journal" and "ordering_journal_v2" in storage.key and data[
            "records"
        ][0]["state"] == "CONFIRMED_SUCCEEDED":
            raise OSError("synthetic journal save failure")
        if failure_layer == "safety" and "ordering_safety_v2" in storage.key and data.get(
            "manual_binding", {}
        ).get("resolution") == "provider_succeeded":
            raise OSError("synthetic safety save failure")
        await original_save(storage, data)

    monkeypatch.setattr(ha_runtime.Store, "async_save", failing_save)
    hass = ha_runtime.FakeHass()
    entry = ha_runtime.FakeEntry({})
    assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
    result = _call_ws(
        ha_runtime,
        hass,
        "glovo/ordering/live/checkout_status",
        {"type": "glovo/ordering/live/checkout_status", "generation": 4},
    )
    manager = entry.runtime_data.ordering_manager
    assert result.errors == []
    assert result.results[0][1] == {
        "status": "unavailable",
        "manualCheckRequired": True,
    }
    assert manager.manual_check_required is True
    assert manager.integrity_fault is False


def test_G_recovery_frontend_has_only_nonretrying_challenge_acknowledged_outcomes() -> None:
    source = (GLOVO_ROOT / "frontend" / "glovo-ordering-panel.js").read_text()
    lowered = source.lower()
    assert source.count('data-resolution="') == 3
    for resolution in (
        "found_succeeded",
        "found_failed_or_cancelled",
        "still_unknown",
    ):
        assert source.count(f'data-resolution="{resolution}"') == 1
    assert "do not place this order again" in lowered
    assert lowered.index("if (state.manualcheckrequired)") < lowered.index(
        '"live/addresses"'
    )
    assert "prepared.challenge" in source
    assert 'acknowledged: true' in source
    # Fail closed unless the box is literally checked; truthy/non-boolean values
    # cannot authorize manual resolution.
    assert '.checked !== true' in source
    assert source.count(".innerHTML") == 1
    assert ".textContent" in source
    for prohibited in (
        "localstorage",
        "sessionstorage",
        "console.",
        "analytics",
        "glovo/ordering/retry",
        "glovo/ordering/clear",
        "clear_integrity",
        "retryattempt",
    ):
        assert prohibited not in lowered


def test_H_default_off_tracking_and_production_preparation_inventory(
    ha_runtime: SimpleNamespace,
) -> None:
    disabled_hass = ha_runtime.FakeHass()
    disabled_entry = ha_runtime.FakeEntry({})
    assert run(
        ha_runtime.integration.async_setup_entry(disabled_hass, disabled_entry)
    ) is True
    assert disabled_entry.runtime_data.refreshed is True
    assert disabled_entry.runtime_data.ordering_surface is None
    assert disabled_hass.config_entries.forwarded
    assert ha_runtime.commands == []

    enabled_hass = ha_runtime.FakeHass()
    enabled_entry = ha_runtime.FakeEntry(
        {"allow_ordering": True, "ordering_acknowledged": True}
    )
    assert run(
        ha_runtime.integration.async_setup_entry(enabled_hass, enabled_entry)
    ) is True
    assert set(_command_map(ha_runtime)) == _enabled_commands(ha_runtime)
    assert enabled_entry.runtime_data.ordering_manager.enabled is True
    assert enabled_entry.runtime_data.ordering_manager._catalog is None
    assert enabled_entry.runtime_data.ordering_manager._checkout_adapter is None
    assert enabled_entry.runtime_data.ordering_runtime.live_checkout_available is False
    assert enabled_hass.config_entries.forwarded
    assert socket.create_connection.__name__ == "blocked"
