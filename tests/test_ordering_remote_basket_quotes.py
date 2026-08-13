"""Offline safety tests for remote baskets and authoritative templates."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import math
import socket
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]
GLOVO_ROOT = ROOT / "custom_components" / "glovo"
MODULES = (
    "ordering_models",
    "ordering_contracts",
    "api_session",
    "ordering_remote_basket",
    "ordering_live_quote",
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def socket_guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    attempts: list[str] = []

    def blocked(*args: Any, **kwargs: Any) -> Any:
        attempts.append("blocked")
        pytest.fail("remote basket/template test attempted external network access")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    yield
    assert attempts == []


@pytest.fixture()
def live() -> dict[str, ModuleType]:
    package_name = "glovo_remote_ordering_under_test"
    package = ModuleType(package_name)
    package.__path__ = [str(GLOVO_ROOT)]  # type: ignore[attr-defined]
    sys.modules[package_name] = package
    loaded: dict[str, ModuleType] = {}
    try:
        for name in MODULES:
            module_name = f"{package_name}.{name}"
            spec = importlib.util.spec_from_file_location(module_name, GLOVO_ROOT / f"{name}.py")
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            loaded[name] = module
        yield loaded
    finally:
        for name in tuple(sys.modules):
            if name == package_name or name.startswith(f"{package_name}."):
                sys.modules.pop(name, None)


class Clock:
    def __init__(self, value: float = 500.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class FixtureTransport:
    """Explicit fixture transport with a complete method/path/body ledger."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str], Any]] = []
        self.responses: list[Any] = []
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def get(self, method: str, access: str, path: str, query: dict[str, str]) -> Any:
        assert method == "GET" and access == "access-ok"
        self.calls.append((method, path, query, None))
        return self._next()

    async def mutate(
        self,
        method: str,
        access: str,
        path: str,
        query: dict[str, str],
        body: dict[str, Any] | None,
    ) -> Any:
        assert access == "access-ok"
        self.calls.append((method, path, query, copy.deepcopy(body)))
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        return self._next()

    def _next(self) -> Any:
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return copy.deepcopy(response)


def session(live: dict[str, ModuleType], transport: FixtureTransport, **overrides: Any) -> Any:
    values = {
        "token_source": lambda: "stored-token",
        "persist_token": lambda value: None,
        "ensure_token": lambda value: ("access-ok", value),
        "transport": transport.get,
        "mutation_transport": transport.mutate,
    }
    values.update(overrides)
    return live["api_session"].SerializedApiSession(**values)


def product(quantity: int = 2) -> dict[str, Any]:
    return {
        "ids": {
            "id": "product-1",
            "legacyId": "product-1",
            "externalId": "external-1",
            "storeProductId": "store-product-1",
        },
        "quantity": {"increments": quantity},
        "customizations": [
            {
                "ids": {
                    "groupLegacyId": "group-1",
                    "groupId": "group-1",
                    "groupExternalId": "group-ext-1",
                    "groupPosition": 0,
                    "legacyId": "attribute-1",
                    "externalId": "attribute-ext-1",
                },
                "name": "Preparation",
                "quantity": {"increments": 1},
                "customizationName": "Standard",
                "groupName": "Preparation",
            }
        ],
    }


def rich_product(quantity: int = 2, *, sponsored: bool | None = None) -> dict[str, Any]:
    result = product(quantity)
    result["ids"]["basketProductId"] = "basket-product-1"
    result["quantity"] = {
        "increments": quantity,
        "incrementsLimit": 10,
        "limitType": "MAX_SALES_QUANTITY",
    }
    result.update(
        {
            "price": {
                "totalFormatted": "5,500.00 AMD",
                "final": {"minor": 550000, "major": 5500.0, "formatted": "5,500.00 AMD"},
                "unitaryBasePrice": {"minor": 275000, "major": 2750.0, "formatted": "2,750.00 AMD"},
                "unitaryTotalPrice": {"minor": 275000, "major": 2750.0, "formatted": "2,750.00 AMD"},
                "productTotalDiscount": None,
            },
            "name": "Fixture meal",
            "productName": "Fixture meal",
            "description": "Sanitized fixture product",
            "imageUrl": None,
            "isCustomizable": True,
            "discounts": [],
            "productReplacement": None,
            "weighableInfo": None,
            "packaging": None,
        }
    )
    if sponsored is not None:
        result["sponsored"] = sponsored
    return result


def intent_payload() -> dict[str, Any]:
    return {
        "customerId": 42,
        "storeId": 71,
        "storeAddressId": 81,
        "storeCategoryId": 4,
        "handlingStrategy": "DELIVERY",
        "products": [product()],
    }


def basket_payload(*, version: str = "basket-v1", quantity: int = 2) -> dict[str, Any]:
    return {
        "basketId": "basket-private-1",
        "basketVersion": version,
        "customerId": 42,
        "storeId": 71,
        "storeAddressId": 81,
        "storeCategoryId": 4,
        "handlingStrategy": "DELIVERY",
        "products": [rich_product(quantity)],
        "basketPrice": {
            "totalFormatted": "5,500.00 AMD",
            "final": {"minor": 550000, "major": 5500.0, "formatted": "5,500.00 AMD"},
        },
        "productSuggestions": [],
        "mbs": {
            "surchargePrice": "0 AMD",
            "savedSurchargePrice": "0 AMD",
            "barThreshold": {
                "totalFormatted": "5,000 AMD",
                "final": {"minor": 500000, "major": 5000.0, "formatted": "5,000 AMD"},
            },
            "currentPrice": {
                "totalFormatted": "5,500 AMD",
                "final": {"minor": 550000, "major": 5500.0, "formatted": "5,500 AMD"},
            },
            "currentThreshold": "5,000 AMD",
        },
        "isPrimeSubscriptionSimulated": False,
        "cityCode": "YRV",
        "usingDhBasket": False,
    }


