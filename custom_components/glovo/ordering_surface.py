"""Admin-only live-ordering and recovery WebSocket surface semantics.

The Home Assistant adapter authenticates the caller.  This module deliberately
forwards only the frozen operation name and frozen request body to the manager;
the authenticated user's id is the *only* owner input.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, cast

from .ordering_manager import OrderingManager, OrderingUser

PANEL_URL_PATH = "glovo-ordering"
Handler = Callable[[OrderingUser, Mapping[str, Any]], Awaitable[dict[str, Any]]]

# These command names deliberately have no service/entity/event counterpart.
# Keep them in lock-step with ordering_live_api.PUBLIC_OPERATIONS.
PUBLIC_OPERATION_COMMANDS: dict[str, str] = {
    "state": "glovo/ordering/state",
    "live/addresses": "glovo/ordering/live/addresses",
    "live/stores": "glovo/ordering/live/stores",
    "live/store_menu": "glovo/ordering/live/store_menu",
    "live/payment_methods": "glovo/ordering/live/payment_methods",
    "live/basket": "glovo/ordering/live/basket",
    "live/basket_set": "glovo/ordering/live/basket_set",
    "live/basket_clear": "glovo/ordering/live/basket_clear",
    "live/basket_reconcile": "glovo/ordering/live/basket_reconcile",
    "live/create_quote": "glovo/ordering/live/create_quote",
    "live/prepare_confirmation": "glovo/ordering/live/prepare_confirmation",
    "live/execute_checkout": "glovo/ordering/live/execute_checkout",
    "live/checkout_status": "glovo/ordering/live/checkout_status",
}

RECOVERY_COMMANDS: tuple[str, ...] = (
    "glovo/ordering/manual_checks",
    "glovo/ordering/manual_check",
    "glovo/ordering/prepare_manual_resolution",
    "glovo/ordering/resolve_manual_check",
)


class OrderingSurfaceAdapter(Protocol):
    """Small Home Assistant frontend/WebSocket adapter boundary."""

    async def async_register_panel(
        self,
        *,
        url_path: str,
        title: str,
        icon: str,
        require_admin: bool,
    ) -> None: ...

    async def async_remove_panel(self) -> None: ...

    async def async_register_handler(self, name: str, handler: Handler) -> None: ...

    async def async_remove_handlers(self) -> None: ...


class OrderingSurface:
    """Register the one admin-only public surface, including blocked recovery.

    Command shells remain registered across a HA unload because HA does not
    support unregistering them.  The adapter clears their active callbacks, so a
    stale shell fails closed rather than retaining a previous manager.
    """

    def __init__(self, manager: OrderingManager, adapter: OrderingSurfaceAdapter) -> None:
        self._manager = manager
        self._adapter = adapter
        self._registered = False

    @property
    def registered(self) -> bool:
        return self._registered

    async def async_setup(self) -> None:
        if self._registered:
            return
        handlers: dict[str, Handler] = {
            PUBLIC_OPERATION_COMMANDS[operation]: self._operation_handler(operation)
            for operation in PUBLIC_OPERATION_COMMANDS
        }
        handlers.update(
            {
                "glovo/ordering/manual_checks": self._manual_checks,
                "glovo/ordering/manual_check": self._manual_check,
                "glovo/ordering/prepare_manual_resolution": self._prepare_manual_resolution,
                "glovo/ordering/resolve_manual_check": self._resolve_manual_check,
            }
        )
        try:
            # Register the recovery controls and live-operation shells even with
            # gates closed.  The manager returns a safe unavailable state for
            # `state`, and rejects all inaccessible mutation operations.
            for name, handler in handlers.items():
                await self._adapter.async_register_handler(name, handler)
        except Exception:
            await self._adapter.async_remove_handlers()
            raise
        self._registered = True
        try:
            await self._adapter.async_register_panel(
                url_path=PANEL_URL_PATH,
                title="Glovo Ordering",
                icon="mdi:cart-outline",
                require_admin=True,
            )
        except Exception:
            # The WebSocket surface (especially recovery) remains authoritative.
            await self._adapter.async_remove_panel()

    async def async_unload(self) -> None:
        if not self._registered:
            return
        await self._adapter.async_remove_handlers()
        await self._adapter.async_remove_panel()
        self._registered = False

    def _operation_handler(self, operation: str) -> Handler:
        async def handler(user: OrderingUser, message: Mapping[str, Any]) -> dict[str, Any]:
            # Owner identity never comes from the request.  The manager is the
            # sole owner-aware dispatcher supplied by the live ordering runtime.
            request = {key: value for key, value in message.items() if key not in {"id", "type"}}
            dispatcher = cast(
                Callable[..., Awaitable[dict[str, Any]]],
                getattr(self._manager, "async_dispatch", None),
            )
            if not callable(dispatcher):
                # A partially upgraded runtime must fail closed rather than
                # accidentally reviving the retired mock command set.
                raise RuntimeError("live ordering dispatcher is unavailable")
            result = await dispatcher(
                owner_key=user.user_id,
                operation=operation,
                request=request,
            )
            return dict(result)

        return handler

    async def _manual_checks(
        self, user: OrderingUser, _message: Mapping[str, Any]
    ) -> dict[str, Any]:
        return await self._manager.async_list_manual_checks(user)

    async def _manual_check(
        self, user: OrderingUser, message: Mapping[str, Any]
    ) -> dict[str, Any]:
        return await self._manager.async_get_manual_check(user, message["attemptRef"])

    async def _prepare_manual_resolution(
        self, user: OrderingUser, message: Mapping[str, Any]
    ) -> dict[str, Any]:
        return await self._manager.async_prepare_manual_resolution(
            user,
            attempt_id=message["attemptRef"],
            expected_revision=message["expectedRecordRevision"],
            expected_state=message["expectedState"],
            resolution=message["resolution"],
        )

    async def _resolve_manual_check(
        self, user: OrderingUser, message: Mapping[str, Any]
    ) -> dict[str, Any]:
        return await self._manager.async_resolve_manual_check(
            user,
            attempt_id=message["attemptRef"],
            expected_revision=message["expectedRecordRevision"],
            expected_state=message["expectedState"],
            resolution=message["resolution"],
            challenge=message["challenge"],
            acknowledged=message["acknowledged"],
        )
