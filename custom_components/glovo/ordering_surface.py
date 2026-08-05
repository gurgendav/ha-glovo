"""Admin-only panel and WebSocket registration semantics for mock ordering."""

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
    """Register no ordering surface unless the live manager gate is enabled."""

    def __init__(self, manager: OrderingManager, adapter: OrderingSurfaceAdapter) -> None:
        self._manager = manager
        self._adapter = adapter
        self._registered = False

    @property
    def registered(self) -> bool:
        return self._registered

    async def async_setup(self) -> None:
        if self._registered or not self._manager.enabled:
            return
        handlers: dict[str, Handler] = {
            "glovo/ordering/state": self._state,
            "glovo/ordering/catalog": self._catalog,
            "glovo/ordering/basket": self._basket,
            "glovo/ordering/basket_add_fixture_item": self._basket_add,
            "glovo/ordering/basket_clear": self._basket_clear,
            "glovo/ordering/fixture_quote": self._quote,
            "glovo/ordering/prepare_mock_confirmation": self._prepare,
            "glovo/ordering/execute_mock_checkout": self._execute_mock,
        }
        try:
            await self._adapter.async_register_panel(
                url_path=PANEL_URL_PATH,
                title="Glovo Mock Ordering",
                icon="mdi:cart-outline",
                require_admin=True,
            )
            for name, handler in handlers.items():
                await self._adapter.async_register_handler(name, handler)
            self._registered = True
        except Exception:
            # Registration spans several HA APIs; roll back callable handlers and
            # the panel if any later step fails. Retained WS shells fail closed.
            await self._adapter.async_remove_handlers()
            await self._adapter.async_remove_panel()
            raise

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
