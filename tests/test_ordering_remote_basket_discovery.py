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


def current_full_basket_payload() -> dict[str, Any]:
    payload = basket_payload()
    money = {"minor": 550000, "major": 5500.0, "formatted": "5,500 AMD"}
    payload.update(
        {
            "baseOrderUrn": None,
            "basketCreationWidgetId": None,
            "basketPriceBeforeLastRequest": copy.deepcopy(payload["basketPrice"]),
            "catalogLanguageCode": "hy",
            "countryCode": "AM",
            "createdAt": "2026-08-16T12:00:00Z",
            "currencyCode": "AMD",
            "expiresAt": "2026-08-16T13:00:00Z",
            "status": "ACTIVE",
            "storeInfo": {
                "logo": "https://example.invalid/logo.png",
                "name": "Fixture Kitchen",
                "vertical": None,
            },
            "updatedAt": "2026-08-16T12:01:00Z",
        }
    )
    payload["basketPrice"]["total"] = copy.deepcopy(money)
    payload["basketPriceBeforeLastRequest"]["total"] = copy.deepcopy(money)
    payload["mbs"].update(
        {
            "muxApplied": False,
            "surcharge": {
                "totalFormatted": "0 AMD",
                "final": {"minor": 0, "major": 0.0, "formatted": "0 AMD"},
            },
            "tiers": [{"feeMinor": 0, "thresholdMinor": 500000}],
        }
    )
    for item in payload["products"]:
        item.update(
            {
                "availability": "AVAILABLE",
                "eans": [],
                "imageServiceId": "fixture-image",
                "isEnergyDrink": False,
                "nmrAdId": None,
                "restrictions": [],
                "tags": [],
                "teasingDiscounts": [],
                "weight": None,
            }
        )
        item["quantity"].update({"items": 2, "itemsLimit": 10})
        item["price"].update(
            {
                "originalUnitaryBasePrice": None,
                "originalUnitaryTotalPrice": None,
                "productTotalTeasingDiscount": None,
            }
        )
    return payload


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


def test_current_live_eta_and_store_availability_summary_shape_is_accepted(
    live: dict[str, ModuleType],
) -> None:
    current = summary()
    current["eta"] = {"lowerBound": 20, "upperBound": 35}
    current["storeAvailability"] = {
        "nextOpeningTime": None,
        "nextSchedulingTime": "2026-08-16T12:00:00Z",
        "storeStatus": "OPEN",
    }
    client, intent, fixture = client_and_intent(
        live, [current], basket_payload()
    )

    result = run(discover(client, intent, live))

    discovery = live["ordering_remote_basket_discovery"]
    assert result.status is discovery.RemoteBasketDiscoveryStatus.ADOPTED
    assert result.snapshot is not None
    assert_calls(live, fixture, full=True)


def test_current_live_full_basket_metadata_is_validated_then_discarded(
    live: dict[str, ModuleType],
) -> None:
    current_summary = summary()
    current_summary["eta"] = {"lowerBound": 20, "upperBound": 35}
    current_summary["storeAvailability"] = {
        "nextOpeningTime": None,
        "nextSchedulingTime": "2026-08-16T12:00:00Z",
        "storeStatus": "OPEN",
    }
    client, intent, fixture = client_and_intent(
        live, [current_summary], current_full_basket_payload()
    )

    result = run(discover(client, intent, live))

    discovery = live["ordering_remote_basket_discovery"]
    assert result.status is discovery.RemoteBasketDiscoveryStatus.ADOPTED
    assert result.snapshot is not None
    projection = result.snapshot.provider_projection_bytes
    for discarded in (
        b"catalogLanguageCode",
        b"storeInfo",
        b"availability",
        b"imageServiceId",
        b"tiers",
        b"itemsLimit",
    ):
        assert discarded not in projection
    assert_calls(live, fixture, full=True)


