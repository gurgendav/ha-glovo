"""Serialized account-scoped manager for default-off mock ordering."""

from __future__ import annotations

import asyncio
import hmac
import math
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .ordering_adapter import MOCK_MODE, MockCheckoutAdapter
from .ordering_basket import BasketManager
from .ordering_catalog import SyntheticCatalogProvider
from .ordering_journal import AttemptJournal, JournalFault, JournalState
from .ordering_models import BasketSnapshot, MaskedPaymentSummary, SavedAddressSummary
from .ordering_quote import CheckoutFixture, canonical_fingerprint
from .ordering_state import (
    DurableOrderingState,
    MemoryOrderingStateStorage,
    OrderingStateFault,
)

CONFIRMATION_TTL_SECONDS = 45


class OrderingError(RuntimeError):
    """Base redaction-safe ordering error."""


class OrderingDisabled(OrderingError):
    """Ordering gate is closed."""


class OrderingAdminRequired(OrderingError):
    """Ordering surface requires a Home Assistant administrator."""


class StaleOrderingGeneration(OrderingError):
    """Request belongs to an invalidated runtime generation."""


class InvalidConfirmation(OrderingError):
    """Confirmation is absent, stale, changed, expired, or already used."""


class OrderingSecurityFault(OrderingError):
    """Persistent safety state is corrupt; ordering remains disabled."""


@dataclass(frozen=True, slots=True)
class OrderingUser:
    """Minimal authenticated caller context."""

    user_id: str
    is_admin: bool

    def __post_init__(self) -> None:
        if not isinstance(self.user_id, str) or not self.user_id or len(self.user_id) > 64:
            raise ValueError("user key is invalid")
        if not isinstance(self.is_admin, bool):
            raise TypeError("admin flag must be boolean")


@dataclass(frozen=True, slots=True)
class _QuoteContext:
    owner_key: str = field(repr=False)
    generation: int
    basket: BasketSnapshot
    address: SavedAddressSummary = field(repr=False)
    payment: MaskedPaymentSummary = field(repr=False)
    fixture: CheckoutFixture
    fingerprint: str


@dataclass(frozen=True, slots=True, repr=False)
class _Confirmation:
    owner_key: str
    generation: int
    fingerprint: str
    challenge: str
    expires_at: float

    def __repr__(self) -> str:
        return "_Confirmation(<redacted>)"


