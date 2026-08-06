"""DataUpdateCoordinator for the Glovo integration."""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import timedelta
from functools import partial
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as ha_dt

from . import glovo
from .api_session import ApiSessionError, SerializedApiSession
from .const import (
    CACHE_TERMINAL_HOLD_SEC,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN,
    DEFAULT_SCAN_INTERVAL,
    DATA_PROVENANCE_ATTRIBUTE,
    DOMAIN,
    FIXTURES_REL_PATH,
    INTEGRATION_TRUST_ID,
    INTEGRATION_TRUST_ID_ATTRIBUTE,
    PROVENANCE_FIXTURE,
    PROVENANCE_LIVE_API,
    PROVENANCE_UNAVAILABLE,
    PROVENANCE_UNKNOWN,
    TERMINAL_OVERALL_STATUSES,
)

_LOGGER = logging.getLogger(__name__)

GlovoConfigEntry = ConfigEntry["GlovoDataUpdateCoordinator"]

_REQUIRED_SUMMARY_KEYS = frozenset(
    {
        "active",
        "courier_lat",
        "courier_lon",
        "order_count",
        "order_id",
        "overall_status",
        "store_lat",
        "store_lon",
    }
)


class GlovoDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll the Glovo API for the last active order summary."""

    config_entry: GlovoConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: GlovoConfigEntry,
        api_session: SerializedApiSession | None = None,
    ) -> None:
        """Initialize the coordinator."""
        self._base_interval = timedelta(
            seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        # Grace-period cache: remember the last order we tracked so its final
        # status keeps showing for CACHE_TERMINAL_HOLD_SEC after it leaves the
        # active list, instead of immediately flipping to "unknown".
        self._cached_order_id: int | str | None = None
        self._terminal_since: float | None = None
        # Remember the first ETA text seen for the displayed order so it stays
        # constant throughout the order lifecycle (original_eta).
        self._original_eta_order_id: int | str | None = None
        self._original_eta_value: str | None = None
        # This state is authoritative for access control. It is intentionally
        # separate from coordinator.data because DataUpdateCoordinator retains
        # stale data after a failed refresh.
        self._runtime_provenance = PROVENANCE_UNKNOWN
        self.api_session = api_session
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=self._base_interval,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        # Fail closed while this refresh is in flight. The source is promoted
        # only after its fetch, parse, validation, and local transformations all
        # complete successfully.
        self._runtime_provenance = PROVENANCE_UNAVAILABLE
        fallback_order_id = self._cached_order_id
        now = ha_dt.now(ha_dt.get_time_zone(self.hass.config.time_zone))
        fixtures_dir = self.hass.config.path(FIXTURES_REL_PATH)
        if glovo.fixtures_available(fixtures_dir):
            source = PROVENANCE_FIXTURE
            _LOGGER.warning(
                "Serving Glovo data from local fixtures in %s (API disabled)",
                fixtures_dir,
            )
            try:
                summary = await self.hass.async_add_executor_job(
                    partial(
                        glovo.get_last_active_order_summary_from_fixtures,
                        fixtures_dir,
                        fallback_order_id=fallback_order_id,
                        now=now,
                    )
                )
            except (OSError, json.JSONDecodeError, RuntimeError) as err:
                raise UpdateFailed(f"Invalid Glovo fixture files: {err}") from err
        else:
            source = PROVENANCE_LIVE_API
            token_json = self.config_entry.data[CONF_TOKEN]
            try:
                if self.api_session is not None:
                    summary = await self.api_session.async_legacy_read(
                        lambda current_token: glovo.get_last_active_order_summary(
                            current_token,
                            fallback_order_id=fallback_order_id,
                            now=now,
                        )
                    )
                    new_token = token_json
                else:
                    summary, new_token = await self.hass.async_add_executor_job(
                        partial(
                            glovo.get_last_active_order_summary,
                            token_json,
                            fallback_order_id=fallback_order_id,
                            now=now,
                        )
                    )
            except ApiSessionError as err:
                if err.category == "auth" or err.status in (401, 403):
                    raise ConfigEntryAuthFailed(
                        "Glovo token rejected, re-authentication required"
                    ) from err
                raise UpdateFailed("Glovo API read failed") from err
            except glovo.GlovoApiError as err:
                if err.status in (401, 403):
                    raise ConfigEntryAuthFailed(
                        "Glovo token rejected, re-authentication required"
                    ) from err
                raise UpdateFailed(f"Glovo API error: {err}") from err
            except RuntimeError as err:
                # Raised when the refresh token is missing/invalid.
                raise ConfigEntryAuthFailed(str(err)) from err
            except (TypeError, ValueError) as err:
                raise UpdateFailed("Malformed Glovo API result") from err

            if self.api_session is None and new_token != token_json:
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data={**self.config_entry.data, CONF_TOKEN: new_token},
                )

        summary = self._validated_summary(summary)
        try:
            summary = self._apply_terminal_cache(summary)
            summary = self._apply_original_eta(summary)
            self._apply_dynamic_interval(summary)
        except (TypeError, ValueError, OverflowError) as err:
            raise UpdateFailed("Malformed Glovo summary values") from err

        # Overwrite any same-named API/fixture keys. Only this coordinator branch
        # may assert provenance or the build pin.
        self._runtime_provenance = source
        summary.update(self.access_control_attributes())
        return summary

    @property
    def runtime_provenance(self) -> str:
        """Return authoritative provenance for the current refresh state."""
        return self._runtime_provenance

    @property
    def integration_trust_id(self) -> str:
        """Return the stable build identifier intended for policy pinning."""
        return INTEGRATION_TRUST_ID

    def access_control_attributes(self) -> dict[str, str]:
        """Return fail-closed attributes shared by status and courier entities."""
        return {
            DATA_PROVENANCE_ATTRIBUTE: self._runtime_provenance,
            INTEGRATION_TRUST_ID_ATTRIBUTE: INTEGRATION_TRUST_ID,
        }

    @staticmethod
    def _validated_summary(summary: Any) -> dict[str, Any]:
        """Validate the minimum normalized summary contract without coercion."""
        if not isinstance(summary, dict) or not summary:
            raise UpdateFailed("Empty or malformed Glovo summary")

        missing = _REQUIRED_SUMMARY_KEYS.difference(summary)
        if missing:
            raise UpdateFailed("Incomplete Glovo summary")

        order_count = summary["order_count"]
        if isinstance(order_count, bool) or not isinstance(order_count, int):
            raise UpdateFailed("Invalid Glovo order count")
        if order_count < 0:
            raise UpdateFailed("Invalid Glovo order count")

        order_id = summary["order_id"]
        if isinstance(order_id, bool) or (
            order_id is not None and not isinstance(order_id, (int, str))
        ):
            raise UpdateFailed("Invalid Glovo order identifier")
        if order_count > 0 and order_id is None:
            raise UpdateFailed("Active Glovo summary has no order identifier")

        active = summary["active"]
        if active is not None and not isinstance(active, bool):
            raise UpdateFailed("Invalid Glovo active flag")

        status = summary["overall_status"]
        if status is not None and not isinstance(status, str):
            raise UpdateFailed("Invalid Glovo status")

        coordinate_bounds = {
            "courier_lat": (-90, 90),
            "courier_lon": (-180, 180),
            "store_lat": (-90, 90),
            "store_lon": (-180, 180),
        }
        for key, (lower, upper) in coordinate_bounds.items():
            value = summary[key]
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise UpdateFailed("Invalid Glovo coordinate")
            try:
                valid_coordinate = math.isfinite(value) and lower <= value <= upper
            except (OverflowError, TypeError):
                valid_coordinate = False
            if not valid_coordinate:
                raise UpdateFailed("Invalid Glovo coordinate")

        return summary

    def _apply_terminal_cache(self, summary: dict[str, Any]) -> dict[str, Any]:
        """Hold a just-finished order's status for a short grace period.

        Glovo drops delivered/canceled orders from the active list immediately,
        so without this the status would jump straight to "unknown". We remember
        the order id and keep surfacing its final state until it has been
        terminal for CACHE_TERMINAL_HOLD_SEC, then forget it.
        """
        order_id = summary.get("order_id")
        status = summary.get("overall_status")
        order_count = summary.get("order_count") or 0
        is_terminal = status in TERMINAL_OVERALL_STATUSES
        now = time.monotonic()

        if order_count > 0:
            # An order is active right now: track it and disarm the hold timer.
            self._cached_order_id = order_id
            self._terminal_since = None
            return summary

        # No active order. If the summary describes our cached order (fallback
        # tracking succeeded), decide whether to keep showing it.
        if (
            self._cached_order_id is not None
            and order_id == self._cached_order_id
            and status is not None
        ):
            if is_terminal:
                if self._terminal_since is None:
                    self._terminal_since = now
                elif now - self._terminal_since >= CACHE_TERMINAL_HOLD_SEC:
                    self._forget_cached_order()
                    return glovo.empty_active_order_summary()
            else:
                # Still not terminal (rare): keep showing without arming timer.
                self._terminal_since = None
            return summary

        # Nothing active and no usable cached order: report "no order".
        self._forget_cached_order()
        return glovo.empty_active_order_summary()

    def _forget_cached_order(self) -> None:
        """Clear the grace-period cache."""
        self._cached_order_id = None
        self._terminal_since = None

    def _apply_original_eta(self, summary: dict[str, Any]) -> dict[str, Any]:
        """Pin original_eta to the first ETA value seen for the current order.

        Resets when the displayed order id changes or the order disappears.
        """
        order_id = summary.get("order_id")

        if order_id is None:
            self._original_eta_order_id = None
            self._original_eta_value = None
            return summary

        if order_id != self._original_eta_order_id:
            self._original_eta_order_id = order_id
            self._original_eta_value = None

        current_eta = summary.get("eta_former") or summary.get("eta_text")
        has_numeric_eta = summary.get("eta_min") is not None
        if self._original_eta_value is None and current_eta and has_numeric_eta:
            self._original_eta_value = current_eta

        summary["original_eta"] = self._original_eta_value
        return summary

    def _apply_dynamic_interval(self, summary: dict[str, Any]) -> None:
        """Use the API-recommended poll interval while an order is active."""
        poll_interval = summary.get("poll_interval_sec")
        if summary.get("active") and poll_interval:
            self.update_interval = timedelta(seconds=float(poll_interval))
        else:
            self.update_interval = self._base_interval