def test_public_unknown_and_display_extensions_are_bounded_and_discarded_together(
    live: dict[str, ModuleType],
) -> None:
    current_summary = summary()
    current_summary["distance"] = "1.2 km"
    current_summary["storeAvailability"] = ["changed-provider-shape"]
    current_summary["futureSummaryDisplay"] = {"nested": [1, True, None]}
    full = current_full_basket_payload()
    full["baseOrderUrn"] = {"changed": True}
    full["basketCreationWidgetId"] = ["changed"]
    full["basketPriceBeforeLastRequest"] = "changed-display-shape"
    full["catalogLanguageCode"] = {"changed": True}
    full["futureRootDisplay"] = {"nested": [1, True, None]}
    full["storeInfo"]["vertical"] = {"changed": True}
    product = full["products"][0]
    product["availability"] = {"changed": True}
    product["eans"] = {"changed": True}
    product["imageServiceId"] = ["changed"]
    product["isEnergyDrink"] = "changed"
    product["nmrAdId"] = {"changed": True}
    product["weight"] = ["changed"]
    product["restrictions"] = {"changed": True}
    product["tags"] = ["nonempty"]
    product["teasingDiscounts"] = {"changed": True}
    product["futureProductDisplay"] = {"nested": [1, True, None]}
    product["quantity"]["items"] = ["changed"]
    product["quantity"]["itemsLimit"] = {"changed": True}
    client, intent, fixture = client_and_intent(live, [current_summary], full)

    result = run(discover(client, intent, live))

    discovery = live["ordering_remote_basket_discovery"]
    assert result.status is discovery.RemoteBasketDiscoveryStatus.ADOPTED
    assert result.snapshot is not None
    projection = result.snapshot.provider_projection_bytes
    for discarded in (
        b"futureRootDisplay",
        b"futureProductDisplay",
        b"availability",
        b"nmrAdId",
        b"tags",
        b"itemsLimit",
        b"storeInfo",
    ):
        assert discarded not in projection
    assert_calls(live, fixture, full=True)


def test_nullable_store_logo_is_accepted_and_discarded(
    live: dict[str, ModuleType],
) -> None:
    full = current_full_basket_payload()
    full["storeInfo"]["logo"] = None
    client, intent, fixture = client_and_intent(live, [summary()], full)

    result = run(discover(client, intent, live))

    discovery = live["ordering_remote_basket_discovery"]
    assert result.status is discovery.RemoteBasketDiscoveryStatus.ADOPTED
    assert result.snapshot is not None
    assert b"storeInfo" not in result.snapshot.provider_projection_bytes
    assert_calls(live, fixture, full=True)


@pytest.mark.parametrize("limit", [0, 51, 2_147_483_647])
def test_provider_unbounded_increments_limit_is_normalized_to_no_limit(
    live: dict[str, ModuleType], limit: int
) -> None:
    full = current_full_basket_payload()
    full["products"][0]["quantity"]["incrementsLimit"] = limit
    client, intent, fixture = client_and_intent(live, [summary()], full)

    result = run(discover(client, intent, live))

    discovery = live["ordering_remote_basket_discovery"]
    assert result.status is discovery.RemoteBasketDiscoveryStatus.ADOPTED
    assert result.snapshot is not None
    assert b'"incrementsLimit":null' in result.snapshot.provider_projection_bytes
    assert_calls(live, fixture, full=True)


def test_in_range_increments_limit_remains_exact(
    live: dict[str, ModuleType],
) -> None:
    full = current_full_basket_payload()
    full["products"][0]["quantity"]["incrementsLimit"] = 10
    client, intent, fixture = client_and_intent(live, [summary()], full)

    result = run(discover(client, intent, live))

    assert result.snapshot is not None
    assert b'"incrementsLimit":10' in result.snapshot.provider_projection_bytes
    assert_calls(live, fixture, full=True)


