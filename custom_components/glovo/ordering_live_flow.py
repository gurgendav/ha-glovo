"""Durable, default-off owner for live ordering preparation and final dispatch.

This module is deliberately transport-free.  It composes already reviewed private
clients, a PreparationMutationAuthority, and an optionally injected final adapter.
Production setup provides that adapter only behind fresh literal paid gates.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from .ordering_basket_authority_store import BasketAuthorityFault, DurableBasketAuthority
from .ordering_live_api import PackageSaveStageError, PublicContractError, validate_public_request
from .ordering_prep_authority import PreparationMutationAuthority


class FinalDispatchAdapter(Protocol):
    """Private single-submit seam. Implementations must not retry or poll."""

    async def async_submit(self, request: Any) -> Any: ...


class LiveFlowUnavailable(RuntimeError):
    """Live preparation or checkout is unavailable without exposing internals."""

    def __init__(self, message: str, *, stage: str | None = None) -> None:
        self.stage = stage
        super().__init__(message)


class OrderingLiveFlow:
    """Serialize live capability state and preserve a default-off paid boundary."""

    def __init__(
        self,
        *,
        facade: Any,
        preparation_authority: PreparationMutationAuthority,
        live_options: Callable[[], Mapping[str, object]],
        final_adapter: FinalDispatchAdapter | None = None,
        final_request_factory: Callable[[str], Any] | None = None,
        manager: Any | None = None,
        basket_authority: DurableBasketAuthority | None = None,
    ) -> None:
        self._facade = facade
        self._authority = preparation_authority
        self._options = live_options
        self._final_adapter = final_adapter
        self._final_request_factory = final_request_factory
        self._manager = manager
        self._basket_authority = basket_authority
        self._lock = asyncio.Lock()
        self._loaded = False
        self._active = False
        self._invalidated_generation: int | None = None

    def _basket_store_available(self) -> bool:
        authority = self._basket_authority
        return bool(
            authority is None
            or (authority.loaded and not authority.integrity_fault)
        )

    @property
    def live_checkout_available(self) -> bool:
        """Truthful final capability including distinct literal spending consent."""
        try:
            options = self._options()
            return (
                self._active
                and self._final_adapter is not None
                and self._final_request_factory is not None
                and self._authority.loaded
                and not self._authority.integrity_fault
                and not self._authority.unresolved
                and self._basket_store_available()
                and options.get("allow_ordering", False) is True
                and options.get("ordering_acknowledged", False) is True
                and options.get("allow_live_checkout", False) is True
                and options.get("live_checkout_acknowledged", False) is True
            )
        except Exception:
            return False

    @property
    def live_ordering_available(self) -> bool:
        """Preparation is available only while this initialized facade is active."""
        return self._active and self._preparation_gate()

    def _preparation_gate(self) -> bool:
        try:
            options = self._options()
            return (
                options.get("allow_ordering", False) is True
                and options.get("ordering_acknowledged", False) is True
                and self._authority.loaded
                and not self._authority.integrity_fault
                and not self._authority.unresolved
                and self._basket_store_available()
                and self._active
            )
        except Exception:
            return False

    def _checkout_gate(self) -> bool:
        try:
            options = self._options()
            return (
                self._preparation_gate()
                and options.get("allow_live_checkout", False) is True
                and options.get("live_checkout_acknowledged", False) is True
                and self.live_checkout_available
            )
        except Exception:
            return False

    def _options_snapshot(self) -> dict[str, object] | None:
        """Copy the exact live option state around one awaited facade call."""
        try:
            options = self._options()
            if not isinstance(options, Mapping):
                return None
            return dict(options)
        except Exception:
            return None

    def _post_await_dispatch_is_current(
        self,
        *,
        generation: int,
        invalidated_generation: int | None,
        options: Mapping[str, object],
    ) -> bool:
        """Reject a result if its runtime, generation, or option lease changed."""
        try:
            current_options = self._options_snapshot()
            if (
                not self._loaded
                or not self._active
                or not self._preparation_gate()
                or self._invalidated_generation != invalidated_generation
                or (
                    self._invalidated_generation is not None
                    and generation < self._invalidated_generation
                )
                or current_options is None
                or current_options != dict(options)
            ):
                return False
            if self._manager is not None and (
                self._manager.generation != generation
                or self._manager.enabled is not True
            ):
                return False
            return True
        except Exception:
            return False

    def _invalidate_facade_ephemeral_authority(self) -> None:
        """Best-effort fail-closed invalidation after a stale awaited result."""
        invalidate = getattr(self._facade, "invalidate_all", None)
        if not callable(invalidate):
            return
        try:
            invalidate()
        except Exception:
            # Facade invalidation clears basket/quote authority before its handle
            # clients. Never let a secondary cleanup error publish the stale result.
            pass

    async def async_initialize(self) -> None:
        async with self._lock:
            if self._loaded:
                return
            await self._authority.async_load()
            basket_authority = self._basket_authority
            if basket_authority is not None:
                if not basket_authority.loaded:
                    await basket_authority.async_load()
                generation = getattr(self._manager, "generation", None)
                if (
                    isinstance(generation, bool)
                    or not isinstance(generation, int)
                    or generation < 1
                ):
                    raise LiveFlowUnavailable("live ordering is unavailable")
                try:
                    # Loaded evidence never restores runtime authority, including
                    # an old UNKNOWN from this same durable generation.
                    await basket_authority.async_record_unknown(
                        generation=generation
                    )
                except BasketAuthorityFault:
                    if (
                        basket_authority.integrity_fault
                        or not basket_authority.transient_write_fault
                    ):
                        raise
                    # A transient save fault exposes only explicit read-only
                    # adoption, which retries UNKNOWN before its provider GET.
            self._loaded = True
            self._active = True

    async def async_invalidate(self, generation: int) -> None:
        """Forget every local capability only after caller durably advances it."""
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise LiveFlowUnavailable("live ordering is unavailable")
        async with self._lock:
            self._active = False
            self._invalidated_generation = generation
            invalidate = getattr(self._facade, "invalidate_all", None)
            if callable(invalidate):
                invalidate()
            if self._basket_authority is not None:
                await self._basket_authority.async_record_unknown(
                    generation=generation
                )

    async def _async_install_basket_authority(
        self, hook_name: str, *, generation: int, values: Mapping[str, Any]
    ) -> None:
        """Serialize one transport-free discovery result into the facade core."""
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise LiveFlowUnavailable("live ordering is unavailable")
        async with self._lock:
            if (
                not self._loaded
                or not self._active
                or (
                    self._invalidated_generation is not None
                    and generation < self._invalidated_generation
                )
            ):
                raise LiveFlowUnavailable("live ordering is unavailable")
            hook = getattr(self._facade, hook_name, None)
            if not callable(hook):
                raise LiveFlowUnavailable("live ordering is unavailable")
            try:
                hook(generation=generation, **dict(values))
            except Exception:
                raise LiveFlowUnavailable("live ordering is unavailable") from None

    async def _async_record_basket_absent_verified(
        self, *, generation: int, **values: Any
    ) -> None:
        """Install an exact verified-absence binding; performs no discovery itself."""
        await self._async_install_basket_authority(
            "_record_basket_absent_verified",
            generation=generation,
            values=values,
        )

    async def _async_adopt_basket_snapshot(
        self, *, generation: int, **values: Any
    ) -> None:
        """Install one exact snapshot binding; performs no provider call itself."""
        await self._async_install_basket_authority(
            "_adopt_basket_snapshot", generation=generation, values=values
        )

    async def _async_record_basket_conflict(
        self, *, generation: int, **values: Any
    ) -> None:
        """Install a closed conflict result; performs no provider call itself."""
        await self._async_install_basket_authority(
            "_record_basket_conflict", generation=generation, values=values
        )

    async def async_live_dispatch(
        self, owner_key: str, operation: str, request: Mapping[str, Any]
    ) -> dict[str, Any]:
        """The single operation dispatcher used by runtime/manager dependencies."""
        try:
            operation, copied, generation = validate_public_request(operation, request)
        except PublicContractError:
            raise LiveFlowUnavailable("live ordering is unavailable") from None
        if not isinstance(owner_key, str) or not owner_key:
            raise LiveFlowUnavailable("live ordering is unavailable")
        async with self._lock:
            if not self._loaded:
                raise LiveFlowUnavailable("live ordering is unavailable")
            if operation == "state":
                # State is answered by OrderingManager so it can publish the
                # durable current generation even while recovery blocks writes.
                raise LiveFlowUnavailable("live ordering is unavailable")
            assert generation is not None
            if not self._preparation_gate():
                raise LiveFlowUnavailable("live ordering is unavailable")
            if self._invalidated_generation is not None and generation < self._invalidated_generation:
                raise LiveFlowUnavailable("live ordering is unavailable")
            entry_options = self._options_snapshot()
            entry_invalidated_generation = self._invalidated_generation
            if entry_options is None:
                raise LiveFlowUnavailable("live ordering is unavailable")
            if operation == "live/execute_checkout":
                if not self._checkout_gate():
                    raise LiveFlowUnavailable("live checkout is unavailable")
                consume = getattr(self._facade, "async_consume_final_quote", None)
                if not callable(consume) or self._final_adapter is None or self._final_request_factory is None:
                    raise LiveFlowUnavailable("live checkout is unavailable")
                challenge = copied["challenge"]
                if not isinstance(challenge, str):
                    raise LiveFlowUnavailable("live checkout is unavailable")
                try:
                    quote = consume(
                        owner=owner_key, generation=generation, challenge=challenge
                    )
                    final_request = self._final_request_factory(quote)
                    # This is the sole final adapter invocation. No retry/status
                    # poll/fallback/compensating mutation is permitted here.
                    if self._manager is None:
                        raise LiveFlowUnavailable("live checkout is unavailable")
                    from .ordering_manager import OrderingUser

                    return await self._manager.async_execute_live_final(
                        OrderingUser(owner_key, True),
                        generation,
                        quote,
                        final_request,
                        self._final_adapter,
                    )
                except LiveFlowUnavailable:
                    raise
                except Exception:
                    # A final response that is malformed, cancelled, pending, or
                    # cannot be durably classified is never presented as success.
                    raise LiveFlowUnavailable("live checkout requires manual review") from None
            # The facade performs each preparatory mutation through the durable
            # authority injected at construction.  No fallback/replay exists here.
            try:
                result = await self._facade.async_dispatch(
                    owner=owner_key, operation=operation, request=copied
                )
            except PackageSaveStageError as err:
                raise LiveFlowUnavailable(
                    "package save is unavailable", stage=err.stage
                ) from None
            except PublicContractError:
                raise LiveFlowUnavailable("live ordering is unavailable") from None
            if not isinstance(result, dict):
                raise LiveFlowUnavailable("live ordering is unavailable")
            if not self._post_await_dispatch_is_current(
                generation=generation,
                invalidated_generation=entry_invalidated_generation,
                options=entry_options,
            ):
                self._invalidate_facade_ephemeral_authority()
                raise LiveFlowUnavailable("live ordering is unavailable")
            return result

    def public_state(self, generation: int) -> dict[str, Any]:
        """Strictly privacy-safe state; provider/session/attempt values stay private."""
        return {
            "enabled": self._preparation_gate(),
            "liveCheckoutAvailable": self.live_checkout_available and self._checkout_gate(),
            "generation": generation,
            "allowedOperations": [],
            "basket": "unavailable",
            "quote": "unavailable",
            "manualCheckRequired": bool(self._authority.unresolved),
            "recoveryRequired": bool(self._authority.unresolved or self._authority.integrity_fault),
        }