def address(contracts: ModuleType) -> Any:
    return contracts.AddressSnapshot(
        remote_id=17,
        address_line="Private Street 10",
        details="Private details",
        latitude=40.177,
        longitude=44.513,
        country_code="AM",
        city_code="YRV",
        city_name="Yerevan",
        kind="APARTMENT",
        tag="Home",
        fields=(contracts.AddressField("STREET_NAME", "Private Street"),),
    )


def payment(contracts: ModuleType) -> Any:
    return contracts.SavedPayment(
        payment_instrument_id="instrument-private",
        metadata_id=33,
        display_name="Visa",
        display_description="Card ending 4242",
        last_four_digits="4242",
        selected=True,
    )


def quote_response(**changes: Any) -> dict[str, Any]:
    order_details = {
        "checkoutSessionId": "checkout-private-1",
        "versionId": 3,
        "templateId": 9,
        "storeId": 71,
        "storeAddressId": 81,
        "basketId": "basket-private-1",
        "basketWidgetId": None,
        "currencyCode": "AMD",
        "purchaseTotalCents": 560000,
        "basketVersion": "basket-v1",
        "eta": "20–30 min",
        "legalVerificationRequired": False,
    }
    order_details.update(changes.pop("order_details", {}))
    checkout = {
        "name": "Store checkout",
        "orderType": "STORES",
        "enabled": True,
        "orderDetails": order_details,
        "components": [
            {
                "type": "PAYMENT_METHOD_PICKER",
                "data": {
                    "paymentMethodPickerData": {"orderTotal": 560000, "currencyCode": "AMD"}
                },
            },
            {
                "type": "PRICE_BREAKDOWN",
                "data": {
                    "priceBreakdownData": {
                        "breakDown": [
                            {"title": "Products", "value": "5,500.00 AMD", "type": "OTHER"},
                            {"title": "Delivery", "value": "100.00 AMD", "type": "DELIVERY"},
                            {
                                "title": "Total",
                                "value": "intentionally not parsed",
                                "type": "TOTAL",
                                "style": "EMPHASIS",
                                "notes": ["Taxes included"],
                                "actions": [],
                            },
                        ]
                    }
                },
            },
            {"type": "DELIVERY_ETA", "data": {"eta": "20–30 min"}},
            {
                "type": "STORE_CAPABILITIES",
                "data": {"capabilities": ["DELIVERY", "CREDIT_CARD", "IMMEDIATE"]},
            },
        ],
        "analytics": {"sourceScreen": "BASKET", "templateReceived": True},
    }
    checkout.update(changes)
    return {"response": {"data": {"checkout": checkout}}}


def make_request(live: dict[str, ModuleType], *, generation: int = 7, owner: str = "admin-a") -> Any:
    basket = live["ordering_remote_basket"].parse_remote_basket(
        basket_payload(), live["ordering_remote_basket"].parse_basket_intent(intent_payload())
    )
    return live["ordering_live_quote"].QuoteRequest(
        owner_key=owner,
        generation=generation,
        intent_key="intent-local-1",
        source_screen="BASKET",
        basket=basket,
        delivery_address=address(live["ordering_contracts"]),
        payment=payment(live["ordering_contracts"]),
        masked_address="Saved home ••••",
        masked_payment="Saved card •••• 4242",
        store_display_name="Fixture Kitchen",
    )


def test_session_write_routes_are_purpose_typed_allowlisted_and_single_attempt(
    live: dict[str, ModuleType],
) -> None:
    api = live["api_session"]
    fixture = FixtureTransport()
    fixture.responses = [{"ok": True}] * 5
    client = session(live, fixture)
    allowed = (
        (api.MutationPurpose.CREATE_BASKET, "POST", "/v1/authenticated/customers/42/baskets"),
        (
            api.MutationPurpose.REPLACE_BASKET_PRODUCTS,
            "PUT",
            "/v1/authenticated/customers/42/baskets/basket-1/products",
        ),
        (
            api.MutationPurpose.CHANGE_BASKET_QUANTITY,
            "PATCH",
            "/v1/authenticated/customers/42/baskets/basket-1/products/quantity",
        ),
        (
            api.MutationPurpose.DELETE_BASKET,
            "DELETE",
            "/v1/authenticated/customers/42/baskets/basket-1",
        ),
        (api.MutationPurpose.CREATE_QUOTE_TEMPLATE, "POST", "/v3/checkouts/order/1/template"),
    )
    for purpose, method, path in allowed:
        assert run(client.async_mutate(purpose, method, path, {"fixture": True})) == {"ok": True}
    assert [(item[0], item[1]) for item in fixture.calls] == [
        (item[1], item[2]) for item in allowed
    ]

    prohibited = (
        (api.MutationPurpose.CREATE_QUOTE_TEMPLATE, "POST", "/v3/checkouts/order/1"),
        (api.MutationPurpose.CREATE_QUOTE_TEMPLATE, "POST", "/v3/checkouts/order/1/payment"),
        (api.MutationPurpose.CREATE_BASKET, "POST", "/v3/stores/71/product-view"),
        (api.MutationPurpose.CREATE_BASKET, "POST", "/oauth/refresh"),
        (api.MutationPurpose.DELETE_BASKET, "DELETE", "/v1/customer/orders/1"),
        ("CREATE_BASKET", "POST", "/v1/authenticated/customers/42/baskets"),
    )
    for purpose, method, path in prohibited:
        with pytest.raises(api.ApiSessionError) as raised:
            run(client.async_mutate(purpose, method, path, {}))
        assert raised.value.category == "invalid_request"
    with pytest.raises(api.ApiSessionError):
        run(
            client.async_get(
                "account",
                "/v1/authenticated/customers/42/baskets/basket-1",
            )
        )
    assert len(fixture.calls) == len(allowed)


