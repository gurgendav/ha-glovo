"""Sensor platform for the Glovo integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import glovo
from .coordinator import GlovoConfigEntry, GlovoDataUpdateCoordinator
from .entity import GlovoEntity


@dataclass(frozen=True, kw_only=True)
class GlovoSensorEntityDescription(SensorEntityDescription):
    """Describes a Glovo sensor."""

    value_fn: Callable[[dict[str, Any]], Any]
    primary: bool = False


SENSORS: tuple[GlovoSensorEntityDescription, ...] = (
    GlovoSensorEntityDescription(
        key="overall_status",
        translation_key="overall_status",
        device_class=SensorDeviceClass.ENUM,
        options=glovo.ha_enum_options("overall.status"),
        icon="mdi:moped",
        value_fn=lambda s: s.get("overall_status"),
        primary=True,
    ),
    GlovoSensorEntityDescription(
        key="step",
        translation_key="step",
        device_class=SensorDeviceClass.ENUM,
        options=glovo.ha_enum_options("tracking.step"),
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:progress-clock",
        value_fn=lambda s: s.get("step"),
    ),
    GlovoSensorEntityDescription(
        key="store_name",
        translation_key="store_name",
        icon="mdi:storefront",
        value_fn=lambda s: s.get("store_name"),
    ),
    GlovoSensorEntityDescription(
        key="order_count",
        translation_key="order_count",
        icon="mdi:format-list-numbered",
        value_fn=lambda s: s.get("order_count"),
    ),
    GlovoSensorEntityDescription(
        key="courier_name",
        translation_key="courier_name",
        icon="mdi:account",
        value_fn=lambda s: s.get("courier_name"),
    ),
    GlovoSensorEntityDescription(
        key="courier_status",
        translation_key="courier_status",
        device_class=SensorDeviceClass.ENUM,
        options=glovo.ha_enum_options("tracking.courierStatus"),
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:bike-fast",
        value_fn=lambda s: s.get("courier_status"),
    ),
    GlovoSensorEntityDescription(
        key="partner_status",
        translation_key="partner_status",
        device_class=SensorDeviceClass.ENUM,
        options=glovo.ha_enum_options("tracking.partnerStatus"),
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:chef-hat",
        value_fn=lambda s: s.get("partner_status"),
    ),
    GlovoSensorEntityDescription(
        key="progress_percent",
        translation_key="progress_percent",
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:percent",
        value_fn=lambda s: s.get("progress_percent"),
    ),
    GlovoSensorEntityDescription(
        key="eta_min",
        translation_key="eta_min",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        icon="mdi:clock-start",
        value_fn=lambda s: s.get("eta_min"),
    ),
    GlovoSensorEntityDescription(
        key="eta_max",
        translation_key="eta_max",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        icon="mdi:clock-end",
        value_fn=lambda s: s.get("eta_max"),
    ),
    GlovoSensorEntityDescription(
        key="original_eta",
        translation_key="original_eta",
        icon="mdi:clock-outline",
        value_fn=lambda s: s.get("original_eta"),
    ),
    GlovoSensorEntityDescription(
        key="poll_interval_sec",
        translation_key="poll_interval_sec",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:timer-sync-outline",
        value_fn=lambda s: s.get("poll_interval_sec"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GlovoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Glovo sensors."""
    coordinator = entry.runtime_data
    async_add_entities(GlovoSensor(coordinator, desc) for desc in SENSORS)


class GlovoSensor(GlovoEntity, SensorEntity):
    """A single Glovo order field as a sensor."""

    entity_description: GlovoSensorEntityDescription

    def __init__(
        self,
        coordinator: GlovoDataUpdateCoordinator,
        description: GlovoSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        """Return the current value."""
        return self.entity_description.value_fn(self.coordinator.data or {})

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the full summary on the primary status sensor."""
        if not self.entity_description.primary:
            return None
        attributes = dict(self.coordinator.data or {})
        # Override retained coordinator.data on failures with the coordinator's
        # fail-closed runtime provenance and immutable build pin.
        attributes.update(self.coordinator.access_control_attributes())
        return attributes
