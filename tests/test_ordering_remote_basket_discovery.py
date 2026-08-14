"""Offline fixture-ledger tests for bounded provider basket discovery."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import socket
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from test_ordering_remote_basket_quotes import basket_payload, intent_payload

ROOT = Path(__file__).parents[1]
GLOVO_ROOT = ROOT / "custom_components" / "glovo"
MODULES = (
    "api_session",
    "ordering_remote_basket",
    "ordering_remote_basket_discovery",
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def socket_guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    attempts: list[str] = []

    def blocked(*args: Any, **kwargs: Any) -> Any:
        attempts.append("blocked")
        pytest.fail("basket discovery test attempted external network access")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    yield
    assert attempts == []


@pytest.fixture()
def live() -> Iterator[dict[str, ModuleType]]:
    package_name = "glovo_basket_discovery_under_test"
    package = ModuleType(package_name)
    package.__path__ = [str(GLOVO_ROOT)]  # type: ignore[attr-defined]
    sys.modules[package_name] = package
    loaded: dict[str, ModuleType] = {}
    try:
        for name in MODULES:
            module_name = f"{package_name}.{name}"
            spec = importlib.util.spec_from_file_location(
                module_name, GLOVO_ROOT / f"{name}.py"
            )
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


class FixtureReadSession:
    """Read-only fixture seam with an exact family/path/query ledger."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, str], Any]] = []

    async def async_get(
        self,
        family: str,
        path: str,
        query: dict[str, str] | None = None,
        *,
        delivery_location: Any = None,
    ) -> Any:
        self.calls.append((family, path, dict(query or {}), delivery_location))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return copy.deepcopy(response)


def summary(
    *,
    basket_id: str = "basket-private-1",
    version: str = "basket-v1",
    customer_id: int | str = 42,
    store_id: int = 71,
    store_address_id: int = 81,
    store_category_id: int = 4,
    handling_strategy: str = "DELIVERY",
) -> dict[str, Any]:
    return {
        "basketId": basket_id,
        "basketVersion": version,
        "basketItems": 1,
        "basketPriceFormatted": "5,500.00 AMD",
        "customerId": customer_id,
        "deliveryFeeInfo": None,
        "distance": None,
        "eta": None,
        "handlingStrategy": handling_strategy,
        "outOfDeliveryArea": False,
        "storeAddressId": store_address_id,
        "storeCategoryId": store_category_id,
        "storeId": store_id,
        "storeImage": None,
        "storeName": "Fixture Kitchen",
        "updatedAt": "2026-08-14T12:00:00Z",
    }


def client_and_intent(
    live: dict[str, ModuleType], *responses: Any
) -> tuple[Any, Any, FixtureReadSession]:
    fixture = FixtureReadSession(*responses)
    remote = live["ordering_remote_basket"]
    discovery = live["ordering_remote_basket_discovery"]
    intent = remote.parse_basket_intent(intent_payload())
    return discovery.RemoteBasketDiscoveryClient(fixture), intent, fixture


def delivery_location(live: dict[str, ModuleType]) -> Any:
    return live["api_session"].DeliveryLocation("AM", "YRV", 40.177, 44.513)


def discover(client: Any, intent: Any, live: dict[str, ModuleType]) -> Any:
    return client.async_discover(intent, delivery_location(live))


def assert_calls(
    live: dict[str, ModuleType], fixture: FixtureReadSession, *, full: bool
) -> None:
    location = delivery_location(live)
    expected = [
        (
            "basket",
            "/v1/authenticated/customers/42/baskets",
            {},
            location,
        )
    ]
    if full:
        expected.append(
            (
                "basket",
                "/v1/authenticated/customers/42/baskets/stores/71",
                {},
                location,
            )
        )
    assert fixture.calls == expected
    assert fixture.responses == []


@pytest.mark.parametrize(
    "collection",
    [[], [summary(store_id=72, store_address_id=82)]],
    ids=["empty", "other-store"],
)
def test_absent_verified_uses_exactly_one_collection_get(
    live: dict[str, ModuleType], collection: list[dict[str, Any]]
) -> None:
    client, intent, fixture = client_and_intent(live, collection)

    result = run(discover(client, intent, live))

    discovery = live["ordering_remote_basket_discovery"]
    assert result.status is discovery.RemoteBasketDiscoveryStatus.ABSENT_VERIFIED
    assert result.snapshot is None
    assert_calls(live, fixture, full=False)


