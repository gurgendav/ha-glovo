"""Strict offline tests for GET-only live account/catalog clients."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import math
import socket
import sys
from collections.abc import Iterator
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
    "ordering_account",
    "ordering_live_catalog",
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def socket_guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    attempts: list[str] = []

    def blocked(*args: Any, **kwargs: Any) -> Any:
        attempts.append("blocked")
        pytest.fail("live GET client test attempted outbound network access")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    yield
    assert attempts == []


@pytest.fixture()
def live() -> dict[str, ModuleType]:
    package_name = "glovo_live_get_under_test"
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
    value = 1_800_000_000.0

    def __call__(self) -> float:
        return self.value


class FixtureTransport:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def __call__(
        self, method: str, access_token: str, path: str, query: dict[str, str]
    ) -> Any:
        assert method == "GET"
        assert access_token.startswith("access-")
        self.calls.append((method, path, query))
        response = self.responses[path]
        if isinstance(response, BaseException):
            raise response
        return copy.deepcopy(response)


class SessionHarness:
    def __init__(self, live: dict[str, ModuleType], responses: dict[str, Any]) -> None:
        self.tokens = json.dumps(
            {"access_token": "access-old", "refresh_token": "refresh-old", "expires_at": 2_000_000_000}
        )
        self.transport = FixtureTransport(responses)
        self.persisted: list[str] = []
        session = live["api_session"].SerializedApiSession(
            token_source=lambda: self.tokens,
            persist_token=self.persist,
            ensure_token=self.ensure,
            transport=self.transport,
        )
        self.session = session

    async def persist(self, token: str) -> None:
        self.tokens = token
        self.persisted.append(token)

    @staticmethod
    def ensure(token_json: str) -> tuple[str, str]:
        return json.loads(token_json)["access_token"], token_json


def address_payload() -> dict[str, Any]:
    return {
        "data": {
            "data": {
                "addresses": [
                    {
                        "entryType": "SAVED_ADDRESS",
                        "entry": {
                            "address": {
                                "id": 17,
                                "addressLine": "Private Street 10",
                                "details": "Private details",
                                "latitude": 40.177,
                                "longitude": 44.513,
                                "countryCode": "AM",
                                "cityCode": "YRV",
                                "cityName": "Yerevan",
                                "kind": "APARTMENT",
                                "tag": "Home",
                                "fields": [
                                    {"type": "STREET_NAME", "value": "Private Street"},
                                    {"type": "STREET_NUMBER", "value": "10"},
                                ],
                            }
                        },
                    }
                ]
            }
        }
    }


def payment_payload() -> dict[str, Any]:
    return {
        "data": {
            "data": {
                "paymentMethods": [
                    {
                        "type": "CREDIT_CARD",
                        "paymentInstrumentId": "instrument-private",
                        "selected": True,
                        "metadata": {"id": 33, "lastFourDigits": "4242"},
                        "display": {"name": "Visa", "description": "Card ending 4242"},
                    }
                ],
                "actions": [],
            }
        }
    }


def store_payload() -> dict[str, Any]:
    return {
        "id": 71,
        "name": "Fixture Kitchen",
        "slug": "fixture-kitchen",
        "open": True,
        "rating": 4.7,
        "filters": ["MEALS"],
        "categoryId": 4,
        "category": "RESTAURANT",
        "addressId": 81,
        "cityCode": "YRV",
        "enabled": True,
        "primeAvailable": False,
        "imageId": "store-image",
        "viewType": "LIST_VIEW",
        "schedule": [{"day": "MONDAY", "openingTime": "09:00", "closingTime": "22:00"}],
        "schedulingEnabled": True,
        "nextOpening": None,
        "deliveryFeeInfo": {"fee": {"amount": 10000, "currency": "AMD"}},
        "serviceFee": {"amount": 5000, "currency": "AMD"},
        "itemsType": "FOOD",
    }


def menu_payload() -> dict[str, Any]:
    return {
        "type": "LIST_VIEW_LAYOUT",
        "data": {
            "storeAddressId": 81,
            "body": [
                {"type": "SECTION_HEADER", "data": {"title": "Meals"}},
                {
                    "type": "PRODUCT_ROW",
                    "data": {
                        "id": "product-1",
                        "externalId": "external-1",
                        "storeProductId": "store-product-1",
                        "name": "Fixture meal",
                        "price": {"amount": 550000, "currency": "AMD"},
                        "sponsored": False,
                        "requiresProductView": False,
                        "attributes": {
                            "directlyOrderable": True,
                            "customizationComplete": True,
                            "variableWeight": False,
                            "openPrice": False,
                            "substitutionsAllowed": False,
                            "freeFormInstructions": False,
                            "legalVerificationRequired": False,
                        },
                        "promotions": [
                            {"id": "promo-1", "type": "PERCENTAGE", "label": "Fixture offer"}
                        ],
                        "optionGroups": [
                            {
                                "id": "group-1",
                                "externalId": "group-ext-1",
                                "name": "Choose one",
                                "min": 1,
                                "max": 1,
                                "position": 0,
                                "multipleSelection": False,
                                "collapsed": False,
                                "attributes": [
                                    {
                                        "id": "option-1",
                                        "externalId": "option-ext-1",
                                        "name": "Standard",
                                        "priceImpact": {"amount": 0, "currency": "AMD"},
                                        "selected": True,
                                    }
                                ],
                            }
                        ],
                    },
                },
            ],
        },
    }


def test_me_customer_id_is_response_derived_positive_strict_int(live: dict[str, ModuleType]) -> None:
    parser = live["ordering_contracts"].parse_customer
    assert parser({"id": 42}).customer_id == 42
    assert "42" not in repr(parser({"id": 42}))
    for bad in ({}, {"id": True}, {"id": 0}, {"id": -1}, {"id": "42"}, {"id": 1, "email": "private"}):
        with pytest.raises(live["ordering_contracts"].ContractError):
            parser(bad)


def test_all_contracts_accept_minimal_forms_and_reject_missing_wrong_types(
    live: dict[str, ModuleType],
) -> None:
    contracts = live["ordering_contracts"]

    minimal_address = address_payload()
    address = minimal_address["data"]["data"]["addresses"][0]["entry"]["address"]
    address["tag"] = None
    address["fields"] = []
    address["details"] = ""
    assert len(contracts.parse_saved_addresses(minimal_address)) == 1

    minimal_payment = payment_payload()
    minimal_payment["data"]["data"]["paymentMethods"][0]["metadata"].pop(
        "lastFourDigits"
    )
    assert contracts.parse_saved_payments(minimal_payment)[0].last_four_digits is None

    minimal_store = store_payload()
    minimal_store["rating"] = None
    minimal_store["schedule"] = []
    assert contracts.parse_store(minimal_store).rating is None

    minimal_menu = menu_payload()
    minimal_menu["type"] = "GRID_VIEW_LAYOUT"
    minimal_menu["data"]["body"] = [minimal_menu["data"]["body"][1]]
    product = minimal_menu["data"]["body"][0]["data"]
    product["storeProductId"] = None
    product["promotions"] = []
    product["optionGroups"] = []
    assert len(contracts.parse_menu(minimal_menu, expected_store_address_id=81).products) == 1

    malformed: tuple[tuple[Any, Any], ...] = (
        (contracts.parse_saved_addresses, {"data": {"data": {}}}),
        (
            contracts.parse_saved_addresses,
            {"data": {"data": {"addresses": "not-an-array"}}},
        ),
        (contracts.parse_saved_payments, {"data": {"data": {"paymentMethods": []}}}),
        (
            contracts.parse_saved_payments,
            {"data": {"data": {"paymentMethods": [], "actions": False}}},
        ),
        (contracts.parse_store, {"id": 1}),
    )
    for parser, payload in malformed:
        with pytest.raises(contracts.ContractError):
            parser(payload)

    wrong_payment = payment_payload()
    wrong_payment["data"]["data"]["paymentMethods"][0]["metadata"]["id"] = True
    with pytest.raises(contracts.ContractError):
        contracts.parse_saved_payments(wrong_payment)
    wrong_store = store_payload()
    wrong_store["categoryId"] = True
    with pytest.raises(contracts.ContractError):
        contracts.parse_store(wrong_store)
    wrong_menu = menu_payload()
    wrong_menu["data"]["body"][1]["data"]["optionGroups"][0]["min"] = True
    with pytest.raises(contracts.ContractError):
        contracts.parse_menu(wrong_menu, expected_store_address_id=81)


def test_address_parser_strict_maximal_privacy_fingerprint_and_bounds(live: dict[str, ModuleType]) -> None:
    contracts = live["ordering_contracts"]
    parsed = contracts.parse_saved_addresses(address_payload())
    assert len(parsed) == 1
    snapshot = parsed[0]
    assert "Private" not in repr(snapshot)
    assert snapshot.canonical_fingerprint == snapshot.canonical_fingerprint
    assert contracts.address_snapshots_equal(snapshot, copy.deepcopy(snapshot))

    mutations = []
    for path, value in (
        (("id",), True),
        (("latitude",), math.nan),
        (("kind",), "CASTLE"),
        (("fields",), [{"type": "UNKNOWN", "value": "x"}]),
        (("fields",), [{"type": "STREET_NAME", "value": "x", "extra": 1}]),
    ):
        payload = address_payload()
        address = payload["data"]["data"]["addresses"][0]["entry"]["address"]
        address[path[0]] = value
        mutations.append(payload)
    duplicate = address_payload()
    duplicate["data"]["data"]["addresses"].append(
        copy.deepcopy(duplicate["data"]["data"]["addresses"][0])
    )
    mutations.append(duplicate)
    oversized = address_payload()
    oversized["data"]["data"]["addresses"][0]["entry"]["address"]["addressLine"] = "x" * 501
    mutations.append(oversized)
    for payload in mutations:
        with pytest.raises(contracts.ContractError):
            contracts.parse_saved_addresses(payload)


def test_address_handles_are_random_owner_generation_ttl_bound_and_revalidated(
    live: dict[str, ModuleType],
) -> None:
    path = "/customer_profile/api/v1/address_book/me/addresses"
    clock = Clock()
    harness = SessionHarness(live, {path: address_payload()})
    client = live["ordering_account"].AccountClient(harness.session, clock=clock)
    first = run(client.async_saved_addresses(owner_key="admin-a", generation=9))[0]
    second = run(client.async_saved_addresses(owner_key="admin-a", generation=9))[0]
    assert first.public_dict().keys() == {"key", "label"}
    assert first.selection_key != second.selection_key
    encoded = json.dumps(first.public_dict())
    assert "Private" not in encoded and "17" not in encoded
    resolved = client.resolve_address(first.selection_key, owner_key="admin-a", generation=9)
    assert resolved.remote_id == 17
    for owner, generation in (("admin-b", 9), ("admin-a", 10)):
        with pytest.raises(live["ordering_account"].InvalidSelection):
            client.resolve_address(first.selection_key, owner_key=owner, generation=generation)
    clock.value += client.selection_ttl_seconds + 1
    with pytest.raises(live["ordering_account"].InvalidSelection):
        client.resolve_address(first.selection_key, owner_key="admin-a", generation=9)

    clock.value -= client.selection_ttl_seconds + 1
    current = run(client.async_saved_addresses(owner_key="admin-a", generation=9))[0]
    assert run(client.async_revalidate_address(current.selection_key, owner_key="admin-a", generation=9))
    changed = address_payload()
    changed["data"]["data"]["addresses"][0]["entry"]["address"]["details"] = "changed"
    harness.transport.responses[path] = changed
    assert not run(client.async_revalidate_address(current.selection_key, owner_key="admin-a", generation=9))
    client.invalidate()
    with pytest.raises(live["ordering_account"].InvalidSelection):
        client.resolve_address(current.selection_key, owner_key="admin-a", generation=9)


def test_payment_query_is_bounded_exact_and_saved_card_only(live: dict[str, ModuleType]) -> None:
    contracts = live["ordering_contracts"]
    assert contracts.build_payment_query(amount_minor=1250, currency="AMD") == {
        "amount": "1250",
        "currency": "AMD",
        "context": "checkout",
    }
    query = contracts.build_payment_query(
        amount_minor=1250,
        currency="AMD",
        checkout_session="session-1",
        store_address_id=81,
        client_supports=("CREDIT_CARD",),
        client_ready=True,
    )
    assert set(query) == {
        "amount", "currency", "context", "checkoutSessionId", "storeAddressId",
        "clientSupports", "clientReady",
    }
    for kwargs in (
        {"amount_minor": True, "currency": "AMD"},
        {"amount_minor": -1, "currency": "AMD"},
        {"amount_minor": 1, "currency": "ZZZ"},
        {"amount_minor": 1, "currency": "AMD", "store_address_id": True},
        {"amount_minor": 1, "currency": "AMD", "client_supports": ("CASH",)},
        {"amount_minor": 1, "currency": "AMD", "checkout_session": "x" * 300},
    ):
        with pytest.raises(contracts.ContractError):
            contracts.build_payment_query(**kwargs)

    parsed = contracts.parse_saved_payments(payment_payload())
    assert len(parsed) == 1 and "instrument-private" not in repr(parsed[0])
    for method_type in ("CASH", "ALTERNATIVE", "PAYPAL", "UNKNOWN"):
        payload = payment_payload()
        payload["data"]["data"]["paymentMethods"][0]["type"] = method_type
        with pytest.raises(contracts.ContractError):
            contracts.parse_saved_payments(payload)
    for mutation in ("actions", "raw"):
        payload = payment_payload()
        if mutation == "actions":
            payload["data"]["data"]["actions"] = [{"type": "ADD_CARD"}]
        else:
            payload["data"]["data"]["paymentMethods"][0]["cardNumber"] = "4111111111111111"
        with pytest.raises(contracts.ContractError):
            contracts.parse_saved_payments(payload)


def test_payment_public_selection_is_masked_private_and_owned(live: dict[str, ModuleType]) -> None:
    path = "/v4/payment_methods"
    harness = SessionHarness(live, {path: payment_payload()})
    client = live["ordering_account"].AccountClient(harness.session, clock=Clock())
    public = run(
        client.async_saved_payments(
            owner_key="admin-a", generation=3, amount_minor=550000, currency="AMD"
        )
    )[0]
    assert set(public.public_dict()) == {"key", "label"}
    assert "4242" in public.masked_label
    assert "instrument-private" not in json.dumps(public.public_dict())
    private = client.resolve_payment(public.selection_key, owner_key="admin-a", generation=3)
    assert private.payment_instrument_id == "instrument-private"
    with pytest.raises(live["ordering_account"].InvalidSelection):
        client.resolve_payment(public.selection_key, owner_key="admin-b", generation=3)


def test_store_parser_eligibility_cost_enum_duplicates_and_strict_types(live: dict[str, ModuleType]) -> None:
    contracts = live["ordering_contracts"]
    store = contracts.parse_store(store_payload())
    assert store.store_id == 71 and store.address_id == 81
    for key, value in (
        ("id", True), ("open", False), ("enabled", False), ("viewType", "UNKNOWN"),
        ("itemsType", "PHARMACY"), ("rating", math.inf), ("serviceFee", {"amount": -1, "currency": "AMD"}),
    ):
        payload = store_payload()
        payload[key] = value
        with pytest.raises(contracts.ContractError):
            contracts.parse_store(payload)
    duplicate = store_payload()
    duplicate["filters"] = ["MEALS", "MEALS"]
    with pytest.raises(contracts.ContractError):
        contracts.parse_store(duplicate)


def test_menu_parser_direct_products_options_promotions_and_fail_closed_cases(
    live: dict[str, ModuleType],
) -> None:
    contracts = live["ordering_contracts"]
    menu = contracts.parse_menu(menu_payload(), expected_store_address_id=81)
    assert len(menu.products) == 1
    product = menu.products[0]
    assert product.product_id == "product-1"
    assert set(product.public_dict()) == {"key", "label", "priceCents", "currencyCode", "sponsored", "optionGroups"}

    mutations: list[dict[str, Any]] = []
    for field, value in (
        ("requiresProductView", True),
        ("price", {"amount": math.nan, "currency": "AMD"}),
        ("sponsored", 1),
    ):
        payload = menu_payload()
        payload["data"]["body"][1]["data"][field] = value
        mutations.append(payload)
    for attr in (
        "variableWeight", "openPrice", "substitutionsAllowed", "freeFormInstructions",
        "legalVerificationRequired",
    ):
        payload = menu_payload()
        payload["data"]["body"][1]["data"]["attributes"][attr] = True
        mutations.append(payload)
    unknown = menu_payload()
    unknown["data"]["body"][1]["data"]["unknownOrderAction"] = {"type": "POST"}
    mutations.append(unknown)
    duplicate = menu_payload()
    duplicate["data"]["body"].append(copy.deepcopy(duplicate["data"]["body"][1]))
    mutations.append(duplicate)
    bad_option = menu_payload()
    bad_option["data"]["body"][1]["data"]["optionGroups"][0]["max"] = 2
    mutations.append(bad_option)
    wrong_address = menu_payload()
    wrong_address["data"]["storeAddressId"] = 82
    mutations.append(wrong_address)
    unknown_element = menu_payload()
    unknown_element["data"]["body"].append({"type": "PURCHASE_ACTION", "data": {}})
    mutations.append(unknown_element)
    deep = menu_payload()
    nested: dict[str, Any] = {}
    for _ in range(20):
        nested = {"x": nested}
    deep["data"]["body"][0]["data"]["nested"] = nested
    mutations.append(deep)
    oversized = menu_payload()
    oversized["data"]["body"] *= 300
    mutations.append(oversized)
    for payload in mutations:
        with pytest.raises(contracts.ContractError):
            contracts.parse_menu(payload, expected_store_address_id=81)


def test_catalog_client_uses_preferred_get_and_narrow_legacy_fallback(live: dict[str, ModuleType]) -> None:
    api_error = live["api_session"].ApiSessionError
    preferred = "/v4/stores/71/addresses/81/content/main"
    legacy_path = "/v3/stores/71/addresses/81/node/store_menu"
    store_path = "/v3/stores/fixture-kitchen"
    harness = SessionHarness(
        live,
        {store_path: store_payload(), preferred: menu_payload(), legacy_path: menu_payload()},
    )
    client = live["ordering_live_catalog"].LiveCatalogClient(harness.session)
    store = run(client.async_store("fixture-kitchen"))
    menu = run(client.async_menu(store))
    assert menu.products[0].product_id == "product-1"
    assert [call[1] for call in harness.transport.calls] == [store_path, preferred]
    assert harness.transport.calls[0][2] == {"includeClosed": "true", "includeDisabled": "false"}

    for category, fallback_expected in (("not_found", True), ("unsupported", True), ("auth", False), ("transport", False), ("schema", False)):
        harness.transport.calls.clear()
        harness.transport.responses[preferred] = api_error(category=category, status=404, endpoint_family="catalog")
        if fallback_expected:
            assert run(client.async_menu(store)).products
            assert [call[1] for call in harness.transport.calls] == [preferred, legacy_path]
        else:
            with pytest.raises(api_error):
                run(client.async_menu(store))
            assert [call[1] for call in harness.transport.calls] == [preferred]

    harness.transport.calls.clear()
    harness.transport.responses[preferred] = {"error": {"code": "UNSUPPORTED_ENDPOINT"}}
    assert run(client.async_menu(store)).products
    assert [call[1] for call in harness.transport.calls] == [preferred, legacy_path]

    harness.transport.calls.clear()
    harness.transport.responses[preferred] = {"error": {"code": "UNKNOWN", "detail": "raw"}}
    with pytest.raises(live["ordering_contracts"].ContractError):
        run(client.async_menu(store))
    assert [call[1] for call in harness.transport.calls] == [preferred]


def test_session_serializes_refresh_once_persists_before_reads_and_no_retry(
    live: dict[str, ModuleType],
) -> None:
    session_module = live["api_session"]

    async def scenario() -> tuple[list[Any], list[str], list[str]]:
        token = json.dumps({"access_token": "old", "refresh_token": "rotating", "expires_at": 0})
        persisted: list[str] = []
        events: list[str] = []
        refresh_count = 0
        entered = asyncio.Event()
        release = asyncio.Event()

        async def ensure(value: str) -> tuple[str, str]:
            nonlocal refresh_count
            state = json.loads(value)
            if state["expires_at"] == 0:
                refresh_count += 1
                events.append("refresh")
                entered.set()
                await release.wait()
                state = {"access_token": "access-new", "refresh_token": "rotated", "expires_at": 2_000_000_000}
            return state["access_token"], json.dumps(state)

        async def persist(value: str) -> None:
            nonlocal token
            events.append("persist")
            token = value
            persisted.append(value)

        async def transport(method: str, access: str, path: str, query: dict[str, str]) -> Any:
            events.append(f"read:{path}")
            return {"ok": True}

        session = session_module.SerializedApiSession(
            token_source=lambda: token,
            persist_token=persist,
            ensure_token=ensure,
            transport=transport,
        )
        first = asyncio.create_task(session.async_get("account", "/v3/me"))
        await entered.wait()
        second = asyncio.create_task(session.async_get("address", "/customer_profile/api/v1/address_book/me/addresses"))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second)
        assert refresh_count == 1
        return results, events, persisted

    results, events, persisted = run(scenario())
    assert results == [{"ok": True}, {"ok": True}]
    assert len(persisted) == 1
    assert events.index("persist") < events.index("read:/v3/me")
    assert events.count("refresh") == 1


def test_simultaneous_legacy_and_new_read_share_rotating_token_authority(
    live: dict[str, ModuleType],
) -> None:
    session_module = live["api_session"]

    async def scenario() -> tuple[int, list[str]]:
        token = json.dumps(
            {"access_token": "access-old", "refresh_token": "rotating", "expires_at": 0}
        )
        refresh_count = 0
        events: list[str] = []
        legacy_entered = asyncio.Event()
        release_legacy = asyncio.Event()

        async def ensure(value: str) -> tuple[str, str]:
            nonlocal refresh_count
            state = json.loads(value)
            if state["expires_at"] == 0:
                refresh_count += 1
                state = {
                    "access_token": "access-new",
                    "refresh_token": "rotated",
                    "expires_at": 2_000_000_000,
                }
            return state["access_token"], json.dumps(state)

        async def persist(value: str) -> None:
            nonlocal token
            events.append("persist")
            token = value

        async def legacy(value: str) -> tuple[dict[str, bool], str]:
            access, updated = await ensure(value)
            assert access == "access-new"
            events.append("legacy-enter")
            legacy_entered.set()
            await release_legacy.wait()
            events.append("legacy-read")
            return {"legacy": True}, updated

        async def transport(method: str, access: str, path: str, query: dict[str, str]) -> Any:
            assert access == "access-new"
            events.append("new-read")
            return {"new": True}

        session = session_module.SerializedApiSession(
            token_source=lambda: token,
            persist_token=persist,
            ensure_token=ensure,
            transport=transport,
        )
        legacy_task = asyncio.create_task(session.async_legacy_read(legacy))
        await legacy_entered.wait()
        new_task = asyncio.create_task(session.async_get("account", "/v3/me"))
        await asyncio.sleep(0)
        assert "new-read" not in events
        release_legacy.set()
        assert await legacy_task == {"legacy": True}
        assert await new_task == {"new": True}
        return refresh_count, events

    refresh_count, events = run(scenario())
    assert refresh_count == 1
    assert events == ["legacy-enter", "legacy-read", "persist", "new-read"]


def test_session_persistence_failure_releases_no_stale_authority_and_errors_are_sanitized(
    live: dict[str, ModuleType],
) -> None:
    session_module = live["api_session"]
    reads = 0

    async def ensure(value: str) -> tuple[str, str]:
        return "access-private", '{"access_token":"access-private","refresh_token":"private"}'

    async def persist(value: str) -> None:
        raise OSError("private token path and raw body")

    async def transport(*args: Any) -> Any:
        nonlocal reads
        reads += 1
        return {}

    session = session_module.SerializedApiSession(
        token_source=lambda: '{"refresh_token":"private"}',
        persist_token=persist,
        ensure_token=ensure,
        transport=transport,
    )
    with pytest.raises(session_module.ApiSessionError) as raised:
        run(session.async_get("account", "/v3/me"))
    assert reads == 0
    with pytest.raises(session_module.ApiSessionError) as stale:
        run(session.async_get("account", "/v3/me"))
    assert stale.value.category == "auth"
    assert reads == 0
    text = repr(raised.value) + str(raised.value)
    for forbidden in ("private", "token path", "raw body", "/v3/me"):
        assert forbidden not in text.lower()
    assert raised.value.public_dict() == {
        "category": "persistence",
        "status": None,
        "endpointFamily": "account",
    }


def test_session_transport_is_get_allowlisted_bounded_and_single_attempt(live: dict[str, ModuleType]) -> None:
    session_module = live["api_session"]
    calls = 0

    async def transport(method: str, access: str, path: str, query: dict[str, str]) -> Any:
        nonlocal calls
        calls += 1
        raise TimeoutError("raw URL and identifier")

    session = session_module.SerializedApiSession(
        token_source=lambda: '{"access_token":"access-ok","refresh_token":"r","expires_at":2000000000}',
        persist_token=lambda value: None,
        ensure_token=lambda value: ("access-ok", value),
        transport=transport,
    )
    with pytest.raises(session_module.ApiSessionError) as raised:
        run(session.async_get("account", "/v3/me"))
    assert calls == 1
    assert raised.value.category == "transport"
    for family, path in (("payment", "/oauth/refresh"), ("catalog", "/v3/stores/x/addresses/1/node/basket")):
        with pytest.raises(session_module.ApiSessionError):
            run(session.async_get(family, path))
    assert calls == 1


def test_diagnostics_redact_new_private_provider_refs(live: dict[str, ModuleType]) -> None:
    models = live["ordering_models"]
    payload = {
        "paymentInstrumentId": "pi-private",
        "metadata": {"token": "meta-private"},
        "customerId": 11,
        "addressId": 12,
        "storeAddressId": 13,
        "storeId": 14,
        "sessionId": "session-private",
        "orderRef": "order-private",
        "safe": "ok",
    }
    redacted = models.redact_mapping(payload)
    encoded = json.dumps(redacted)
    assert "private" not in encoded and "11" not in encoded and "12" not in encoded
    assert redacted["safe"] == "ok"


def test_added_live_modules_have_no_mutation_or_public_purchase_seam() -> None:
    source = "\n".join((GLOVO_ROOT / f"{name}.py").read_text().lower() for name in MODULES)
    for prohibited in (
        "method=\"post\"", "method='post'", "method=\"put\"", "method='put'",
        "method=\"delete\"", "method='delete'", "place_order", "checkout/submit",
        "product-view", "product_view", "basket/mutate", "payment_intent",
        "websocket", "mqtt", "automation.", '"intent"',
    ):
        assert prohibited not in source
