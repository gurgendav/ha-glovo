"""Config and options flow for the Glovo integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import glovo
from .const import (
    CONF_ALLOW_ORDERING,
    CONF_ORDERING_ACKNOWLEDGED,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .coordinator import GlovoConfigEntry

_LOGGER = logging.getLogger(__name__)

_REFRESH_TOKEN_SELECTOR = selector.TextSelector(
    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
)


def _scan_interval_selector() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=MIN_SCAN_INTERVAL,
            max=MAX_SCAN_INTERVAL,
            step=1,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="s",
        )
    )


async def _validate_refresh_token(hass, refresh_token: str) -> tuple[str | None, str | None]:
    """Validate a refresh token. Returns (token_json, error_code)."""
    try:
        token_json = await hass.async_add_executor_job(
            glovo.build_token_json, refresh_token
        )
    except glovo.GlovoApiError as err:
        if err.status in (400, 401, 403):
            return None, "invalid_auth"
        return None, "cannot_connect"
    except Exception:  # noqa: BLE001 - surface any failure as a flow error
        _LOGGER.exception("Unexpected error validating Glovo refresh token")
        return None, "cannot_connect"
    return token_json, None


class GlovoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Glovo config flow."""

    VERSION = 1
    MINOR_VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: GlovoConfigEntry) -> GlovoOptionsFlow:
        """Return the options flow handler."""
        return GlovoOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        errors: dict[str, str] = {}
        if user_input is not None:
            token_json, error = await _validate_refresh_token(
                self.hass, user_input[CONF_REFRESH_TOKEN]
            )
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Glovo",
                    data={CONF_TOKEN: token_json},
                    options={
                        CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                        CONF_ALLOW_ORDERING: False,
                        CONF_ORDERING_ACKNOWLEDGED: False,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_REFRESH_TOKEN): _REFRESH_TOKEN_SELECTOR,
                    vol.Required(
                        CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                    ): _scan_interval_selector(),
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication when the token gets rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user for a new refresh token."""
        errors: dict[str, str] = {}
        if user_input is not None:
            token_json, error = await _validate_refresh_token(
                self.hass, user_input[CONF_REFRESH_TOKEN]
            )
            if error:
                errors["base"] = error
            else:
                entry = self._get_reauth_entry()
                return self.async_update_reload_and_abort(
                    entry,
                    data={**entry.data, CONF_TOKEN: token_json},
                    options={
                        **entry.options,
                        CONF_ALLOW_ORDERING: False,
                        CONF_ORDERING_ACKNOWLEDGED: False,
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {vol.Required(CONF_REFRESH_TOKEN): _REFRESH_TOKEN_SELECTOR}
            ),
            errors=errors,
        )


class GlovoOptionsFlow(OptionsFlow):
    """Handle tracking options and explicit mock-ordering acknowledgement."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options; ordering is never inferred or enabled implicitly."""
        errors: dict[str, str] = {}
        current_interval = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        current_allow_ordering = (
            self.config_entry.options.get(CONF_ALLOW_ORDERING, False) is True
        )
        current_acknowledged = (
            current_allow_ordering
            and self.config_entry.options.get(CONF_ORDERING_ACKNOWLEDGED, False) is True
        )

        if user_input is not None:
            allow_ordering = user_input.get(CONF_ALLOW_ORDERING, False) is True
            acknowledged = user_input.get(CONF_ORDERING_ACKNOWLEDGED, False) is True
            if allow_ordering and not acknowledged:
                errors["base"] = "ordering_ack_required"

            refresh_token = (user_input.get(CONF_REFRESH_TOKEN) or "").strip()
            if refresh_token and not errors:
                token_json, error = await _validate_refresh_token(
                    self.hass, refresh_token
                )
                if error:
                    errors["base"] = error
                else:
                    self.hass.config_entries.async_update_entry(
                        self.config_entry,
                        data={**self.config_entry.data, CONF_TOKEN: token_json},
                    )
            if not errors:
                return self.async_create_entry(
                    data={
                        CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                        CONF_ALLOW_ORDERING: allow_ordering,
                        CONF_ORDERING_ACKNOWLEDGED: allow_ordering and acknowledged,
                    }
                )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL, default=current_interval
                    ): _scan_interval_selector(),
                    vol.Optional(CONF_REFRESH_TOKEN): _REFRESH_TOKEN_SELECTOR,
                    vol.Required(
                        CONF_ALLOW_ORDERING, default=current_allow_ordering
                    ): selector.BooleanSelector(),
                    vol.Required(
                        CONF_ORDERING_ACKNOWLEDGED, default=current_acknowledged
                    ): selector.BooleanSelector(),
                }
            ),
            errors=errors,
        )
