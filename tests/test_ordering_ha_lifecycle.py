"""Behavioral Home Assistant boundary tests using realistic API/service stubs."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

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
        def decorate(function: Any) -> Any:
            function.ws_schema = schema
            return function

        return decorate

    def identity(function: Any) -> Any:
        return function

    def async_register_command(hass: Any, command: Any) -> None:
        commands.append(command)

    frontend.async_remove_panel = async_remove_panel
    panel_custom.async_register_panel = async_register_panel
    websocket_api.websocket_command = websocket_command
    websocket_api.require_admin = identity
    websocket_api.async_response = identity
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
        def __init__(self, hass: Any, entry: Any) -> None:
            self.hass = hass
            self.entry = entry
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
    monkeypatch.setitem(sys.modules, glovo_api.__name__, glovo_api)

    for module_name in (
        "ordering_models",
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
    assert len(runtime.commands) == 8

    state_shell = runtime.commands[0]
    admin_connection = runtime.Connection(admin=True)
    run(state_shell(hass, admin_connection, {"id": 1, "type": "glovo/ordering/state"}))
    assert admin_connection.results[0][1]["mockOnly"] is True

    non_admin = runtime.Connection(admin=False)
    run(state_shell(hass, non_admin, {"id": 2, "type": "glovo/ordering/state"}))
    assert non_admin.errors[0][1] == "invalid_mock_ordering_request"

    # Options mutate before the update listener/reload: the retained active handler
    # reads entry.options live and rejects immediately.
    entry.options = {
        "scan_interval": 15,
        "allow_ordering": True,
        "ordering_acknowledged": False,
    }
    raced = runtime.Connection(admin=True)
    run(state_shell(hass, raced, {"id": 3, "type": "glovo/ordering/state"}))
    assert raced.errors[0][1] == "invalid_mock_ordering_request"

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
    assert len(runtime.commands) == 8
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


def test_partial_panel_registration_failure_rolls_back_and_tracking_continues(
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
    assert entry.runtime_data.ordering_manager.enabled is False
    assert entry.runtime_data.ordering_surface is None
    assert "glovo-ordering" in ha_runtime.removed_panels
    assert hass.config_entries.forwarded