def test_exact_basket_is_adopted_after_ordered_collection_and_store_gets(
    live: dict[str, ModuleType],
) -> None:
    client, intent, fixture = client_and_intent(live, [summary()], basket_payload())

    result = run(discover(client, intent, live))

    discovery = live["ordering_remote_basket_discovery"]
    assert result.status is discovery.RemoteBasketDiscoveryStatus.ADOPTED
    assert result.snapshot is not None
    assert result.snapshot.intent() == intent
    assert "basket-private" not in repr(result)
    assert_calls(live, fixture, full=True)


@pytest.mark.parametrize("difference", ["product", "quantity", "customization"])
def test_structurally_valid_different_intent_is_closed_conflict(
    live: dict[str, ModuleType], difference: str
) -> None:
    full = basket_payload()
    if difference == "product":
        full["products"][0]["ids"]["id"] = "different-product"
    elif difference == "quantity":
        full["products"][0]["quantity"]["increments"] = 3
    else:
        full["products"][0]["customizations"][0]["ids"]["externalId"] = (
            "different-attribute"
        )
    client, intent, fixture = client_and_intent(live, [summary()], full)

    result = run(discover(client, intent, live))

    discovery = live["ordering_remote_basket_discovery"]
    assert result.status is discovery.RemoteBasketDiscoveryStatus.CONFLICT
    assert result.snapshot is None
    assert_calls(live, fixture, full=True)


def test_duplicate_selected_store_is_conflict_without_second_get(
    live: dict[str, ModuleType],
) -> None:
    client, intent, fixture = client_and_intent(
        live,
        [summary(), summary(basket_id="basket-private-2", version="basket-v2")],
    )

    result = run(discover(client, intent, live))

    discovery = live["ordering_remote_basket_discovery"]
    assert result.status is discovery.RemoteBasketDiscoveryStatus.CONFLICT
    assert result.snapshot is None
    assert_calls(live, fixture, full=False)


def test_collection_local_hard_cap_accepts_20_and_rejects_21(
    live: dict[str, ModuleType],
) -> None:
    discovery = live["ordering_remote_basket_discovery"]
    at_cap = [
        summary(
            basket_id=f"other-basket-{index}",
            version=f"other-version-{index}",
            store_id=100 + index,
            store_address_id=200 + index,
        )
        for index in range(discovery.MAX_BASKET_SUMMARIES)
    ]
    client, intent, fixture = client_and_intent(live, at_cap)
    result = run(discover(client, intent, live))
    assert result.status is discovery.RemoteBasketDiscoveryStatus.ABSENT_VERIFIED
    assert_calls(live, fixture, full=False)

    above_cap = at_cap + [
        summary(
            basket_id="other-basket-overflow",
            version="other-version-overflow",
            store_id=999,
            store_address_id=998,
        )
    ]
    client, intent, fixture = client_and_intent(live, above_cap)
    with pytest.raises(discovery.RemoteBasketDiscoveryError) as raised:
        run(discover(client, intent, live))
    assert raised.value.category == "contract"
    assert_calls(live, fixture, full=False)


@pytest.mark.parametrize(
    "collection",
    [
        {"baskets": []},
        [None],
        [dict(summary(), storeId=True)],
        [dict(summary(), customerId=None)],
        [dict(summary(), storeId="71")],
        [dict(summary(), unexpected=True)],
    ],
    ids=["envelope", "null-member", "bool-int", "null", "coercion", "unknown"],
)
def test_malformed_collection_is_redacted_contract_error(
    live: dict[str, ModuleType], collection: Any
) -> None:
    client, intent, fixture = client_and_intent(live, collection)
    discovery = live["ordering_remote_basket_discovery"]

    with pytest.raises(discovery.RemoteBasketDiscoveryError) as raised:
        run(discover(client, intent, live))

    assert raised.value.category == "contract"
    assert "basket-private" not in str(raised.value)
    assert_calls(live, fixture, full=False)


def test_collection_rejects_oversized_string_and_full_rejects_oversized_items(
    live: dict[str, ModuleType],
) -> None:
    discovery = live["ordering_remote_basket_discovery"]
    oversized = summary()
    oversized["storeName"] = "x" * 161
    client, intent, fixture = client_and_intent(live, [oversized])
    with pytest.raises(discovery.RemoteBasketDiscoveryError):
        run(discover(client, intent, live))
    assert_calls(live, fixture, full=False)

    full = basket_payload()
    full["products"] *= 51
    client, intent, fixture = client_and_intent(live, [summary()], full)
    with pytest.raises(discovery.RemoteBasketDiscoveryError):
        run(discover(client, intent, live))
    assert_calls(live, fixture, full=True)


