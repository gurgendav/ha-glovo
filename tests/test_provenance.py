"""Security regression tests for coordinator-authoritative runtime provenance."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


class _ConfigEntry:
    @classmethod
    def __class_getitem__(cls, _item: Any) -> type[_ConfigEntry]:
        return cls


class _DataUpdateCoordinator:
    @classmethod
    def __class_getitem__(cls, _item: Any) -> type[_DataUpdateCoordinator]:
        return cls

    def __init__(
        self,
        hass: Any,
        _logger: Any,
        *,
        name: str,
        config_entry: Any,
        update_interval: timedelta,
    ) -> None:
        self.hass = hass
        self.name = name
        self.config_entry = config_entry
        self.update_interval = update_interval
        self.data: dict[str, Any] | None = None
        self.last_update_success = True


class _UpdateFailed(RuntimeError):
    pass


class _ConfigEntryAuthFailed(RuntimeError):
    pass


class _SourceType(Enum):
    GPS = "gps"


class _TrackerEntity:
    pass


class _SensorEntity:
    pass


class _SensorDeviceClass(Enum):
    ENUM = "enum"
    DURATION = "duration"


@dataclass(frozen=True, kw_only=True)
class _SensorEntityDescription:
    key: str
    translation_key: str | None = None
    device_class: Any = None
    options: Any = None
    icon: str | None = None
    entity_category: Any = None
    native_unit_of_measurement: Any = None
    entity_registry_enabled_default: bool = True


class _GlovoEntity:
    def __init__(self, coordinator: Any, key: str) -> None:
        self.coordinator = coordinator
        self.key = key

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success


class _FakeGlovo(ModuleType):
    class GlovoApiError(RuntimeError):
        def __init__(self, status: int = 500) -> None:
            self.status = status

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.fixture_mode = False
        self.fixture_result: Any = _summary(2)
        self.live_result: Any = (_summary(1), "token")
        self.fixture_error: BaseException | None = None
        self.live_error: BaseException | None = None
        self.fixture_calls = 0
        self.live_calls = 0

    def fixtures_available(self, _fixtures_dir: str) -> bool:
        return self.fixture_mode

    def get_last_active_order_summary_from_fixtures(self, *_args: Any, **_kwargs: Any) -> Any:
        self.fixture_calls += 1
        if self.fixture_error is not None:
            raise self.fixture_error
        return self.fixture_result.copy() if isinstance(self.fixture_result, dict) else self.fixture_result

    def get_last_active_order_summary(self, *_args: Any, **_kwargs: Any) -> Any:
        self.live_calls += 1
        if self.live_error is not None:
            raise self.live_error
        summary, token = self.live_result
        return (summary.copy() if isinstance(summary, dict) else summary), token

    @staticmethod
    def empty_active_order_summary() -> dict[str, Any]:
        return _summary(None, order_count=0)

    @staticmethod
    def ha_enum_options(_enum_key: str) -> list[str]:
        return []


def _summary(
    order_id: int | None,
    *,
    order_count: int = 1,
    status: str | None = "on_the_way",
) -> dict[str, Any]:
    return {
        "order_count": order_count,
        "order_id": order_id,
        "overall_status": status,
        "active": order_count > 0,
        "eta_min": 5 if order_id is not None else None,
        "eta_text": "5 minutes" if order_id is not None else None,
        "courier_lat": 41.0 if order_id is not None else None,
        "courier_lon": 29.0 if order_id is not None else None,
        "courier_heading": 90 if order_id is not None else None,
        "courier_name": "Courier" if order_id is not None else None,
        "courier_count": 1 if order_id is not None else None,
        "store_lat": 41.1 if order_id is not None else None,
        "store_lon": 29.1 if order_id is not None else None,
        "poll_interval_sec": 10 if order_count > 0 else None,
    }


@pytest.fixture()
def integration_modules() -> tuple[ModuleType, ModuleType, ModuleType, _FakeGlovo]:
    """Load coordinator, status sensor and tracker with no HA/network dependency."""
    package_name = "glovo_provenance_under_test"
    package = ModuleType(package_name)
    package.__path__ = []  # type: ignore[attr-defined]

    ha = ModuleType("homeassistant")
    config_entries = ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = _ConfigEntry
    core = ModuleType("homeassistant.core")
    core.HomeAssistant = object
    exceptions = ModuleType("homeassistant.exceptions")
    exceptions.ConfigEntryAuthFailed = _ConfigEntryAuthFailed
    helpers = ModuleType("homeassistant.helpers")
    update_coordinator = ModuleType("homeassistant.helpers.update_coordinator")
    update_coordinator.DataUpdateCoordinator = _DataUpdateCoordinator
    update_coordinator.UpdateFailed = _UpdateFailed
    entity_platform = ModuleType("homeassistant.helpers.entity_platform")
    entity_platform.AddConfigEntryEntitiesCallback = object
    util = ModuleType("homeassistant.util")
    dt = ModuleType("homeassistant.util.dt")
    dt.get_time_zone = lambda _zone: None
    dt.now = lambda _zone: None
    components = ModuleType("homeassistant.components")
    device_tracker = ModuleType("homeassistant.components.device_tracker")
    device_tracker.SourceType = _SourceType
    device_tracker.TrackerEntity = _TrackerEntity
    sensor = ModuleType("homeassistant.components.sensor")
    sensor.SensorDeviceClass = _SensorDeviceClass
    sensor.SensorEntity = _SensorEntity
    sensor.SensorEntityDescription = _SensorEntityDescription
    const_ha = ModuleType("homeassistant.const")
    const_ha.PERCENTAGE = "%"
    const_ha.EntityCategory = SimpleNamespace(DIAGNOSTIC="diagnostic")
    const_ha.UnitOfTime = SimpleNamespace(MINUTES="min", SECONDS="s")

    stubs = {
        "homeassistant": ha,
        "homeassistant.config_entries": config_entries,
        "homeassistant.core": core,
        "homeassistant.exceptions": exceptions,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.update_coordinator": update_coordinator,
        "homeassistant.helpers.entity_platform": entity_platform,
        "homeassistant.util": util,
        "homeassistant.util.dt": dt,
        "homeassistant.components": components,
        "homeassistant.components.device_tracker": device_tracker,
        "homeassistant.components.sensor": sensor,
        "homeassistant.const": const_ha,
    }
    previous = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    sys.modules[package_name] = package

    const = ModuleType(f"{package_name}.const")
    const.CACHE_TERMINAL_HOLD_SEC = 60
    const.CONF_SCAN_INTERVAL = "scan_interval"
    const.CONF_TOKEN = "token"
    const.DEFAULT_SCAN_INTERVAL = 15
    const.DOMAIN = "glovo"
    const.FIXTURES_REL_PATH = "projects/ha-glovo"
    const.TERMINAL_OVERALL_STATUSES = ("delivered", "canceled")
    const.DATA_PROVENANCE_ATTRIBUTE = "data_provenance"
    const.INTEGRATION_TRUST_ID_ATTRIBUTE = "integration_trust_id"
    const.INTEGRATION_TRUST_ID = "test-build-sha256"
    const.PROVENANCE_FIXTURE = "fixture"
    const.PROVENANCE_LIVE_API = "live_api"
    const.PROVENANCE_UNAVAILABLE = "unavailable"
    const.PROVENANCE_UNKNOWN = "unknown"
    sys.modules[const.__name__] = const

    fake_glovo = _FakeGlovo(f"{package_name}.glovo")
    sys.modules[fake_glovo.__name__] = fake_glovo
    entity = ModuleType(f"{package_name}.entity")
    entity.GlovoEntity = _GlovoEntity
    sys.modules[entity.__name__] = entity

    root = Path(__file__).parents[1] / "custom_components" / "glovo"

    def load(name: str) -> ModuleType:
        module_name = f"{package_name}.{name}"
        spec = importlib.util.spec_from_file_location(module_name, root / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    coordinator = load("coordinator")
    tracker = load("device_tracker")
    status_sensor = load("sensor")
    try:
        yield coordinator, tracker, status_sensor, fake_glovo
    finally:
        for name, old_module in previous.items():
            if old_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old_module
        for name in tuple(sys.modules):
            if name == package_name or name.startswith(f"{package_name}."):
                sys.modules.pop(name, None)


def _coordinator(module: ModuleType) -> Any:
    class Config:
        time_zone = "UTC"

        @staticmethod
        def path(relative: str) -> str:
            return f"/config/{relative}"

    class ConfigEntries:
        @staticmethod
        def async_update_entry(*_args: Any, **_kwargs: Any) -> None:
            return None

    class Hass:
        config = Config()
        config_entries = ConfigEntries()

        @staticmethod
        async def async_add_executor_job(func: Any) -> Any:
            return func()

    entry = SimpleNamespace(
        data={"token": "token"},
        options={},
        entry_id="entry-1",
    )
    return module.GlovoDataUpdateCoordinator(Hass(), entry)


def _update(coordinator: Any) -> dict[str, Any]:
    result = asyncio.run(coordinator._async_update_data())
    coordinator.data = result
    coordinator.last_update_success = True
    return result


def test_import_is_side_effect_free(integration_modules: tuple[Any, ...]) -> None:
    """Importing platforms must not read fixtures or call the API."""
    *_, fake_glovo = integration_modules
    assert fake_glovo.live_calls == 0
    assert fake_glovo.fixture_calls == 0


def test_live_api_provenance_is_set_only_after_valid_fetch(
    integration_modules: tuple[Any, ...],
) -> None:
    coordinator_module, _, _, fake_glovo = integration_modules
    coordinator = _coordinator(coordinator_module)

    assert coordinator.runtime_provenance == "unknown"
    result = _update(coordinator)

    assert fake_glovo.live_calls == 1
    assert fake_glovo.fixture_calls == 0
    assert result["data_provenance"] == "live_api"
    assert coordinator.runtime_provenance == "live_api"
    assert result["integration_trust_id"] == coordinator.integration_trust_id


def test_fixture_overwrites_spoofed_live_provenance(
    integration_modules: tuple[Any, ...],
) -> None:
    coordinator_module, _, _, fake_glovo = integration_modules
    fake_glovo.fixture_mode = True
    fake_glovo.fixture_result = {
        **_summary(2),
        "data_provenance": "live_api",
        "integration_trust_id": "attacker-controlled",
    }
    coordinator = _coordinator(coordinator_module)

    result = _update(coordinator)

    assert fake_glovo.fixture_calls == 1
    assert fake_glovo.live_calls == 0
    assert result["data_provenance"] == "fixture"
    assert coordinator.runtime_provenance == "fixture"
    assert result["integration_trust_id"] == coordinator.integration_trust_id


@pytest.mark.parametrize(
    "malformed",
    [
        None,
        True,
        False,
        [],
        {},
        {**_summary(1), "order_count": True},
        {**_summary(1), "order_count": "1"},
        {**_summary(1), "active": 1},
        {**_summary(1), "courier_lat": True},
        {**_summary(1), "courier_lat": 10**1000},
    ],
)
def test_empty_boolean_and_malformed_results_never_become_live(
    integration_modules: tuple[Any, ...], malformed: Any
) -> None:
    coordinator_module, _, _, fake_glovo = integration_modules
    fake_glovo.live_result = (malformed, "token")
    coordinator = _coordinator(coordinator_module)

    with pytest.raises(_UpdateFailed):
        asyncio.run(coordinator._async_update_data())

    assert coordinator.runtime_provenance == "unavailable"
    assert coordinator.access_control_attributes()["data_provenance"] == "unavailable"


def test_fixture_failure_and_malformed_data_never_emit_live_api(
    integration_modules: tuple[Any, ...],
) -> None:
    coordinator_module, _, _, fake_glovo = integration_modules
    fake_glovo.fixture_mode = True
    coordinator = _coordinator(coordinator_module)

    fake_glovo.fixture_result = True
    with pytest.raises(_UpdateFailed):
        asyncio.run(coordinator._async_update_data())
    assert coordinator.runtime_provenance == "unavailable"
    assert fake_glovo.live_calls == 0

    fake_glovo.fixture_error = OSError("fixture unreadable")
    with pytest.raises(_UpdateFailed):
        asyncio.run(coordinator._async_update_data())
    assert coordinator.runtime_provenance == "unavailable"
    assert fake_glovo.live_calls == 0


def test_canonical_no_order_result_is_valid_live_api(
    integration_modules: tuple[Any, ...],
) -> None:
    coordinator_module, _, _, fake_glovo = integration_modules
    fake_glovo.live_result = (_summary(None, order_count=0, status=None), "token")
    coordinator = _coordinator(coordinator_module)

    result = _update(coordinator)

    assert result["order_count"] == 0
    assert result["data_provenance"] == "live_api"


def test_failed_refresh_marks_stale_live_data_unavailable(
    integration_modules: tuple[Any, ...],
) -> None:
    coordinator_module, _, _, fake_glovo = integration_modules
    coordinator = _coordinator(coordinator_module)
    prior = _update(coordinator)
    assert prior["data_provenance"] == "live_api"

    fake_glovo.live_error = fake_glovo.GlovoApiError(500)
    coordinator.last_update_success = False
    with pytest.raises(_UpdateFailed):
        asyncio.run(coordinator._async_update_data())

    assert coordinator.data is prior
    assert coordinator.runtime_provenance == "unavailable"
    assert coordinator.access_control_attributes() == {
        "data_provenance": "unavailable",
        "integration_trust_id": coordinator.integration_trust_id,
    }


def test_live_fixture_live_transitions_and_terminal_retention(
    integration_modules: tuple[Any, ...],
) -> None:
    coordinator_module, _, _, fake_glovo = integration_modules
    coordinator = _coordinator(coordinator_module)

    assert _update(coordinator)["data_provenance"] == "live_api"

    fake_glovo.fixture_mode = True
    fake_glovo.fixture_result = _summary(1, order_count=0, status="delivered")
    retained = _update(coordinator)
    assert retained["order_id"] == 1
    assert retained["overall_status"] == "delivered"
    assert retained["data_provenance"] == "fixture"

    fake_glovo.fixture_mode = False
    fake_glovo.live_result = (_summary(1, order_count=0, status="delivered"), "token")
    assert _update(coordinator)["data_provenance"] == "live_api"

    fake_glovo.fixture_mode = True
    fake_glovo.fixture_result = _summary(1, order_count=0, status="delivered")
    assert _update(coordinator)["data_provenance"] == "fixture"


def test_status_and_courier_tracker_expose_identical_access_attributes(
    integration_modules: tuple[Any, ...],
) -> None:
    coordinator_module, tracker_module, sensor_module, fake_glovo = integration_modules
    fake_glovo.fixture_mode = True
    coordinator = _coordinator(coordinator_module)
    _update(coordinator)

    status_description = next(item for item in sensor_module.SENSORS if item.primary)
    status = sensor_module.GlovoSensor(coordinator, status_description)
    tracker = tracker_module.GlovoCourierTracker(coordinator)

    status_attrs = status.extra_state_attributes
    tracker_attrs = tracker.extra_state_attributes
    assert status_attrs["data_provenance"] == tracker_attrs["data_provenance"] == "fixture"
    assert (
        status_attrs["integration_trust_id"]
        == tracker_attrs["integration_trust_id"]
        == coordinator.integration_trust_id
    )

    fake_glovo.fixture_error = OSError("fixture unreadable")
    coordinator.last_update_success = False
    with pytest.raises(_UpdateFailed):
        asyncio.run(coordinator._async_update_data())
    assert status.extra_state_attributes["data_provenance"] == "unavailable"
    assert tracker.extra_state_attributes["data_provenance"] == "unavailable"
    assert "order_id" not in tracker_attrs
    assert "courier_lat" not in tracker_attrs
    assert "courier_lon" not in tracker_attrs