def test_session_persists_rotating_token_before_mutation_and_never_replays(
    live: dict[str, ModuleType],
) -> None:
    events: list[str] = []
    fixture = FixtureTransport()
    fixture.responses = [TimeoutError("URL / private basket and body")]

    async def persist(value: str) -> None:
        events.append("persist")

    async def ensure(value: str) -> tuple[str, str]:
        events.append("refresh")
        return "access-ok", "rotated-token"

    original = fixture.mutate

    async def mutate(*args: Any) -> Any:
        events.append("dispatch")
        return await original(*args)

    client = session(
        live,
        fixture,
        persist_token=persist,
        ensure_token=ensure,
        mutation_transport=mutate,
    )
    with pytest.raises(live["api_session"].ApiSessionError) as raised:
        run(
            client.async_mutate(
                live["api_session"].MutationPurpose.CREATE_BASKET,
                "POST",
                "/v1/authenticated/customers/42/baskets",
                {"private": "body"},
            )
        )
    assert events == ["refresh", "persist", "dispatch"]
    assert len(fixture.calls) == 1
    text = repr(raised.value) + str(raised.value) + json.dumps(raised.value.public_dict())
    assert "private" not in text.lower() and "/v1/" not in text


def test_session_auth_or_persistence_failure_has_zero_mutation_calls(live: dict[str, ModuleType]) -> None:
    api = live["api_session"]
    for failing in ("auth", "persist"):
        fixture = FixtureTransport()

        async def ensure(value: str) -> tuple[str, str]:
            if failing == "auth":
                raise RuntimeError("secret auth body")
            return "access-ok", "rotated"

        async def persist(value: str) -> None:
            if failing == "persist":
                raise OSError("secret persistence path")

        client = session(live, fixture, ensure_token=ensure, persist_token=persist)
        with pytest.raises(api.ApiSessionError):
            run(
                client.async_mutate(
                    api.MutationPurpose.CREATE_BASKET,
                    "POST",
                    "/v1/authenticated/customers/42/baskets",
                    {},
                )
            )
        assert fixture.calls == []


def test_cancellation_before_dispatch_has_zero_calls_after_dispatch_is_ambiguous(
    live: dict[str, ModuleType],
) -> None:
    api = live["api_session"]

    async def before() -> None:
        fixture = FixtureTransport()
        entered = asyncio.Event()

        async def ensure(value: str) -> tuple[str, str]:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError

        client = session(live, fixture, ensure_token=ensure)
        task = asyncio.create_task(
            client.async_mutate(
                api.MutationPurpose.CREATE_BASKET,
                "POST",
                "/v1/authenticated/customers/42/baskets",
                {},
            )
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fixture.calls == []

    async def after() -> None:
        fixture = FixtureTransport()
        fixture.responses = [basket_payload()]
        fixture.entered = asyncio.Event()
        fixture.release = asyncio.Event()
        client = session(live, fixture)
        task = asyncio.create_task(
            client.async_mutate(
                api.MutationPurpose.CREATE_BASKET,
                "POST",
                "/v1/authenticated/customers/42/baskets",
                {},
            )
        )
        await fixture.entered.wait()
        task.cancel()
        with pytest.raises(api.MutationDispatchUncertain):
            await task
        assert len(fixture.calls) == 1

    run(before())
    run(after())


def test_cancellation_during_rotating_token_persistence_is_pre_dispatch(
    live: dict[str, ModuleType],
) -> None:
    api = live["api_session"]

    async def scenario() -> None:
        fixture = FixtureTransport()
        entered = asyncio.Event()

        async def persist(value: str) -> None:
            entered.set()
            await asyncio.Event().wait()

        client = session(
            live,
            fixture,
            ensure_token=lambda value: ("access-ok", "rotated"),
            persist_token=persist,
        )
        task = asyncio.create_task(
            client.async_mutate(
                api.MutationPurpose.CREATE_BASKET,
                "POST",
                "/v1/authenticated/customers/42/baskets",
                {},
            )
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert fixture.calls == []
        with pytest.raises(api.ApiSessionError) as raised:
            await client.async_mutate(
                api.MutationPurpose.CREATE_BASKET,
                "POST",
                "/v1/authenticated/customers/42/baskets",
                {},
            )
        assert raised.value.category == "auth"
        assert fixture.calls == []

    run(scenario())


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("customerId",), True),
        (("storeId",), 0),
        (("handlingStrategy",), "PICKUP"),
        (("products", 0, "quantity", "increments"), True),
        (("products", 0, "quantity", "increments"), 0),
        (("products", 0, "ids", "id"), ""),
        (("products", 0, "customizations", 0, "ids", "groupPosition"), True),
        (("products", 0, "customizations", 0, "quantity", "increments"), 0),
    ],
)
def test_basket_intent_strict_type_strategy_bounds_matrix(
    live: dict[str, ModuleType], path: tuple[Any, ...], value: Any
) -> None:
    payload = intent_payload()
    target: Any = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(live["ordering_remote_basket"].BasketContractError):
        live["ordering_remote_basket"].parse_basket_intent(payload)