@pytest.mark.parametrize("limit", [-1, True, "0", 1.5, 2_147_483_648])
def test_malformed_increments_limit_is_rejected_at_exact_path(
    live: dict[str, ModuleType], limit: Any
) -> None:
    full = current_full_basket_payload()
    full["products"][0]["quantity"]["incrementsLimit"] = limit
    client, intent, fixture = client_and_intent(live, [summary()], full)
    discovery = live["ordering_remote_basket_discovery"]

    with pytest.raises(discovery.RemoteBasketDiscoveryError) as raised:
        run(discover(client, intent, live))

    assert raised.value.stage == "full_parse"
    assert raised.value.reason == "schema"
    assert raised.value.path == "products.quantity.incrementsLimit"
    assert_calls(live, fixture, full=True)


def _second_response_product(full: dict[str, Any]) -> dict[str, Any]:
    second = copy.deepcopy(full["products"][0])
    second["ids"]["id"] = "product-2"
    second["ids"]["basketProductId"] = "basket-product-2"
    return second


def test_duplicate_optional_product_enrichment_ids_are_accepted(
    live: dict[str, ModuleType],
) -> None:
    full = current_full_basket_payload()
    second = _second_response_product(full)
    full["products"].append(second)
    _, intent, _ = client_and_intent(live, [summary()], full)
    remote = live["ordering_remote_basket"]

    snapshot = remote.parse_remote_basket_identity(
        full,
        intent,
        expected_basket_id=full["basketId"],
        expected_basket_version=full["basketVersion"],
    )

    assert len(snapshot.products) == 2
    assert snapshot.products[0].external_id == snapshot.products[1].external_id
    assert snapshot.products[0].legacy_id == snapshot.products[1].legacy_id
    assert snapshot.products[0].store_product_id == snapshot.products[1].store_product_id


@pytest.mark.parametrize(
    ("duplicate_key", "expected_path"),
    [
        ("id", "products.ids.id"),
        ("basketProductId", "products.ids.basketProductId"),
    ],
)
def test_duplicate_authoritative_product_ids_remain_rejected_at_exact_path(
    live: dict[str, ModuleType], duplicate_key: str, expected_path: str
) -> None:
    full = current_full_basket_payload()
    second = _second_response_product(full)
    second["ids"][duplicate_key] = full["products"][0]["ids"][duplicate_key]
    full["products"].append(second)
    _, intent, _ = client_and_intent(live, [summary()], full)
    remote = live["ordering_remote_basket"]

    with pytest.raises(remote.BasketContractError) as raised:
        remote.parse_remote_basket_identity(
            full,
            intent,
            expected_basket_id=full["basketId"],
            expected_basket_version=full["basketVersion"],
        )

    assert raised.value.path == expected_path


def test_total_product_quantity_cap_has_exact_path(
    live: dict[str, ModuleType],
) -> None:
    full = current_full_basket_payload()
    products = []
    for index in range(3):
        item = copy.deepcopy(full["products"][0])
        item["ids"]["id"] = f"product-{index}"
        item["ids"]["basketProductId"] = f"basket-product-{index}"
        item["quantity"]["increments"] = 40
        item["quantity"]["incrementsLimit"] = 50
        products.append(item)
    full["products"] = products
    _, intent, _ = client_and_intent(live, [summary()], full)
    remote = live["ordering_remote_basket"]

    with pytest.raises(remote.BasketContractError) as raised:
        remote.parse_remote_basket_identity(
            full,
            intent,
            expected_basket_id=full["basketId"],
            expected_basket_version=full["basketVersion"],
        )

    assert raised.value.path == "products.totalQuantity"


@pytest.mark.parametrize(
    "logo",
    [
        {},
        [],
        True,
        1,
        1.5,
        "x" * 1_000,
    ],
    ids=["object", "array", "boolean", "integer", "number", "bounded-string"],
)
def test_store_info_extension_is_opaque_bounded_and_discarded(
    live: dict[str, ModuleType], logo: Any
) -> None:
    full = current_full_basket_payload()
    full["storeInfo"]["logo"] = logo
    client, intent, fixture = client_and_intent(live, [summary()], full)
    result = run(discover(client, intent, live))

    assert result.status is live["ordering_remote_basket_discovery"].RemoteBasketDiscoveryStatus.ADOPTED
    assert result.snapshot is not None
    assert b"storeInfo" not in result.snapshot.provider_projection_bytes
    assert_calls(live, fixture, full=True)


