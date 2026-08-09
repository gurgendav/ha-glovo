"""Durable, default-off owner for live ordering preparation and final dispatch.

This module is deliberately transport-free.  It composes already reviewed private
clients, a PreparationMutationAuthority, and an optionally injected final adapter.
No production setup path provides that adapter.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from .ordering_live_api import PublicContractError, validate_public_request
from .ordering_prep_authority import PreparationMutationAuthority


class FinalDispatchAdapter(Protocol):
    """Private single-submit seam. Implementations must not retry or poll."""

    async def async_submit(self, request: Any) -> Any: ...


class LiveFlowUnavailable(RuntimeError):
    """Live preparation or checkout is unavailable without exposing internals."""


class OrderingLiveFlow:
    """Serialize live capability state and preserve a no-adapter production default."""

    def __init__(
        self,
        *,
        facade: Any,
        preparation_authority: PreparationMutationAuthority,
        live_options: Callable[[], Mapping[str, object]],
        final_adapter: FinalDispatchAdapter | None = None,
        final_request_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self._facade = facade
        self._authority = preparation_authority
        self._options = live_options
        self._final_adapter = final_adapter
        self._final_request_factory = final_request_factory
        self._lock = asyncio.Lock()
        self._loaded = False
        self._invalidated_generation: int | None = None

    @property
    def live_checkout_available(self) -> bool:
        # Fixture adapters may be injected in isolated tests, but production
        # availability is always false until a separately reviewed adapter exists.
        return self._final_adapter is not None and self._final_request_factory is not None

    def _preparation_gate(self) -> bool:
        try:
            options = self._options()
            return (
                options.get("allow_ordering", False) is True
                and options.get("ordering_acknowledged", False) is True
                and self._authority.loaded
                and not self._authority.integrity_fault
                and not self._authority.unresolved
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

    async def async_initialize(self) -> None:
        async with self._lock:
            if self._loaded:
                return
            await self._authority.async_load()
            self._loaded = True

    async def async_invalidate(self, generation: int) -> None:
        """Forget every local capability only after caller durably advances it."""
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise LiveFlowUnavailable("live ordering is unavailable")
        async with self._lock:
            self._invalidated_generation = generation
            invalidate = getattr(self._facade, "invalidate_all", None)
            if callable(invalidate):
                invalidate()

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
                    outcome = await self._final_adapter.async_submit(final_request)
                    state = getattr(outcome, "state", None)
                    if state not in {"COMPLETED", "REJECTED"}:
                        raise LiveFlowUnavailable("live checkout requires manual review")
                    return {"status": "succeeded" if state == "COMPLETED" else "failed"}
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
            except PublicContractError:
                raise LiveFlowUnavailable("live ordering is unavailable") from None
            if not isinstance(result, dict):
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