def test_basket_intent_rejects_unknown_restricted_duplicate_oversized_and_deep(
    live: dict[str, ModuleType],
) -> None:
    parser = live["ordering_remote_basket"].parse_basket_intent
    mutations: list[dict[str, Any]] = []
    unknown = intent_payload()
    unknown["products"][0]["instructions"] = "free form"
    mutations.append(unknown)
    restricted = intent_payload()
    restricted["products"][0]["variableWeight"] = True
    mutations.append(restricted)
    duplicate = intent_payload()
    duplicate["products"].append(copy.deepcopy(duplicate["products"][0]))
    mutations.append(duplicate)
    duplicate_customization = intent_payload()
    duplicate_customization["products"][0]["customizations"] *= 2
    mutations.append(duplicate_customization)
    oversized = intent_payload()
    oversized["products"][0]["ids"]["id"] = "x" * 200
    mutations.append(oversized)
    deep = intent_payload()
    deep["extra"] = {"x": {"x": {"x": {"x": {"x": {"x": {"x": {}}}}}}}}
    mutations.append(deep)
    for payload in mutations:
        with pytest.raises(live["ordering_remote_basket"].BasketContractError):
            parser(payload)


def test_strict_basket_response_exact_match_types_privacy_and_price_contract(
    live: dict[str, ModuleType],
) -> None:
    remote = live["ordering_remote_basket"]
    intent = remote.parse_basket_intent(intent_payload())
    basket = remote.parse_remote_basket(basket_payload(), intent)
    assert basket.basket_version == "basket-v1"
    assert "basket-private" not in repr(basket)
    assert "basket-private" not in repr(basket.products[0])

    mutations: list[dict[str, Any]] = []
    for key, value in (
        ("basketVersion", 1),
        ("customerId", 43),
        ("storeId", 72),
        ("handlingStrategy", "PICKUP"),
        ("isPrimeSubscriptionSimulated", 0),
        ("usingDhBasket", None),
    ):
        payload = basket_payload()
        payload[key] = value
        mutations.append(payload)
    bad_minor = basket_payload()
    bad_minor["basketPrice"]["final"]["minor"] = True
    mutations.append(bad_minor)
    bad_major = basket_payload()
    bad_major["basketPrice"]["final"]["major"] = math.nan
    mutations.append(bad_major)
    mismatched_products = basket_payload(quantity=3)
    mutations.append(mismatched_products)
    unknown = basket_payload()
    unknown["checkoutId"] = "private"
    mutations.append(unknown)
    for payload in mutations:
        with pytest.raises(remote.BasketContractError):
            remote.parse_remote_basket(payload, intent)


def test_response_accepts_first_party_partial_products_but_keeps_safety_fields_required(
    live: dict[str, ModuleType],
) -> None:
    remote = live["ordering_remote_basket"]
    intent = remote.parse_basket_intent(intent_payload())
    sparse = basket_payload()
    sparse_product = sparse["products"][0]
    for key in (
        "price",
        "name",
        "productName",
        "description",
        "imageUrl",
        "isCustomizable",
        "discounts",
        "productReplacement",
        "weighableInfo",
        "packaging",
    ):
        sparse_product.pop(key, None)
    parsed = remote.parse_remote_basket(sparse, intent)
    assert parsed.products[0].canonical_dict() == sparse_product
    assert parsed.products[0].name is None
    assert parsed.products[0].product_name is None

    for key in ("ids", "quantity", "customizations"):
        missing = copy.deepcopy(sparse)
        missing["products"][0].pop(key)
        with pytest.raises(remote.BasketContractError):
            remote.parse_remote_basket(missing, intent)

    malformed_optional = copy.deepcopy(sparse)
    malformed_optional["products"][0]["price"] = {"arbitrary": True}
    with pytest.raises(remote.BasketContractError):
        remote.parse_remote_basket(malformed_optional, intent)


def test_create_serialization_exact_current_shapes_and_optional_provider_ids(
    live: dict[str, ModuleType],
) -> None:
    remote = live["ordering_remote_basket"]
    plain = remote.RemoteBasketProduct(product_id="plain-1", quantity=1)
    customized = remote.RemoteBasketProduct(
        product_id="product-1",
        external_id="external-1",
        legacy_id="product-1",
        store_product_id="store-product-1",
        quantity=3,
        customizations=(
            remote.RemoteCustomization(
                group_id="group-1",
                group_external_id="group-ext-1",
                group_position=0,
                attribute_id="attribute-1",
                attribute_external_id="attribute-ext-1",
                group_name="Preparation",
                attribute_name="Standard",
                quantity=remote.StructuredQuantity(2),
            ),
        ),
    )
    intent = remote.BasketIntent(
        "42", 71, 81, 4, "DELIVERY", (plain, customized)
    )
    assert intent.create_body() == {
        "products": [
            {
                "ids": {"id": "plain-1"},
                "quantity": {"increments": 1},
            },
            {
                "ids": {
                    "id": "product-1",
                    "legacyId": "product-1",
                    "externalId": "external-1",
                    "storeProductId": "store-product-1",
                },
                "quantity": {"increments": 3},
                "customizations": [
                    {
                        "ids": {
                            "groupLegacyId": "group-1",
                            "groupId": "group-1",
                            "groupExternalId": "group-ext-1",
                            "groupPosition": 0,
                            "legacyId": "attribute-1",
                            "externalId": "attribute-ext-1",
                        },
                        "name": "Preparation",
                        "quantity": {"increments": 2},
                        "customizationName": "Standard",
                        "groupName": "Preparation",
                    }
                ],
            },
        ],
        "storeId": 71,
        "storeAddressId": 81,
        "storeCategoryId": 4,
        "handlingStrategy": "DELIVERY",
    }