def test_nested_price_and_mbs_extensions_are_stripped_wholesale(
    live: dict[str, ModuleType],
) -> None:
    variants: list[dict[str, Any]] = []
    price_shape = current_full_basket_payload()
    price_shape["basketPrice"]["total"]["private"] = "x"
    variants.append(price_shape)

    mbs_tier = current_full_basket_payload()
    mbs_tier["mbs"]["tiers"][0]["private"] = "x"
    variants.append(mbs_tier)

    for full in variants:
        client, intent, fixture = client_and_intent(live, [summary()], full)
        result = run(discover(client, intent, live))
        assert result.status is live["ordering_remote_basket_discovery"].RemoteBasketDiscoveryStatus.ADOPTED
        assert result.snapshot is not None
        assert b'"private"' not in result.snapshot.provider_projection_bytes
        assert_calls(live, fixture, full=True)


def test_full_basket_failure_path_is_closed_and_value_free(
    live: dict[str, ModuleType],
) -> None:
    full = current_full_basket_payload()
    full["products"][0]["ids"]["privateIdentity"] = "private-value"
    client, intent, fixture = client_and_intent(live, [summary()], full)
    discovery = live["ordering_remote_basket_discovery"]

    with pytest.raises(discovery.RemoteBasketDiscoveryError) as raised:
        run(discover(client, intent, live))

    assert raised.value.stage == "full_parse"
    assert raised.value.reason == "schema"
    assert raised.value.path == "products.ids"
    assert "basket-private" not in str(raised.value)
    assert_calls(live, fixture, full=True)

    sanitized = discovery.RemoteBasketDiscoveryError(
        stage="full_parse", reason="schema", path="PRIVATE/VALUE"
    )
    assert sanitized.path == "unknown"
    assert "PRIVATE" not in str(sanitized)


def test_public_eta_numbers_and_unknown_keys_are_display_only(
    live: dict[str, ModuleType],
) -> None:
    reversed_eta = summary()
    reversed_eta["eta"] = {"lowerBound": 40, "upperBound": 20}
    eta_extra = summary()
    eta_extra["eta"] = {"lowerBound": 20, "upperBound": 40, "private": "x"}
    for collection_item in (reversed_eta, eta_extra):
        client, intent, fixture = client_and_intent(
            live, [collection_item], basket_payload()
        )
        result = run(discover(client, intent, live))
        assert result.status is live["ordering_remote_basket_discovery"].RemoteBasketDiscoveryStatus.ADOPTED
        assert_calls(live, fixture, full=True)


