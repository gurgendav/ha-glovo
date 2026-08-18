"""Frozen, privacy-safe public contracts for the live ordering capability.

This is deliberately a facade rather than an HA service registration.  It accepts
only local handles, rejects unknown fields, and never returns provider IDs,
paths, request bodies, fingerprints, or raw errors.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import secrets
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final

from .api_session import ApiSessionError, DeliveryLocation
from .ordering_account import AccountClient, InvalidSelection
from .ordering_basket_authority_store import (
    BasketAuthorityFault,
    DurableBasketAuthority,
)
from .ordering_contracts import CustomerIdentity
from .ordering_live_catalog import LiveCatalogClient
from .ordering_live_quote import (
    AuthoritativeConfirmationManager,
    AuthoritativeQuote,
    InvalidQuoteConfirmation,
    QuoteRequest,
    QuoteTemplateClient,
)
from .ordering_live_selection import LiveSelectionError, LiveSelectionRegistry, parse_selected_products
from .ordering_packages import (
    PackageLibrary,
    PackageLibraryError,
    PackageLibraryUnavailable,
    PackageStale,
    account_digest,
    recipe_items_from_capture,
    reconcile_recipe,
    store_digest,
)
from .ordering_remote_basket import BasketIntent, RemoteBasketClient, RemoteBasketSnapshot
from .ordering_remote_basket_discovery import (
    RemoteBasketDiscoveryClient,
    RemoteBasketDiscoveryError,
    RemoteBasketDiscoveryResult,
    RemoteBasketDiscoveryStatus,
)

_LOGGER = logging.getLogger(__name__)
_DETERMINISTIC_REJECTION_STATUSES: Final = frozenset(
    {400, 401, 403, 404, 405, 406, 409, 410, 415, 422, 429}
)
BASKET_AUTHORITY_LEASE_SECONDS: Final = 60.0
_BASKET_SNAPSHOT_DIGEST_DOMAIN: Final = (
    b"glovo.ordering.basket_authority.provider_snapshot.v1\0"
)


@contextmanager
def _basket_sync_stage(name: str):
    """Log only a closed stage and redaction-safe exception classification."""
    try:
        yield
    except Exception as err:
        status = getattr(err, "status", None)
        if status not in _DETERMINISTIC_REJECTION_STATUSES:
            status = None
        _LOGGER.warning(
            "Glovo basket sync rejected at stage=%s class=%s category=%s status=%s",
            name,
            type(err).__name__,
            (
                getattr(err, "category", None)
                if getattr(err, "category", None) == "provider_rejection"
                else None
            ),
            status,
        )
        raise

PUBLIC_OPERATIONS: Final = (
    "state", "live/addresses", "live/stores", "live/store_menu", "live/payment_methods",
    "live/basket", "live/basket_adopt", "live/basket_set", "live/basket_clear", "live/basket_reconcile",
    "live/create_quote", "live/prepare_confirmation", "live/execute_checkout", "live/checkout_status",
    "library/list", "library/package_save", "library/package_delete",
    "library/package_prepare",
)
# These are schemas, not permissive examples: unknown keys are rejected exactly.
OPERATION_REQUEST_FIELDS: Final = {
    # State is a bootstrap response that establishes the first generation.
    "state": frozenset(),
    "live/addresses": frozenset({"generation"}),
    "live/stores": frozenset({"generation", "storeSlug", "addressHandle"}),
    "live/store_menu": frozenset({"generation", "storeHandle", "addressHandle"}),
    "live/payment_methods": frozenset({"generation"}),
    "live/basket": frozenset({"generation"}),
    "live/basket_adopt": frozenset(
        {"generation", "addressHandle", "storeHandle", "products"}
    ),
    "live/basket_set": frozenset({"generation", "expectedRevision", "storeHandle", "addressHandle", "products"}),
    "live/basket_clear": frozenset({"generation", "expectedRevision"}),
    "live/basket_reconcile": frozenset({"generation"}),
    "live/create_quote": frozenset({"generation", "addressHandle", "paymentHandle"}),
    "live/prepare_confirmation": frozenset({"generation"}),
    "live/execute_checkout": frozenset({"generation", "challenge", "acknowledged"}),
    "live/checkout_status": frozenset({"generation"}),
    "library/list": frozenset({"generation"}),
    "library/package_save": frozenset(
        {
            "generation", "expectedStoreRevision", "packageRef",
            "expectedRevision", "name", "aliases", "storeHandle", "products",
        }
    ),
    "library/package_delete": frozenset(
        {"generation", "expectedStoreRevision", "packageRef", "expectedRevision"}
    ),
    "library/package_prepare": frozenset(
        {"generation", "packageKey", "addressHandle"}
    ),
}


class PublicContractError(ValueError):
    """A stable non-sensitive rejection suitable for the public boundary."""

    def __init__(self) -> None:
        super().__init__("live ordering request is unavailable or invalid")


class PreparationMutationRejected(PublicContractError):
    """Closed deterministic provider diagnostic with a generic public message."""

    category = "provider_rejection"

    def __init__(self, status: int | None) -> None:
        self.status = (
            status
            if status in _DETERMINISTIC_REJECTION_STATUSES
            else None
        )
        super().__init__()


class PackageSaveStageError(PublicContractError):
    """Privacy-safe package-save failure with an allowlisted diagnostic stage."""

    _STAGES = frozenset({"account", "request", "selection", "validation", "storage"})

    def __init__(self, stage: str) -> None:
        if stage not in self._STAGES:
            stage = "request"
        self.stage = stage
        super().__init__()


@dataclass(frozen=True, slots=True, repr=False)
class _BasketState:
    generation: int
    revision: int
    store_handle: str
    address_handle: str = field(repr=False)
    currency: str
    store_label: str
    lines: tuple[dict[str, Any], ...]
    snapshot: RemoteBasketSnapshot
    store: Any = field(repr=False)
    address_fingerprint: str = field(repr=False)
    delivery_location: DeliveryLocation = field(repr=False)


class _BasketAuthorityMode(Enum):
    """Closed account-wide knowledge about the one remote basket."""

    UNKNOWN = "UNKNOWN"
    ABSENT_VERIFIED = "ABSENT_VERIFIED"
    ADOPTED = "ADOPTED"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True, slots=True, repr=False)
class _BasketAuthority:
    """Short-lived account binding; provider and selection identity stay private."""

    mode: _BasketAuthorityMode
    generation: int | None = None
    account_digest: str | None = field(default=None, repr=False)
    intent_fingerprint: str | None = field(default=None, repr=False)
    selection_fingerprint: str | None = field(default=None, repr=False)
    store_handle: str | None = field(default=None, repr=False)
    address_handle: str | None = field(default=None, repr=False)
    expires_at: float | None = field(default=None, repr=False)
    state: _BasketState | None = field(default=None, repr=False)


@dataclass(slots=True, repr=False)
class _QuoteState:
    generation: int
    address_handle: str
    payment_handle: str
    quote: AuthoritativeQuote


def _generation(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PublicContractError
    return value


def _handle(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise PublicContractError
    return value


def _revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PublicContractError
    return value


def validate_public_request(
    operation: object, request: object
) -> tuple[str, dict[str, Any], int | None]:
    if not isinstance(operation, str) or operation not in PUBLIC_OPERATIONS or not isinstance(request, Mapping):
        raise PublicContractError
    fields = OPERATION_REQUEST_FIELDS[operation]
    if set(request) != fields:
        raise PublicContractError
    copied = dict(request)
    if operation == "state":
        return operation, copied, None
    generation = _generation(copied["generation"])
    if operation == "live/execute_checkout" and (
        copied["acknowledged"] is not True or not isinstance(copied["challenge"], str) or not copied["challenge"] or len(copied["challenge"]) > 64
    ):
        raise PublicContractError
    return operation, copied, generation


class LiveOrderingFacade:
    """Single explicit public surface; checkout execution consumes authority only.

    The first release intentionally has no dispatch/order creation implementation.
    `live/execute_checkout` verifies the literal acknowledgement and consumes the
    challenge, then returns a non-order status.  A future dispatch must be a
    separately reviewed capability, not an accidental extension of this facade.
    """

    def __init__(
        self,
        *,
        account: AccountClient,
        catalog: LiveCatalogClient,
        selections: LiveSelectionRegistry,
        baskets: RemoteBasketClient,
        quotes: QuoteTemplateClient,
        confirmations: AuthoritativeConfirmationManager,
        preparation_authority: Any | None = None,
        preparation_attempt_source: Any | None = None,
        package_library: PackageLibrary | None = None,
        basket_authority_clock: Callable[[], float] = time.monotonic,
        discovery_client: RemoteBasketDiscoveryClient | None = None,
        basket_authority: DurableBasketAuthority | None = None,
    ) -> None:
        self._account = account
        self._catalog = catalog
        self._selections = selections
        self._baskets = baskets
        self._quotes = quotes
        self._confirmations = confirmations
        # Deliberately opaque to keep this frozen facade importable without
        # enabling any transport. Mutations are denied unless an authority was
        # explicitly loaded and injected by OrderingLiveFlow/runtime.
        self._preparation_authority = preparation_authority
        self._preparation_attempt_source = preparation_attempt_source or (
            lambda: f"prep-{secrets.token_hex(16)}"
        )
        self._package_library = package_library
        self._basket_authority_clock = basket_authority_clock
        self._discovery_client = discovery_client
        self._basket_evidence = basket_authority
        self._basket_authority = _BasketAuthority(_BasketAuthorityMode.UNKNOWN)
        self._quote: dict[str, _QuoteState] = {}

    def invalidate_all(self) -> None:
        """Discard every ephemeral handle/quote/basket on a lifecycle change."""
        self.invalidate_basket_mutation_authority()
        self._account.invalidate()
        self._selections.invalidate()

    def invalidate_preparation_authority(self) -> None:
        """Backward-compatible alias for a basket-changing provider write."""
        self.invalidate_basket_mutation_authority()

    def invalidate_basket_mutation_authority(self) -> None:
        """Make account basket knowledge unknown before a provider write.

        Saved address/payment bindings are deliberately retained until the quote
        response has been revalidated against the provider; clearing them here
        would turn that mandatory post-template verification into an impossible
        local lookup. The exact catalog handles used by a successful basket write
        are also retained for the authority's shorter lease. Lifecycle
        invalidation uses :meth:`invalidate_all` to clear every handle.
        """
        self._set_basket_unknown()
        self._quote.clear()
        self._confirmations.invalidate_all()

    def invalidate_quote_mutation_authority(self) -> None:
        """Invalidate quote/catalog authority while retaining the remote basket.

        Creating a checkout template does not mutate the basket. Keeping its exact
        snapshot is required so the administrator can still inspect or explicitly
        delete that remote basket after quote preparation.
        """
        self._quote.clear()
        self._confirmations.invalidate_all()

    def _owner(self, owner: object) -> str:
        return _handle(owner)

    def _set_basket_unknown(self) -> None:
        self._basket_authority = _BasketAuthority(_BasketAuthorityMode.UNKNOWN)

    def _basket_evidence_ready(self) -> bool:
        evidence = self._basket_evidence
        return bool(
            evidence is None
            or (
                evidence.loaded
                and not evidence.integrity_fault
                and not evidence.transient_write_fault
            )
        )

    async def _async_record_basket_unknown(self, generation: int) -> None:
        evidence = self._basket_evidence
        if evidence is not None:
            await evidence.async_record_unknown(generation=generation)

    async def _async_finalize_basket_unknown_after_dispatch(
        self, generation: int, *, retain_transient_fault: bool = False
    ) -> None:
        """Finish UNKNOWN despite repeated caller cancellation; never mask outcome."""
        self._set_basket_unknown()
        evidence = self._basket_evidence
        if evidence is None:
            return
        recovery = asyncio.create_task(
            evidence.async_record_unknown(generation=generation)
        )
        while not recovery.done():
            try:
                await asyncio.shield(recovery)
            except asyncio.CancelledError:
                continue
            except BasketAuthorityFault:
                break
        try:
            recovery.result()
        except (asyncio.CancelledError, BasketAuthorityFault):
            pass
        if retain_transient_fault:
            # Only a later explicit read-only adoption may clear a post-success
            # basket-store fault, even when cancellation recovery saved UNKNOWN.
            evidence.transient_write_fault = True

    @staticmethod
    def _basket_snapshot_digest(snapshot: RemoteBasketSnapshot) -> str:
        if not isinstance(snapshot, RemoteBasketSnapshot):
            raise PublicContractError
        projection = snapshot.provider_projection_bytes
        if not isinstance(projection, bytes):
            raise PublicContractError
        return hashlib.sha256(
            _BASKET_SNAPSHOT_DIGEST_DOMAIN + projection
        ).hexdigest()

    def _basket_evidence_digests(
        self,
        *,
        customer: CustomerIdentity,
        store: Any,
        intent: BasketIntent,
    ) -> tuple[str, str, str]:
        try:
            return (
                account_digest(customer),
                store_digest(store),
                self._expectation_hash("basket_intent", intent),
            )
        except Exception:
            raise PublicContractError from None

    async def _async_publish_basket_absent(
        self,
        *,
        generation: int,
        customer: CustomerIdentity,
        store: Any,
        intent: BasketIntent,
        store_handle: str,
        address_handle: str,
        products: object,
    ) -> None:
        evidence = self._basket_evidence
        if evidence is not None:
            account_hash, store_hash, intent_hash = self._basket_evidence_digests(
                customer=customer, store=store, intent=intent
            )
            await evidence.async_record_absent(
                generation=generation,
                account_digest=account_hash,
                store_digest=store_hash,
                intent_digest=intent_hash,
            )
        self._record_basket_absent_verified(
            generation=generation,
            customer=customer,
            intent=intent,
            store_handle=store_handle,
            address_handle=address_handle,
            products=products,
        )

    async def _async_publish_basket_present(
        self,
        *,
        generation: int,
        customer: CustomerIdentity,
        store: Any,
        intent: BasketIntent,
        state: _BasketState,
        products: object,
    ) -> None:
        evidence = self._basket_evidence
        if evidence is not None:
            account_hash, store_hash, intent_hash = self._basket_evidence_digests(
                customer=customer, store=store, intent=intent
            )
            await evidence.async_record_present(
                generation=generation,
                account_digest=account_hash,
                store_digest=store_hash,
                intent_digest=intent_hash,
                snapshot_digest=self._basket_snapshot_digest(state.snapshot),
            )
        self._adopt_basket_snapshot(
            generation=generation,
            customer=customer,
            intent=intent,
            state=state,
            products=products,
        )

    async def _async_publish_basket_conflict(
        self,
        *,
        generation: int,
        customer: CustomerIdentity,
        store: Any,
        intent: BasketIntent,
    ) -> None:
        evidence = self._basket_evidence
        if evidence is not None:
            account_hash, store_hash, intent_hash = self._basket_evidence_digests(
                customer=customer, store=store, intent=intent
            )
            await evidence.async_record_conflict(
                generation=generation,
                account_digest=account_hash,
                store_digest=store_hash,
                intent_digest=intent_hash,
            )
        self._record_basket_conflict(generation=generation, customer=customer)

    def _authority_now(self) -> float:
        try:
            now = float(self._basket_authority_clock())
        except (TypeError, ValueError, OverflowError):
            raise PublicContractError from None
        if not math.isfinite(now):
            raise PublicContractError
        return now

    def _lease_expiry(self) -> float:
        limits = (
            BASKET_AUTHORITY_LEASE_SECONDS,
            getattr(self._account, "selection_ttl_seconds", BASKET_AUTHORITY_LEASE_SECONDS),
            getattr(self._selections, "selection_ttl_seconds", BASKET_AUTHORITY_LEASE_SECONDS),
        )
        try:
            lease = min(float(value) for value in limits)
        except (TypeError, ValueError, OverflowError):
            raise PublicContractError from None
        now = self._authority_now()
        expires_at = now + lease
        if not math.isfinite(lease) or lease <= 0 or not math.isfinite(expires_at):
            raise PublicContractError
        return expires_at

    def _active_basket_authority(self, generation: int) -> _BasketAuthority:
        if not self._basket_evidence_ready():
            self._set_basket_unknown()
            raise PublicContractError
        authority = self._basket_authority
        if authority.mode is _BasketAuthorityMode.UNKNOWN:
            raise PublicContractError
        if authority.generation != generation:
            self._set_basket_unknown()
            raise PublicContractError
        if authority.expires_at is None or self._authority_now() >= authority.expires_at:
            self._set_basket_unknown()
            raise PublicContractError
        return authority

    @classmethod
    def _selection_fingerprint(
        cls,
        *,
        store_handle: object,
        address_handle: object,
        products: object,
    ) -> str:
        store = _handle(store_handle)
        address = _handle(address_handle)
        parsed = parse_selected_products(products)
        canonical = [
            {
                "productHandle": item.product_handle,
                "quantity": item.quantity,
                "options": [
                    {"groupHandle": group, "optionHandles": list(options)}
                    for group, options in item.option_groups
                ],
            }
            for item in parsed
        ]
        return cls._expectation_hash(
            "basket_selection",
            {"storeHandle": store, "addressHandle": address, "products": canonical},
        )

    @classmethod
    def _basket_binding(
        cls,
        *,
        generation: object,
        customer: object,
        intent: object,
        store_handle: object,
        address_handle: object,
        products: object,
    ) -> tuple[int, str, str, str, str, str]:
        current_generation = _generation(generation)
        if not isinstance(customer, CustomerIdentity) or not isinstance(intent, BasketIntent):
            raise PublicContractError
        if str(customer.customer_id) != intent.customer_id:
            raise PublicContractError
        store = _handle(store_handle)
        address = _handle(address_handle)
        try:
            current_account = account_digest(customer)
        except Exception:
            raise PublicContractError from None
        return (
            current_generation,
            current_account,
            cls._expectation_hash("basket_intent", intent),
            cls._selection_fingerprint(
                store_handle=store, address_handle=address, products=products
            ),
            store,
            address,
        )

    def _record_basket_absent_verified(
        self,
        *,
        generation: int,
        customer: CustomerIdentity,
        intent: BasketIntent,
        store_handle: str,
        address_handle: str,
        products: object,
    ) -> None:
        """Install a short exact absence proof supplied by later GET integration."""
        bound = self._basket_binding(
            generation=generation,
            customer=customer,
            intent=intent,
            store_handle=store_handle,
            address_handle=address_handle,
            products=products,
        )
        self._basket_authority = _BasketAuthority(
            _BasketAuthorityMode.ABSENT_VERIFIED,
            generation=bound[0],
            account_digest=bound[1],
            intent_fingerprint=bound[2],
            selection_fingerprint=bound[3],
            store_handle=bound[4],
            address_handle=bound[5],
            expires_at=self._lease_expiry(),
        )
        self._quote.clear()
        self._confirmations.invalidate_all()

    def _adopt_basket_snapshot(
        self,
        *,
        generation: int,
        customer: CustomerIdentity,
        intent: BasketIntent,
        state: _BasketState,
        products: object,
    ) -> None:
        """Adopt exactly one verified snapshot without performing provider I/O."""
        if not isinstance(state, _BasketState) or state.generation != generation:
            raise PublicContractError
        bound = self._basket_binding(
            generation=generation,
            customer=customer,
            intent=intent,
            store_handle=state.store_handle,
            address_handle=state.address_handle,
            products=products,
        )
        try:
            expected_handles = tuple(
                item.product_handle for item in parse_selected_products(products)
            )
            state_handles = tuple(line["productHandle"] for line in state.lines)
        except (KeyError, TypeError, LiveSelectionError):
            raise PublicContractError from None
        if state_handles != expected_handles:
            raise PublicContractError
        try:
            snapshot_fingerprint = self._expectation_hash(
                "basket_intent", state.snapshot.intent()
            )
        except Exception:
            raise PublicContractError from None
        if snapshot_fingerprint != bound[2]:
            raise PublicContractError
        self._basket_authority = _BasketAuthority(
            _BasketAuthorityMode.ADOPTED,
            generation=bound[0],
            account_digest=bound[1],
            intent_fingerprint=bound[2],
            selection_fingerprint=bound[3],
            store_handle=bound[4],
            address_handle=bound[5],
            expires_at=self._lease_expiry(),
            state=state,
        )

    def _record_basket_conflict(
        self, *, generation: int, customer: CustomerIdentity
    ) -> None:
        """Record non-unique/ambiguous discovery without exposing provider data."""
        current_generation = _generation(generation)
        try:
            current_account = account_digest(customer)
        except Exception:
            raise PublicContractError from None
        self._basket_authority = _BasketAuthority(
            _BasketAuthorityMode.CONFLICT,
            generation=current_generation,
            account_digest=current_account,
            expires_at=self._lease_expiry(),
        )
        self._quote.clear()
        self._confirmations.invalidate_all()

    def _state(self, generation: int) -> _BasketState:
        authority = self._active_basket_authority(generation)
        state = authority.state
        if authority.mode is not _BasketAuthorityMode.ADOPTED or state is None:
            raise PublicContractError
        return state

    async def _async_require_store_open(self, store: Any, address: Any) -> None:
        """GET-only freshness barrier immediately before issuing authority."""
        fresh_store = await self._catalog.async_store(store.slug, address)
        if (
            getattr(fresh_store, "is_open", False) is not True
            or store_digest(fresh_store) != store_digest(store)
        ):
            raise PublicContractError

    @staticmethod
    def _basket_public(state: _BasketState) -> dict[str, Any]:
        provider_total = state.snapshot.basket_price.minor
        return {
            "status": "adopted",
            "revision": state.revision,
            "storeHandle": state.store_handle,
            "storeLabel": state.store_label,
            "itemCount": sum(
                item.quantity.increments for item in state.snapshot.products
            ),
            "currency": state.currency,
            "providerTotal": provider_total,
            "lines": [dict(line) for line in state.lines],
        }

    @staticmethod
    def _empty_basket_public() -> dict[str, Any]:
        return {
            "revision": 0,
            "storeHandle": "",
            "storeLabel": "",
            "itemCount": 0,
            "currency": "",
            "providerTotal": None,
            "lines": [],
        }

    @staticmethod
    def _expectation_hash(operation: str, value: object) -> str:
        """Hash a canonical private expectation without retaining provider data."""
        try:
            candidate: Any = value
            if hasattr(candidate, "create_body"):
                candidate = candidate.create_body()
            elif hasattr(candidate, "canonical_dict"):
                candidate = candidate.canonical_dict()
            encoded = json.dumps(
                {"operation": operation, "expected": candidate},
                default=lambda item: item.__class__.__name__,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        except (TypeError, ValueError, OverflowError):
            raise PublicContractError from None
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _deterministic_provider_rejection(error: BaseException) -> int | None:
        """Return a status only for the exact closed provider-rejection proof."""
        try:
            category = getattr(error, "category", None)
            status = getattr(error, "status", None)
        except BaseException:
            return None
        if (
            category == "provider_rejection"
            and type(status) is int
            and status in _DETERMINISTIC_REJECTION_STATUSES
        ):
            return status
        return None

    @staticmethod
    def _provider_rejection_evidence(
        *, purpose_name: str, error: BaseException, status: int
    ) -> str:
        """Hash only closed classification fields, never provider material."""
        encoded = json.dumps(
            {
                "category": "provider_rejection",
                "class": type(error).__name__,
                "purpose": purpose_name,
                "status": status,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    async def _async_preparation_mutation(
        self,
        *,
        operation: str,
        generation: int,
        purpose_name: str,
        expected_category: str,
        expected: object,
        invoke: Any,
    ) -> Any:
        """Persist intent/DISPATCHING before exactly one private adapter call."""
        authority = self._preparation_authority
        if authority is None or not getattr(authority, "loaded", False) or getattr(authority, "unresolved", ()):
            raise PublicContractError
        try:
            from .ordering_prep_authority import PreparationPurpose

            expectation_hash = self._expectation_hash(operation, expected)
            attempt_id = self._preparation_attempt_source()
            if not isinstance(attempt_id, str) or not attempt_id.startswith("prep-"):
                raise PublicContractError
            attempt = await authority.async_acquire(
                attempt_id=attempt_id,
                generation=generation,
                purpose=PreparationPurpose(purpose_name),
                expectation_hash=expectation_hash,
                expected_category=expected_category,
            )
        except PublicContractError:
            raise
        except Exception:
            raise PublicContractError from None
        try:
            # The sole mutation syntax. Do not add retry/fallback/refresh here.
            result = await invoke()
        except BaseException as err:
            status = self._deterministic_provider_rejection(err)
            if status is None:
                await authority.async_record_outcome(
                    attempt_id=attempt.attempt_id,
                    expected_revision=attempt.record_revision,
                    outcome_category="ambiguous",
                )
            else:
                await authority.async_record_outcome(
                    attempt_id=attempt.attempt_id,
                    expected_revision=attempt.record_revision,
                    outcome_category="provider_failure",
                    provider_evidence_hash=self._provider_rejection_evidence(
                        purpose_name=purpose_name, error=err, status=status
                    ),
                )
                raise PreparationMutationRejected(status) from None
            if isinstance(err, asyncio.CancelledError):
                raise
            raise PublicContractError from None
        evidence = hashlib.sha256(repr(result).encode()).hexdigest()
        await authority.async_record_outcome(
            attempt_id=attempt.attempt_id,
            expected_revision=attempt.record_revision,
            outcome_category="provider_success",
            provider_evidence_hash=evidence,
        )
        return result

    def async_consume_final_quote(
        self, *, owner: str, generation: int, challenge: str
    ) -> AuthoritativeQuote:
        """Consume an exact current confirmation for an injected final seam only."""
        owner = self._owner(owner)
        self._state(generation)
        state = self._quote.get(owner)
        if state is None or state.generation != generation or not isinstance(challenge, str):
            raise PublicContractError
        try:
            quote = self._confirmations.consume(
                owner_key=owner, challenge=challenge, current=state.quote
            )
        except (InvalidQuoteConfirmation, ValueError):
            raise PublicContractError from None
        self._quote.pop(owner, None)
        return quote

    async def async_dispatch(self, *, owner: str, operation: str, request: Mapping[str, Any]) -> dict[str, Any]:
        owner = self._owner(owner)
        operation, request, generation = validate_public_request(operation, request)
        if operation == "state":
            # The HA surface owns authoritative generation bootstrap; a direct
            # facade caller gets no owner-bound basket projection before it has
            # one, and cannot smuggle a generation into this request.
            return {
                "operations": list(PUBLIC_OPERATIONS),
                "broadStoreDiscovery": "unsupported",
                "basket": None,
            }
        assert generation is not None
        try:
            if operation.startswith("library/"):
                library = self._package_library
                if library is None or not library.loaded:
                    raise PublicContractError
                if operation == "library/list":
                    return await library.async_list()
                if operation == "library/package_save":
                    try:
                        customer = await self._account.async_customer()
                        current_account_digest = account_digest(customer)
                    except Exception as err:
                        raise PackageSaveStageError("account") from err
                    package_ref = request["packageRef"]
                    aliases = request["aliases"]
                    if (
                        not isinstance(package_ref, str)
                        or len(package_ref) > 64
                        or not isinstance(request["name"], str)
                        or not isinstance(aliases, list)
                    ):
                        raise PackageSaveStageError("request")
                    try:
                        store_handle = _handle(request["storeHandle"])
                        choices = parse_selected_products(request["products"])
                        store, captured = self._selections.capture_package_selection(
                            owner=owner,
                            generation=generation,
                            store_handle=store_handle,
                            selections=choices,
                        )
                        items = recipe_items_from_capture(captured)
                    except Exception as err:
                        raise PackageSaveStageError("selection") from err
                    try:
                        record = await library.async_save_package(
                            expected_store_revision=_revision(request["expectedStoreRevision"]),
                            package_ref=package_ref,
                            expected_revision=_revision(request["expectedRevision"]),
                            name=request["name"],
                            aliases=aliases,
                            store=store,
                            items=items,
                            current_account_digest=current_account_digest,
                        )
                    except PackageLibraryUnavailable as err:
                        raise PackageSaveStageError("storage") from err
                    except PackageLibraryError as err:
                        raise PackageSaveStageError("validation") from err
                    return {
                        "storeRevision": library.revision,
                        "package": record.public_dict(),
                    }
                if operation == "library/package_delete":
                    package_ref = request["packageRef"]
                    if not isinstance(package_ref, str) or len(package_ref) > 64:
                        raise PublicContractError
                    await library.async_delete_package(
                        expected_store_revision=_revision(request["expectedStoreRevision"]),
                        package_ref=package_ref,
                        expected_revision=_revision(request["expectedRevision"]),
                    )
                    return {"deleted": True, "storeRevision": library.revision}
                if operation == "library/package_prepare":
                    customer = await self._account.async_customer()
                    current_account_digest = account_digest(customer)
                    package_key = request["packageKey"]
                    if not isinstance(package_key, str):
                        raise PublicContractError
                    address_handle = _handle(request["addressHandle"])
                    package, store_revision = await library.async_prepare_records(
                        package_key=package_key
                    )
                    try:
                        await library.async_assert_account(current_account_digest)
                    except PackageStale:
                        return {
                            "status": "stale",
                            "packageRef": package.package_ref,
                            "packageRevision": package.revision,
                            "reason": "account_changed",
                        }
                    fresh_address = self._account.resolve_address(
                        address_handle,
                        owner_key=owner,
                        generation=generation,
                    )
                    try:
                        store = await self._catalog.async_store(
                            package.store_slug, fresh_address
                        )
                    except ApiSessionError as err:
                        if err.category == "http" and err.status == 404:
                            return {
                                "status": "stale",
                                "packageRef": package.package_ref,
                                "packageRevision": package.revision,
                                "reason": "store_missing_or_changed",
                            }
                        raise
                    if getattr(store, "is_open", False) is not True:
                        return {
                            "status": "blocked",
                            "packageRef": package.package_ref,
                            "packageRevision": package.revision,
                            "reason": "store_closed",
                        }
                    try:
                        if package.store_digest != store_digest(store):
                            raise PackageStale("store_missing_or_changed")
                        menu = await self._catalog.async_menu(store, fresh_address)
                        reconciled = reconcile_recipe(package, store, menu.products)
                    except PackageStale as err:
                        return {
                            "status": "stale",
                            "packageRef": package.package_ref,
                            "packageRevision": package.revision,
                            "reason": err.reason,
                        }
                    address_public = {
                        "key": address_handle,
                        "label": "Saved destination ••••",
                        "fullAddress": fresh_address.address_line,
                    }
                    store_public, menu_public, selection = (
                        self._selections.issue_reconciled_selection(
                            store=store,
                            address_handle=address_public["key"],
                            menu=menu,
                            captured=reconciled,
                            owner=owner,
                            generation=generation,
                        )
                    )
                    await library.async_assert_current(
                        store_revision=store_revision,
                        package_ref=package.package_ref,
                        package_revision=package.revision,
                    )
                    package_public = package.public_dict()
                    package_public["storeLabel"] = store.name
                    package_public["items"] = [
                        {
                            "label": product.name,
                            "quantity": quantity,
                            "options": [
                                option.label
                                for _group, options in groups
                                for option in options
                            ],
                        }
                        for product, quantity, groups in reconciled
                    ]
                    return {
                        "status": "ready",
                        "package": package_public,
                        "address": address_public,
                        "store": store_public,
                        "menu": menu_public,
                        "selection": selection,
                        "selectionComplete": True,
                        "issues": [],
                    }
                raise PublicContractError
            if operation == "live/addresses":
                self.invalidate_basket_mutation_authority()
                await self._async_record_basket_unknown(generation)
                return {"addresses": [item.public_dict() for item in await self._account.async_saved_addresses(owner_key=owner, generation=generation)]}
            if operation == "live/stores":
                self.invalidate_basket_mutation_authority()
                await self._async_record_basket_unknown(generation)
                slug = request["storeSlug"]
                address_handle = _handle(request["addressHandle"])
                if not isinstance(slug, str):
                    raise PublicContractError
                delivery_address = self._account.resolve_address(
                    address_handle, owner_key=owner, generation=generation
                )
                # No address-wide discovery endpoint has been proven; only explicit lookup exists.
                store = await self._catalog.async_store(slug, delivery_address)
                return {
                    "stores": [
                        self._selections.issue_store(
                            store,
                            owner=owner,
                            generation=generation,
                            address_handle=address_handle,
                        )
                    ],
                    "broadStoreDiscovery": "unsupported",
                }
            if operation == "live/store_menu":
                self.invalidate_basket_mutation_authority()
                await self._async_record_basket_unknown(generation)
                handle = _handle(request["storeHandle"])
                address_handle = _handle(request["addressHandle"])
                delivery_address = self._account.resolve_address(
                    address_handle, owner_key=owner, generation=generation
                )
                store = self._selections.resolve_store(
                    handle,
                    owner=owner,
                    generation=generation,
                    address_handle=address_handle,
                )
                menu = await self._catalog.async_menu(store, delivery_address)
                return self._selections.issue_menu(handle, menu, owner=owner, generation=generation)
            if operation == "live/basket":
                authority = self._basket_authority
                if authority.mode is _BasketAuthorityMode.UNKNOWN:
                    return {"status": "unknown"}
                authority = self._active_basket_authority(generation)
                if authority.mode is _BasketAuthorityMode.CONFLICT:
                    return {"status": "conflict"}
                if authority.mode is _BasketAuthorityMode.ABSENT_VERIFIED:
                    result = self._empty_basket_public()
                    result["status"] = "absent_verified"
                    return result
                state = authority.state
                if state is None:
                    self._set_basket_unknown()
                    raise PublicContractError
                return self._basket_public(state)
            if operation == "live/basket_adopt":
                # Adoption is the only operation that can turn fresh/reloaded
                # UNKNOWN state into provider-backed authority. Invalidate first,
                # before any fallible local/provider read, so stale or malformed
                # input can never preserve an older quote or basket proof.
                self.invalidate_basket_mutation_authority()
                evidence = self._basket_evidence
                if evidence is not None and (
                    not evidence.loaded or evidence.integrity_fault
                ):
                    raise PublicContractError
                # This explicit read-only action is the only transient-fault
                # recovery path. UNKNOWN must persist before the provider GET.
                await self._async_record_basket_unknown(generation)
                discovery = self._discovery_client
                if discovery is None:
                    raise PublicContractError
                store_handle = _handle(request["storeHandle"])
                address_handle = _handle(request["addressHandle"])
                delivery_address = self._account.resolve_address(
                    address_handle, owner_key=owner, generation=generation
                )
                address_fingerprint = delivery_address.canonical_fingerprint
                delivery_location = DeliveryLocation(
                    delivery_address.country_code,
                    delivery_address.city_code,
                    delivery_address.latitude,
                    delivery_address.longitude,
                )
                store = self._selections.resolve_store(
                    store_handle,
                    owner=owner,
                    generation=generation,
                    address_handle=address_handle,
                )
                choices = parse_selected_products(request["products"])
                captured = self._selections.capture_selection(
                    owner=owner,
                    generation=generation,
                    store_handle=store_handle,
                    selections=choices,
                )
                product_handles = [item.product_handle for item in choices]
                lines = self._selections.safe_lines(captured, product_handles)
                customer: CustomerIdentity = await self._account.async_customer()
                intent = self._selections.compile_intent(
                    owner=owner,
                    generation=generation,
                    customer_id=customer.customer_id,
                    store_handle=store_handle,
                    selections=choices,
                )
                currency = self._selections.product_currency(
                    choices[0].product_handle,
                    owner=owner,
                    generation=generation,
                    store_handle=store_handle,
                )
                await self._async_require_store_open(store, delivery_address)
                try:
                    # One discovery call owns its bounded summary/full GET sequence.
                    # There is no retry, polling, fallback, or mutation here.
                    discovered = await discovery.async_discover(
                        intent, delivery_location
                    )
                    if not isinstance(discovered, RemoteBasketDiscoveryResult):
                        raise PublicContractError

                    # The provider read may outlive local handle leases. Re-resolve
                    # every caller capability and recompile the exact intent before
                    # installing its result; stale in-flight results remain UNKNOWN.
                    current_address = self._account.resolve_address(
                        address_handle, owner_key=owner, generation=generation
                    )
                    current_location = DeliveryLocation(
                        current_address.country_code,
                        current_address.city_code,
                        current_address.latitude,
                        current_address.longitude,
                    )
                    current_store = self._selections.resolve_store(
                        store_handle,
                        owner=owner,
                        generation=generation,
                        address_handle=address_handle,
                    )
                    current_captured = self._selections.capture_selection(
                        owner=owner,
                        generation=generation,
                        store_handle=store_handle,
                        selections=choices,
                    )
                    current_intent = self._selections.compile_intent(
                        owner=owner,
                        generation=generation,
                        customer_id=customer.customer_id,
                        store_handle=store_handle,
                        selections=choices,
                    )
                    if (
                        current_address.canonical_fingerprint != address_fingerprint
                        or current_location != delivery_location
                        or store_digest(current_store) != store_digest(store)
                        or current_intent != intent
                    ):
                        raise PublicContractError
                    current_lines = self._selections.safe_lines(
                        current_captured, product_handles
                    )

                    if (
                        discovered.status
                        is RemoteBasketDiscoveryStatus.ABSENT_VERIFIED
                    ):
                        await self._async_publish_basket_absent(
                            generation=generation,
                            customer=customer,
                            store=current_store,
                            intent=intent,
                            store_handle=store_handle,
                            address_handle=address_handle,
                            products=request["products"],
                        )
                        result = self._empty_basket_public()
                        result["status"] = "absent_verified"
                        return result
                    if discovered.status is RemoteBasketDiscoveryStatus.ADOPTED:
                        snapshot = discovered.snapshot
                        if not isinstance(snapshot, RemoteBasketSnapshot):
                            raise PublicContractError
                        state = _BasketState(
                            generation=generation,
                            revision=1,
                            store_handle=store_handle,
                            address_handle=address_handle,
                            currency=currency,
                            store_label=current_store.name,
                            lines=tuple(current_lines),
                            snapshot=snapshot,
                            store=current_store,
                            address_fingerprint=address_fingerprint,
                            delivery_location=delivery_location,
                        )
                        await self._async_publish_basket_present(
                            generation=generation,
                            customer=customer,
                            store=current_store,
                            intent=intent,
                            state=state,
                            products=request["products"],
                        )
                        return self._basket_public(state)
                    if discovered.status is RemoteBasketDiscoveryStatus.CONFLICT:
                        await self._async_publish_basket_conflict(
                            generation=generation,
                            customer=customer,
                            store=current_store,
                            intent=intent,
                        )
                        return {"status": "conflict"}
                    raise PublicContractError
                except BaseException:
                    self._set_basket_unknown()
                    raise
            if operation == "live/basket_set":
                with _basket_sync_stage("request"):
                    expected = _revision(request["expectedRevision"])
                authority = self._active_basket_authority(generation)
                if authority.mode not in {
                    _BasketAuthorityMode.ABSENT_VERIFIED,
                    _BasketAuthorityMode.ADOPTED,
                }:
                    raise PublicContractError
                existing = authority.state
                if authority.mode is _BasketAuthorityMode.ABSENT_VERIFIED:
                    if existing is not None or expected != 0:
                        raise PublicContractError
                elif existing is None or existing.revision != expected:
                    raise PublicContractError
                with _basket_sync_stage("address"):
                    store_handle = _handle(request["storeHandle"])
                    address_handle = _handle(request["addressHandle"])
                    delivery_address = self._account.resolve_address(
                        address_handle, owner_key=owner, generation=generation
                    )
                address_fingerprint = delivery_address.canonical_fingerprint
                delivery_location = DeliveryLocation(
                    delivery_address.country_code,
                    delivery_address.city_code,
                    delivery_address.latitude,
                    delivery_address.longitude,
                )
                with _basket_sync_stage("store"):
                    store = self._selections.resolve_store(
                        store_handle,
                        owner=owner,
                        generation=generation,
                        address_handle=address_handle,
                    )
                if existing is not None and (
                    store_digest(existing.store) != store_digest(store)
                    or existing.address_fingerprint != address_fingerprint
                    or existing.delivery_location != delivery_location
                ):
                    self._set_basket_unknown()
                    await self._async_record_basket_unknown(generation)
                    raise PublicContractError
                with _basket_sync_stage("customer"):
                    customer: CustomerIdentity = await self._account.async_customer()
                with _basket_sync_stage("selection"):
                    choices = parse_selected_products(request["products"])
                    captured = self._selections.capture_selection(
                        owner=owner,
                        generation=generation,
                        store_handle=store_handle,
                        selections=choices,
                    )
                    lines = self._selections.safe_lines(
                        captured, [item.product_handle for item in choices]
                    )
                    intent = self._selections.compile_intent(
                        owner=owner,
                        generation=generation,
                        customer_id=customer.customer_id,
                        store_handle=store_handle,
                        selections=choices,
                    )
                    currency = self._selections.product_currency(
                        choices[0].product_handle,
                        owner=owner,
                        generation=generation,
                        store_handle=store_handle,
                    )
                bound = self._basket_binding(
                    generation=generation,
                    customer=customer,
                    intent=intent,
                    store_handle=store_handle,
                    address_handle=address_handle,
                    products=request["products"],
                )
                if bound[1] != authority.account_digest:
                    self._set_basket_unknown()
                    await self._async_record_basket_unknown(generation)
                    raise PublicContractError
                if authority.mode is _BasketAuthorityMode.ABSENT_VERIFIED and (
                    bound[2] != authority.intent_fingerprint
                    or bound[3] != authority.selection_fingerprint
                    or bound[4] != authority.store_handle
                    or bound[5] != authority.address_handle
                ):
                    raise PublicContractError
                if (
                    authority.mode is _BasketAuthorityMode.ADOPTED
                    and bound[2] == authority.intent_fingerprint
                ):
                    assert existing is not None
                    rebound = _BasketState(
                        generation=generation,
                        revision=existing.revision,
                        store_handle=store_handle,
                        address_handle=address_handle,
                        currency=currency,
                        store_label=store.name,
                        lines=tuple(lines),
                        snapshot=existing.snapshot,
                        store=store,
                        address_fingerprint=address_fingerprint,
                        delivery_location=delivery_location,
                    )
                    self._basket_authority = _BasketAuthority(
                        _BasketAuthorityMode.ADOPTED,
                        generation=generation,
                        account_digest=bound[1],
                        intent_fingerprint=bound[2],
                        selection_fingerprint=bound[3],
                        store_handle=bound[4],
                        address_handle=bound[5],
                        # A local no-op must never extend provider knowledge.
                        expires_at=authority.expires_at,
                        state=rebound,
                    )
                    return self._basket_public(rebound)
                with _basket_sync_stage("fresh_store"):
                    await self._async_require_store_open(store, delivery_address)
                try:
                    with _basket_sync_stage("provider_mutation"):
                        snapshot = await self._async_preparation_mutation(
                            operation="live/basket_set",
                            generation=generation,
                            purpose_name=(
                                "basket_create" if existing is None else "basket_replace"
                            ),
                            expected_category="expected_present",
                            expected=intent,
                            invoke=(
                                (
                                    lambda: self._baskets.async_create(
                                        intent, delivery_location
                                    )
                                )
                                if existing is None
                                else (
                                    lambda: self._baskets.async_replace(
                                        existing.snapshot,
                                        intent.products,
                                        delivery_location,
                                    )
                                )
                            ),
                        )
                except PreparationMutationRejected:
                    # A closed replace rejection proves the old snapshot survived.
                    self._basket_authority = (
                        authority if existing is not None else _BasketAuthority(_BasketAuthorityMode.UNKNOWN)
                    )
                    raise
                except BaseException:
                    # Cancellation, transport/schema ambiguity, and outcome-record
                    # failure can all mean the provider changed remotely.
                    await self._async_finalize_basket_unknown_after_dispatch(
                        generation
                    )
                    raise
                state = _BasketState(
                    generation=generation,
                    revision=expected + 1,
                    store_handle=store_handle,
                    address_handle=address_handle,
                    currency=currency,
                    store_label=store.name,
                    lines=tuple(lines),
                    snapshot=snapshot,
                    store=store,
                    address_fingerprint=address_fingerprint,
                    delivery_location=delivery_location,
                )
                try:
                    await self._async_publish_basket_present(
                        generation=generation,
                        customer=customer,
                        store=store,
                        intent=intent,
                        state=state,
                        products=request["products"],
                    )
                except BaseException as err:
                    self._set_basket_unknown()
                    if isinstance(err, asyncio.CancelledError):
                        await self._async_finalize_basket_unknown_after_dispatch(
                            generation, retain_transient_fault=True
                        )
                    raise
                self._quote.clear()
                self._confirmations.invalidate_all()
                return self._basket_public(state)
            if operation == "live/basket_clear":
                authority = self._active_basket_authority(generation)
                state = self._state(generation)
                if state.revision != _revision(request["expectedRevision"]):
                    raise PublicContractError
                customer = await self._account.async_customer()
                try:
                    current_account_digest = account_digest(customer)
                except Exception:
                    raise PublicContractError from None
                if current_account_digest != authority.account_digest:
                    self._set_basket_unknown()
                    await self._async_record_basket_unknown(generation)
                    raise PublicContractError
                address = self._account.resolve_address(
                    state.address_handle, owner_key=owner, generation=generation
                )
                if (
                    address.canonical_fingerprint != state.address_fingerprint
                    or DeliveryLocation(
                        address.country_code,
                        address.city_code,
                        address.latitude,
                        address.longitude,
                    )
                    != state.delivery_location
                ):
                    self._set_basket_unknown()
                    await self._async_record_basket_unknown(generation)
                    raise PublicContractError
                try:
                    await self._async_preparation_mutation(
                        operation="live/basket_clear",
                        generation=generation,
                        purpose_name="basket_delete",
                        expected_category="expected_absent",
                        expected=state.snapshot,
                        invoke=lambda: self._baskets.async_delete(
                            state.snapshot,
                            explicit_user_intent=True,
                            delivery_location=state.delivery_location,
                        ),
                    )
                except PreparationMutationRejected:
                    self._basket_authority = authority
                    raise
                except BaseException:
                    await self._async_finalize_basket_unknown_after_dispatch(
                        generation
                    )
                    raise
                # A successful delete is not a fresh selection-bound absence
                # discovery. Require the explicit GET integration hook before a
                # later create can become enabled.
                self._set_basket_unknown()
                try:
                    await self._async_record_basket_unknown(generation)
                except asyncio.CancelledError:
                    await self._async_finalize_basket_unknown_after_dispatch(
                        generation, retain_transient_fault=True
                    )
                    raise
                self._quote.clear()
                self._confirmations.invalidate_all()
                return {"cleared": True, "revision": state.revision + 1}
            if operation == "live/basket_reconcile":
                # No public ambiguity token or provider state is exposed.
                return {"status": "unsupported"}
            if operation == "live/payment_methods":
                state = self._state(generation)
                address = self._account.resolve_address(
                    state.address_handle, owner_key=owner, generation=generation
                )
                if (
                    address.canonical_fingerprint != state.address_fingerprint
                    or DeliveryLocation(
                        address.country_code,
                        address.city_code,
                        address.latitude,
                        address.longitude,
                    )
                    != state.delivery_location
                    or not await self._account.async_revalidate_address(
                        state.address_handle,
                        owner_key=owner,
                        generation=generation,
                    )
                ):
                    self._set_basket_unknown()
                    await self._async_record_basket_unknown(generation)
                    raise PublicContractError
                await self._async_require_store_open(state.store, address)
                methods = await self._account.async_saved_payments(
                    owner_key=owner,
                    generation=generation,
                    store_address_id=state.snapshot.store_address_id,
                )
                result: dict[str, Any] = {
                    "paymentMethods": [item.public_dict() for item in methods]
                }
                diagnostics = getattr(self._account, "last_payment_diagnostics", None)
                if diagnostics is not None:
                    result["diagnostics"] = diagnostics
                return result
            if operation == "live/create_quote":
                state = self._state(generation)
                address_handle = _handle(request["addressHandle"])
                payment_handle = _handle(request["paymentHandle"])
                address = self._account.resolve_address(address_handle, owner_key=owner, generation=generation)
                if (
                    address.canonical_fingerprint != state.address_fingerprint
                    or DeliveryLocation(
                        address.country_code,
                        address.city_code,
                        address.latitude,
                        address.longitude,
                    )
                    != state.delivery_location
                ):
                    self._set_basket_unknown()
                    await self._async_record_basket_unknown(generation)
                    raise PublicContractError
                payment = self._account.resolve_payment(
                    payment_handle, owner_key=owner, generation=generation
                )
                if payment.selected is not True:
                    raise PublicContractError
                store = state.store
                await self._async_require_store_open(store, address)
                quote_request = QuoteRequest(
                    owner_key=owner,
                    generation=generation,
                    intent_key=f"intent-{state.revision}",
                    source_screen="CART",
                    basket=state.snapshot,
                    delivery_address=address,
                    payment=payment,
                    masked_address="Saved destination ••••",
                    masked_payment=f"Saved card •••• {payment.last_four_digits or ''}".strip(),
                    store_display_name=store.name,
                    full_address=address.address_line,
                    item_display=tuple(
                        {
                            "name": line["label"],
                            "quantity": line["quantity"],
                            "options": [
                                label
                                for group in line["options"]
                                for label in group["optionLabels"]
                            ],
                        }
                        for line in state.lines
                    ),
                )
                # A replacement attempt removes previous authority before dispatch.
                self._confirmations.invalidate(owner)
                self._quote.pop(owner, None)
                quote = await self._async_preparation_mutation(
                    operation="live/create_quote",
                    generation=generation,
                    purpose_name="quote_template_create",
                    expected_category="expected_template",
                    expected=quote_request,
                    invoke=lambda: self._quotes.async_create(quote_request),
                )
                if not await self._account.async_revalidate_address(address_handle, owner_key=owner, generation=generation):
                    raise PublicContractError
                if not await self._account.async_revalidate_payment(payment_handle, owner_key=owner, generation=generation, amount_minor=quote.total.amount_minor, currency=quote.total.currency, checkout_session=quote.checkout_session_id, store_address_id=quote.store_address_id):
                    raise PublicContractError
                public = self._confirmations.install(quote)
                self._quote[owner] = _QuoteState(generation, address_handle, payment_handle, quote)
                return public
            if operation == "live/prepare_confirmation":
                state = self._quote.get(owner)
                if state is None or state.generation != generation:
                    raise PublicContractError
                basket_state = self._state(generation)
                address = self._account.resolve_address(
                    basket_state.address_handle,
                    owner_key=owner,
                    generation=generation,
                )
                try:
                    await self._async_require_store_open(basket_state.store, address)
                except PublicContractError:
                    self._quote.pop(owner, None)
                    self._confirmations.invalidate(owner)
                    raise
                return self._confirmations.prepare(owner_key=owner)
            if operation == "live/execute_checkout":
                if request["acknowledged"] is not True or not isinstance(request["challenge"], str):
                    raise PublicContractError
                state = self._quote.get(owner)
                if state is None or state.generation != generation:
                    raise PublicContractError
                basket_state = self._state(generation)
                address = self._account.resolve_address(
                    basket_state.address_handle,
                    owner_key=owner,
                    generation=generation,
                )
                try:
                    await self._async_require_store_open(basket_state.store, address)
                except PublicContractError:
                    self._quote.pop(owner, None)
                    self._confirmations.invalidate(owner)
                    raise
                self._confirmations.consume(owner_key=owner, challenge=request["challenge"], current=state.quote)
                self._quote.pop(owner, None)
                return {"status": "not_dispatched"}
            if operation == "live/checkout_status":
                # There is no order identifier or provider polling endpoint in this release.
                return {"status": "unsupported"}
        except PackageSaveStageError:
            raise
        except PreparationMutationRejected:
            raise
        except RemoteBasketDiscoveryError as err:
            _LOGGER.warning(
                "Glovo basket discovery rejected stage=%s reason=%s path=%s",
                err.stage,
                err.reason,
                err.path,
            )
            raise PublicContractError from None
        except ApiSessionError:
            # This exception contains only allowlisted category/family/status fields.
            # Preserve it so the HA boundary can log an operationally useful,
            # privacy-safe failure rather than collapsing every read to unavailable.
            raise
        except (LiveSelectionError, InvalidSelection, InvalidQuoteConfirmation, PackageLibraryError, ValueError):
            raise PublicContractError from None
        except Exception:
            # Provider/session/transport errors are never a public diagnostic channel.
            raise PublicContractError from None
        raise PublicContractError
