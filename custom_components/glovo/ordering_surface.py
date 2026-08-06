"""Admin-only ordering and recovery panel registration semantics."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from .ordering_manager import OrderingManager, OrderingUser

PANEL_URL_PATH = "glovo-ordering"
Handler = Callable[[OrderingUser, Mapping[str, Any]], Awaitable[dict[str, Any]]]


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
    """Expose mutations only when enabled, but retain narrow recovery while blocked."""

    def __init__(self, manager: OrderingManager, adapter: OrderingSurfaceAdapter) -> None:
        self._manager = manager
        self._adapter = adapter
        self._registered = False

    @property
    def registered(self) -> bool:
        return self._registered

    async def async_setup(self) -> None:
        if self._registered or not (
            self._manager.enabled or self._manager.recovery_required
        ):
            return
        recovery_handlers: dict[str, Handler] = {
            "glovo/ordering/state": self._state,
            "glovo/ordering/manual_checks": self._manual_checks,
            "glovo/ordering/manual_check": self._manual_check,
            "glovo/ordering/prepare_manual_resolution": self._prepare_manual_resolution,
            "glovo/ordering/resolve_manual_check": self._resolve_manual_check,
        }
        normal_handlers: dict[str, Handler] = {}
        if self._manager.enabled:
            normal_handlers = {
                "glovo/ordering/catalog": self._catalog,
                "glovo/ordering/basket": self._basket,
                "glovo/ordering/basket_add_fixture_item": self._basket_add,
                "glovo/ordering/basket_clear": self._basket_clear,
                "glovo/ordering/fixture_quote": self._quote,
                "glovo/ordering/prepare_mock_confirmation": self._prepare,
                "glovo/ordering/execute_mock_checkout": self._execute_mock,
            }
        try:
            # Required recovery commands are independent of the optional panel and
            # are always installed first, including when ordering options are off.
            for name, handler in recovery_handlers.items():
                await self._adapter.async_register_handler(name, handler)
            for name, handler in normal_handlers.items():
                await self._adapter.async_register_handler(name, handler)
        except Exception:
            await self._adapter.async_remove_handlers()
            raise
        self._registered = True
        try:
            await self._adapter.async_register_panel(
                url_path=PANEL_URL_PATH,
                title=(
                    "Glovo Ordering Recovery"
                    if self._manager.recovery_required
                    else "Glovo Mock Ordering"
                ),
                icon="mdi:cart-alert" if self._manager.recovery_required else "mdi:cart-outline",
                require_admin=True,
            )
        except Exception:
            # WebSocket recovery is the authority. A panel/static-path failure must
            # never remove already-registered recovery handlers.
            await self._adapter.async_remove_panel()

    async def async_unload(self) -> None:
        if not self._registered:
            return
        await self._adapter.async_remove_handlers()
        await self._adapter.async_remove_panel()
        self._registered = False

    async def _state(
        self, user: OrderingUser, _message: Mapping[str, Any]
    ) -> dict[str, Any]:
        return await self._manager.async_state(user)

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

    async def _catalog(
        self, user: OrderingUser, message: Mapping[str, Any]
    ) -> dict[str, Any]:
        return await self._manager.async_catalog(user, message["generation"])

    async def _basket(
        self, user: OrderingUser, message: Mapping[str, Any]
    ) -> dict[str, Any]:
        return await self._manager.async_get_basket(user, message["generation"])

    async def _basket_add(
        self, user: OrderingUser, message: Mapping[str, Any]
    ) -> dict[str, Any]:
        return await self._manager.async_add_item(
            user,
            message["generation"],
            store_key=message["storeKey"],
            product_key=message["productKey"],
            variant_key=message["variantKey"],
            modifier_keys=tuple(message.get("modifierKeys", ())),
            quantity=message["quantity"],
        )

    async def _basket_clear(
        self, user: OrderingUser, message: Mapping[str, Any]
    ) -> dict[str, Any]:
        return await self._manager.async_clear_basket(user, message["generation"])

    async def _quote(
        self, user: OrderingUser, message: Mapping[str, Any]
    ) -> dict[str, Any]:
        return await self._manager.async_quote(
            user,
            message["generation"],
            address_key=message["addressKey"],
            payment_key=message["paymentKey"],
        )

    async def _prepare(
        self, user: OrderingUser, message: Mapping[str, Any]
    ) -> dict[str, Any]:
        return await self._manager.async_prepare_mock_confirmation(
            user,
            message["generation"],
            message["fingerprint"],
        )

    async def _execute_mock(
        self, user: OrderingUser, message: Mapping[str, Any]
    ) -> dict[str, Any]:
        result = await self._manager.async_execute_mock_checkout(
            user,
            message["generation"],
            message["challenge"],
            message["fingerprint"],
        )
        return dict(result)