def test_response_rejects_obsolete_flat_products_and_stale_root_fields(
    live: dict[str, ModuleType],
) -> None:
    remote = live["ordering_remote_basket"]
    intent = remote.parse_basket_intent(intent_payload())
    stale_flat = basket_payload()
    stale_flat["products"] = [
        {
            "productId": "product-1",
            "externalId": "external-1",
            "storeProductId": "store-product-1",
            "quantity": 2,
            "customizations": [],
        }
    ]
    stale_suggestions = basket_payload()
    stale_suggestions["suggestions"] = stale_suggestions.pop("productSuggestions")
    missing_dh = basket_payload()
    missing_dh.pop("usingDhBasket")
    for payload in (stale_flat, stale_suggestions, missing_dh):
        with pytest.raises(remote.BasketContractError):
            remote.parse_remote_basket(payload, intent)


def test_response_current_nullable_fields_and_customer_union_are_typed(
    live: dict[str, ModuleType],
) -> None:
    remote = live["ordering_remote_basket"]
    intent = remote.parse_basket_intent(intent_payload())
    nullable = basket_payload()
    nullable["mbs"] = None
    nullable["isPrimeSubscriptionSimulated"] = None
    nullable["customerId"] = "42"
    parsed = remote.parse_remote_basket(nullable, intent)
    assert parsed.customer_id == "42"
    assert parsed.is_prime_subscription_simulated is None
    bad_mbs = basket_payload()
    bad_mbs["mbs"] = {"arbitrary": "shape"}
    with pytest.raises(remote.BasketContractError):
        remote.parse_remote_basket(bad_mbs, intent)


def test_remote_client_create_replace_quantity_delete_are_one_call_and_version_bound(
    live: dict[str, ModuleType],
) -> None:
    remote = live["ordering_remote_basket"]
    fixture = FixtureTransport()
    fixture.responses = [
        basket_payload(version="v1"),
        basket_payload(version="v2"),
        basket_payload(version="v3", quantity=3),
        None,
    ]
    invalidations: list[str] = []
    client = remote.RemoteBasketClient(
        session(live, fixture), invalidate_authority=lambda: invalidations.append("invalidated")
    )
    intent = remote.parse_basket_intent(intent_payload())
    created = run(client.async_create(intent))
    replaced = run(client.async_replace(created, intent.products))
    changed = run(
        client.async_change_quantity(
            replaced, basket_product_id="basket-product-1", increment=1, limit=10
        )
    )
    run(client.async_delete(changed, explicit_user_intent=True))
    assert [item[0] for item in fixture.calls] == ["POST", "PUT", "PATCH", "DELETE"]
    assert fixture.calls[0][3] == intent.create_body()
    expected_put = basket_payload(version="v1")
    expected_put["customerId"] = "42"
    assert fixture.calls[1][3] == expected_put
    assert fixture.calls[2][3] == {
        "handlingStrategy": "DELIVERY",
        "basketVersion": "v2",
        "products": [
            {"basketProductId": "basket-product-1", "quantity": 3}
        ],
    }
    assert fixture.calls[3][3] is None
    assert len(invalidations) == 4
    with pytest.raises(remote.BasketContractError):
        run(client.async_delete(changed, explicit_user_intent=False))
    assert len(fixture.calls) == 4


def test_replace_put_accepts_changed_request_product_without_fabricating_rich_fields(
    live: dict[str, ModuleType],
) -> None:
    remote = live["ordering_remote_basket"]
    intent = remote.parse_basket_intent(intent_payload())
    current = remote.parse_remote_basket(basket_payload(), intent)
    proposed = (replace(intent.products[0], quantity=3),)
    fixture = FixtureTransport()
    fixture.responses = [basket_payload(version="basket-v2", quantity=3)]
    client = remote.RemoteBasketClient(session(live, fixture))

    changed = run(client.async_replace(current, proposed))

    assert changed.basket_version == "basket-v2"
    assert len(fixture.calls) == 1
    assert fixture.calls[0][0:2] == (
        "PUT",
        "/v1/authenticated/customers/42/baskets/basket-private-1/products",
    )
    expected = basket_payload()
    expected["customerId"] = "42"
    expected["products"] = [proposed[0].canonical_dict()]
    assert fixture.calls[0][3] == expected
    assert set(fixture.calls[0][3]["products"][0]) == {
        "ids",
        "quantity",
        "customizations",
    }
    assert "price" not in fixture.calls[0][3]["products"][0]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 429])
def test_remote_4xx_is_provider_rejection_without_response_parser(
    live: dict[str, ModuleType], status: int
) -> None:
    remote = live["ordering_remote_basket"]
    api = live["api_session"]
    fixture = FixtureTransport()
    fixture.responses = [
        api.ApiSessionError(
            category="http",
            endpoint_family="basket",
            status=status,
            purpose=api.MutationPurpose.CREATE_BASKET,
        )
    ]
    client = remote.RemoteBasketClient(session(live, fixture))
    with pytest.raises(remote.RemoteBasketRejected) as raised:
        run(client.async_create(remote.parse_basket_intent(intent_payload())))
    assert raised.value.status == status
    assert raised.value.category == "provider_rejection"
    assert len(fixture.calls) == 1


