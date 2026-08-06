"""The Glovo integration."""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant

from . import glovo
from .api_session import SerializedApiSession
from .const import (
    CONF_ALLOW_ORDERING,
    CONF_ORDERING_ACKNOWLEDGED,
    CONF_TOKEN,
    PLATFORMS,
)
from .coordinator import GlovoConfigEntry, GlovoDataUpdateCoordinator
from .ordering_adapter import MockCheckoutAdapter
from .ordering_catalog import SyntheticCatalogProvider
from .ordering_journal import AttemptJournal, HomeAssistantJournalStorage
from .ordering_manager import OrderingManager
from .ordering_account import AccountClient
from .ordering_live_catalog import LiveCatalogClient
from .ordering_state import (
    DurableOrderingState,
    HomeAssistantOrderingStateStorage,
    OrderingStateFault,
)

_LOGGER = logging.getLogger(__name__)
CONFIG_ENTRY_VERSION = 1
CONFIG_ENTRY_MINOR_VERSION = 2


def _ordering_options(entry: GlovoConfigEntry) -> dict[str, object]:
    """Read, rather than cache, both explicit ordering gates."""
    return {
        CONF_ALLOW_ORDERING: entry.options.get(CONF_ALLOW_ORDERING, False),
        CONF_ORDERING_ACKNOWLEDGED: entry.options.get(
            CONF_ORDERING_ACKNOWLEDGED, False
        ),
    }


async def async_setup_entry(hass: HomeAssistant, entry: GlovoConfigEntry) -> bool:
    """Set up tracking and an independently gated fixture-only ordering runtime."""
    durable_state = DurableOrderingState(
        HomeAssistantOrderingStateStorage(hass, entry.entry_id)
    )
    try:
        # Durable authority is loaded before manager construction so reloads and
        # Home Assistant restarts can never reset generation to one.
        await durable_state.async_load()
    except OrderingStateFault:
        _LOGGER.error("Glovo mock ordering safety state is unavailable; ordering disabled")

    options = _ordering_options(entry)
    journal = AttemptJournal(
        HomeAssistantJournalStorage(hass, entry.entry_id),
        clock=time.time,
    )
    ordering_manager = OrderingManager(
        allow_ordering=options[CONF_ALLOW_ORDERING],
        ordering_acknowledged=options[CONF_ORDERING_ACKNOWLEDGED],
        live_options=lambda: entry.options,
        catalog=SyntheticCatalogProvider(clock=time.time),
        journal=journal,
        checkout_adapter=MockCheckoutAdapter(),
        clock=time.time,
        durable_state=durable_state,
    )
    await ordering_manager.async_initialize()

    async def persist_token(token_json: str) -> None:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_TOKEN: token_json},
        )

    # One lock owns access/rotating-refresh-token use and persistence for both
    # legacy tracking and every private live-ordering GET client.
    api_session = SerializedApiSession(
        token_source=lambda: entry.data[CONF_TOKEN],
        persist_token=persist_token,
        ensure_token=glovo.ensure_access_token,
        transport=glovo.single_attempt_authed_get,
        executor=hass.async_add_executor_job,
    )
    coordinator = GlovoDataUpdateCoordinator(hass, entry, api_session)
    coordinator._account_client = AccountClient(api_session)  # noqa: SLF001
    coordinator._catalog_client = LiveCatalogClient(api_session)  # noqa: SLF001

    ordering_surface = None
    if ordering_manager.enabled or ordering_manager.recovery_required:
        try:
            # Recovery remains registered while a manual/integrity block exists,
            # even when the default-off mutation gates are closed.
            from .ordering_ha import HomeAssistantOrderingSurfaceAdapter
            from .ordering_surface import OrderingSurface

            ordering_surface = OrderingSurface(
                ordering_manager,
                HomeAssistantOrderingSurfaceAdapter(hass),
            )
            await ordering_surface.async_setup()
        except Exception:  # noqa: BLE001 - recovery failure must fail setup closed
            if ordering_manager.recovery_required:
                _LOGGER.exception(
                    "Glovo ordering recovery API unavailable; setup remains blocked"
                )
                raise
            _LOGGER.exception(
                "Glovo mock ordering API unavailable; tracking will continue safely"
            )
            await ordering_manager.async_set_enabled(False)
            ordering_surface = None

    # Keep the coordinator as runtime_data to preserve every existing platform,
    # entity id, provenance check, and trust identity. Ordering is account-scoped
    # auxiliary runtime state and is never exposed as entity state.
    coordinator.ordering_manager = ordering_manager
    coordinator.ordering_surface = ordering_surface
    coordinator.api_session = api_session
    # Config-entry update listeners run for both options and internal data. Keep
    # the options applied to this runtime so token persistence can be ignored
    # without missing a real scan-interval or ordering-gate change.
    coordinator.loaded_options = dict(entry.options)
    entry.runtime_data = coordinator
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_shutdown_ordering(entry: GlovoConfigEntry) -> None:
    coordinator = entry.runtime_data
    manager: OrderingManager | None = getattr(coordinator, "ordering_manager", None)
    surface: Any = getattr(coordinator, "ordering_surface", None)
    account_client: Any = getattr(coordinator, "_account_client", None)
    api_session: Any = getattr(coordinator, "api_session", None)
    if account_client is not None:
        account_client.invalidate()
    if api_session is not None:
        api_session.invalidate()
    if manager is not None:
        await manager.async_set_enabled(False)
    if surface is not None:
        # HA retains WebSocket command shells. Every unload/reload boundary must
        # remove their callables; a subsequent setup restores recovery from the
        # durable journal/latch before exposing any normal ordering handlers.
        await surface.async_unload()


