"""Serialized account-scoped manager for default-off mock ordering."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import math
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .ordering_adapter import (
    MOCK_MODE,
    FixtureHTTP5xx,
    FixtureMalformedResponse,
    MockCheckoutAdapter,
)
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
MANUAL_RESOLUTION_TTL_SECONDS = 60


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


class OrderingManualCheckRequired(OrderingError):
    """A potentially consequential live attempt requires administrator review."""


class InvalidManualResolution(OrderingError):
    """Manual resolution schema, state, revision, or challenge is invalid."""


class OrderingRecoveryWriteFailed(OrderingError):
    """Recovery persistence failed and ordering remains blocked."""


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


@dataclass(frozen=True, slots=True, repr=False)
class _ManualResolutionChallenge:
    owner_key: str
    attempt_id: str
    record_revision: int
    expected_state: str
    resolution: str
    generation: int
    challenge: str
    expires_at: float

    def __repr__(self) -> str:
        return "_ManualResolutionChallenge(<redacted>)"


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
        execution_mode: str = MOCK_MODE,
    ) -> None:
        if execution_mode not in {MOCK_MODE, "live"}:
            raise ValueError("unsupported execution mode")
        self._configured_allow = allow_ordering
        self._configured_ack = ordering_acknowledged
        self._live_options = live_options
        self._enabled = allow_ordering is True and ordering_acknowledged is True
        self._catalog = catalog
        self.journal = journal
        self._checkout_adapter = checkout_adapter
        self._execution_mode = execution_mode
        self._clock = clock
        self._challenge_source = challenge_source or (lambda: secrets.token_urlsafe(32))
        self._attempt_source = attempt_source or (lambda: secrets.token_hex(16))
        self._lock = asyncio.Lock()
        self._durable_state = durable_state or DurableOrderingState(MemoryOrderingStateStorage())
        self._baskets = BasketManager(clock=clock)
        self._quotes: dict[str, _QuoteContext] = {}
        self._confirmations: dict[str, _Confirmation] = {}
        self._manual_challenges: dict[str, _ManualResolutionChallenge] = {}
        self._security_fault = self._durable_state.integrity_fault
        self._manual_check = self._durable_state.manual_check_required
        self._initialized = False

    @property
    def enabled(self) -> bool:
        return (
            self._enabled
            and not self._security_fault
            and not self._manual_check
            and not self._durable_state.safety_fault
            and self._live_gate()
        )

    @property
    def generation(self) -> int:
        return self._durable_state.generation

    @property
    def security_fault(self) -> bool:
        return self._security_fault

    @property
    def integrity_fault(self) -> bool:
        return self._security_fault or self._durable_state.integrity_fault

    @property
    def manual_check_required(self) -> bool:
        return self._manual_check or self._durable_state.manual_check_required

    @property
    def recovery_required(self) -> bool:
        return self.manual_check_required or self.integrity_fault

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

    async def _async_write_security_marker(self, attempt_id: str | None = None) -> bool:
        """Persist a non-clearable journal marker, preferring the affected attempt."""
        try:
            if attempt_id is not None:
                current = self.journal.get(attempt_id)
                if current.state not in {
                    JournalState.CONFIRMED_SUCCEEDED,
                    JournalState.CONFIRMED_FAILED,
                    JournalState.LEGACY_MOCK_NO_REMOTE_EFFECT,
                    JournalState.INTEGRITY_FAULT,
                }:
                    await self.journal.async_transition(
                        attempt_id, JournalState.INTEGRITY_FAULT
                    )
                    return True
                if current.state is JournalState.INTEGRITY_FAULT:
                    return True
            marker = await self.journal.async_create(
                f"attempt-{self._attempt_source()}", self.generation, "0" * 64
            )
            await self.journal.async_transition(
                marker.attempt_id, JournalState.INTEGRITY_FAULT
            )
            return True
        except JournalFault:
            return False

    async def _async_force_integrity_fault(self, attempt_id: str | None = None) -> None:
        """Attempt both independent permanent latches despite storage cancellation."""
        self._security_fault = True
        self._enabled = False
        self._invalidate_all_ephemeral()
        state_saved = False
        try:
            await self._durable_state.async_latch_fault()
            state_saved = self._durable_state.integrity_fault
        except (OrderingStateFault, asyncio.CancelledError):
            pass
        if not state_saved:
            try:
                await self._async_write_security_marker(attempt_id)
            except asyncio.CancelledError:
                # A storage implementation may use cancellation as a fault. Both
                # durable layers were still attempted and this manager stays shut.
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
        await self._async_force_integrity_fault()

    async def _async_latch_manual_check(
        self, *, attempt_id: str, record_revision: int
    ) -> None:
        """Persist the exact resolvable latch and invalidate old authority."""
        self._manual_check = True
        self._invalidate_all_ephemeral()
        try:
            await self._durable_state.async_latch_manual_check(
                attempt_id=attempt_id, record_revision=record_revision
            )
        except OrderingStateFault:
            await self._async_force_integrity_fault(attempt_id)
            raise

    async def async_initialize(self) -> None:
        """Migrate/recover durable authority before opening any ordering API."""
        async with self._lock:
            if self._initialized:
                return
            if not self._durable_state.loaded:
                try:
                    await self._durable_state.async_load()
                    self._security_fault = self._durable_state.integrity_fault
                    self._manual_check = self._durable_state.manual_check_required
                except OrderingStateFault:
                    self._security_fault = True
                    self._enabled = False
            try:
                await self.journal.async_load()
            except JournalFault:
                await self._async_latch_safety_fault()

            sources = {self._durable_state.load_source, self.journal.load_source}
            if "v1" in sources and len(sources) != 1:
                await self._async_latch_safety_fault()
            if self.journal.integrity_fault:
                await self._async_latch_safety_fault()

            unresolved = self.journal.unresolved_manual_checks
            binding = self._durable_state.manual_binding
            if len(unresolved) > 1:
                await self._async_force_integrity_fault()
            elif len(unresolved) == 1 and not self._security_fault:
                record = unresolved[0]
                self._manual_check = True
                if self._durable_state.manual_check_required:
                    if (
                        binding is None
                        or binding.attempt_id != record.attempt_id
                        or binding.record_revision != record.record_revision
                        or binding.resolution is not None
                        or binding.generation != self.generation
                    ):
                        await self._async_force_integrity_fault(record.attempt_id)
                else:
                    try:
                        await self._async_latch_manual_check(
                            attempt_id=record.attempt_id,
                            record_revision=record.record_revision,
                        )
                    except OrderingStateFault:
                        pass
            elif self._durable_state.manual_check_required and not self._security_fault:
                valid_pending_clear = False
                if binding is not None and binding.resolution is not None:
                    try:
                        record = self.journal.get(binding.attempt_id)
                    except JournalFault:
                        record = None
                    valid_pending_clear = bool(
                        record is not None
                        and record.record_revision == binding.record_revision
                        and record.resolution == binding.resolution
                        and record.evidence_source == "admin_review"
                        and record.state
                        in {
                            JournalState.CONFIRMED_SUCCEEDED,
                            JournalState.CONFIRMED_FAILED,
                        }
                        and binding.generation == self.generation
                    )
                if valid_pending_clear:
                    self._manual_check = True
                else:
                    await self._async_force_integrity_fault(
                        binding.attempt_id if binding is not None else None
                    )

            if self._security_fault and not self._durable_state.integrity_fault:
                await self._async_write_security_marker()
            if self._security_fault:
                self._enabled = False
                self._invalidate_all_ephemeral()
            self._initialized = True

    def _invalidate_all_ephemeral(self) -> None:
        self._baskets.invalidate_all()
        self._quotes.clear()
        self._confirmations.clear()
        self._manual_challenges.clear()

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
                and not self.manual_check_required
                and not self.journal.integrity_fault
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

    @staticmethod
    def _admin_guard(user: OrderingUser) -> None:
        if not user.is_admin:
            raise OrderingAdminRequired("administrator access is required")

    def _guard(self, user: OrderingUser, generation: int | None = None) -> None:
        self._admin_guard(user)
        if self.integrity_fault:
            raise OrderingSecurityFault("ordering disabled by an integrity fault")
        if self.manual_check_required:
            raise OrderingManualCheckRequired("manual account reconciliation is required")
        if not self.enabled:
            raise OrderingDisabled("ordering is disabled")
        if generation is not None and (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation != self.generation
        ):
            raise StaleOrderingGeneration("ordering generation is stale")

    async def async_state(self, user: OrderingUser) -> dict[str, Any]:
        async with self._lock:
            self._admin_guard(user)
            if self.recovery_required:
                return {
                    "enabled": False,
                    "mockOnly": True,
                    "liveOrderingAvailable": False,
                    "manualCheckRequired": self.manual_check_required,
                    "integrityFault": self.integrity_fault,
                    "orderingBlocked": True,
                }
            self._guard(user)
            return {
                "enabled": True,
                "generation": self.generation,
                "mockOnly": True,
                "liveOrderingAvailable": False,
                "manualCheckRequired": False,
                "integrityFault": False,
                "orderingBlocked": False,
            }

    def _bound_manual_record(self) -> Any:
        """Return only the record named by the durable manual binding."""
        binding = self._durable_state.manual_binding
        if binding is None or binding.generation != self.generation:
            raise OrderingSecurityFault("manual recovery binding is unavailable")
        try:
            record = self.journal.get(binding.attempt_id)
        except JournalFault as err:
            raise OrderingSecurityFault("manual recovery binding is unavailable") from err
        if record.record_revision != binding.record_revision:
            raise OrderingSecurityFault("manual recovery binding changed")
        if binding.resolution is None:
            valid = record.state is JournalState.MANUAL_CHECK_REQUIRED
        else:
            valid = (
                record.state
                in {JournalState.CONFIRMED_SUCCEEDED, JournalState.CONFIRMED_FAILED}
                and record.resolution == binding.resolution
                and record.evidence_source == "admin_review"
            )
        if not valid:
            raise OrderingSecurityFault("manual recovery binding changed")
        return record

    async def async_list_manual_checks(self, user: OrderingUser) -> dict[str, Any]:
        """List only privacy-allowlisted records needed to recover this account."""
        async with self._lock:
            self._admin_guard(user)
            if not self.recovery_required:
                raise OrderingDisabled("ordering recovery is unavailable")
            records = (
                list(self.journal.unresolved_manual_checks)
                if self.integrity_fault
                else [self._bound_manual_record()]
            )
            return {"attempts": [record.recovery_dict() for record in records]}

    async def async_get_manual_check(
        self, user: OrderingUser, attempt_id: str
    ) -> dict[str, Any]:
        async with self._lock:
            self._admin_guard(user)
            if not self.recovery_required:
                raise OrderingDisabled("ordering recovery is unavailable")
            try:
                record = self.journal.get(attempt_id)
            except JournalFault as err:
                raise InvalidManualResolution("manual check does not exist") from err
            if self.integrity_fault:
                allowed = record.state is JournalState.MANUAL_CHECK_REQUIRED
            else:
                allowed = record == self._bound_manual_record()
            if not allowed:
                raise InvalidManualResolution("manual check is unavailable")
            return record.recovery_dict()

    @staticmethod
    def _validate_resolution_request(
        attempt_id: object,
        expected_revision: object,
        expected_state: object,
        resolution: object,
    ) -> tuple[str, int, str, str]:
        if (
            not isinstance(attempt_id, str)
            or not attempt_id.startswith("attempt-")
            or len(attempt_id) > 100
            or isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
            or not isinstance(expected_state, str)
            or expected_state
            not in {
                JournalState.MANUAL_CHECK_REQUIRED.value,
                JournalState.CONFIRMED_SUCCEEDED.value,
                JournalState.CONFIRMED_FAILED.value,
            }
            or not isinstance(resolution, str)
            or resolution
            not in {
                "found_succeeded",
                "found_failed_or_cancelled",
                "still_unknown",
            }
        ):
            raise InvalidManualResolution("manual resolution request is invalid")
        return attempt_id, expected_revision, expected_state, resolution

    async def async_prepare_manual_resolution(
        self,
        user: OrderingUser,
        *,
        attempt_id: str,
        expected_revision: int,
        expected_state: str,
        resolution: str,
    ) -> dict[str, Any]:
        """Create a short-lived same-admin challenge bound to exact durable state."""
        async with self._lock:
            self._admin_guard(user)
            if self.integrity_fault:
                raise OrderingSecurityFault("integrity fault is not clearable")
            if not self.manual_check_required:
                raise OrderingDisabled("manual recovery is unavailable")
            attempt_id, expected_revision, expected_state, resolution = (
                self._validate_resolution_request(
                    attempt_id, expected_revision, expected_state, resolution
                )
            )
            try:
                record = self.journal.get(attempt_id)
            except JournalFault as err:
                raise InvalidManualResolution("manual check does not exist") from err
            if record != self._bound_manual_record():
                raise InvalidManualResolution("manual check is not durably bound")
            if (
                record.record_revision != expected_revision
                or record.state.value != expected_state
            ):
                raise InvalidManualResolution("manual check state or revision changed")
            if record.state is not JournalState.MANUAL_CHECK_REQUIRED:
                if resolution == "still_unknown" or record.resolution != resolution:
                    raise InvalidManualResolution("manual conclusion conflicts with history")
            challenge = self._challenge_source()
            if not isinstance(challenge, str) or len(challenge) < 24:
                raise OrderingSecurityFault("manual challenge source failed")
            now = self._now()
            expires_at = now + MANUAL_RESOLUTION_TTL_SECONDS
            if not math.isfinite(expires_at):
                raise OrderingSecurityFault("manual challenge expiry is invalid")
            for key, value in tuple(self._manual_challenges.items()):
                if value.owner_key == user.user_id:
                    self._manual_challenges.pop(key, None)
            self._manual_challenges[challenge] = _ManualResolutionChallenge(
                owner_key=user.user_id,
                attempt_id=attempt_id,
                record_revision=expected_revision,
                expected_state=expected_state,
                resolution=resolution,
                generation=self.generation,
                challenge=challenge,
                expires_at=expires_at,
            )
            return {
                "challenge": challenge,
                "attemptRef": attempt_id,
                "recordRevision": expected_revision,
                "resolution": resolution,
                "expiresAt": expires_at,
                "acknowledgementRequired": True,
            }

    async def async_resolve_manual_check(
        self,
        user: OrderingUser,
        *,
        attempt_id: str,
        expected_revision: int,
        expected_state: str,
        resolution: str,
        challenge: str,
        acknowledged: object,
    ) -> dict[str, Any]:
        """Resolve under the account lock; never redispatch or offer retry."""
        async with self._lock:
            self._admin_guard(user)
            if self.integrity_fault:
                raise OrderingSecurityFault("integrity fault is not clearable")
            if acknowledged is not True or not isinstance(challenge, str):
                raise InvalidManualResolution("literal acknowledgement is required")
            attempt_id, expected_revision, expected_state, resolution = (
                self._validate_resolution_request(
                    attempt_id, expected_revision, expected_state, resolution
                )
            )
            prepared = self._manual_challenges.pop(challenge, None)
            if (
                prepared is None
                or prepared.owner_key != user.user_id
                or prepared.attempt_id != attempt_id
                or prepared.record_revision != expected_revision
                or prepared.expected_state != expected_state
                or prepared.resolution != resolution
                or prepared.generation != self.generation
                or self._now() >= prepared.expires_at
                or not hmac.compare_digest(prepared.challenge, challenge)
            ):
                raise InvalidManualResolution("manual resolution challenge is invalid")
            try:
                current = self.journal.get(attempt_id)
            except JournalFault as err:
                raise InvalidManualResolution("manual check does not exist") from err
            if current != self._bound_manual_record():
                raise InvalidManualResolution("manual check is not durably bound")
            if (
                current.record_revision != expected_revision
                or current.state.value != expected_state
            ):
                raise InvalidManualResolution("manual check state or revision changed")

            if current.state is not JournalState.MANUAL_CHECK_REQUIRED:
                if resolution == "still_unknown" or current.resolution != resolution:
                    raise InvalidManualResolution("manual conclusion conflicts with history")
                try:
                    await self._durable_state.async_clear_manual_check(
                        attempt_id=current.attempt_id,
                        record_revision=current.record_revision,
                        resolution=resolution,
                        generation=self.generation,
                    )
                except OrderingStateFault as err:
                    raise OrderingRecoveryWriteFailed("manual latch remains blocked") from err
                self._manual_check = False
                return {"resolved": True, "manualCheckRequired": False}

            if resolution == "still_unknown":
                try:
                    updated = await self.journal.async_review_unknown(
                        attempt_id, expected_revision=expected_revision
                    )
                    await self._durable_state.async_update_manual_binding(
                        attempt_id=updated.attempt_id,
                        record_revision=updated.record_revision,
                        resolution=None,
                    )
                except JournalFault as err:
                    raise OrderingRecoveryWriteFailed("manual review was not persisted") from err
                except OrderingStateFault as err:
                    await self._async_force_integrity_fault(attempt_id)
                    raise OrderingSecurityFault("manual review binding failed") from err
                self._manual_check = True
                return {
                    "resolved": False,
                    "manualCheckRequired": True,
                    "attempt": updated.recovery_dict(),
                }

            # Generation advances while the exact unresolved binding remains set.
            self._invalidate_all_ephemeral()
            try:
                await self._async_bump_generation()
            except OrderingStateFault as err:
                raise OrderingSecurityFault("ordering authority could not advance") from err
            try:
                updated = await self.journal.async_resolve(
                    attempt_id,
                    expected_revision=expected_revision,
                    resolution=resolution,
                )
            except JournalFault as err:
                self._manual_check = True
                raise OrderingRecoveryWriteFailed("manual conclusion was not persisted") from err
            try:
                # This second uniquely-bound write makes a failed clear safely and
                # exactly idempotent after reload; no historical search is allowed.
                await self._durable_state.async_update_manual_binding(
                    attempt_id=updated.attempt_id,
                    record_revision=updated.record_revision,
                    resolution=resolution,
                )
            except OrderingStateFault as err:
                await self._async_force_integrity_fault(attempt_id)
                raise OrderingSecurityFault("manual conclusion binding failed") from err
            if self.journal.unresolved_manual_checks:
                await self._async_force_integrity_fault(attempt_id)
                raise OrderingSecurityFault("multiple manual checks are not resolvable")
            try:
                await self._durable_state.async_clear_manual_check(
                    attempt_id=updated.attempt_id,
                    record_revision=updated.record_revision,
                    resolution=resolution,
                    generation=self.generation,
                )
            except OrderingStateFault as err:
                self._manual_check = True
                raise OrderingRecoveryWriteFailed("manual latch remains blocked") from err
            self._manual_check = False
            return {"resolved": True, "manualCheckRequired": False}

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
            current = self.journal.get(attempt_id)
            if current.state in {JournalState.DISPATCHING, JournalState.VERIFYING}:
                if current.execution_mode == "live":
                    await self.journal.async_transition(
                        attempt_id,
                        JournalState.MANUAL_CHECK_REQUIRED,
                        failure_class="persistence_failure",
                    )
                else:
                    await self.journal.async_transition(
                        attempt_id,
                        JournalState.LEGACY_MOCK_NO_REMOTE_EFFECT,
                        resolution="legacy_mock_no_remote_effect",
                        evidence_source="mock_adapter",
                    )
        except JournalFault:
            pass

    async def _async_recover_live_ambiguity(
        self, attempt_id: str, failure_class: str
    ) -> None:
        """Require exact journal and latch writes, otherwise persist integrity."""
        self._manual_check = True
        self._invalidate_all_ephemeral()
        try:
            current = self.journal.get(attempt_id)
        except JournalFault:
            await self._async_force_integrity_fault(attempt_id)
            return
        if current.execution_mode != "live" or current.dispatch_started_at is None:
            await self._async_force_integrity_fault(attempt_id)
            return
        if current.state in {JournalState.DISPATCHING, JournalState.VERIFYING}:
            expected_revision = current.record_revision + 1
        elif current.state is JournalState.MANUAL_CHECK_REQUIRED:
            expected_revision = current.record_revision
        else:
            await self._async_force_integrity_fault(attempt_id)
            return

        journal_saved = False
        latch_saved = False
        try:
            if current.state in {JournalState.DISPATCHING, JournalState.VERIFYING}:
                await self.journal.async_transition(
                    attempt_id,
                    JournalState.MANUAL_CHECK_REQUIRED,
                    failure_class=failure_class,
                )
            recovered = self.journal.get(attempt_id)
            journal_saved = (
                recovered.state is JournalState.MANUAL_CHECK_REQUIRED
                and recovered.record_revision == expected_revision
            )
        except (JournalFault, asyncio.CancelledError):
            pass
        try:
            await self._durable_state.async_latch_manual_check(
                attempt_id=attempt_id,
                record_revision=expected_revision,
            )
            binding = self._durable_state.manual_binding
            latch_saved = bool(
                binding is not None
                and binding.attempt_id == attempt_id
                and binding.record_revision == expected_revision
                and binding.resolution is None
                and binding.generation == self.generation
            )
        except (OrderingStateFault, asyncio.CancelledError):
            pass
        if not (journal_saved and latch_saved):
            await self._async_force_integrity_fault(attempt_id)

    async def _async_recover_cancelled_attempt(self, attempt_id: str | None) -> None:
        """Reach the safest durable state after cancellation at the execution seam."""
        if attempt_id is not None:
            try:
                current = self.journal.get(attempt_id)
            except JournalFault:
                current = None
            if (
                current is not None
                and current.execution_mode == "live"
                and current.dispatch_started_at is not None
            ):
                await self._async_recover_live_ambiguity(attempt_id, "cancelled")
                return

        if attempt_id is not None:
            try:
                await self._async_best_effort_uncertain(attempt_id)
            except asyncio.CancelledError:
                pass
        await self._async_force_integrity_fault(attempt_id)

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
                    attempt_key,
                    generation,
                    context.fingerprint,
                    amount_minor=context.fixture.purchase_total.amount_minor,
                    currency=context.fixture.purchase_total.currency,
                    execution_mode=self._execution_mode,
                    provider_session_hash=hashlib.sha256(
                        context.fixture.checkout_session_id.encode()
                    ).hexdigest(),
                    store_display_name=context.basket.store_label,
                    item_count=sum(item.quantity for item in context.basket.items),
                    item_summary=(
                        f"{sum(item.quantity for item in context.basket.items)} fixture item(s)"
                    ),
                    masked_payment_label=context.payment.masked_label,
                    masked_address_alias=context.address.masked_label,
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
                    mode=self._execution_mode,
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
            except (
                TimeoutError,
                ConnectionResetError,
                FixtureHTTP5xx,
                FixtureMalformedResponse,
            ) as err:
                if self._execution_mode == "live" and record is not None:
                    failure_class = (
                        "timeout"
                        if isinstance(err, TimeoutError)
                        else "connection_reset"
                        if isinstance(err, ConnectionResetError)
                        else "http_5xx"
                        if isinstance(err, FixtureHTTP5xx)
                        else "malformed_response"
                    )
                    await self._async_recover_live_ambiguity(
                        record.attempt_id, failure_class
                    )
                    if self.integrity_fault:
                        raise OrderingSecurityFault("ambiguous recovery integrity fault") from err
                    raise OrderingManualCheckRequired(
                        "manual account reconciliation is required"
                    ) from err
                await self._async_latch_safety_fault()
                raise OrderingSecurityFault("fixture execution safety fault") from err
            except (JournalFault, OrderingStateFault) as err:
                if (
                    record is not None
                    and adapter_executed
                    and self._execution_mode == "live"
                ):
                    await self._async_recover_live_ambiguity(
                        record.attempt_id, "persistence_failure"
                    )
                    if not self.integrity_fault:
                        raise OrderingManualCheckRequired(
                            "manual account reconciliation is required"
                        ) from err
                elif record is not None and adapter_executed:
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