def test_home21_t3_t11_complete_public_display_shape_adopts_exact_intent(
    live: dict[str, ModuleType],
) -> None:
    """One production-shaped tranche closes the audited display-only matrix."""
    selected = summary()
    selected.update(
        {
            "basketItems": -1.5,
            "basketPriceFormatted": "  5 500\nAMD  ",
            "deliveryFeeInfo": {
                "deliveryFeeFormatted": "  free\nnow  ",
                "feeType": "Ω",
                "futureDisplay": {"shape": [1, None]},
            },
            "distance": "  1.2\nkm  ",
            "eta": {
                "lowerBound": 40.5,
                "upperBound": -2.25,
                "futureDisplay": True,
            },
            "storeImage": "",
            "storeName": "  Café\n500 Cal  ",
            "updatedAt": "not normalized\n",
            "storeAvailability": ["opaque", {"changed": True}],
            "futureSummaryDisplay": {"bounded": True},
        }
    )
    full = current_full_basket_payload()
    wanted = intent_payload()
    wanted_customization = wanted["products"][0]["customizations"][0]
    wanted_customization["ids"]["groupLegacyId"] = "legacy-group-1"
    wanted_customization["name"] = "Prepared label"
    wanted_customization["groupName"] = "Prepared group label"
    wanted_customization["customizationName"] = "Prepared option label"

    full_customization = full["products"][0]["customizations"][0]
    full_customization["ids"]["groupLegacyId"] = "legacy-group-1"
    full_customization["name"] = "  Returned name\n  "
    full_customization["groupName"] = "Returned group Ω"
    full_customization["customizationName"] = ""
    full_customization["futureDisplay"] = {"ignored": True}

    full.update(
        {
            "baseOrderUrn": {"changed": True},
            "storeInfo": ["entire extension is opaque"],
            "futureRootDisplay": {"nested": [1, True, None]},
            # Suggestions are never basket authority and cannot block adoption.
            "productSuggestions": {"malformedDisplayOnly": object().__class__.__name__},
        }
    )
    full["basketPrice"]["futureDisplay"] = True
    full["basketPrice"]["final"]["major"] = -1.25
    full["basketPrice"]["final"]["formatted"] = "  total\nAMD  "
    full["basketPrice"]["final"]["futureDisplay"] = [1]
    full["basketPrice"]["total"] = {"arbitrary": "discarded"}
    full["mbs"]["futureDisplay"] = True
    full["mbs"]["muxApplied"] = {"changed": True}
    full["mbs"]["tiers"] = "changed"
    full["mbs"]["barThreshold"]["final"]["minor"] = -3.5
    full["mbs"]["barThreshold"]["final"]["major"] = -4.5
    full["mbs"]["barThreshold"]["final"]["futureDisplay"] = True

    product = full["products"][0]
    product.update(
        {
            "name": "  500  Cal\nMeal  ",
            "productName": "",
            "description": "Unicode Ω\nkept exactly",
            "imageUrl": "  relative image\n  ",
            "futureProductDisplay": {"nested": True},
            "availability": {"changed": True},
            "nmrAdId": ["changed"],
            "tags": {"changed": True},
            "weight": ["changed"],
            "weighableInfo": {
                "metric": "  kg\n  ",
                "incrementByWeight": -0.25,
                "incrementByPiece": 0.125,
                "futureDisplay": True,
            },
            "packaging": {
                "type": "  box\n  ",
                "price": -1.5,
                "isEco": None,
                "isReturnable": None,
                "priceFormatted": "  -1.5\nAMD  ",
                "futureDisplay": True,
            },
        }
    )
    product["quantity"]["items"] = {"changed": True}
    product["quantity"]["itemsLimit"] = ["changed"]
    product["price"]["futureDisplay"] = True
    product["price"]["final"].update(
        {"minor": -2.5, "major": -3.75, "formatted": "  x\ny  ", "future": True}
    )
    product["price"]["unitaryBasePrice"]["minor"] = 1.25
    product["price"]["unitaryTotalPrice"]["major"] = -9.5
    product["price"]["productTotalDiscount"] = -0.5
    product["price"]["originalUnitaryBasePrice"] = {"changed": True}

    fixture = FixtureReadSession([selected], full)
    remote = live["ordering_remote_basket"]
    intent = remote.parse_basket_intent(wanted)
    client = live["ordering_remote_basket_discovery"].RemoteBasketDiscoveryClient(fixture)

    result = run(discover(client, intent, live))

    discovery = live["ordering_remote_basket_discovery"]
    assert result.status is discovery.RemoteBasketDiscoveryStatus.ADOPTED
    assert result.snapshot is not None
    projection = result.snapshot.products[0].canonical_dict()
    assert projection["name"] == "  500  Cal\nMeal  "
    assert projection["productName"] == ""
    assert projection["description"] == "Unicode Ω\nkept exactly"
    assert projection["price"]["final"] == {
        "minor": -2.5,
        "major": -3.75,
        "formatted": "  x\ny  ",
    }
    assert projection["weighableInfo"]["incrementByWeight"] == -0.25
    assert projection["packaging"]["price"] == -1.5
    projected_customization = projection["customizations"][0]
    assert projected_customization["ids"]["groupLegacyId"] == "legacy-group-1"
    assert projected_customization["ids"]["groupId"] == "group-1"
    assert projected_customization["name"] == "  Returned name\n  "
    assert projected_customization["groupName"] == "Returned group Ω"
    assert projected_customization["customizationName"] == ""
    encoded = result.snapshot.provider_projection_bytes
    for discarded in (
        b"futureDisplay",
        b"futureRootDisplay",
        b"futureProductDisplay",
        b"baseOrderUrn",
        b"storeInfo",
        b"originalUnitaryBasePrice",
        b"productSuggestions",
        b"itemsLimit",
    ):
        assert discarded not in encoded
    assert result.snapshot.product_suggestions == ()
    assert_calls(live, fixture, full=True)


