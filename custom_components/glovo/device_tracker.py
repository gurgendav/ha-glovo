"""Device tracker platform for Glovo courier and store locations."""

from __future__ import annotations

import unicodedata
from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import GlovoConfigEntry, GlovoDataUpdateCoordinator
from .entity import GlovoEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GlovoConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Glovo location trackers."""
    coordinator = entry.runtime_data
    # Register both entities once. Coordinate-dependent availability lets a
    # store marker appear on a later refresh without dynamic entity creation.
    async_add_entities(
        [GlovoCourierTracker(coordinator), GlovoStoreTracker(coordinator)]
    )


def _courier_initials(name: Any) -> str:
    """Return two uppercase Unicode letters from a courier name, or ``CR``."""
    if not isinstance(name, str):
        return "CR"
    normalized = unicodedata.normalize("NFC", name)
    letters = [character for character in normalized if character.isalpha()]
    if len(letters) < 2:
        return "CR"

    # Some Unicode uppercase mappings expand (for example, ß -> SS), so trim
    # after casing as well to keep the map label strictly two code points.
    uppercase_letters = [
        character
        for character in "".join(letters[:2]).upper()
        if character.isalpha()
    ]
    return "".join(uppercase_letters[:2]) if len(uppercase_letters) >= 2 else "CR"


class GlovoCourierTracker(GlovoEntity, TrackerEntity):
    """Live location of the courier delivering the active order."""

    _attr_translation_key = "courier"
    _attr_icon = "mdi:moped"
    _attr_entity_category = None

    def __init__(self, coordinator: GlovoDataUpdateCoordinator) -> None:
        """Initialize the tracker."""
        super().__init__(coordinator, "courier")

    @property
    def source_type(self) -> SourceType:
        """Return the source type."""
        return SourceType.GPS

    @property
    def latitude(self) -> float | None:
        """Return the courier latitude."""
        value = (self.coordinator.data or {}).get("courier_lat")
        return float(value) if value is not None else None

    @property
    def longitude(self) -> float | None:
        """Return the courier longitude."""
        value = (self.coordinator.data or {}).get("courier_lon")
        return float(value) if value is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional courier attributes."""
        data = self.coordinator.data or {}
        attributes = {
            "heading": data.get("courier_heading"),
            "courier_name": data.get("courier_name"),
            "courier_initials": _courier_initials(data.get("courier_name")),
            "courier_count": data.get("courier_count"),
        }
        attributes.update(self.coordinator.access_control_attributes())
        return attributes


class GlovoStoreTracker(GlovoEntity, TrackerEntity):
    """Pickup location for the active order."""

    _attr_translation_key = "store"
    _attr_icon = "mdi:store-marker"
    _attr_entity_category = None

    def __init__(self, coordinator: GlovoDataUpdateCoordinator) -> None:
        """Initialize the tracker."""
        super().__init__(coordinator, "store")

    @property
    def source_type(self) -> SourceType:
        """Return the source type."""
        return SourceType.GPS

    @property
    def available(self) -> bool:
        """Return whether the coordinator has one unambiguous pickup point."""
        return (
            super().available
            and self.latitude is not None
            and self.longitude is not None
        )

    @property
    def latitude(self) -> float | None:
        """Return the pickup latitude."""
        value = (self.coordinator.data or {}).get("store_lat")
        return float(value) if value is not None else None

    @property
    def longitude(self) -> float | None:
        """Return the pickup longitude."""
        value = (self.coordinator.data or {}).get("store_lon")
        return float(value) if value is not None else None