class OrderingManager:
    """Account-scoped gate, state, serialization, and mock checkout orchestration."""

    def __init__(
        self,
        *,
        allow_ordering: object,
        ordering_acknowledged: object = False,
        live_options: Callable[[], Mapping[str, object]] | None = None,
        catalog: SyntheticCatalogProvider,
        journal: AttemptJournal,
        checkout_adapter: MockCheckoutAdapter,
        clock: Callable[[], float],
        challenge_source: Callable[[], str] | None = None,
        attempt_source: Callable[[], str] | None = None,
        durable_state: DurableOrderingState | None = None,
    ) -> None:
        self._configured_allow = allow_ordering
        self._configured_ack = ordering_acknowledged
        self._live_options = live_options
        self._enabled = allow_ordering is True and ordering_acknowledged is True
        self._catalog = catalog
        self.journal = journal
        self._checkout_adapter = checkout_adapter
        self._clock = clock
        self._challenge_source = challenge_source or (lambda: secrets.token_urlsafe(32))
        self._attempt_source = attempt_source or (lambda: secrets.token_hex(16))
        self._lock = asyncio.Lock()
        self._durable_state = durable_state or DurableOrderingState(MemoryOrderingStateStorage())
        self._baskets = BasketManager(clock=clock)
        self._quotes: dict[str, _QuoteContext] = {}
        self._confirmations: dict[str, _Confirmation] = {}
        self._security_fault = self._durable_state.safety_fault
        self._initialized = False

    @property
    def enabled(self) -> bool:
        return self._enabled and not self._security_fault and self._live_gate()

    @property
    def generation(self) -> int:
        return self._durable_state.generation

    @property
    def security_fault(self) -> bool:
        return self._security_fault

    @property
    def basket_ttl_seconds(self) -> int | float:
        return self._baskets.ttl_seconds

    @property
    def confirmation_ttl_seconds(self) -> int:
        return CONFIRMATION_TTL_SECONDS

    def _live_gate(self) -> bool:
        try:
            if self._live_options is None:
                allow = self._configured_allow
                acknowledged = self._configured_ack
            else:
                options = self._live_options()
                allow = options.get("allow_ordering", False)
                acknowledged = options.get("ordering_acknowledged", False)
            return allow is True and acknowledged is True
        except Exception:
            return False

    def _now(self) -> float:
        value = self._clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise OrderingSecurityFault("authorization clock is invalid")
        return float(value)

    async def _async_write_security_marker(self) -> None:
        """Best-effort secondary durable latch if generation storage itself fails."""
        try:
            marker = await self.journal.async_create(
                f"attempt-{self._attempt_source()}", self.generation, "0" * 64
            )
            await self.journal.async_transition(
                marker.attempt_id,
                JournalState.SECURITY_FAULT,
                outcome="authority_storage_failure",
            )
        except JournalFault:
            pass

    async def _async_bump_generation(self, *, latch_fault: bool = False) -> None:
        """The sole mutation path for the published runtime generation."""
        try:
            if latch_fault:
                await self._durable_state.async_latch_fault()
            else:
                await self._durable_state.async_bump()
        except OrderingStateFault:
            self._security_fault = True
            self._enabled = False
            self._invalidate_all_ephemeral()
            await self._async_write_security_marker()
            raise


    async def _async_latch_safety_fault(self) -> None:
        self._security_fault = True
        self._enabled = False
        self._invalidate_all_ephemeral()
        if not self._durable_state.safety_fault:
            try:
                await self._async_bump_generation(latch_fault=True)
            except OrderingStateFault:
                pass

    async def async_initialize(self) -> None:
        """Recover durable authority and fail closed on any unresolved attempt."""
        async with self._lock:
            if self._initialized:
                return
            if not self._durable_state.loaded:
                try:
                    await self._durable_state.async_load()
                    self._security_fault = self._durable_state.safety_fault
                except OrderingStateFault:
                    self._security_fault = True
                    self._enabled = False
            try:
                await self.journal.async_load()
            except JournalFault:
                await self._async_latch_safety_fault()
            if self._security_fault and not self._durable_state.safety_fault:
                await self._async_write_security_marker()
            if self.journal.unresolved_safety_fault:
                await self._async_latch_safety_fault()
            if self._security_fault:
                self._enabled = False
                self._invalidate_all_ephemeral()
            self._initialized = True

    def _invalidate_all_ephemeral(self) -> None:
        self._baskets.invalidate_all()
        self._quotes.clear()
        self._confirmations.clear()

    def _invalidate_user_checkout(self, owner_key: str) -> None:
        self._quotes.pop(owner_key, None)
        self._confirmations.pop(owner_key, None)

    async def async_set_enabled(self, enabled: object, acknowledged: object = False) -> None:
        """Apply both literal gates; every disable durably invalidates old authority."""
        async with self._lock:
            requested = (
                enabled is True
                and acknowledged is True
                and self._live_gate()
                and not self._security_fault
                and not self._durable_state.safety_fault
                and not self.journal.unresolved_safety_fault
            )
            if not requested:
                self._enabled = False
                self._invalidate_all_ephemeral()
                try:
                    await self._async_bump_generation()
                except OrderingStateFault:
                    pass
                return
            self._enabled = True

    def _guard(self, user: OrderingUser, generation: int | None = None) -> None:
        if not user.is_admin:
            raise OrderingAdminRequired("administrator access is required")
        if not self.enabled:
            if self._security_fault or self._durable_state.safety_fault:
                raise OrderingSecurityFault("ordering disabled by a safety fault")
            raise OrderingDisabled("ordering is disabled")
        if generation is not None and (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation != self.generation
        ):
            raise StaleOrderingGeneration("ordering generation is stale")

    async def async_state(self, user: OrderingUser) -> dict[str, Any]:
        async with self._lock:
            self._guard(user)
            return {
                "enabled": True,
                "generation": self.generation,
                "mockOnly": True,
                "liveOrderingAvailable": False,
                "securityFault": False,
            }

    async def async_catalog(self, user: OrderingUser, generation: int) -> dict[str, Any]:
        async with self._lock:
            self._guard(user, generation)
            return self._catalog.public_catalog()

    async def async_get_basket(self, user: OrderingUser, generation: int) -> dict[str, Any]:
        async with self._lock:
            self._guard(user, generation)
            return self._baskets.public_dict(user.user_id, generation)

    async def async_add_item(
        self,
        user: OrderingUser,
        generation: int,
        *,
        store_key: str,
        product_key: str,
        variant_key: str,
        modifier_keys: Sequence[str],
        quantity: int,
    ) -> dict[str, Any]:
        async with self._lock:
            self._guard(user, generation)
            store_label, item = self._catalog.resolve_item(
                store_key=store_key,
                product_key=product_key,
                variant_key=variant_key,
                modifier_keys=modifier_keys,
                quantity=quantity,
            )
            self._baskets.add_item(
                owner_key=user.user_id,
                generation=generation,
                store_key=store_key,
                store_label=store_label,
                item=item,
            )
            self._invalidate_user_checkout(user.user_id)
            return self._baskets.public_dict(user.user_id, generation)

    async def async_clear_basket(self, user: OrderingUser, generation: int) -> dict[str, Any]:
        async with self._lock:
            self._guard(user, generation)
            self._baskets.clear(user.user_id)
            self._invalidate_user_checkout(user.user_id)
            return self._baskets.public_dict(user.user_id, generation)

    def _fresh_quote_context(self, user: OrderingUser, fingerprint: str) -> _QuoteContext:
        context = self._quotes.get(user.user_id)
        if context is None or not hmac.compare_digest(context.fingerprint, fingerprint):
            raise InvalidConfirmation("quote confirmation does not match")
        now = self._now()
        if not math.isfinite(context.fixture.expires_at):
            self._invalidate_user_checkout(user.user_id)
            raise OrderingSecurityFault("quote expiry is invalid")
        if context.generation != self.generation or now >= context.fixture.expires_at:
            self._invalidate_user_checkout(user.user_id)
            raise InvalidConfirmation("quote is stale")
        try:
            basket = self._baskets.snapshot(user.user_id, self.generation)
        except ValueError as err:
            self._invalidate_user_checkout(user.user_id)
            raise InvalidConfirmation("basket is stale") from err
        current = canonical_fingerprint(basket, context.address, context.payment, context.fixture)
        if not hmac.compare_digest(current, context.fingerprint):
            self._invalidate_user_checkout(user.user_id)
            raise InvalidConfirmation("checkout-bound data changed")
        return context

    async def async_quote(
        self,
        user: OrderingUser,
        generation: int,
        *,
        address_key: str,
        payment_key: str,
    ) -> dict[str, Any]:
        async with self._lock:
            self._guard(user, generation)
            basket = self._baskets.snapshot(user.user_id, generation)
            address = self._catalog.address(address_key)
            payment = self._catalog.payment(payment_key)
            fixture = self._catalog.create_quote(basket)
            fingerprint = canonical_fingerprint(basket, address, payment, fixture)
            self._invalidate_user_checkout(user.user_id)
            self._quotes[user.user_id] = _QuoteContext(
                owner_key=user.user_id,
                generation=generation,
                basket=basket,
                address=address,
                payment=payment,
                fixture=fixture,
                fingerprint=fingerprint,
            )
            return {**fixture.public_dict(), "fingerprint": fingerprint, "mockOnly": True}

    async def async_prepare_mock_confirmation(
        self,
        user: OrderingUser,
        generation: int,
        fingerprint: str,
    ) -> dict[str, Any]:
        async with self._lock:
            self._guard(user, generation)
            context = self._fresh_quote_context(user, fingerprint)
            challenge = self._challenge_source()
            if not isinstance(challenge, str) or len(challenge) < 24:
                raise OrderingSecurityFault("confirmation challenge source failed")
            now = self._now()
            ttl = CONFIRMATION_TTL_SECONDS
            if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not math.isfinite(ttl):
                raise OrderingSecurityFault("confirmation TTL is invalid")
            expires_at = min(now + ttl, context.fixture.expires_at)
            if not math.isfinite(expires_at):
                raise OrderingSecurityFault("confirmation expiry is invalid")
            confirmation = _Confirmation(
                owner_key=user.user_id,
                generation=generation,
                fingerprint=context.fingerprint,
                challenge=challenge,
                expires_at=expires_at,
            )
            self._confirmations[user.user_id] = confirmation
            return {
                "challenge": challenge,
                "fingerprint": context.fingerprint,
                "expiresAt": expires_at,
                "actionText": f"Run mock checkout — {context.fixture.purchase_total.action_amount}",
                "mockOnly": True,
            }

    async def _async_best_effort_uncertain(self, attempt_id: str) -> None:
        try:
            current = next(item for item in self.journal.records if item.attempt_id == attempt_id)
            if current.state in {JournalState.DISPATCHING, JournalState.VERIFYING}:
                await self.journal.async_transition(
                    attempt_id, JournalState.UNCERTAIN, outcome="journal_write_failure"
                )
        except (JournalFault, StopIteration):
            pass

    async def _async_recover_cancelled_attempt(self, attempt_id: str | None) -> None:
        """Reach the safest durable state after cancellation at the execution seam."""
        self._security_fault = True
        self._enabled = False
        self._invalidate_all_ephemeral()

        if attempt_id is not None:
            try:
                await self._async_best_effort_uncertain(attempt_id)
            except asyncio.CancelledError:
                # This child is shielded from caller cancellation. If a storage
                # implementation itself raises cancellation, still attempt the
                # independent durable safety latch below.
                pass
        try:
            await self._async_latch_safety_fault()
        except asyncio.CancelledError:
            # A cancelled safety-store write must not reopen this manager. Try
            # the secondary journal marker before recovery is considered done.
            try:
                await self._async_write_security_marker()
            except asyncio.CancelledError:
                pass

    async def _async_complete_cancellation_recovery(self, attempt_id: str | None) -> None:
        """Protect recovery from repeated cancellation of the executing caller."""
        recovery = asyncio.create_task(self._async_recover_cancelled_attempt(attempt_id))
        while not recovery.done():
            try:
                await asyncio.shield(recovery)
            except asyncio.CancelledError:
                continue
            except Exception:
                break

        # Observe child failures without replacing the original cancellation.
        # KeyboardInterrupt and SystemExit remain intentionally unhandled.
        try:
            recovery.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def async_execute_mock_checkout(
        self,
        user: OrderingUser,
        generation: int,
        challenge: str,
        fingerprint: str,
    ) -> dict[str, object]:
        """Consume one confirmation and execute only the in-process fixture adapter."""
        async with self._lock:
            self._guard(user, generation)
            confirmation = self._confirmations.pop(user.user_id, None)
            if (
                confirmation is None
                or confirmation.owner_key != user.user_id
                or confirmation.generation != generation
                or self._now() >= confirmation.expires_at
                or not math.isfinite(confirmation.expires_at)
                or not isinstance(challenge, str)
                or not isinstance(fingerprint, str)
                or not hmac.compare_digest(confirmation.challenge, challenge)
                or not hmac.compare_digest(confirmation.fingerprint, fingerprint)
            ):
                raise InvalidConfirmation("mock confirmation is invalid or already used")
            context = self._fresh_quote_context(user, fingerprint)
            attempt_key = f"attempt-{self._attempt_source()}"
            adapter_executed = False
            record = None
            try:
                record = await self.journal.async_create(
                    attempt_key, generation, context.fingerprint
                )
                await self.journal.async_transition(record.attempt_id, JournalState.QUOTED)
                # Persist a restart-recoverable state before the only execution boundary.
                await self.journal.async_transition(record.attempt_id, JournalState.DISPATCHING)
                # Re-read both current options and durable generation immediately before
                # executing the fixture-only adapter. A pre-reload option race fails closed.
                self._guard(user, generation)
                await self._durable_state.async_assert_current(generation)
                self._guard(user, generation)
                result = await self._checkout_adapter.async_execute(
                    mode=MOCK_MODE,
                    fingerprint=context.fingerprint,
                )
                adapter_executed = True
                final_state = (
                    JournalState.CONFIRMED_SUCCEEDED
                    if result.outcome == "synthetic_success"
                    else JournalState.CONFIRMED_FAILED
                )
                await self.journal.async_transition(
                    record.attempt_id,
                    final_state,
                    outcome=result.outcome,
                )
            except asyncio.CancelledError:
                await self._async_complete_cancellation_recovery(
                    record.attempt_id if record is not None else None
                )
                raise
            except (JournalFault, OrderingStateFault) as err:
                if record is not None and adapter_executed:
                    await self._async_best_effort_uncertain(record.attempt_id)
                await self._async_latch_safety_fault()
                raise OrderingSecurityFault("journal safety fault") from err
            except Exception as err:
                if record is not None:
                    await self._async_best_effort_uncertain(record.attempt_id)
                await self._async_latch_safety_fault()
                raise OrderingSecurityFault("mock execution safety fault") from err
            self._quotes.pop(user.user_id, None)
            self._baskets.clear(user.user_id)
            return result.public_dict()