@pytest.mark.parametrize(
    ("operation", "method"),
    [
        ("create", "POST"),
        ("replace", "PUT"),
        ("quantity", "PATCH"),
        ("delete", "DELETE"),
    ],
)
def test_every_remote_mutation_malformed_outcome_is_one_call_ambiguous(
    live: dict[str, ModuleType], operation: str, method: str
) -> None:
    remote = live["ordering_remote_basket"]
    fixture = FixtureTransport()
    fixture.responses = [{"malformed": True}]
    client = remote.RemoteBasketClient(session(live, fixture))
    intent = remote.parse_basket_intent(intent_payload())
    current = remote.parse_remote_basket(basket_payload(), intent)
    with pytest.raises(remote.RemoteBasketAmbiguous):
        if operation == "create":
            run(client.async_create(intent))
        elif operation == "replace":
            run(client.async_replace(current, intent.products))
        elif operation == "quantity":
            run(
                client.async_change_quantity(
                    current,
                    basket_product_id="basket-product-1",
                    increment=1,
                    limit=10,
                )
            )
        else:
            run(client.async_delete(current, explicit_user_intent=True))
    assert len(fixture.calls) == 1
    assert fixture.calls[0][0] == method


@pytest.mark.parametrize(
    "failure",
    [TimeoutError("private"), ConnectionResetError("private"), RuntimeError("private")],
)
def test_ambiguous_mutation_never_retries_or_auto_deletes(
    live: dict[str, ModuleType], failure: BaseException
) -> None:
    remote = live["ordering_remote_basket"]
    fixture = FixtureTransport()
    fixture.responses = [failure]
    client = remote.RemoteBasketClient(session(live, fixture))
    with pytest.raises(remote.RemoteBasketAmbiguous) as raised:
        run(client.async_create(remote.parse_basket_intent(intent_payload())))
    assert len(fixture.calls) == 1
    assert fixture.calls[0][0] == "POST"
    assert "private" not in str(raised.value).lower()


def test_malformed_mutation_response_is_ambiguous_and_reconciliation_is_explicit_get_only(
    live: dict[str, ModuleType],
) -> None:
    remote = live["ordering_remote_basket"]
    fixture = FixtureTransport()
    fixture.responses = [{"malformed": True}, basket_payload(version="expected-v2")]
    client = remote.RemoteBasketClient(session(live, fixture))
    intent = remote.parse_basket_intent(intent_payload())
    with pytest.raises(remote.RemoteBasketAmbiguous) as info:
        run(client.async_create(intent))
    assert [call[0] for call in fixture.calls] == ["POST"]
    expected = remote.ReconciliationExpectation(
        purpose=live["api_session"].MutationPurpose.CREATE_BASKET,
        intent=intent,
        basket_id="basket-private-1",
        basket_version="expected-v2",
        products=intent.products,
        deleted=False,
    )
    result = run(client.async_reconcile_ambiguous(info.value, expected))
    assert result.snapshot is not None and result.proven is True
    assert [call[0] for call in fixture.calls] == ["POST", "GET"]


def test_reconciliation_mismatch_or_get_failure_stays_ambiguous_and_never_mutates(
    live: dict[str, ModuleType],
) -> None:
    remote = live["ordering_remote_basket"]
    api = live["api_session"]
    intent = remote.parse_basket_intent(intent_payload())
    ambiguity = remote.RemoteBasketAmbiguous(api.MutationPurpose.CREATE_BASKET)
    expected = remote.ReconciliationExpectation(
        purpose=api.MutationPurpose.CREATE_BASKET,
        intent=intent,
        basket_id="basket-private-1",
        basket_version="expected-v2",
        products=intent.products,
        deleted=False,
    )
    for response in (basket_payload(version="other-v"), TimeoutError("private")):
        fixture = FixtureTransport()
        fixture.responses = [response]
        client = remote.RemoteBasketClient(session(live, fixture))
        with pytest.raises(remote.RemoteBasketAmbiguous):
            run(client.async_reconcile_ambiguous(ambiguity, expected))
        assert [item[0] for item in fixture.calls] == ["GET"]


def test_quote_request_is_exact_private_and_uses_canonical_basket_address_payment(
    live: dict[str, ModuleType],
) -> None:
    request = make_request(live)
    body = request.private_body()
    assert set(body) == {"checkout"}
    checkout = body["checkout"]
    assert set(checkout) == {"orderDetails", "components", "analytics", "basketDetails"}
    assert checkout["orderDetails"]["basketId"] == "basket-private-1"
    assert checkout["orderDetails"]["handlingStrategy"] == {"type": "DELIVERY"}
    assert checkout["components"]["productList"] == [rich_product()]
    assert checkout["components"]["deliveryAddress"]["id"] == 17
    assert checkout["components"]["paymentMethod"]["paymentInstrumentId"] == "instrument-private"
    assert checkout["analytics"] == {"templateReceived": None}
    assert "Private Street" not in repr(request)