async def async_unload_entry(hass: HomeAssistant, entry: GlovoConfigEntry) -> bool:
    """Durably invalidate authority and remove the panel before tracking unload."""
    await _async_shutdown_ordering(entry)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: GlovoConfigEntry) -> None:
    """Reload only when user-facing options changed, not for token persistence."""
    coordinator = entry.runtime_data
    if dict(entry.options) == coordinator.loaded_options:
        return

    options = _ordering_options(entry)
    if not (
        options[CONF_ALLOW_ORDERING] is True
        and options[CONF_ORDERING_ACKNOWLEDGED] is True
    ):
        await _async_shutdown_ordering(entry)
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: Any) -> bool:
    """Force a fresh ordering opt-in for every entry predating minor version two."""
    if entry.version != CONFIG_ENTRY_VERSION:
        return False
    options = dict(entry.options)
    minor_version = getattr(entry, "minor_version", 1)
    migrating_to_opt_in = (
        isinstance(minor_version, bool)
        or not isinstance(minor_version, int)
        or minor_version < CONFIG_ENTRY_MINOR_VERSION
    )
    gates_present_and_boolean = all(
        key in options and isinstance(options[key], bool)
        for key in (CONF_ALLOW_ORDERING, CONF_ORDERING_ACKNOWLEDGED)
    )
    if migrating_to_opt_in or not gates_present_and_boolean:
        allow_ordering = False
        acknowledged = False
    else:
        allow_ordering = options[CONF_ALLOW_ORDERING] is True
        acknowledged = (
            allow_ordering and options[CONF_ORDERING_ACKNOWLEDGED] is True
        )
    normalized = {
        **options,
        CONF_ALLOW_ORDERING: allow_ordering,
        CONF_ORDERING_ACKNOWLEDGED: acknowledged,
    }
    if normalized != options or migrating_to_opt_in:
        hass.config_entries.async_update_entry(
            entry,
            options=normalized,
            version=CONFIG_ENTRY_VERSION,
            minor_version=CONFIG_ENTRY_MINOR_VERSION,
        )
    return True