@pytest.mark.parametrize(
    ("path", "value", "expected_path"),
    [
        (("products", 0, "ids", "futureIdentity"), "x", "products.ids"),
        (("products", 0, "quantity", "increments"), 1.5, "products.quantity.increments"),
        (("products", 0, "price", "final", "major"), "1", "products.price"),
        (("products", 0, "weighableInfo"), {"metric": "kg", "incrementByWeight": "1", "incrementByPiece": 1}, "products.weighableInfo"),
        (("products", 0, "packaging"), {"type": None, "price": "1", "isEco": None, "isReturnable": None, "priceFormatted": "x"}, "products.packaging"),
        (("products", 0, "customizations", 0, "ids", "legacyId"), None, "products.customizations"),
        (("products", 0, "customizations", 0, "ids", "futureIdentity"), "x", "products.customizations"),
        (("basketPrice", "final", "minor"), -1, "basketPrice"),
        (("basketPrice", "final", "minor"), 1.5, "basketPrice"),
    ],
)
def test_home21_malformed_neighbors_preserve_authority_boundaries(
    live: dict[str, ModuleType],
    path: tuple[Any, ...],
    value: Any,
    expected_path: str,
) -> None:
    full = current_full_basket_payload()
    target: Any = full
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    client, intent, fixture = client_and_intent(live, [summary()], full)
    discovery = live["ordering_remote_basket_discovery"]

    with pytest.raises(discovery.RemoteBasketDiscoveryError) as raised:
        run(discover(client, intent, live))

    assert raised.value.stage == "full_parse"
    assert raised.value.reason == "schema"
    assert raised.value.path == expected_path
    assert_calls(live, fixture, full=True)


@pytest.mark.parametrize(
    "difference",
    [
        "groupLegacyId",
        "groupId",
        "groupExternalId",
        "groupPosition",
        "legacyId",
        "externalId",
        "quantity",
    ],
)
def test_home21_every_customization_identity_dimension_remains_exact(
    live: dict[str, ModuleType], difference: str
) -> None:
    wanted = intent_payload()
    wanted["products"][0]["customizations"][0]["ids"]["groupLegacyId"] = "legacy-group-1"
    full = basket_payload()
    actual = full["products"][0]["customizations"][0]
    actual["ids"]["groupLegacyId"] = "legacy-group-1"
    if difference == "quantity":
        actual["quantity"]["increments"] = 2
    elif difference == "groupPosition":
        actual["ids"][difference] = 1
    else:
        actual["ids"][difference] = "different-id"
    client, intent, fixture = client_and_intent(live, [summary()], full)
    intent = live["ordering_remote_basket"].parse_basket_intent(wanted)
    client = live["ordering_remote_basket_discovery"].RemoteBasketDiscoveryClient(fixture)

    result = run(discover(client, intent, live))

    assert result.status is live["ordering_remote_basket_discovery"].RemoteBasketDiscoveryStatus.CONFLICT
    assert result.snapshot is None
    assert_calls(live, fixture, full=True)