@pytest.mark.parametrize(
    "full",
    [
        {"basket": basket_payload()},
        [],
        dict(basket_payload(), usingDhBasket=0),
        dict(basket_payload(), basketVersion=None),
        dict(basket_payload(), unknownProviderField=True),
    ],
    ids=["envelope", "array", "bool", "null", "unknown"],
)
def test_malformed_full_basket_is_redacted_contract_error(
    live: dict[str, ModuleType], full: Any
) -> None:
    client, intent, fixture = client_and_intent(live, [summary()], full)
    discovery = live["ordering_remote_basket_discovery"]

    with pytest.raises(discovery.RemoteBasketDiscoveryError) as raised:
        run(discover(client, intent, live))

    assert raised.value.category == "contract"
    assert_calls(live, fixture, full=True)


def test_matching_summary_then_empty_full_is_inconsistency_not_absence(
    live: dict[str, ModuleType],
) -> None:
    client, intent, fixture = client_and_intent(live, [summary()], None)
    discovery = live["ordering_remote_basket_discovery"]

    with pytest.raises(discovery.RemoteBasketDiscoveryError) as raised:
        run(discover(client, intent, live))

    assert raised.value.category == "inconsistent"
    assert_calls(live, fixture, full=True)


@pytest.mark.parametrize(
    ("target", "field", "value", "expects_full"),
    [
        ("summary", "customerId", 43, False),
        ("summary", "storeAddressId", 82, False),
        ("summary", "storeCategoryId", 5, False),
        ("summary", "handlingStrategy", "PICKUP", False),
        ("full", "basketId", "different-basket", True),
        ("full", "basketVersion", "different-version", True),
        ("full", "customerId", 43, True),
        ("full", "storeId", 72, True),
        ("full", "storeAddressId", 82, True),
        ("full", "storeCategoryId", 5, True),
        ("full", "handlingStrategy", "PICKUP", True),
    ],
)
def test_every_summary_intent_and_full_summary_identity_mismatch_fails_closed(
    live: dict[str, ModuleType],
    target: str,
    field: str,
    value: Any,
    expects_full: bool,
) -> None:
    selected = summary()
    full = basket_payload()
    if target == "summary":
        selected[field] = value
    else:
        full[field] = value
    responses = ([selected], full) if expects_full else ([selected],)
    client, intent, fixture = client_and_intent(live, *responses)
    discovery = live["ordering_remote_basket_discovery"]

    with pytest.raises(discovery.RemoteBasketDiscoveryError) as raised:
        run(discover(client, intent, live))

    assert raised.value.category == "contract"
    assert_calls(live, fixture, full=expects_full)


def test_missing_or_malformed_location_fails_before_discovery_transport(
    live: dict[str, ModuleType],
) -> None:
    client, intent, fixture = client_and_intent(live, [])
    discovery = live["ordering_remote_basket_discovery"]

    for malformed in (None, object(), {"countryCode": "AM"}):
        with pytest.raises(discovery.RemoteBasketDiscoveryError) as raised:
            run(client.async_discover(intent, malformed))
        assert raised.value.category == "contract"
    assert fixture.calls == []
    assert fixture.responses == [[]]


@pytest.mark.parametrize("stage", ["collection", "full"])
def test_cancellation_at_each_read_reraises_without_result(
    live: dict[str, ModuleType], stage: str
) -> None:
    responses: tuple[Any, ...]
    if stage == "collection":
        responses = (asyncio.CancelledError(),)
    else:
        responses = ([summary()], asyncio.CancelledError())
    client, intent, fixture = client_and_intent(live, *responses)

    with pytest.raises(asyncio.CancelledError):
        run(discover(client, intent, live))

    location = delivery_location(live)
    assert fixture.calls == [
        ("basket", "/v1/authenticated/customers/42/baskets", {}, location)
    ] + (
        [
            (
                "basket",
                "/v1/authenticated/customers/42/baskets/stores/71",
                {},
                location,
            )
        ]
        if stage == "full"
        else []
    )
    assert fixture.responses == []


@pytest.mark.parametrize("stage", ["collection", "full"])
def test_transport_error_at_each_read_is_redacted_and_never_retried(
    live: dict[str, ModuleType], stage: str
) -> None:
    responses: tuple[Any, ...]
    if stage == "collection":
        responses = (TimeoutError("private URL and token"),)
    else:
        responses = ([summary()], ConnectionResetError("private basket ID"))
    client, intent, fixture = client_and_intent(live, *responses)
    discovery = live["ordering_remote_basket_discovery"]

    with pytest.raises(discovery.RemoteBasketDiscoveryError) as raised:
        run(discover(client, intent, live))

    assert raised.value.category == "transport"
    assert "private" not in str(raised.value).lower()
    assert len(fixture.calls) == (1 if stage == "collection" else 2)
    assert fixture.responses == []
