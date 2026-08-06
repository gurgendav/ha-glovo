"""Behavioral Home Assistant boundary tests using realistic API/service stubs."""

from __future__ import annotations

import asyncio
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
    monkeypatch.setitem(sys.modules, glovo_api.__name__, glovo_api)

    for module_name in (
        "ordering_models",
        "ordering_contracts",
        "api_session",
        "ordering_account",
        "ordering_live_catalog",
        "ordering_basket",
        "ordering_quote",
        "ordering_catalog",
        "ordering_journal",
        "ordering_state",
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


RECOVERY_COMMANDS = {
    "glovo/ordering/state",
    "glovo/ordering/manual_checks",
    "glovo/ordering/manual_check",
    "glovo/ordering/prepare_manual_resolution",
    "glovo/ordering/resolve_manual_check",
}


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


def _manual_record(*, integrity: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
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
        "checkout_id": "checkout-private-synthetic",
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


def _seed_manual_recovery(runtime: SimpleNamespace, *, integrity: bool = False) -> None:
    record, state = _manual_record(integrity=integrity)
    runtime.Store.values["glovo.ordering_journal_v2.entry-one"] = {
        "version": 2,
        "records": [record],
    }
    runtime.Store.values["glovo.ordering_safety_v2.entry-one"] = state


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
    assert runtime.panel_calls[0]["require_admin"] is True
    assert len(hass.http.static_paths) == 1
    assert len(runtime.commands) == 12

    state_shell = runtime.commands[0]
    admin_connection = runtime.Connection(admin=True)
    run(state_shell(hass, admin_connection, {"id": 1, "type": "glovo/ordering/state"}))
    assert admin_connection.results[0][1]["mockOnly"] is True

    non_admin = runtime.Connection(admin=False)
    run(state_shell(hass, non_admin, {"id": 2, "type": "glovo/ordering/state"}))
    assert non_admin.errors[0][1] == "admin_required"

    # Options mutate before the update listener/reload: the retained active handler
    # reads entry.options live and rejects immediately.
    entry.options = {
        "scan_interval": 15,
        "allow_ordering": True,
        "ordering_acknowledged": False,
    }
    raced = runtime.Connection(admin=True)
    run(state_shell(hass, raced, {"id": 3, "type": "glovo/ordering/state"}))
    assert raced.errors[0][1] == "ordering_disabled"

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
    assert len(runtime.commands) == 12
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
    for options in (
        {},
        {"allow_ordering": "yes", "ordering_acknowledged": True},
        {"allow_ordering": True},
        {"allow_ordering": True, "ordering_acknowledged": True},
    ):
        hass = ha_runtime.FakeHass()
        entry = ha_runtime.FakeEntry(options)
        assert entry.minor_version == 1
        assert run(ha_runtime.integration.async_migrate_entry(hass, entry)) is True
        assert entry.options["allow_ordering"] is False
        assert entry.options["ordering_acknowledged"] is False
        assert entry.minor_version == 2

        assert run(ha_runtime.integration.async_setup_entry(hass, entry)) is True
        assert entry.runtime_data.refreshed is True
        assert entry.runtime_data.ordering_manager.enabled is False
        assert entry.runtime_data.ordering_surface is None
        assert hass.config_entries.forwarded

    assert not ha_runtime.panel_calls


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


def test_options_flow_requires_acknowledgement_behaviorally(
    ha_runtime: SimpleNamespace,
) -> None:
    entry = ha_runtime.FakeEntry(
        {"scan_interval": 15, "allow_ordering": False, "ordering_acknowledged": False}
    )
    flow = ha_runtime.flow.GlovoOptionsFlow()
    flow.hass = ha_runtime.FakeHass()
    flow.config_entry = entry
    rejected = run(
        flow.async_step_init(
            {
                "scan_interval": 15,
                "allow_ordering": True,
                "ordering_acknowledged": False,
            }
        )
    )
    assert rejected["type"] == "form"
    assert rejected["errors"]["base"] == "ordering_ack_required"
    accepted = run(
        flow.async_step_init(
            {
                "scan_interval": 15,
                "allow_ordering": True,
                "ordering_acknowledged": True,
            }
        )
    )
    assert accepted["type"] == "create_entry"
    assert accepted["data"]["allow_ordering"] is True
    assert accepted["data"]["ordering_acknowledged"] is True


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
    assert len(ha_runtime.commands) == 12
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
    assert set(commands) == RECOVERY_COMMANDS

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
        (False, "invalid_manual_resolution"),
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
    assert set(_command_map(ha_runtime)) == RECOVERY_COMMANDS
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
    assert set(_command_map(ha_runtime)) == RECOVERY_COMMANDS


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
    assert set(_command_map(ha_runtime)) == RECOVERY_COMMANDS


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
    assert set(_command_map(ha_runtime)) == RECOVERY_COMMANDS
    assert not {
        command
        for command in _command_map(ha_runtime)
        if "retry" in command or "clear" in command
    }
    state = _call_ws(
        ha_runtime, hass, "glovo/ordering/state", {"type": "glovo/ordering/state"}
    )
    assert state.results[0][1] == {
        "enabled": False,
        "mockOnly": True,
        "liveOrderingAvailable": False,
        "manualCheckRequired": False,
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
        (1, "invalid_ordering_request", "Ordering request is invalid")
    ]


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
        'type: "glovo/ordering/catalog"'
    )
    assert "prepared.challenge" in source
    assert 'acknowledged: true' in source
    assert '.checked === true' in source
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


def test_H_default_off_tracking_and_mock_only_command_inventory_remain_unchanged(
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
    assert set(_command_map(ha_runtime)) == RECOVERY_COMMANDS | {
        "glovo/ordering/catalog",
        "glovo/ordering/basket",
        "glovo/ordering/basket_add_fixture_item",
        "glovo/ordering/basket_clear",
        "glovo/ordering/fixture_quote",
        "glovo/ordering/prepare_mock_confirmation",
        "glovo/ordering/execute_mock_checkout",
    }
    assert enabled_entry.runtime_data.ordering_manager.enabled is True
    assert enabled_entry.runtime_data.ordering_manager._checkout_adapter.execution_count == 0
    assert enabled_hass.config_entries.forwarded
    assert socket.create_connection.__name__ == "blocked"