def test_empty_public_basket_is_closed_conflict_not_adoption(
    live: dict[str, ModuleType],
) -> None:
    full = basket_payload()
    full["products"] = []
    client, intent, fixture = client_and_intent(live, [summary()], full)

    result = run(discover(client, intent, live))

    assert result.status is live["ordering_remote_basket_discovery"].RemoteBasketDiscoveryStatus.CONFLICT
    assert result.snapshot is None
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
    ],
    ids=["envelope", "null-member", "bool-int", "null", "coercion"],
)
def test_malformed_collection_is_redacted_contract_error(
    live: dict[str, ModuleType], collection: Any
) -> None:
    client, intent, fixture = client_and_intent(live, collection)
    discovery = live["ordering_remote_basket_discovery"]

    with pytest.raises(discovery.RemoteBasketDiscoveryError) as raised:
        run(discover(client, intent, live))

    assert raised.value.category == "contract"
    assert raised.value.stage == "collection_parse"
    assert raised.value.reason == "schema"
    assert "basket-private" not in str(raised.value)
    assert_calls(live, fixture, full=False)


def test_collection_rejects_oversized_string_and_full_rejects_oversized_items(
    live: dict[str, ModuleType],
) -> None:
    discovery = live["ordering_remote_basket_discovery"]
    oversized = summary()
    oversized["storeName"] = "x" * 1_001
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
    ],
    ids=["envelope", "array", "bool", "null"],
)
def test_malformed_full_basket_is_redacted_contract_error(
    live: dict[str, ModuleType], full: Any
) -> None:
    client, intent, fixture = client_and_intent(live, [summary()], full)
    discovery = live["ordering_remote_basket_discovery"]

    with pytest.raises(discovery.RemoteBasketDiscoveryError) as raised:
        run(discover(client, intent, live))

    assert raised.value.category == "contract"
    assert raised.value.stage == "full_parse"
    assert raised.value.reason in {"schema", "unsupported", "mismatch"}
    assert_calls(live, fixture, full=True)


def test_matching_summary_then_empty_full_is_inconsistency_not_absence(
    live: dict[str, ModuleType],
) -> None:
    client, intent, fixture = client_and_intent(live, [summary()], None)
    discovery = live["ordering_remote_basket_discovery"]

    with pytest.raises(discovery.RemoteBasketDiscoveryError) as raised:
        run(discover(client, intent, live))

    assert raised.value.category == "inconsistent"
    assert raised.value.stage == "full_empty"
    assert raised.value.reason == "inconsistent"
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
    assert raised.value.stage == (
        "selected_summary" if target == "summary" else "full_parse"
    )
    assert raised.value.reason == (
        "unsupported"
        if target == "full" and field == "handlingStrategy"
        else "mismatch"
    )
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
        assert raised.value.stage == "input"
        assert raised.value.reason == "schema"
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
    assert raised.value.stage == (
        "collection_get" if stage == "collection" else "full_get"
    )
    assert raised.value.reason == "transport"
    assert "private" not in str(raised.value).lower()
    assert len(fixture.calls) == (1 if stage == "collection" else 2)
    assert fixture.responses == []


@pytest.mark.parametrize("stage", ["collection", "full"])
def test_api_session_error_survives_each_read_without_retry(
    live: dict[str, ModuleType], stage: str
) -> None:
    api = live["api_session"]
    error = api.ApiSessionError(
        category="http", endpoint_family="basket", status=503
    )
    responses: tuple[Any, ...] = (
        (error,) if stage == "collection" else ([summary()], error)
    )
    client, intent, fixture = client_and_intent(live, *responses)

    with pytest.raises(api.ApiSessionError) as raised:
        run(discover(client, intent, live))

    assert raised.value is error
    assert raised.value.category == "http"
    assert raised.value.endpoint_family == "basket"
    assert raised.value.status == 503
    assert len(fixture.calls) == (1 if stage == "collection" else 2)
    assert fixture.responses == []
