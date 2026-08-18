"""Serialized token and single-attempt request authority for approved Glovo routes."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from functools import partial
from typing import Any, Final

_ALLOWED_FAMILIES: Final = frozenset(
    {"account", "address", "payment", "catalog", "tracking", "basket", "quote", "checkout"}
)
_ALLOWED_CATEGORIES: Final = frozenset(
    {
        "auth",
        "http",
        "transport",
        "persistence",
        "invalid_request",
        "unsupported",
        "not_found",
        "schema",
    }
)
_ALLOWED_STATUS: Final = frozenset(
    {400, 401, 403, 404, 405, 406, 409, 410, 415, 422, 429, 500, 502, 503, 504}
)
_GET_PATHS: Final = (
    re.compile(r"^/v3/me$"),
    re.compile(r"^/customer_profile/api/v1/address_book/me/addresses$"),
    re.compile(r"^/v4/payment_methods$"),
    re.compile(r"^/v3/stores/[a-z0-9][a-z0-9-]{0,99}$"),
    re.compile(r"^/v4/stores/[1-9]\d{0,9}/addresses/[1-9]\d{0,9}/content/main$"),
    re.compile(r"^/v3/stores/[1-9]\d{0,9}/addresses/[1-9]\d{0,9}/node/store_menu$"),
    re.compile(r"^/v1/authenticated/customers/[1-9]\d{0,9}/baskets$"),
    re.compile(
        r"^/v1/authenticated/customers/[1-9]\d{0,9}/baskets/stores/"
        r"[1-9]\d{0,9}$"
    ),
)
_MAX_QUERY_ITEMS: Final = 12
_MAX_QUERY_LENGTH: Final = 1_000
_MAX_MUTATION_BYTES: Final = 256_000
_MAX_MUTATION_DEPTH: Final = 12
_COUNTRY_CODE_RE: Final = re.compile(r"^[A-Z]{2}$")
_CITY_CODE_RE: Final = re.compile(r"^[A-Z0-9][A-Z0-9_-]{1,19}$")
_LOCATION_GET_FAMILIES: Final = frozenset({"payment", "catalog", "basket"})

# Keep these literal contracts in lockstep with ``glovo.py``. The transport
# seam accepts an explicit purpose, whereas the standalone helper uses the
# string value solely to make drift detectable in offline tests.
_PHASE_MUTATION_ALLOWLIST: Final[tuple[tuple[str, str, str], ...]] = (
    ("create_basket", "POST", r"^/v1/authenticated/customers/[1-9]\d{0,9}/baskets$"),
    (
        "replace_basket_products",
        "PUT",
        r"^/v1/authenticated/customers/[1-9]\d{0,9}/baskets/[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}/products$",
    ),
    (
        "change_basket_quantity",
        "PATCH",
        r"^/v1/authenticated/customers/[1-9]\d{0,9}/baskets/[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}/products/quantity$",
    ),
    (
        "delete_basket",
        "DELETE",
        r"^/v1/authenticated/customers/[1-9]\d{0,9}/baskets/[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
    ),
    ("create_quote_template", "POST", r"^/v3/checkouts/order/1/template$"),
    ("final_checkout", "POST", r"^/v3/checkouts/order/1$"),
)
_MUTATION_QUERY_CONTRACT: Final[dict[str, frozenset[str]]] = {
    purpose: frozenset() for purpose, _, _ in _PHASE_MUTATION_ALLOWLIST
}


class MutationPurpose(str, Enum):
    """Closed set of non-checkout write purposes approved for this phase."""

    CREATE_BASKET = "create_basket"
    REPLACE_BASKET_PRODUCTS = "replace_basket_products"
    CHANGE_BASKET_QUANTITY = "change_basket_quantity"
    DELETE_BASKET = "delete_basket"
    CREATE_QUOTE_TEMPLATE = "create_quote_template"
    FINAL_CHECKOUT = "final_checkout"


_MUTATION_ROUTES: Final[dict[MutationPurpose, tuple[str, re.Pattern[str], str]]] = {
    MutationPurpose(purpose): (
        method,
        re.compile(pattern),
        "checkout"
        if purpose == MutationPurpose.FINAL_CHECKOUT.value
        else "quote"
        if purpose == MutationPurpose.CREATE_QUOTE_TEMPLATE.value
        else "basket",
    )
    for purpose, method, pattern in _PHASE_MUTATION_ALLOWLIST
}


class ApiSessionError(RuntimeError):
    """Redaction-safe error containing only approved classification fields."""

    def __init__(
        self,
        *,
        category: str,
        endpoint_family: str,
        status: int | None = None,
        purpose: MutationPurpose | None = None,
    ) -> None:
        self.category = category if category in _ALLOWED_CATEGORIES else "transport"
        self.endpoint_family = (
            endpoint_family if endpoint_family in _ALLOWED_FAMILIES else "catalog"
        )
        self.status = status if status in _ALLOWED_STATUS else None
        self.purpose = purpose if isinstance(purpose, MutationPurpose) else None
        operation = "mutation" if self.purpose is not None else "read"
        super().__init__(
            f"Glovo {operation} failed ({self.endpoint_family}/{self.category})"
        )

    def public_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "category": self.category,
            "status": self.status,
            "endpointFamily": self.endpoint_family,
        }
        if self.purpose is not None:
            result["purpose"] = self.purpose.value
        return result


class MutationDispatchUncertain(ApiSessionError):
    """Cancellation was observed only after the sole transport invocation began."""

    def __init__(self, *, endpoint_family: str, purpose: MutationPurpose) -> None:
        super().__init__(
            category="transport", endpoint_family=endpoint_family, purpose=purpose
        )


@dataclass(frozen=True, slots=True, repr=False)
class DeliveryLocation:
    """Private validated delivery context for location-bound web reads."""

    country_code: str = field(repr=False)
    city_code: str = field(repr=False)
    latitude: float = field(repr=False)
    longitude: float = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.country_code, str)
            or not _COUNTRY_CODE_RE.fullmatch(self.country_code)
            or not isinstance(self.city_code, str)
            or not _CITY_CODE_RE.fullmatch(self.city_code)
        ):
            raise ApiSessionError(category="invalid_request", endpoint_family="catalog")
        for value, minimum, maximum in (
            (self.latitude, -90.0, 90.0),
            (self.longitude, -180.0, 180.0),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not minimum <= float(value) <= maximum
            ):
                raise ApiSessionError(
                    category="invalid_request", endpoint_family="catalog"
                )
        object.__setattr__(self, "latitude", float(self.latitude))
        object.__setattr__(self, "longitude", float(self.longitude))

    def transport_context(self) -> dict[str, str]:
        """Return a closed private transport shape, never a public projection."""
        return {
            "countryCode": self.country_code,
            "cityCode": self.city_code,
            "latitude": str(self.latitude),
            "longitude": str(self.longitude),
        }


class SerializedApiSession:
    """Own one lock for token refresh, persistence, reads, and approved writes."""

    def __init__(
        self,
        *,
        token_source: Callable[[], str],
        persist_token: Callable[[str], Any],
        ensure_token: Callable[[str], Any],
        transport: Callable[[str, str, str, dict[str, str]], Any],
        location_transport: Callable[
            [str, str, str, dict[str, str], dict[str, str]], Any
        ]
        | None = None,
        mutation_transport: Callable[
            [
                str,
                str,
                str,
                dict[str, str],
                dict[str, Any] | None,
                dict[str, str],
            ],
            Any,
        ]
        | None = None,
        executor: Callable[[Callable[[], Any]], Awaitable[Any]] | None = None,
    ) -> None:
        self._token_source = token_source
        self._persist_token = persist_token
        self._ensure_token = ensure_token
        self._transport = transport
        self._location_transport = location_transport
        self._mutation_transport = mutation_transport
        self._executor = executor
        self._lock = asyncio.Lock()
        self._valid = True
        self._token_authority: str | None = None

    @property
    def lock(self) -> asyncio.Lock:
        """Expose lock identity for tests; callers must never acquire it directly."""
        return self._lock

    def invalidate(self) -> None:
        self._valid = False

    async def _invoke(self, function: Callable[..., Any], *args: Any) -> Any:
        if self._executor is None:
            result = function(*args)
        else:
            # ``async_add_executor_job``/``run_in_executor`` cannot stop a
            # synchronous socket call after its coroutine waiter is cancelled.
            # Shield and retain its real Future, then keep this lock owner alive
            # until that thread finishes. Otherwise a second write could start
            # while the cancelled first write is still on the wire.
            pending = asyncio.ensure_future(self._executor(partial(function, *args)))
            try:
                result = await asyncio.shield(pending)
            except asyncio.CancelledError:
                # A cancellation cannot stop the synchronous executor call. Keep
                # this lock owner alive until its retained Future is definitive;
                # each later cancellation is only another untrusted caller outcome.
                while not pending.done():
                    try:
                        await asyncio.shield(pending)
                    except asyncio.CancelledError:
                        continue
                    except BaseException:
                        # Its result is deliberately unknowable to the cancelled
                        # caller, but its completion still returns lock authority.
                        pass
                try:
                    pending.result()
                except BaseException:
                    # Consume any completed executor failure before reporting the
                    # original cancellation as a dispatch uncertainty.
                    pass
                raise
        if inspect.isawaitable(result):
            return await result
        return result

    @staticmethod
    def _valid_query(query: Mapping[str, str]) -> bool:
        if not isinstance(query, Mapping) or len(query) > _MAX_QUERY_ITEMS:
            return False
        length = 0
        for key, value in query.items():
            if not isinstance(key, str) or not isinstance(value, str):
                return False
            if not key or len(key) > 64 or len(value) > 300:
                return False
            if any(ord(char) < 32 for char in key + value):
                return False
            length += len(key) + len(value) + 2
        return length <= _MAX_QUERY_LENGTH

    @staticmethod
    def _valid_path(endpoint_family: str, path: str) -> bool:
        if not isinstance(path, str):
            return False
        family_patterns: dict[str, tuple[re.Pattern[str], ...]] = {
            "account": (_GET_PATHS[0],),
            "address": (_GET_PATHS[1],),
            "payment": (_GET_PATHS[2],),
            "catalog": _GET_PATHS[3:6],
            "basket": _GET_PATHS[6:8],
        }
        return any(
            pattern.fullmatch(path)
            for pattern in family_patterns.get(endpoint_family, ())
        )

    @staticmethod
    def _valid_body(body: object) -> bool:
        if body is not None and not isinstance(body, dict):
            return False
        try:
            encoded = json.dumps(
                body, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            )
        except (TypeError, ValueError, OverflowError):
            return False
        if len(encoded.encode()) > _MAX_MUTATION_BYTES:
            return False

        def walk(value: object, depth: int) -> bool:
            if depth > _MAX_MUTATION_DEPTH:
                return False
            if isinstance(value, dict):
                if len(value) > 200:
                    return False
                return all(
                    isinstance(key, str)
                    and 0 < len(key) <= 100
                    and walk(item, depth + 1)
                    for key, item in value.items()
                )
            if isinstance(value, list):
                return len(value) <= 200 and all(
                    walk(item, depth + 1) for item in value
                )
            if isinstance(value, str):
                return len(value) <= 1_000 and not any(
                    ord(char) < 32 for char in value
                )
            if isinstance(value, float):
                return math.isfinite(value)
            return value is None or isinstance(value, (bool, int))

        return walk(body, 0)

    async def _persist_if_changed(
        self, current_token: str, updated_token: str, endpoint_family: str
    ) -> None:
        if not isinstance(updated_token, str) or not updated_token:
            raise ApiSessionError(category="auth", endpoint_family=endpoint_family)
        if updated_token == current_token:
            self._token_authority = current_token
            return
        try:
            await self._invoke(self._persist_token, updated_token)
        except asyncio.CancelledError:
            self._valid = False
            raise
        except Exception:
            self._valid = False
            raise ApiSessionError(
                category="persistence", endpoint_family=endpoint_family
            ) from None
        self._token_authority = updated_token

    async def _async_authorize(self, endpoint_family: str) -> str:
        try:
            current_token = self._token_authority or self._token_source()
            access_token, updated_token = await self._invoke(
                self._ensure_token, current_token
            )
            if not isinstance(access_token, str) or not access_token:
                raise TypeError
        except asyncio.CancelledError:
            raise
        except ApiSessionError:
            raise
        except Exception as err:
            raise ApiSessionError(
                category="auth",
                endpoint_family=endpoint_family,
                status=_safe_status(err),
            ) from None
        await self._persist_if_changed(current_token, updated_token, endpoint_family)
        return access_token

    async def async_get(
        self,
        endpoint_family: str,
        path: str,
        query: Mapping[str, str] | None = None,
        *,
        delivery_location: DeliveryLocation | None = None,
    ) -> Any:
        """Perform one allowlisted GET; never retry or replay the request."""
        location_bound = endpoint_family in _LOCATION_GET_FAMILIES
        if (
            endpoint_family not in _ALLOWED_FAMILIES
            or not self._valid_path(endpoint_family, path)
            or not self._valid_query(query or {})
            or (endpoint_family == "basket" and bool(query))
            or (
                location_bound
                and (
                    not isinstance(delivery_location, DeliveryLocation)
                    or self._location_transport is None
                )
            )
            or (not location_bound and delivery_location is not None)
        ):
            raise ApiSessionError(
                category="invalid_request", endpoint_family=endpoint_family
            )
        async with self._lock:
            if not self._valid:
                raise ApiSessionError(category="auth", endpoint_family=endpoint_family)
            access_token = await self._async_authorize(endpoint_family)
            try:
                if delivery_location is None:
                    return await self._invoke(
                        self._transport,
                        "GET",
                        access_token,
                        path,
                        dict(query or {}),
                    )
                assert self._location_transport is not None
                return await self._invoke(
                    self._location_transport,
                    "GET",
                    access_token,
                    path,
                    dict(query or {}),
                    delivery_location.transport_context(),
                )
            except asyncio.CancelledError:
                raise
            except ApiSessionError:
                raise
            except Exception as err:
                status = _safe_status(err)
                category = (
                    "auth"
                    if status in {401, 403}
                    else "http"
                    if status
                    else "transport"
                )
                raise ApiSessionError(
                    category=category,
                    endpoint_family=endpoint_family,
                    status=status,
                ) from None

    async def async_final_status(self, path: str) -> Any:
        """Perform one explicit GET for one exact learned checkout identifier."""
        if (
            not isinstance(path, str)
            or re.fullmatch(
                r"/v3/checkouts/order/[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", path
            )
            is None
        ):
            raise ApiSessionError(
                category="invalid_request", endpoint_family="checkout"
            )
        async with self._lock:
            if not self._valid:
                raise ApiSessionError(category="auth", endpoint_family="checkout")
            access_token = await self._async_authorize("checkout")
            try:
                return await self._invoke(
                    self._transport, "GET", access_token, path, {}
                )
            except asyncio.CancelledError:
                raise
            except Exception as err:
                status = _safe_status(err)
                raise ApiSessionError(
                    category="http" if status else "transport",
                    endpoint_family="checkout",
                    status=status,
                ) from None

    async def async_mutate(
        self,
        purpose: MutationPurpose,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        query: Mapping[str, str] | None = None,
        *,
        delivery_location: DeliveryLocation | None = None,
    ) -> Any:
        """Dispatch exactly one purpose-bound mutation after token persistence."""
        route = _MUTATION_ROUTES.get(purpose) if isinstance(purpose, MutationPurpose) else None
        endpoint_family = route[2] if route is not None else "basket"
        if (
            route is None
            or method != route[0]
            or not isinstance(path, str)
            or route[1].fullmatch(path) is None
            or (
                query is not None
                and (
                    not isinstance(query, Mapping)
                    or set(query) != _MUTATION_QUERY_CONTRACT[purpose.value]
                )
            )
            or not self._valid_body(body)
            or self._mutation_transport is None
            or not isinstance(delivery_location, DeliveryLocation)
        ):
            raise ApiSessionError(
                category="invalid_request",
                endpoint_family=endpoint_family,
                purpose=purpose if isinstance(purpose, MutationPurpose) else None,
            )
        async with self._lock:
            if not self._valid:
                raise ApiSessionError(
                    category="auth",
                    endpoint_family=endpoint_family,
                    purpose=purpose,
                )
            access_token = await self._async_authorize(endpoint_family)
            try:
                # This is the sole low-level invocation. No branch below retries,
                # refreshes, replays, compensates, or dispatches another mutation.
                return await self._invoke(
                    self._mutation_transport,
                    method,
                    access_token,
                    path,
                    dict(query or {}),
                    body,
                    delivery_location.transport_context(),
                )
            except asyncio.CancelledError:
                raise MutationDispatchUncertain(
                    endpoint_family=endpoint_family, purpose=purpose
                ) from None
            except ApiSessionError:
                raise
            except Exception as err:
                status = _safe_status(err)
                category = (
                    "auth"
                    if status in {401, 403}
                    else "http"
                    if status
                    else "transport"
                )
                raise ApiSessionError(
                    category=category,
                    endpoint_family=endpoint_family,
                    status=status,
                    purpose=purpose,
                ) from None

    async def async_legacy_read(
        self,
        operation: Callable[[str], Any],
    ) -> Any:
        """Serialize one existing tracking transaction and its rotated token save."""
        async with self._lock:
            if not self._valid:
                raise ApiSessionError(category="auth", endpoint_family="tracking")
            current_token = self._token_authority or self._token_source()
            try:
                result, updated_token = await self._invoke(operation, current_token)
            except asyncio.CancelledError:
                raise
            except ApiSessionError:
                raise
            except Exception as err:
                status = _safe_status(err)
                category = (
                    "auth"
                    if status in {401, 403}
                    else "http"
                    if status
                    else "transport"
                )
                raise ApiSessionError(
                    category=category,
                    endpoint_family="tracking",
                    status=status,
                ) from None
            await self._persist_if_changed(current_token, updated_token, "tracking")
            return result


def _safe_status(error: BaseException) -> int | None:
    status = getattr(error, "status", None)
    if isinstance(status, bool) or not isinstance(status, int):
        return None
    return status if status in _ALLOWED_STATUS else None