def test_authoritative_quote_exact_envelope_types_cross_checks_and_one_total(
    live: dict[str, ModuleType],
) -> None:
    quote = live["ordering_live_quote"]
    clock = Clock()
    request = make_request(live)
    parsed = quote.parse_quote_template(quote_response(), request=request, received_at=clock())
    assert type(parsed.version_id) is int
    assert type(parsed.basket_version) is str
    assert parsed.total.amount_minor == 560000
    assert parsed.is_fresh(clock())
    assert "checkout-private" not in repr(parsed)
    public = parsed.public_confirmation()
    encoded = json.dumps(public, ensure_ascii=False)
    for forbidden in (
        "checkout-private",
        "basket-private",
        "instrument-private",
        "Private Street",
        "latitude",
        "longitude",
        "fingerprint",
        "generation",
    ):
        assert forbidden not in encoded
    assert public["priceLines"][2]["value"] == "intentionally not parsed"
    assert public["purchaseTotalCents"] == 560000


@pytest.mark.parametrize(
    "mutation",
    [
        "bad_envelope",
        "disabled",
        "bool_version",
        "mismatch_store",
        "mismatch_address",
        "mismatch_basket",
        "mismatch_version",
        "picker_total",
        "picker_currency",
        "legal",
        "duplicate_component",
        "unknown_component",
        "no_total",
        "two_totals",
        "action",
        "unknown_line_key",
        "private_display",
    ],
)
def test_authoritative_quote_fail_closed_matrix(live: dict[str, ModuleType], mutation: str) -> None:
    quote = live["ordering_live_quote"]
    request = make_request(live)
    payload = quote_response()
    checkout = payload["response"]["data"]["checkout"]
    details = checkout["orderDetails"]
    components = checkout["components"]
    lines = components[1]["data"]["priceBreakdownData"]["breakDown"]
    if mutation == "bad_envelope":
        payload = {"data": {"checkout": checkout}}
    elif mutation == "disabled":
        checkout["enabled"] = False
    elif mutation == "bool_version":
        details["versionId"] = True
    elif mutation == "mismatch_store":
        details["storeId"] = 72
    elif mutation == "mismatch_address":
        details["storeAddressId"] = 82
    elif mutation == "mismatch_basket":
        details["basketId"] = "other"
    elif mutation == "mismatch_version":
        details["basketVersion"] = "other"
    elif mutation == "picker_total":
        components[0]["data"]["paymentMethodPickerData"]["orderTotal"] += 1
    elif mutation == "picker_currency":
        components[0]["data"]["paymentMethodPickerData"]["currencyCode"] = "USD"
    elif mutation == "legal":
        details["legalVerificationRequired"] = True
    elif mutation == "duplicate_component":
        components.append(copy.deepcopy(components[0]))
    elif mutation == "unknown_component":
        components.append({"type": "TIP_PICKER", "data": {}})
    elif mutation == "no_total":
        lines[2]["type"] = "OTHER"
    elif mutation == "two_totals":
        lines[0]["type"] = "TOTAL"
    elif mutation == "action":
        lines[2]["actions"] = [{"type": "POST"}]
    elif mutation == "unknown_line_key":
        lines[0]["amount"] = 550000
    elif mutation == "private_display":
        lines[0]["title"] = "checkoutSessionId=checkout-private-1"
    with pytest.raises(quote.QuoteContractError):
        quote.parse_quote_template(payload, request=request, received_at=500.0)


def test_price_formatted_lines_are_never_parsed_or_summed(live: dict[str, ModuleType]) -> None:
    payload = quote_response()
    lines = payload["response"]["data"]["checkout"]["components"][1]["data"][
        "priceBreakdownData"
    ]["breakDown"]
    lines[0]["value"] = "not money"
    lines[1]["value"] = None
    lines[2]["value"] = "does not equal numeric total"
    parsed = live["ordering_live_quote"].parse_quote_template(
        payload, request=make_request(live), received_at=500.0
    )
    assert [line.value for line in parsed.price_lines] == [
        "not money",
        None,
        "does not equal numeric total",
    ]


def test_quote_client_posts_once_and_malformed_or_transport_is_never_replayed(
    live: dict[str, ModuleType],
) -> None:
    quote = live["ordering_live_quote"]
    for response in (quote_response(), {"malformed": True}, TimeoutError("private")):
        fixture = FixtureTransport()
        fixture.responses = [response]
        authority = quote.AuthoritativeConfirmationManager(clock=Clock())
        client = quote.QuoteTemplateClient(
            session(live, fixture), clock=Clock(), invalidate_authority=authority.invalidate_all
        )
        if response == quote_response():
            run(client.async_create(make_request(live)))
        else:
            with pytest.raises(quote.QuoteTemplateAmbiguous):
                run(client.async_create(make_request(live)))
        assert len(fixture.calls) == 1
        assert fixture.calls[0][:2] == ("POST", "/v3/checkouts/order/1/template")


def test_quote_45_second_monotonic_boundary(live: dict[str, ModuleType]) -> None:
    quote = live["ordering_live_quote"]
    parsed = quote.parse_quote_template(
        quote_response(), request=make_request(live), received_at=500.0
    )
    assert parsed.is_fresh(544.999999)
    assert not parsed.is_fresh(545.0)
    assert not parsed.is_fresh(545.000001)
    for bad in (math.nan, math.inf, True):
        with pytest.raises(quote.QuoteContractError):
            parsed.is_fresh(bad)


