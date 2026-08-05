"""Narrow Home Assistant adapter for the admin-only mock ordering surface."""

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
from .ordering_manager import OrderingError, OrderingUser
from .ordering_surface import Handler, PANEL_URL_PATH

_LOGGER = logging.getLogger(__name__)
_DATA_HANDLERS = "ordering_websocket_handlers"
_DATA_COMMANDS = "ordering_websocket_commands"
_DATA_STATIC_REGISTERED = "ordering_static_registered"
_STATIC_URL = "/glovo_ordering/glovo-ordering-panel.js"


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

        schema = {
            vol.Required("type"): name,
            vol.Optional("generation"): int,
            vol.Optional("storeKey"): str,
            vol.Optional("productKey"): str,
            vol.Optional("variantKey"): str,
            vol.Optional("modifierKeys"): [str],
            vol.Optional("quantity"): int,
            vol.Optional("addressKey"): str,
            vol.Optional("paymentKey"): str,
            vol.Optional("fingerprint"): str,
            vol.Optional("challenge"): str,
        }

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
                    message["id"], "ordering_disabled", "Mock ordering is disabled"
                )
                return
            try:
                user = OrderingUser(
                    user_id=connection.user.id,
                    is_admin=connection.user.is_admin,
                )
                result = await active(user, message)
            except (OrderingError, KeyError, TypeError, ValueError) as err:
                # Error classes/messages are deliberately generic and contain no basket,
                # payment, destination, credential, or challenge values.
                _LOGGER.debug("Rejected Glovo mock ordering request: %s", type(err).__name__)
                connection.send_error(
                    message["id"], "invalid_mock_ordering_request", str(err)
                )
                return
            connection.send_result(message["id"], result)

        websocket_api.async_register_command(self._hass, async_command)
        commands.add(name)
        handlers[name] = handler

    async def async_remove_handlers(self) -> None:
        # Home Assistant has no supported dynamic WebSocket unregister operation.
        # Remove every callable instead; retained command shells then fail closed.
        handlers: Mapping[str, Handler] = self._hass.data.get(DOMAIN, {}).get(
            _DATA_HANDLERS, {}
        )
        if hasattr(handlers, "clear"):
            handlers.clear()  # type: ignore[attr-defined]
