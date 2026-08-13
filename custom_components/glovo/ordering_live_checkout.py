"""Strict production final-checkout contract and single-attempt adapter.

The adapter owns no HTTP client, token refresh, retry, polling, completion,
cancellation, or payment continuation.  It accepts only an authoritative quote
whose exact submit projection was captured from the provider template.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Final, NoReturn, cast

from .api_session import ApiSessionError, MutationDispatchUncertain, MutationPurpose
from .ordering_live_quote import AuthoritativeQuote

FINAL_CHECKOUT_PATH: Final = "/v3/checkouts/order/1"
FINAL_STATUS_PATH_PREFIX: Final = "/v3/checkouts/order/"
_MAX_BYTES: Final = 512_000
_MAX_DEPTH: Final = 14
_MAX_ARRAY: Final = 200
_MAX_STRING: Final = 2_000
_ID_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_STATUSES: Final = frozenset({"AUTH_REQUIRED", "NO_AUTH_PENDING", "COMPLETED", "FAILED", "CANCELLED"})
_ACTIONS: Final = frozenset({"AUTH", "NO_AUTH", "NO_AUTH_POLL", "PROCESS_PAYMENT", None})
_POST_AUTH: Final = frozenset({"NONE", "COMPLETE_CHECKOUT", None})


class FinalCheckoutContractError(ValueError):
    def __init__(self, category: str = "schema") -> None:
        self.category = category if category in {"schema", "mismatch", "unsupported", "stale"} else "schema"
        super().__init__("final checkout did not satisfy the reviewed contract")


class FinalCheckoutAmbiguous(RuntimeError):
    def __init__(self, checkout_id: str | None = None, category: str = "unknown_ambiguous") -> None:
        self.checkout_id = checkout_id if isinstance(checkout_id, str) and _ID_RE.fullmatch(checkout_id) else None
        self.category = category if category in {
            "timeout", "connection_reset", "http_error", "malformed_response", "cancelled",
            "pending_or_interactive", "contradictory_response", "unknown_ambiguous",
        } else "unknown_ambiguous"
        super().__init__("final checkout requires manual reconciliation")


class FinalCheckoutRejected(RuntimeError):
    """Closed deterministic 4xx rejection with no provider text or identifiers."""

    category = "provider_rejection"

    def __init__(self, status: int) -> None:
        if status not in {400, 401, 403, 404, 405, 406, 409, 410, 415, 422, 429}:
            _fail()
        self.status = status
        self.evidence_hash = hashlib.sha256(
            f"final-provider-rejection:{status}".encode()
        ).hexdigest()
        super().__init__("final checkout was rejected")


class FinalCheckoutUnsupported(RuntimeError):
    pass


def _fail(category: str = "schema") -> NoReturn:
    raise FinalCheckoutContractError(category)


def _id(value: object) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        _fail()
    return value


def _integer(value: object, *, minimum: int = 0, maximum: int = 100_000_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        _fail()
    return value


def _bounded(value: object) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError):
        _fail()
    if len(encoded.encode()) > _MAX_BYTES:
        _fail()

    def walk(item: object, depth: int) -> None:
        if depth > _MAX_DEPTH:
            _fail()
        if isinstance(item, dict):
            if len(item) > 200:
                _fail()
            for key, child in item.items():
                if not isinstance(key, str) or not key or len(key) > 100:
                    _fail()
                walk(child, depth + 1)
        elif isinstance(item, list):
            if len(item) > _MAX_ARRAY:
                _fail()
            for child in item:
                walk(child, depth + 1)
        elif isinstance(item, str):
            if len(item) > _MAX_STRING or any(ord(char) < 32 and char not in "\t\n\r" for char in item):
                _fail()
        elif isinstance(item, float):
            if not math.isfinite(item):
                _fail()
        elif item is not None and not isinstance(item, (bool, int)):
            _fail()
    walk(value, 0)


def _canonical(value: object) -> bytes:
    _bounded(value)
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    except (TypeError, ValueError, OverflowError):
        _fail()


@dataclass(frozen=True, slots=True, repr=False)
class FinalCheckoutRequest:
    quote: AuthoritativeQuote = field(repr=False)
    quote_fingerprint: str = field(repr=False)
    body_bytes: bytes = field(repr=False)

    @classmethod
    def from_quote(cls, quote: object, *, now: object) -> "FinalCheckoutRequest":
        if not isinstance(quote, AuthoritativeQuote):
            _fail()
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(float(now)):
            _fail()
        try:
            if not quote.is_fresh(float(now)):
                _fail("stale")
            projection = quote.exact_submit_projection()
            body = {"checkout": projection}
            fingerprint = quote.fingerprint
        except FinalCheckoutContractError:
            raise
        except Exception:
            _fail("mismatch")
        return cls(quote, fingerprint, _canonical(body))

    def __post_init__(self) -> None:
        if not isinstance(self.quote, AuthoritativeQuote):
            _fail()
        if not isinstance(self.quote_fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", self.quote_fingerprint):
            _fail()
        expected = {"checkout": self.quote.exact_submit_projection()}
        if self.quote.fingerprint != self.quote_fingerprint or not isinstance(self.body_bytes, bytes):
            _fail("mismatch")
        if not hashlib.sha256(_canonical(expected)).digest() == hashlib.sha256(self.body_bytes).digest():
            _fail("mismatch")

    def private_body(self) -> dict[str, Any]:
        self.__post_init__()
        try:
            value = json.loads(self.body_bytes)
        except (TypeError, ValueError):
            _fail("mismatch")
        return cast(dict[str, Any], value)


@dataclass(frozen=True, slots=True, repr=False)
class FinalCheckoutStatus:
    checkout_id: str = field(repr=False)
    status: str
    action: str | None
    post_auth_action: str | None
    minimal_auth_and_void: bool
    terminal: bool
    succeeded: bool
    evidence_hash: str = field(repr=False)

    @property
    def state(self) -> str:
        return "COMPLETED" if self.succeeded else "REJECTED" if self.terminal else self.status

    @property
    def requires_manual_completion(self) -> bool:
        return not self.terminal


def _checkout_payload(payload: object) -> dict[str, Any]:
    _bounded(payload)
    if not isinstance(payload, dict):
        _fail()
    if set(payload) == {"response"}:
        response = payload["response"]
        if not isinstance(response, dict) or set(response) != {"data"}:
            _fail()
        payload = response["data"]
    if not isinstance(payload, dict):
        _fail()
    if set(payload) == {"checkout"}:
        payload = payload["checkout"]
    if not isinstance(payload, dict):
        _fail()
    return payload


def parse_final_response(payload: object, *, quote: AuthoritativeQuote | None = None, expected_checkout_id: str | None = None) -> FinalCheckoutStatus:
    checkout = _checkout_payload(payload)
    required = {"checkoutId", "status", "action", "postAuthAction", "willDoMinimalAmountAuthAndVoid", "order", "payments"}
    allowed = required | {"errors", "error", "staticCode"}
    if not required.issubset(checkout) or not set(checkout).issubset(allowed):
        _fail()
    checkout_id = _id(checkout["checkoutId"])
    if expected_checkout_id is not None and checkout_id != _id(expected_checkout_id):
        _fail("mismatch")
    status = checkout["status"]
    action = checkout["action"]
    post_auth = checkout["postAuthAction"]
    if status not in _STATUSES or action not in _ACTIONS or post_auth not in _POST_AUTH:
        _fail("unsupported")
    minimal = checkout["willDoMinimalAmountAuthAndVoid"]
    if not isinstance(minimal, bool) or not isinstance(checkout["payments"], list):
        _fail()
    order = checkout["order"]
    payments = checkout["payments"]
    terminal = status in {"COMPLETED", "FAILED", "CANCELLED"}
    succeeded = status == "COMPLETED"
    if succeeded:
        if not isinstance(order, dict):
            _fail("mismatch")
        if not {"basketId", "total", "currencyCode"}.issubset(order):
            _fail()
        _id(order["basketId"])
        _integer(order["total"])
        if not isinstance(order["currencyCode"], str):
            _fail()
        if action is not None or post_auth not in {None, "NONE"}:
            _fail("mismatch")
        if quote is not None and (
            order["basketId"] != quote.basket_id
            or order["total"] != quote.total.amount_minor
            or order["currencyCode"] != quote.total.currency
        ):
            _fail("mismatch")
    elif terminal:
        # A provider rejection is deterministic only without contradictory order
        # or positive payment evidence. Unknown payment shapes fail ambiguous.
        if order is not None or any(item not in (None, {}) for item in payments):
            _fail("mismatch")
    elif order is not None:
        _fail("mismatch")
    evidence_hash = hashlib.sha256(_canonical(checkout)).hexdigest()
    return FinalCheckoutStatus(checkout_id, status, action, post_auth, minimal, terminal, succeeded, evidence_hash)


class ProductionFinalCheckoutAdapter:
    """Exactly one purpose-bound POST or one explicit known-ID GET per call."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def async_submit(self, request: FinalCheckoutRequest) -> FinalCheckoutStatus:
        if not isinstance(request, FinalCheckoutRequest):
            _fail()
        body = request.private_body()
        try:
            payload = await self._session.async_mutate(
                MutationPurpose.FINAL_CHECKOUT,
                "POST",
                FINAL_CHECKOUT_PATH,
                body,
                delivery_location=request.quote.delivery_location,
            )
        except MutationDispatchUncertain:
            raise asyncio.CancelledError from None
        except ApiSessionError as err:
            if err.status in {400, 401, 403, 404, 405, 406, 409, 410, 415, 422, 429}:
                raise FinalCheckoutRejected(err.status) from None
            category = "cancelled" if err.category == "cancelled" else "http_error" if err.status else "unknown_ambiguous"
            raise FinalCheckoutAmbiguous(category=category) from None
        try:
            status = parse_final_response(payload, quote=request.quote)
        except FinalCheckoutContractError as err:
            known = None
            try:
                known = _id(_checkout_payload(payload).get("checkoutId"))
            except Exception:
                pass
            category = "contradictory_response" if err.category == "mismatch" else "malformed_response"
            raise FinalCheckoutAmbiguous(known, category) from None
        if not status.terminal:
            raise FinalCheckoutAmbiguous(status.checkout_id, "pending_or_interactive")
        return status

    async def async_status(
        self,
        checkout_id: str,
        quote: AuthoritativeQuote | None = None,
        *,
        expected_basket_hash: str | None = None,
        expected_amount_minor: int | None = None,
        expected_currency: str | None = None,
    ) -> FinalCheckoutStatus:
        known = _id(checkout_id)
        try:
            payload = await self._session.async_final_status(FINAL_STATUS_PATH_PREFIX + known)
            status = parse_final_response(payload, quote=quote, expected_checkout_id=known)
            if status.succeeded and quote is None:
                order = _checkout_payload(payload).get("order")
                if (
                    not isinstance(order, dict)
                    or not isinstance(expected_basket_hash, str)
                    or re.fullmatch(r"[0-9a-f]{64}", expected_basket_hash) is None
                    or hashlib.sha256(_id(order.get("basketId")).encode()).hexdigest()
                    != expected_basket_hash
                    or _integer(order.get("total")) != expected_amount_minor
                    or order.get("currencyCode") != expected_currency
                ):
                    _fail("mismatch")
            return status
        except FinalCheckoutContractError:
            raise FinalCheckoutAmbiguous(known, "malformed_response") from None
        except ApiSessionError:
            raise FinalCheckoutAmbiguous(known) from None

    async def async_complete(self, checkout_id: str) -> None:
        _id(checkout_id)
        raise FinalCheckoutUnsupported("checkout completion is not implemented")


# Compatibility alias for older offline fixture imports; production and fixture
# tests now exercise the same strict contract through injected sessions.
FixtureOnlyFinalCheckoutAdapter = ProductionFinalCheckoutAdapter
