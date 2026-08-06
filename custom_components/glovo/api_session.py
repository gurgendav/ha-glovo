"""Account-scoped serialized token authority for all Glovo reads."""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Awaitable, Callable, Mapping
from functools import partial
from typing import Any, Final

_ALLOWED_FAMILIES: Final = frozenset(
    {"account", "address", "payment", "catalog", "tracking"}
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
)
_MAX_QUERY_ITEMS: Final = 12
_MAX_QUERY_LENGTH: Final = 1_000


class ApiSessionError(RuntimeError):
    """Redaction-safe error containing only approved classification fields."""

    def __init__(
        self,
        *,
        category: str,
        endpoint_family: str,
        status: int | None = None,
    ) -> None:
        self.category = category if category in _ALLOWED_CATEGORIES else "transport"
        self.endpoint_family = (
            endpoint_family if endpoint_family in _ALLOWED_FAMILIES else "catalog"
        )
        self.status = status if status in _ALLOWED_STATUS else None
        super().__init__(
            f"Glovo read failed ({self.endpoint_family}/{self.category})"
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "status": self.status,
            "endpointFamily": self.endpoint_family,
        }


class SerializedApiSession:
    """Own one lock for token refresh, persistence, and account-scoped reads."""

    def __init__(
        self,
        *,
        token_source: Callable[[], str],
        persist_token: Callable[[str], Any],
        ensure_token: Callable[[str], Any],
        transport: Callable[[str, str, str, dict[str, str]], Any],
        executor: Callable[[Callable[[], Any]], Awaitable[Any]] | None = None,
    ) -> None:
        self._token_source = token_source
        self._persist_token = persist_token
        self._ensure_token = ensure_token
        self._transport = transport
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
            result = await self._executor(partial(function, *args))
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
    def _valid_path(path: str) -> bool:
        return isinstance(path, str) and any(pattern.fullmatch(path) for pattern in _GET_PATHS)

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
        except Exception as err:
            self._valid = False
            raise ApiSessionError(
                category="persistence", endpoint_family=endpoint_family
            ) from err
        self._token_authority = updated_token

    async def async_get(
        self,
        endpoint_family: str,
        path: str,
        query: Mapping[str, str] | None = None,
    ) -> Any:
        """Perform one allowlisted GET; never retry or replay the request."""
        if (
            endpoint_family not in _ALLOWED_FAMILIES
            or not self._valid_path(path)
            or not self._valid_query(query or {})
        ):
            raise ApiSessionError(
                category="invalid_request", endpoint_family=endpoint_family
            )
        async with self._lock:
            if not self._valid:
                raise ApiSessionError(
                    category="auth", endpoint_family=endpoint_family
                )
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
                    category="auth", endpoint_family=endpoint_family,
                    status=_safe_status(err),
                ) from err
            await self._persist_if_changed(
                current_token, updated_token, endpoint_family
            )
            try:
                return await self._invoke(
                    self._transport,
                    "GET",
                    access_token,
                    path,
                    dict(query or {}),
                )
            except asyncio.CancelledError:
                raise
            except ApiSessionError:
                raise
            except Exception as err:
                status = _safe_status(err)
                category = "auth" if status in {401, 403} else "http" if status else "transport"
                raise ApiSessionError(
                    category=category,
                    endpoint_family=endpoint_family,
                    status=status,
                ) from err

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
                category = "auth" if status in {401, 403} else "http" if status else "transport"
                raise ApiSessionError(
                    category=category,
                    endpoint_family="tracking",
                    status=status,
                ) from err
            await self._persist_if_changed(
                current_token, updated_token, "tracking"
            )
            return result


def _safe_status(error: BaseException) -> int | None:
    status = getattr(error, "status", None)
    if isinstance(status, bool) or not isinstance(status, int):
        return None
    return status if status in _ALLOWED_STATUS else None
