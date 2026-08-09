"""Runtime composition for the durable, gated live-ordering dependency seam.

No production mutation or final-checkout adapter is constructed in this module.
Read-only tracking clients remain independent from the ordering gate.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from .ordering_manager import OrderingManager
from .ordering_prep_authority import PreparationMutationAuthority

if TYPE_CHECKING:
    from .ordering_live_flow import OrderingLiveFlow


class OrderingRuntime:
    """Own ordering lifecycle cancellation/invalidation without HA registration."""

    def __init__(
        self,
        *,
        manager: OrderingManager,
        preparation_authority: PreparationMutationAuthority,
        live_options: Callable[[], Mapping[str, object]],
        facade: Any | None = None,
        final_adapter: Any | None = None,
        final_request_factory: Any | None = None,
    ) -> None:
        self.manager = manager
        self.preparation_authority = preparation_authority
        self._options = live_options
        self.flow: OrderingLiveFlow | None = None
        self._closed = False
        # No facade means no remote preparation clients have been instantiated.
        # This is the normal production configuration until a reviewed adapter is
        # available. Fixtures can inject all three dependencies explicitly.
        if facade is not None:
            from .ordering_live_flow import OrderingLiveFlow

            self.flow = OrderingLiveFlow(
                facade=facade,
                preparation_authority=preparation_authority,
                live_options=live_options,
                final_adapter=final_adapter,
                final_request_factory=final_request_factory,
            )
            self.manager._live_dispatcher = self.flow.async_live_dispatch  # noqa: SLF001

    @property
    def live_checkout_available(self) -> bool:
        return self.flow is not None and self.flow.live_checkout_available

    async def async_initialize(self) -> None:
        """Load durable prep state before a possible live facade becomes callable."""
        if self._closed:
            return
        try:
            await self.preparation_authority.async_load()
            if self.flow is not None:
                await self.flow.async_initialize()
        except Exception:
            # The manager already has its own durable fault mechanics; refuse to
            # expose this optional live dependency if its paired authority failed.
            await self.manager.async_set_enabled(False)
            raise

    async def async_shutdown(self) -> None:
        """Invalidate capability before any lifecycle reload can replace it."""
        if self._closed:
            return
        self._closed = True
        await self.manager.async_set_enabled(False)
        if self.flow is not None:
            await self.flow.async_invalidate(self.manager.generation)
