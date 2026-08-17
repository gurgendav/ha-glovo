"""The Glovo integration."""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant

from . import glovo
from .api_session import SerializedApiSession
from .const import (
    CONF_ALLOW_LIVE_CHECKOUT,
    CONF_ALLOW_ORDERING,
    CONF_LIVE_CHECKOUT_ACKNOWLEDGED,
    CONF_ORDERING_ACKNOWLEDGED,
    CONF_TOKEN,
    PLATFORMS,
)
from .coordinator import GlovoConfigEntry, GlovoDataUpdateCoordinator
from .ordering_journal import AttemptJournal, HomeAssistantJournalStorage
from .ordering_basket_authority_store import (
    BasketAuthorityFault,
    DurableBasketAuthority,
    HomeAssistantBasketAuthorityStorage,
)
from .ordering_prep_authority import (
    HomeAssistantPreparationStorage,
    PreparationMutationAuthority,
)
from .ordering_runtime import OrderingRuntime
from .ordering_manager import OrderingManager
from .ordering_account import AccountClient
from .ordering_live_catalog import LiveCatalogClient
from .ordering_live_checkout import FinalCheckoutRequest, ProductionFinalCheckoutAdapter
from .ordering_live_api import LiveOrderingFacade
from .ordering_live_quote import (
    AuthoritativeConfirmationManager,
    QuoteTemplateClient,
)
from .ordering_live_selection import LiveSelectionRegistry
from .ordering_packages import (
    HomeAssistantPackageLibraryStorage,
    PackageLibrary,
    PackageLibraryError,
)
from .ordering_remote_basket import RemoteBasketClient
from .ordering_remote_basket_discovery import RemoteBasketDiscoveryClient
from .ordering_state import (
    DurableOrderingState,
    HomeAssistantOrderingStateStorage,
    OrderingStateFault,
)

_LOGGER = logging.getLogger(__name__)
CONFIG_ENTRY_VERSION = 1
CONFIG_ENTRY_MINOR_VERSION = 12


def _ordering_options(entry: GlovoConfigEntry) -> dict[str, object]:
    """Read, rather than cache, both explicit ordering gates."""
    return {
        CONF_ALLOW_ORDERING: entry.options.get(CONF_ALLOW_ORDERING, False),
        CONF_ORDERING_ACKNOWLEDGED: entry.options.get(
            CONF_ORDERING_ACKNOWLEDGED, False
        ),
        CONF_ALLOW_LIVE_CHECKOUT: entry.options.get(CONF_ALLOW_LIVE_CHECKOUT, False),
        CONF_LIVE_CHECKOUT_ACKNOWLEDGED: entry.options.get(
            CONF_LIVE_CHECKOUT_ACKNOWLEDGED, False
        ),
    }


