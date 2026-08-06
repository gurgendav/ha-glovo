"""Strict Home Assistant WebSocket adapter for ordering and recovery."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.components import frontend, panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .ordering_manager import (
    InvalidConfirmation,
    InvalidManualResolution,
    OrderingAdminRequired,
    OrderingDisabled,
    OrderingError,
    OrderingManualCheckRequired,
    OrderingRecoveryWriteFailed,
    OrderingSecurityFault,
    OrderingUser,
    StaleOrderingGeneration,
)
from .ordering_surface import Handler, PANEL_URL_PATH

_LOGGER = logging.getLogger(__name__)
_DATA_HANDLERS = "ordering_websocket_handlers"
_DATA_COMMANDS = "ordering_websocket_commands"
_DATA_STATIC_REGISTERED = "ordering_static_registered"
_STATIC_URL = "/glovo_ordering/glovo-ordering-panel.js"


def _strict_positive_int(value: object) -> int:
    """Reject bool-as-int coercion at the Home Assistant routing boundary."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise vol.Invalid("expected positive integer")
    return value


_MANUAL_STATES = vol.In(
    {"MANUAL_CHECK_REQUIRED", "CONFIRMED_SUCCEEDED", "CONFIRMED_FAILED"}
)
_MANUAL_RESOLUTIONS = vol.In(
    {"found_succeeded", "found_failed_or_cancelled", "still_unknown"}
)

_COMMAND_SCHEMAS: dict[str, dict[Any, Any]] = {
    "glovo/ordering/state": {vol.Required("type"): "glovo/ordering/state"},
    "glovo/ordering/manual_checks": {
        vol.Required("type"): "glovo/ordering/manual_checks"
    },
    "glovo/ordering/manual_check": {
        vol.Required("type"): "glovo/ordering/manual_check",
        vol.Required("attemptRef"): str,
    },
    "glovo/ordering/prepare_manual_resolution": {
        vol.Required("type"): "glovo/ordering/prepare_manual_resolution",
        vol.Required("attemptRef"): str,
        vol.Required("expectedRecordRevision"): _strict_positive_int,
        vol.Required("expectedState"): _MANUAL_STATES,
        vol.Required("resolution"): _MANUAL_RESOLUTIONS,
    },
    "glovo/ordering/resolve_manual_check": {
        vol.Required("type"): "glovo/ordering/resolve_manual_check",
        vol.Required("attemptRef"): str,
        vol.Required("expectedRecordRevision"): _strict_positive_int,
        vol.Required("expectedState"): _MANUAL_STATES,
        vol.Required("resolution"): _MANUAL_RESOLUTIONS,
        vol.Required("challenge"): str,
        vol.Required("acknowledged"): bool,
    },
    "glovo/ordering/catalog": {
        vol.Required("type"): "glovo/ordering/catalog",
        vol.Required("generation"): int,
    },
    "glovo/ordering/basket": {
        vol.Required("type"): "glovo/ordering/basket",
        vol.Required("generation"): int,
    },
    "glovo/ordering/basket_add_fixture_item": {
        vol.Required("type"): "glovo/ordering/basket_add_fixture_item",
        vol.Required("generation"): int,
        vol.Required("storeKey"): str,
        vol.Required("productKey"): str,
        vol.Required("variantKey"): str,
        vol.Optional("modifierKeys", default=[]): [str],
        vol.Required("quantity"): int,
    },
    "glovo/ordering/basket_clear": {
        vol.Required("type"): "glovo/ordering/basket_clear",
        vol.Required("generation"): int,
    },
    "glovo/ordering/fixture_quote": {
        vol.Required("type"): "glovo/ordering/fixture_quote",
        vol.Required("generation"): int,
        vol.Required("addressKey"): str,
        vol.Required("paymentKey"): str,
    },
    "glovo/ordering/prepare_mock_confirmation": {
        vol.Required("type"): "glovo/ordering/prepare_mock_confirmation",
        vol.Required("generation"): int,
        vol.Required("fingerprint"): str,
    },
    "glovo/ordering/execute_mock_checkout": {
        vol.Required("type"): "glovo/ordering/execute_mock_checkout",
        vol.Required("generation"): int,
        vol.Required("fingerprint"): str,
        vol.Required("challenge"): str,
    },
}


