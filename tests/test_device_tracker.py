"""Unit tests for Glovo device tracker entities without a HA runtime."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


class _SourceType(Enum):
    GPS = "gps"


class _TrackerEntity:
    """Minimal stand-in for Home Assistant's TrackerEntity."""


class _GlovoEntity:
    """Minimal stand-in that preserves coordinator availability semantics."""

    def __init__(self, coordinator: Any, key: str) -> None:
        self.coordinator = coordinator
        self.key = key

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success


class _Coordinator:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = data
        self.last_update_success = True
        self.config_entry = SimpleNamespace(entry_id="entry-1")

    @staticmethod
    def access_control_attributes() -> dict[str, str]:
        return {
            "data_provenance": "live_api",
            "integration_trust_id": "test-build-sha256",
        }


@pytest.fixture(scope="module")
def tracker_module() -> ModuleType:
    """Load device_tracker.py with the narrow interfaces it consumes stubbed."""
    package_name = "glovo_tracker_under_test"
    package = ModuleType(package_name)
    package.__path__ = []  # type: ignore[attr-defined]
    sys.modules[package_name] = package

    ha = ModuleType("homeassistant")
    components = ModuleType("homeassistant.components")
    device_tracker = ModuleType("homeassistant.components.device_tracker")
    device_tracker.SourceType = _SourceType
    device_tracker.TrackerEntity = _TrackerEntity
    core = ModuleType("homeassistant.core")
    core.HomeAssistant = object
    helpers = ModuleType("homeassistant.helpers")
    entity_platform = ModuleType("homeassistant.helpers.entity_platform")
    entity_platform.AddConfigEntryEntitiesCallback = object

    stubs = {
        "homeassistant": ha,
        "homeassistant.components": components,
        "homeassistant.components.device_tracker": device_tracker,
        "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.entity_platform": entity_platform,
    }
    previous = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)

    coordinator = ModuleType(f"{package_name}.coordinator")
    coordinator.GlovoConfigEntry = object
    coordinator.GlovoDataUpdateCoordinator = _Coordinator
    entity = ModuleType(f"{package_name}.entity")
    entity.GlovoEntity = _GlovoEntity
    sys.modules[coordinator.__name__] = coordinator
    sys.modules[entity.__name__] = entity

    path = (
        Path(__file__).parents[1]
        / "custom_components"
        / "glovo"
        / "device_tracker.py"
    )
    module_name = f"{package_name}.device_tracker"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    yield module

    for name, old_module in previous.items():
        if old_module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = old_module
    for name in (
        module_name,
        coordinator.__name__,
        entity.__name__,
        package_name,
    ):
        sys.modules.pop(name, None)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Perhat", "PE"),
        ("  perhat! ", "PE"),
        ("Élodie", "ÉL"),
        ("E\u0301lodie", "ÉL"),
        ("Перхат", "ПЕ"),
        ("李雷", "李雷"),
        ("", "CR"),
        (None, "CR"),
        ("123 -", "CR"),
        ("A", "CR"),
    ],
)
def test_courier_initials_are_two_unicode_letters_or_fallback(
    tracker_module: ModuleType,
    name: str | None,
    expected: str,
) -> None:
    assert tracker_module._courier_initials(name) == expected


def test_setup_registers_stable_courier_and_store_entities(
    tracker_module: ModuleType,
) -> None:
    coordinator = _Coordinator()
    entry = SimpleNamespace(runtime_data=coordinator)
    added: list[Any] = []

    asyncio.run(
        tracker_module.async_setup_entry(
            object(),
            entry,
            lambda entities: added.extend(entities),
        )
    )

    assert [entity.key for entity in added] == ["courier", "store"]
    assert isinstance(added[0], tracker_module.GlovoCourierTracker)
    assert isinstance(added[1], tracker_module.GlovoStoreTracker)
    assert added[1]._attr_translation_key == "store"


def test_store_tracker_availability_tracks_coordinate_presence(
    tracker_module: ModuleType,
) -> None:
    coordinator = _Coordinator({"store_lat": None, "store_lon": None})
    tracker = tracker_module.GlovoStoreTracker(coordinator)

    assert tracker.available is False
    assert tracker.latitude is None
    assert tracker.longitude is None

    coordinator.data = {"store_lat": 41.0082, "store_lon": None}
    assert tracker.available is False

    coordinator.data = {"store_lat": 41.0082, "store_lon": 28.9784}
    assert tracker.available is True
    assert tracker.latitude == pytest.approx(41.0082)
    assert tracker.longitude == pytest.approx(28.9784)

    coordinator.last_update_success = False
    assert tracker.available is False


def test_courier_initials_update_without_sensitive_attributes(
    tracker_module: ModuleType,
) -> None:
    coordinator = _Coordinator(
        {
            "courier_name": "Perhat",
            "courier_lat": 41.1,
            "courier_lon": 29.1,
            "courier_heading": 120,
            "courier_count": 1,
            "courier_phone": "+000000",
            "order_id": 12345,
        }
    )
    tracker = tracker_module.GlovoCourierTracker(coordinator)

    assert tracker.extra_state_attributes["courier_initials"] == "PE"
    coordinator.data["courier_name"] = "Élodie"
    assert tracker.extra_state_attributes["courier_initials"] == "ÉL"
    coordinator.data["courier_name"] = ""
    assert tracker.extra_state_attributes["courier_initials"] == "CR"
    assert tracker.extra_state_attributes["data_provenance"] == "live_api"
    assert (
        tracker.extra_state_attributes["integration_trust_id"]
        == "test-build-sha256"
    )
    assert "courier_phone" not in tracker.extra_state_attributes
    assert "order_id" not in tracker.extra_state_attributes