def test_confirmation_single_use_new_template_and_exact_total_invalidation(
    live: dict[str, ModuleType],
) -> None:
    quote = live["ordering_live_quote"]
    clock = Clock()
    source = iter(("challenge-local-1-xxxxxxxxxxxxxxxx", "challenge-local-2-xxxxxxxxxxxxxxxx"))
    manager = quote.AuthoritativeConfirmationManager(
        clock=clock, challenge_source=lambda: next(source)
    )
    request = make_request(live)
    first = quote.parse_quote_template(quote_response(), request=request, received_at=clock())
    manager.install(first)
    prepared = manager.prepare(owner_key="admin-a")
    assert prepared["purchaseTotalCents"] == 560000
    assert manager.consume(
        owner_key="admin-a", challenge=prepared["challenge"], current=first
    ) is first
    with pytest.raises(quote.InvalidQuoteConfirmation):
        manager.consume(owner_key="admin-a", challenge=prepared["challenge"], current=first)

    manager.install(first)
    old = manager.prepare(owner_key="admin-a")
    changed_payload = quote_response(order_details={"purchaseTotalCents": 560001})
    changed_payload["response"]["data"]["checkout"]["components"][0]["data"][
        "paymentMethodPickerData"
    ]["orderTotal"] = 560001
    changed = quote.parse_quote_template(changed_payload, request=request, received_at=clock())
    manager.install(changed)
    with pytest.raises(quote.InvalidQuoteConfirmation):
        manager.consume(owner_key="admin-a", challenge=old["challenge"], current=changed)


def test_confirmation_fingerprint_change_matrix_and_rejection_consumes_challenge(
    live: dict[str, ModuleType],
) -> None:
    quote = live["ordering_live_quote"]
    clock = Clock()
    request = make_request(live)
    original = quote.parse_quote_template(quote_response(), request=request, received_at=clock())
    changes = (
        replace(original, checkout_session_id="checkout-private-2"),
        replace(original, version_id=4),
        replace(original, template_id=10),
        replace(original, basket_id="basket-private-2"),
        replace(original, basket_version="changed"),
        replace(original, store_id=72),
        replace(original, store_address_id=82),
        replace(original, exact_products=tuple()),
        replace(original, address_fingerprint="0" * 64),
        replace(original, payment_fingerprint="1" * 64),
        replace(original, total=live["ordering_models"].Money(560001, "AMD")),
        replace(original, total=live["ordering_models"].Money(560000, "USD")),
        replace(original, price_lines=tuple(reversed(original.price_lines))),
        replace(original, eta="changed"),
        replace(original, capability_fingerprint="2" * 64),
        replace(original, generation=8),
        replace(original, owner_key="admin-b"),
        replace(original, intent_key="intent-local-2"),
    )
    for index, changed in enumerate(changes):
        manager = quote.AuthoritativeConfirmationManager(
            clock=clock,
            challenge_source=lambda index=index: f"challenge-{index:02d}-xxxxxxxxxxxxxxxxxxxxxxxx",
        )
        manager.install(original)
        prepared = manager.prepare(owner_key="admin-a")
        with pytest.raises(quote.InvalidQuoteConfirmation):
            manager.consume(
                owner_key="admin-a", challenge=prepared["challenge"], current=changed
            )
        with pytest.raises(quote.InvalidQuoteConfirmation):
            manager.consume(
                owner_key="admin-a", challenge=prepared["challenge"], current=original
            )


def test_confirmation_age_equal_boundary_invalidates_and_consumes(live: dict[str, ModuleType]) -> None:
    quote = live["ordering_live_quote"]
    clock = Clock()
    authority = quote.parse_quote_template(
        quote_response(), request=make_request(live), received_at=clock()
    )
    manager = quote.AuthoritativeConfirmationManager(
        clock=clock, challenge_source=lambda: "challenge-expiry-xxxxxxxxxxxxxxxxxxxxxxxx"
    )
    manager.install(authority)
    prepared = manager.prepare(owner_key="admin-a")
    clock.value += quote.QUOTE_MAX_AGE
    with pytest.raises(quote.InvalidQuoteConfirmation):
        manager.consume(owner_key="admin-a", challenge=prepared["challenge"], current=authority)
    with pytest.raises(quote.InvalidQuoteConfirmation):
        manager.consume(owner_key="admin-a", challenge=prepared["challenge"], current=authority)


def test_added_sources_have_private_auxiliary_capability_only() -> None:
    added = ("ordering_remote_basket", "ordering_live_quote")
    source = "\n".join((GLOVO_ROOT / f"{name}.py").read_text().lower() for name in added)
    for prohibited in (
        "/v3/checkouts/order/1\"",
        "/v3/checkouts/order/1'",
        "payment/status",
        "payment/complete",
        "cancel_order",
        "product-view",
        "websocket",
        "mqtt",
        "webhook",
        "automation",
        "intent_handler",
        "service.register",
        "event.fire",
    ):
        assert prohibited not in source


def test_integration_runtime_wires_reviewed_final_checkout_only_through_strict_seam() -> None:
    """Production composition wires only the reviewed final adapter/session seam."""
    runtime = (GLOVO_ROOT / "__init__.py").read_text()
    for required in (
        "ordering_remote_basket",
        "ordering_live_quote",
        "ordering_live_checkout",
        "ProductionFinalCheckoutAdapter",
        "FinalCheckoutRequest.from_quote",
        "mutation_transport=",
        "single_attempt_authed_phase_mutation",
    ):
        assert required in runtime
    for prohibited in (
        "FixtureOnlyFinalCheckoutAdapter",
        "MockCheckoutAdapter",
        "SyntheticCatalogProvider",
    ):
        assert prohibited not in runtime
