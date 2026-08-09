"""Frozen, privacy-safe public contracts for the live ordering capability.

This is deliberately a facade rather than an HA service registration.  It accepts
only local handles, rejects unknown fields, and never returns provider IDs,
paths, request bodies, fingerprints, or raw errors.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

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
from .ordering_remote_basket import RemoteBasketClient, RemoteBasketSnapshot

PUBLIC_OPERATIONS: Final = (
    "state", "live/addresses", "live/stores", "live/store_menu", "live/payment_methods",
    "live/basket", "live/basket_set", "live/basket_clear", "live/basket_reconcile",
    "live/create_quote", "live/prepare_confirmation", "live/execute_checkout", "live/checkout_status",
)
# These are schemas, not permissive examples: unknown keys are rejected exactly.
OPERATION_REQUEST_FIELDS: Final = {
    # State is a bootstrap response that establishes the first generation.
    "state": frozenset(),
    "live/addresses": frozenset({"generation"}),
    "live/stores": frozenset({"generation", "storeSlug"}),
    "live/store_menu": frozenset({"generation", "storeHandle"}),
    "live/payment_methods": frozenset({"generation"}),
    "live/basket": frozenset({"generation"}),
    "live/basket_set": frozenset({"generation", "expectedRevision", "storeHandle", "products"}),
    "live/basket_clear": frozenset({"generation", "expectedRevision"}),
    "live/basket_reconcile": frozenset({"generation"}),
    "live/create_quote": frozenset({"generation", "addressHandle", "paymentHandle"}),
    "live/prepare_confirmation": frozenset({"generation"}),
    "live/execute_checkout": frozenset({"generation", "challenge", "acknowledged"}),
    "live/checkout_status": frozenset({"generation"}),
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
    currency: str
    snapshot: RemoteBasketSnapshot


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
        self._basket: dict[str, _BasketState] = {}
        self._quote: dict[str, _QuoteState] = {}

    def _owner(self, owner: object) -> str:
        return _handle(owner)

    def _state(self, owner: str, generation: int) -> _BasketState:
        state = self._basket.get(owner)
        if state is None or state.generation != generation:
            raise PublicContractError
        return state

    @staticmethod
    def _basket_public(state: _BasketState) -> dict[str, Any]:
        return {
            "revision": state.revision,
            "storeHandle": state.store_handle,
            "itemCount": sum(item.quantity for item in state.snapshot.products),
            "currency": state.currency,
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
            if operation == "live/addresses":
                return {"addresses": [item.public_dict() for item in await self._account.async_saved_addresses(owner_key=owner, generation=generation)]}
            if operation == "live/stores":
                slug = request["storeSlug"]
                if not isinstance(slug, str):
                    raise PublicContractError
                # No address-wide discovery endpoint has been proven; only explicit lookup exists.
                store = await self._catalog.async_store(slug)
                return {"stores": [self._selections.issue_store(store, owner=owner, generation=generation)], "broadStoreDiscovery": "unsupported"}
            if operation == "live/store_menu":
                handle = _handle(request["storeHandle"])
                store = self._selections.resolve_store(handle, owner=owner, generation=generation)
                menu = await self._catalog.async_menu(store)
                return self._selections.issue_menu(handle, menu, owner=owner, generation=generation)
            if operation == "live/basket":
                return self._basket_public(self._state(owner, generation))
            if operation == "live/basket_set":
                expected = _revision(request["expectedRevision"])
                existing = self._basket.get(owner)
                if existing is not None and (existing.generation != generation or existing.revision != expected):
                    raise PublicContractError
                store_handle = _handle(request["storeHandle"])
                store = self._selections.resolve_store(store_handle, owner=owner, generation=generation)
                customer: CustomerIdentity = await self._account.async_customer()
                choices = parse_selected_products(request["products"])
                intent = self._selections.compile_intent(owner=owner, generation=generation, customer_id=customer.customer_id, store_handle=store_handle, selections=choices)
                snapshot = await self._async_preparation_mutation(
                    operation="live/basket_set",
                    generation=generation,
                    purpose_name=("basket_create" if existing is None else "basket_replace"),
                    expected_category="expected_present",
                    expected=intent,
                    invoke=(
                        (lambda: self._baskets.async_create(intent))
                        if existing is None
                        else (lambda: self._baskets.async_replace(existing.snapshot, intent.products))
                    ),
                )
                state = _BasketState(generation, expected + 1, store_handle, self._selections.product_currency(choices[0].product_handle, owner=owner, generation=generation, store_handle=store_handle), snapshot)
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
                payment = self._account.resolve_payment(payment_handle, owner_key=owner, generation=generation)
                if payment.selected is not True:
                    raise PublicContractError
                store = self._selections.resolve_store(state.store_handle, owner=owner, generation=generation)
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
                return self._confirmations.prepare(owner_key=owner)
            if operation == "live/execute_checkout":
                if request["acknowledged"] is not True or not isinstance(request["challenge"], str):
                    raise PublicContractError
                state = self._quote.get(owner)
                if state is None or state.generation != generation:
                    raise PublicContractError
                self._confirmations.consume(owner_key=owner, challenge=request["challenge"], current=state.quote)
                self._quote.pop(owner, None)
                return {"status": "not_dispatched"}
            if operation == "live/checkout_status":
                # There is no order identifier or provider polling endpoint in this release.
                return {"status": "unsupported"}
        except (LiveSelectionError, InvalidSelection, InvalidQuoteConfirmation, ValueError):
            raise PublicContractError from None
        except Exception:
            # Provider/session/transport errors are never a public diagnostic channel.
            raise PublicContractError from None
        raise PublicContractError
