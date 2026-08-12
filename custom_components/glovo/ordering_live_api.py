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
import secrets
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Final

from .api_session import ApiSessionError
from .ordering_account import AccountClient, InvalidSelection
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
    PackageStale,
    account_digest,
    recipe_items_from_capture,
    reconcile_recipe,
    store_digest,
)
from .ordering_remote_basket import RemoteBasketClient, RemoteBasketSnapshot

_LOGGER = logging.getLogger(__name__)


@contextmanager
def _basket_sync_stage(name: str):
    """Log only a closed stage and redaction-safe exception classification."""
    try:
        yield
    except Exception as err:
        status = getattr(err, "status", None)
        if status not in {400, 401, 403, 404, 405, 406, 409, 410, 415, 422, 429}:
            status = None
        _LOGGER.warning(
            "Glovo basket sync rejected at stage=%s class=%s status=%s",
            name,
            type(err).__name__,
            status,
        )
        raise

PUBLIC_OPERATIONS: Final = (
    "state", "live/addresses", "live/stores", "live/store_menu", "live/payment_methods",
    "live/basket", "live/basket_set", "live/basket_clear", "live/basket_reconcile",
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


@dataclass(slots=True, repr=False)
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
        self._basket: dict[str, _BasketState] = {}
        self._quote: dict[str, _QuoteState] = {}

    def invalidate_all(self) -> None:
        """Discard every ephemeral handle/quote/basket on a lifecycle change."""
        self.invalidate_basket_mutation_authority()
        self._account.invalidate()

    def invalidate_preparation_authority(self) -> None:
        """Backward-compatible alias for a basket-changing provider write."""
        self.invalidate_basket_mutation_authority()

    def invalidate_basket_mutation_authority(self) -> None:
        """Discard state that a basket-changing provider write can make stale.

        Saved address/payment bindings are deliberately retained until the quote
        response has been revalidated against the provider; clearing them here
        would turn that mandatory post-template verification into an impossible
        local lookup. Lifecycle invalidation uses :meth:`invalidate_all`.
        """
        self._basket.clear()
        self._quote.clear()
        self._selections.invalidate()
        self._confirmations.invalidate_all()

    def invalidate_quote_mutation_authority(self) -> None:
        """Invalidate quote/catalog authority while retaining the remote basket.

        Creating a checkout template does not mutate the basket. Keeping its exact
        snapshot is required so the administrator can still inspect or explicitly
        delete that remote basket after quote preparation.
        """
        self._quote.clear()
        self._selections.invalidate()
        self._confirmations.invalidate_all()

    def _owner(self, owner: object) -> str:
        return _handle(owner)

    def _state(self, owner: str, generation: int) -> _BasketState:
        state = self._basket.get(owner)
        if state is None or state.generation != generation:
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
            "revision": state.revision,
            "storeHandle": state.store_handle,
            "storeLabel": state.store_label,
            "itemCount": sum(item.quantity for item in state.snapshot.products),
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
        except Exception as err:
            ambiguous = "Ambiguous" in type(err).__name__ or isinstance(err, asyncio.CancelledError)
            try:
                if ambiguous:
                    await authority.async_record_outcome(
                        attempt_id=attempt.attempt_id,
                        expected_revision=attempt.record_revision,
                        outcome_category="ambiguous",
                    )
                else:
                    evidence = hashlib.sha256(
                        f"{purpose_name}:{type(err).__name__}".encode()
                    ).hexdigest()
                    await authority.async_record_outcome(
                        attempt_id=attempt.attempt_id,
                        expected_revision=attempt.record_revision,
                        outcome_category="provider_failure",
                        provider_evidence_hash=evidence,
                    )
            except Exception:
                # Authority failure is fail closed; it must never make another call.
                pass
            raise PublicContractError from None
        try:
            evidence = hashlib.sha256(repr(result).encode()).hexdigest()
            await authority.async_record_outcome(
                attempt_id=attempt.attempt_id,
                expected_revision=attempt.record_revision,
                outcome_category="provider_success",
                provider_evidence_hash=evidence,
            )
        except Exception:
            raise PublicContractError from None
        return result

    def async_consume_final_quote(
        self, *, owner: str, generation: int, challenge: str
    ) -> AuthoritativeQuote:
        """Consume an exact current confirmation for an injected final seam only."""
        owner = self._owner(owner)
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
                    customer = await self._account.async_customer()
                    current_account_digest = account_digest(customer)
                    package_ref = request["packageRef"]
                    aliases = request["aliases"]
                    if (
                        not isinstance(package_ref, str)
                        or len(package_ref) > 64
                        or not isinstance(request["name"], str)
                        or not isinstance(aliases, list)
                    ):
                        raise PublicContractError
                    store_handle = _handle(request["storeHandle"])
                    parent_address_handle = self._selections.store_address_handle(
                        store_handle, owner=owner, generation=generation
                    )
                    store = self._selections.resolve_store(
                        store_handle,
                        owner=owner,
                        generation=generation,
                        address_handle=parent_address_handle,
                    )
                    choices = parse_selected_products(request["products"])
                    captured = self._selections.capture_selection(
                        owner=owner,
                        generation=generation,
                        store_handle=store_handle,
                        selections=choices,
                    )
                    record = await library.async_save_package(
                        expected_store_revision=_revision(request["expectedStoreRevision"]),
                        package_ref=package_ref,
                        expected_revision=_revision(request["expectedRevision"]),
                        name=request["name"],
                        aliases=aliases,
                        store=store,
                        items=recipe_items_from_capture(captured),
                        current_account_digest=current_account_digest,
                    )
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
                return {"addresses": [item.public_dict() for item in await self._account.async_saved_addresses(owner_key=owner, generation=generation)]}
            if operation == "live/stores":
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
                state = self._basket.get(owner)
                if state is None:
                    return self._empty_basket_public()
                if state.generation != generation:
                    raise PublicContractError
                return self._basket_public(state)
            if operation == "live/basket_set":
                with _basket_sync_stage("request"):
                    expected = _revision(request["expectedRevision"])
                existing = self._basket.get(owner)
                if existing is not None and (
                    existing.generation != generation or existing.revision != expected
                ):
                    raise PublicContractError
                with _basket_sync_stage("address"):
                    store_handle = _handle(request["storeHandle"])
                    address_handle = _handle(request["addressHandle"])
                    delivery_address = self._account.resolve_address(
                        address_handle, owner_key=owner, generation=generation
                    )
                address_fingerprint = delivery_address.canonical_fingerprint
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
                ):
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
                with _basket_sync_stage("fresh_store"):
                    await self._async_require_store_open(store, delivery_address)
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
                            (lambda: self._baskets.async_create(intent))
                            if existing is None
                            else (
                                lambda: self._baskets.async_replace(
                                    existing.snapshot, intent.products
                                )
                            )
                        ),
                    )
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
                )
                self._basket[owner] = state
                self._quote.pop(owner, None)
                self._confirmations.invalidate(owner)
                return self._basket_public(state)
            if operation == "live/basket_clear":
                state = self._state(owner, generation)
                if state.revision != _revision(request["expectedRevision"]):
                    raise PublicContractError
                await self._async_preparation_mutation(
                    operation="live/basket_clear",
                    generation=generation,
                    purpose_name="basket_delete",
                    expected_category="expected_absent",
                    expected=state.snapshot,
                    invoke=lambda: self._baskets.async_delete(
                        state.snapshot, explicit_user_intent=True
                    ),
                )
                self._basket.pop(owner, None)
                self._quote.pop(owner, None)
                self._confirmations.invalidate(owner)
                return {"cleared": True, "revision": state.revision + 1}
            if operation == "live/basket_reconcile":
                # No public ambiguity token or provider state is exposed.
                return {"status": "unsupported"}
            if operation == "live/payment_methods":
                state = self._state(owner, generation)
                address = self._account.resolve_address(
                    state.address_handle, owner_key=owner, generation=generation
                )
                if address.canonical_fingerprint != state.address_fingerprint:
                    raise PublicContractError
                await self._async_require_store_open(state.store, address)
                minor = state.snapshot.basket_price.minor
                if minor is None or not state.currency:
                    raise PublicContractError
                methods = await self._account.async_saved_payments(owner_key=owner, generation=generation, amount_minor=minor, currency=state.currency, store_address_id=state.snapshot.store_address_id, client_supports=("CREDIT_CARD",), client_ready=True)
                return {"paymentMethods": [item.public_dict() for item in methods]}
            if operation == "live/create_quote":
                state = self._state(owner, generation)
                address_handle = _handle(request["addressHandle"])
                payment_handle = _handle(request["paymentHandle"])
                address = self._account.resolve_address(address_handle, owner_key=owner, generation=generation)
                if address.canonical_fingerprint != state.address_fingerprint:
                    raise PublicContractError
                payment = self._account.resolve_payment(payment_handle, owner_key=owner, generation=generation)
                if payment.selected is not True:
                    raise PublicContractError
                store = state.store
                await self._async_require_store_open(store, address)
                quote_request = QuoteRequest(owner_key=owner, generation=generation, intent_key=f"intent-{state.revision}", source_screen="BASKET", basket=state.snapshot, delivery_address=address, payment=payment, masked_address="Saved destination ••••", masked_payment=f"Saved card •••• {payment.last_four_digits or ''}".strip(), store_display_name=store.name)
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
                basket_state = self._state(owner, generation)
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
                basket_state = self._state(owner, generation)
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