async def async_setup_entry(hass: HomeAssistant, entry: GlovoConfigEntry) -> bool:
    """Set up tracking plus a gated, production no-payment preparation facade."""
    durable_state = DurableOrderingState(
        HomeAssistantOrderingStateStorage(hass, entry.entry_id)
    )
    try:
        # State generation/recovery must be durable before any optional ordering
        # capability is even considered.
        await durable_state.async_load()
    except OrderingStateFault:
        _LOGGER.error("Glovo ordering safety state is unavailable; ordering disabled")

    options = _ordering_options(entry)
    package_library = PackageLibrary(
        HomeAssistantPackageLibraryStorage(hass, entry.entry_id)
    )
    try:
        # This independent local store is loaded before any facade is published.
        # Corruption disables only library operations; provider preparation and
        # the ordering safety latches remain independent.
        await package_library.async_load()
    except PackageLibraryError:
        _LOGGER.error("Glovo ordering library is unavailable; library writes disabled")
    journal = AttemptJournal(
        HomeAssistantJournalStorage(hass, entry.entry_id), clock=time.time
    )
    preparation_authority = PreparationMutationAuthority(
        HomeAssistantPreparationStorage(hass, entry.entry_id),
        clock=time.time,
        durable_state=durable_state,
    )
    try:
        # This must complete before constructing the mutation-capable session,
        # basket/template clients, or facade.
        await preparation_authority.async_load()
    except Exception:  # noqa: BLE001 - retain tracking/recovery, fail preparation closed
        _LOGGER.exception("Glovo preparation authority is unavailable; ordering disabled")
    basket_authority = DurableBasketAuthority(
        HomeAssistantBasketAuthorityStorage(hass, entry.entry_id), clock=time.time
    )
    basket_store_ready = False
    try:
        # Persisted evidence is loaded before constructing any facade, but it is
        # never adopted. OrderingLiveFlow overwrites it with current-generation
        # UNKNOWN before publishing the dispatcher.
        await basket_authority.async_load()
        basket_store_ready = True
    except BasketAuthorityFault:
        _LOGGER.error("Glovo basket evidence is unavailable; ordering disabled")

    ordering_manager = OrderingManager(
        allow_ordering=options[CONF_ALLOW_ORDERING],
        ordering_acknowledged=options[CONF_ORDERING_ACKNOWLEDGED],
        allow_live_checkout=options[CONF_ALLOW_LIVE_CHECKOUT],
        live_checkout_acknowledged=options[CONF_LIVE_CHECKOUT_ACKNOWLEDGED],
        live_options=lambda: entry.options,
        # Legacy fixture collaborators are intentionally absent in production.
        catalog=None,
        journal=journal,
        checkout_adapter=None,
        clock=time.time,
        durable_state=durable_state,
        preparation_authority=preparation_authority,
    )
    await ordering_manager.async_initialize()
    if not basket_store_ready:
        await ordering_manager.async_set_enabled(False)
    if ordering_manager.integrity_fault:
        _LOGGER.warning(
            "Glovo ordering remains safely blocked after legacy migration check: %s",
            ordering_manager.legacy_repair_status,
        )
    mutation_ready = (
        ordering_manager.enabled
        and preparation_authority.loaded
        and not preparation_authority.integrity_fault
        and not preparation_authority.unresolved
        and basket_store_ready
    )

    async def persist_token(token_json: str) -> None:
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_TOKEN: token_json},
        )

    # Tracking always has this read-only session. Its one-attempt mutation
    # transport cannot exist unless the explicit preparation consent and durable
    # authority are already clean at this setup boundary.
    api_session = SerializedApiSession(
        token_source=lambda: entry.data[CONF_TOKEN],
        persist_token=persist_token,
        ensure_token=glovo.ensure_access_token,
        transport=glovo.single_attempt_authed_get,
        location_transport=glovo.single_attempt_authed_location_get,
        mutation_transport=(
            glovo.single_attempt_authed_phase_mutation if mutation_ready else None
        ),
        executor=hass.async_add_executor_job,
    )
    account_client = AccountClient(api_session)
    catalog_client = LiveCatalogClient(api_session)
    facade = None
    if mutation_ready:
        selections = LiveSelectionRegistry()
        confirmations = AuthoritativeConfirmationManager()
        facade_ref: dict[str, LiveOrderingFacade] = {}

        def invalidate_basket_handles() -> None:
            # A basket write invalidates the previous basket plus every authority
            # derived from its products or price.
            facade_ref["facade"].invalidate_basket_mutation_authority()

        def invalidate_quote_handles() -> None:
            # Template creation invalidates quote/selection authority, but it does
            # not mutate the remote basket and must not discard that snapshot.
            facade_ref["facade"].invalidate_quote_mutation_authority()

        baskets = RemoteBasketClient(
            api_session, invalidate_authority=invalidate_basket_handles
        )
        quotes = QuoteTemplateClient(
            api_session, invalidate_authority=invalidate_quote_handles
        )
        facade = LiveOrderingFacade(
            account=account_client,
            catalog=catalog_client,
            selections=selections,
            baskets=baskets,
            quotes=quotes,
            confirmations=confirmations,
            preparation_authority=preparation_authority,
            package_library=package_library,
            discovery_client=RemoteBasketDiscoveryClient(api_session),
            basket_authority=basket_authority,
        )
        facade_ref["facade"] = facade

    ordering_runtime = OrderingRuntime(
        manager=ordering_manager,
        preparation_authority=preparation_authority,
        live_options=lambda: entry.options,
        facade=facade,
        basket_authority=basket_authority,
        # A final transport exists only when both literal paid gates and the
        # separately gated preparation runtime were clean at composition time.
        # This avoids constructing paid authority for preparation-only entries.
        final_adapter=(
            ProductionFinalCheckoutAdapter(api_session)
            if mutation_ready
            and options[CONF_ALLOW_LIVE_CHECKOUT] is True
            and options[CONF_LIVE_CHECKOUT_ACKNOWLEDGED] is True
            else None
        ),
        final_request_factory=(
            (lambda quote: FinalCheckoutRequest.from_quote(quote, now=time.monotonic()))
            if mutation_ready
            and options[CONF_ALLOW_LIVE_CHECKOUT] is True
            and options[CONF_LIVE_CHECKOUT_ACKNOWLEDGED] is True
            else None
        ),
    )
    try:
        await ordering_runtime.async_initialize()
    except Exception:  # noqa: BLE001 - keep tracking, fail all optional mutation paths
        _LOGGER.exception("Glovo live preparation authority is unavailable; ordering disabled")

    coordinator = GlovoDataUpdateCoordinator(hass, entry, api_session)
    coordinator._account_client = account_client  # noqa: SLF001
    coordinator._catalog_client = catalog_client  # noqa: SLF001

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
    coordinator.ordering_runtime = ordering_runtime
    coordinator.ordering_surface = ordering_surface
    coordinator.ordering_package_library = package_library
    coordinator.ordering_basket_authority = basket_authority
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
    runtime: OrderingRuntime | None = getattr(coordinator, "ordering_runtime", None)
    surface: Any = getattr(coordinator, "ordering_surface", None)
    account_client: Any = getattr(coordinator, "_account_client", None)
    api_session: Any = getattr(coordinator, "api_session", None)
    if account_client is not None:
        account_client.invalidate()
    if api_session is not None:
        api_session.invalidate()
    if runtime is not None:
        await runtime.async_shutdown()
    elif manager is not None:
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

    # Any user-visible ordering option change invalidates handles/challenges
    # before reload, including a change while both preparation gates remain on.
    await _async_shutdown_ordering(entry)
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: Any) -> bool:
    """Force a fresh preparation and spending opt-in on every migration."""
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
        for key in (
            CONF_ALLOW_ORDERING,
            CONF_ORDERING_ACKNOWLEDGED,
            CONF_ALLOW_LIVE_CHECKOUT,
            CONF_LIVE_CHECKOUT_ACKNOWLEDGED,
        )
    )
    if migrating_to_opt_in or not gates_present_and_boolean:
        allow_ordering = False
        acknowledged = False
        allow_live_checkout = False
        live_checkout_acknowledged = False
    else:
        allow_ordering = options[CONF_ALLOW_ORDERING] is True
        acknowledged = (
            allow_ordering and options[CONF_ORDERING_ACKNOWLEDGED] is True
        )
        allow_live_checkout = (
            allow_ordering
            and acknowledged
            and options[CONF_ALLOW_LIVE_CHECKOUT] is True
        )
        live_checkout_acknowledged = (
            allow_live_checkout
            and options[CONF_LIVE_CHECKOUT_ACKNOWLEDGED] is True
        )
    normalized = {
        **options,
        CONF_ALLOW_ORDERING: allow_ordering,
        CONF_ORDERING_ACKNOWLEDGED: acknowledged,
        CONF_ALLOW_LIVE_CHECKOUT: allow_live_checkout,
        CONF_LIVE_CHECKOUT_ACKNOWLEDGED: live_checkout_acknowledged,
    }
    if normalized != options or migrating_to_opt_in:
        hass.config_entries.async_update_entry(
            entry,
            options=normalized,
            version=CONFIG_ENTRY_VERSION,
            minor_version=CONFIG_ENTRY_MINOR_VERSION,
        )
    return True