def _error_code(error: Exception) -> tuple[str, str]:
    if isinstance(error, OrderingAdminRequired):
        return "admin_required", "Administrator access is required"
    if isinstance(error, OrderingManualCheckRequired):
        return "manual_check_required", "Manual account reconciliation is required"
    if isinstance(error, OrderingSecurityFault):
        return "ordering_integrity_fault", "Ordering is blocked by an integrity fault"
    if isinstance(error, InvalidManualResolution):
        return "invalid_manual_resolution", "Manual resolution request is invalid"
    if isinstance(error, OrderingRecoveryWriteFailed):
        return "recovery_write_failed", "Recovery was not persisted; ordering remains blocked"
    if isinstance(error, StaleOrderingGeneration):
        return "stale_ordering_generation", "Ordering state changed"
    if isinstance(error, InvalidConfirmation):
        return "invalid_confirmation", "Confirmation is invalid or expired"
    if isinstance(error, OrderingDisabled):
        return "ordering_disabled", "Ordering is disabled"
    return "invalid_ordering_request", "Ordering request is invalid"


class HomeAssistantOrderingSurfaceAdapter:
    """Translate tested registration semantics to Home Assistant APIs."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._panel_registered = False
        domain_data = hass.data.setdefault(DOMAIN, {})
        domain_data.setdefault(_DATA_HANDLERS, {})
        domain_data.setdefault(_DATA_COMMANDS, set())
        domain_data.setdefault(_DATA_STATIC_REGISTERED, False)

    async def async_register_panel(
        self,
        *,
        url_path: str,
        title: str,
        icon: str,
        require_admin: bool,
    ) -> None:
        domain_data = self._hass.data[DOMAIN]
        if not domain_data[_DATA_STATIC_REGISTERED]:
            panel_file = Path(__file__).with_name("frontend") / "glovo-ordering-panel.js"
            await self._hass.http.async_register_static_paths(
                [StaticPathConfig(_STATIC_URL, str(panel_file), cache_headers=False)]
            )
            domain_data[_DATA_STATIC_REGISTERED] = True
        self._panel_registered = True
        try:
            await panel_custom.async_register_panel(
                self._hass,
                frontend_url_path=url_path,
                webcomponent_name="glovo-ordering-panel",
                sidebar_title=title,
                sidebar_icon=icon,
                module_url=_STATIC_URL,
                embed_iframe=False,
                trust_external=False,
                require_admin=require_admin,
            )
        except Exception:
            frontend.async_remove_panel(self._hass, PANEL_URL_PATH)
            self._panel_registered = False
            raise

    async def async_remove_panel(self) -> None:
        if self._panel_registered:
            frontend.async_remove_panel(self._hass, PANEL_URL_PATH)
            self._panel_registered = False

    async def async_register_handler(self, name: str, handler: Handler) -> None:
        domain_data = self._hass.data[DOMAIN]
        handlers: dict[str, Handler] = domain_data[_DATA_HANDLERS]
        commands: set[str] = domain_data[_DATA_COMMANDS]
        if name in commands:
            handlers[name] = handler
            return
        schema = _COMMAND_SCHEMAS.get(name)
        if schema is None:
            raise ValueError("unsupported ordering command")

        @websocket_api.websocket_command(schema)
        @websocket_api.require_admin
        @websocket_api.async_response
        async def async_command(
            hass: HomeAssistant,
            connection: websocket_api.ActiveConnection,
            message: dict[str, Any],
        ) -> None:
            active = hass.data.get(DOMAIN, {}).get(_DATA_HANDLERS, {}).get(name)
            if active is None:
                connection.send_error(
                    message["id"], "ordering_disabled", "Ordering recovery is unavailable"
                )
                return
            try:
                user = OrderingUser(
                    user_id=connection.user.id,
                    is_admin=connection.user.is_admin,
                )
                result = await active(user, message)
            except (OrderingError, KeyError, TypeError, ValueError) as err:
                code, public_message = _error_code(err)
                _LOGGER.debug("Rejected Glovo ordering request: %s", type(err).__name__)
                connection.send_error(message["id"], code, public_message)
                return
            connection.send_result(message["id"], result)

        websocket_api.async_register_command(self._hass, async_command)
        commands.add(name)
        handlers[name] = handler

    async def async_remove_handlers(self) -> None:
        # HA has no supported dynamic WebSocket unregister operation. Retained
        # command shells contain no callable and return a stable disabled error.
        handlers: Mapping[str, Handler] = self._hass.data.get(DOMAIN, {}).get(
            _DATA_HANDLERS, {}
        )
        if hasattr(handlers, "clear"):
            handlers.clear()  # type: ignore[attr-defined]
