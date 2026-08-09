"""Strict GET-only live store and menu/catalog client."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

from .api_session import ApiSessionError, DeliveryLocation
from .ordering_contracts import (
    AddressSnapshot,
    CatalogMenu,
    ContractError,
    LiveStore,
    parse_menu,
    parse_store,
)

_SLUG_RE: Final = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
_LANGUAGE_RE: Final = re.compile(r"^[a-z]{2}(?:-[A-Z]{2})?$")
_CLASSIFIED_FALLBACKS: Final = {
    "NOT_FOUND": "not_found",
    "ENDPOINT_NOT_FOUND": "not_found",
    "UNSUPPORTED_ENDPOINT": "unsupported",
    "UNSUPPORTED_VERSION": "unsupported",
}


class LiveCatalogClient:
    """Fetch one eligible store and one bounded directly-orderable menu page."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def async_store(
        self, store_slug: str, delivery_address: AddressSnapshot
    ) -> LiveStore:
        if (
            not isinstance(store_slug, str)
            or not _SLUG_RE.fullmatch(store_slug)
            or not isinstance(delivery_address, AddressSnapshot)
        ):
            raise ApiSessionError(
                category="invalid_request", endpoint_family="catalog"
            )
        location = DeliveryLocation(
            country_code=delivery_address.country_code,
            city_code=delivery_address.city_code,
            latitude=delivery_address.latitude,
            longitude=delivery_address.longitude,
        )
        payload = await self._session.async_get(
            "catalog",
            f"/v3/stores/{store_slug}",
            {"includeClosed": "true", "includeDisabled": "false"},
            delivery_location=location,
        )
        try:
            store = parse_store(payload)
        except ContractError as err:
            raise ApiSessionError(
                category=err.category, endpoint_family="catalog"
            ) from None
        if store.city_code != delivery_address.city_code:
            raise ApiSessionError(category="schema", endpoint_family="catalog")
        return store

    @staticmethod
    def _menu_query(
        translation: str | None,
        approved_query: Mapping[str, str] | None,
    ) -> dict[str, str]:
        query: dict[str, str] = {}
        if translation is not None:
            if not isinstance(translation, str) or not _LANGUAGE_RE.fullmatch(translation):
                raise ApiSessionError(
                    category="invalid_request", endpoint_family="catalog"
                )
            query["language"] = translation
        if approved_query is not None:
            if not isinstance(approved_query, Mapping):
                raise ApiSessionError(
                    category="invalid_request", endpoint_family="catalog"
                )
            for key, value in approved_query.items():
                if key != "deliveryMode" or value != "DELIVERY":
                    raise ApiSessionError(
                        category="invalid_request", endpoint_family="catalog"
                    )
                query[key] = value
        return query

    @staticmethod
    def _raise_classified_outcome(payload: Any) -> None:
        if not isinstance(payload, dict) or set(payload) != {"error"}:
            return
        error = payload["error"]
        if not isinstance(error, dict) or set(error) != {"code"}:
            return
        category = _CLASSIFIED_FALLBACKS.get(error["code"])
        if category is not None:
            raise ApiSessionError(category=category, endpoint_family="catalog", status=404)

    async def async_menu(
        self,
        store: LiveStore,
        delivery_address: AddressSnapshot,
        *,
        translation: str | None = None,
        approved_query: Mapping[str, str] | None = None,
    ) -> CatalogMenu:
        if not isinstance(store, LiveStore) or not isinstance(
            delivery_address, AddressSnapshot
        ):
            raise ApiSessionError(
                category="invalid_request", endpoint_family="catalog"
            )
        location = DeliveryLocation(
            country_code=delivery_address.country_code,
            city_code=delivery_address.city_code,
            latitude=delivery_address.latitude,
            longitude=delivery_address.longitude,
        )
        query = self._menu_query(translation, approved_query)
        preferred = (
            f"/v4/stores/{store.store_id}/addresses/{store.address_id}/content/main"
        )
        legacy = (
            f"/v3/stores/{store.store_id}/addresses/{store.address_id}/node/store_menu"
        )
        try:
            payload = await self._session.async_get(
                "catalog", preferred, query, delivery_location=location
            )
            self._raise_classified_outcome(payload)
        except ApiSessionError as err:
            if err.category not in {"not_found", "unsupported"}:
                raise
            payload = await self._session.async_get(
                "catalog", legacy, query, delivery_location=location
            )
            self._raise_classified_outcome(payload)
        try:
            return parse_menu(
                payload, expected_store_address_id=store.address_id
            )
        except ContractError as err:
            raise ApiSessionError(
                category=err.category, endpoint_family="catalog"
            ) from None
