"""Offline strict-contract tests for the production-shaped final checkout seam."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
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
    "ordering_models", "api_session", "ordering_contracts", "ordering_remote_basket",
    "ordering_live_quote", "ordering_live_checkout",
)


@pytest.fixture(autouse=True)
def socket_guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def blocked(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("final checkout test attempted outbound network access")
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    yield


@pytest.fixture()
def live() -> dict[str, ModuleType]:
    package_name = "glovo_final_checkout_under_test"
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


def quote(live: dict[str, ModuleType], **overrides: Any) -> Any:
    remote = live["ordering_remote_basket"]
    quote_module = live["ordering_live_quote"]
    product_projection = {
        "ids": {
            "id": "product-fixture-1",
            "externalId": "external-fixture-1",
            "storeProductId": "store-product-fixture-1",
            "basketProductId": "basket-product-fixture-1",
        },
        "quantity": {"increments": 2, "incrementsLimit": 10, "limitType": None},
        "price": {
            "totalFormatted": "5,500 AMD",
            "final": {"minor": 550000, "major": 5500, "formatted": "5,500 AMD"},
            "unitaryBasePrice": {"minor": 275000, "major": 2750, "formatted": "2,750 AMD"},
            "unitaryTotalPrice": {"minor": 275000, "major": 2750, "formatted": "2,750 AMD"},
            "productTotalDiscount": None,
        },
        "name": "Fixture item",
        "productName": "Fixture item",
        "description": "Fixture description",
        "isCustomizable": False,
        "productReplacement": None,
        "weighableInfo": None,
        "packaging": None,
    }
    product = remote.RemoteBasketResponseProduct(
        product_id="product-fixture-1",
        external_id="external-fixture-1",
        legacy_id=None,
        store_product_id="store-product-fixture-1",
        basket_product_id="basket-product-fixture-1",
        quantity=remote.StructuredQuantity(
            2,
            increments_limit=10,
            limit_type=None,
            include_increments_limit=True,
            include_limit_type=True,
        ),
        customizations=(),
        name="Fixture item",
        product_name="Fixture item",
        provider_projection_bytes=json.dumps(
            product_projection, sort_keys=True, separators=(",", ":")
        ).encode(),
    )
    projection = {
        "orderDetails": {
            "storeId": 71, "storeAddressId": 81, "basketId": "basket-fixture-1",
            "versionId": 3, "checkoutSessionId": "checkout-session-fixture-1",
        },
        "components": [{"type": "CONFIRMED_FIXTURE", "data": {"selected": True}}],
        "analytics": {"templateReceived": True},
    }
    values = {
        "checkout_session_id": "checkout-session-fixture-1", "version_id": 3,
        "template_id": 9, "basket_id": "basket-fixture-1",
        "basket_version": "basket-version-fixture-1", "customer_id": 101,
        "store_id": 71, "store_address_id": 81, "store_category_id": 201,
        "city_code": "YRV", "handling_strategy": "DELIVERY",
        "exact_products": (product,), "address_fingerprint": "a" * 64,
        "payment_fingerprint": "b" * 64, "capability_fingerprint": "c" * 64,
        "owner_key": "admin-fixture", "generation": 7, "intent_key": "intent-fixture-1",
        "delivery_location": live["api_session"].DeliveryLocation("AM", "YRV", 40.177, 44.513),
        "total": live["ordering_models"].Money(560000, "AMD"),
        "price_lines": (quote_module.ProviderPriceLine("Total", "5,600 AMD", "TOTAL"),),
        "eta": "20 min", "received_at": 100.0, "store_display_name": "Fixture Kitchen",
        "masked_address": "Saved destination ••••", "masked_payment": "Saved card •••• 4242",
        "name": "Fixture checkout",
        "submit_projection_bytes": json.dumps(projection, sort_keys=True, separators=(",", ":")).encode(),
        "full_address": "Fixture delivery address",
        "item_display": ({"name": "Fixture item", "quantity": 2, "options": ["Standard"]},),
    }
    values.update(overrides)
    return quote_module.AuthoritativeQuote(**values)


def response(status: str, *, checkout_id: str = "checkout-fixture-1", q: Any | None = None, **changes: Any) -> dict[str, Any]:
    order = None
    if status == "COMPLETED":
        assert q is not None
        order = {"id": "order-fixture-1", "basketId": q.basket_id, "total": q.total.amount_minor, "currencyCode": q.total.currency}
    value = {
        "checkoutId": checkout_id, "status": status, "action": None,
        "postAuthAction": None, "willDoMinimalAmountAuthAndVoid": False,
        "errors": [], "order": order, "payments": [],
    }
    value.update(changes)
    return {"checkout": value}


class Session:
    def __init__(self, results: list[Any]) -> None:
        self.results = results
        self.calls: list[tuple[str, str, Any]] = []

    async def async_mutate(
        self,
        purpose: Any,
        method: str,
        path: str,
        body: Any,
        *,
        delivery_location: Any,
    ) -> Any:
        assert delivery_location.transport_context() == {
            "countryCode": "AM",
            "cityCode": "YRV",
            "latitude": "40.177",
            "longitude": "44.513",
        }
        self.calls.append((method, path, copy.deepcopy(body)))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def async_final_status(self, path: str) -> Any:
        self.calls.append(("GET", path, None))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def test_exact_private_projection_is_preserved_and_fingerprint_bound(live: dict[str, ModuleType]) -> None:
    checkout = live["ordering_live_checkout"]
    q = quote(live)
    request = checkout.FinalCheckoutRequest.from_quote(q, now=100.0)
    assert request.private_body() == {"checkout": q.exact_submit_projection()}
    assert "checkout-session-fixture-1" not in repr(request)
    object.__setattr__(q, "store_id", 72)
    with pytest.raises(checkout.FinalCheckoutContractError):
        request.private_body()
    q = quote(live)
    request = checkout.FinalCheckoutRequest.from_quote(q, now=100.0)
    object.__setattr__(
        q,
        "delivery_location",
        live["api_session"].DeliveryLocation("AM", "YRV", 40.178, 44.514),
    )
    session = Session([])
    with pytest.raises(checkout.FinalCheckoutContractError):
        asyncio.run(checkout.ProductionFinalCheckoutAdapter(session).async_submit(request))
    assert session.calls == []
    for now in (True, float("nan"), 145.0):
        with pytest.raises(checkout.FinalCheckoutContractError):
            checkout.FinalCheckoutRequest.from_quote(quote(live), now=now)


@pytest.mark.parametrize("status", ["AUTH_REQUIRED", "NO_AUTH_PENDING"])
def test_pending_and_interactive_responses_are_ambiguous_with_learned_id(live: dict[str, ModuleType], status: str) -> None:
    checkout = live["ordering_live_checkout"]
    q = quote(live)
    session = Session([response(status, q=q, action="AUTH" if status == "AUTH_REQUIRED" else "NO_AUTH_POLL")])
    adapter = checkout.ProductionFinalCheckoutAdapter(session)
    with pytest.raises(checkout.FinalCheckoutAmbiguous) as raised:
        asyncio.run(adapter.async_submit(checkout.FinalCheckoutRequest.from_quote(q, now=100.0)))
    assert raised.value.checkout_id == "checkout-fixture-1"
    assert len(session.calls) == 1


@pytest.mark.parametrize("status", ["FAILED", "CANCELLED"])
def test_schema_valid_provider_rejection_is_terminal(live: dict[str, ModuleType], status: str) -> None:
    checkout = live["ordering_live_checkout"]
    q = quote(live)
    session = Session([response(status, q=q)])
    result = asyncio.run(checkout.ProductionFinalCheckoutAdapter(session).async_submit(checkout.FinalCheckoutRequest.from_quote(q, now=100.0)))
    assert result.terminal and not result.succeeded and result.state == "REJECTED"
    assert len(result.evidence_hash) == 64 and len(session.calls) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 429])
def test_http_4xx_is_deterministic_rejection_and_never_retried(
    live: dict[str, ModuleType], status: int
) -> None:
    checkout = live["ordering_live_checkout"]
    api = live["api_session"]
    q = quote(live)
    session = Session(
        [
            api.ApiSessionError(
                category="http",
                endpoint_family="checkout",
                status=status,
                purpose=api.MutationPurpose.FINAL_CHECKOUT,
            )
        ]
    )
    with pytest.raises(checkout.FinalCheckoutRejected) as raised:
        asyncio.run(
            checkout.ProductionFinalCheckoutAdapter(session).async_submit(
                checkout.FinalCheckoutRequest.from_quote(q, now=100.0)
            )
        )
    assert raised.value.status == status
    assert raised.value.category == "provider_rejection"
    assert len(raised.value.evidence_hash) == 64
    assert len(session.calls) == 1


def test_completed_requires_exact_basket_total_currency(live: dict[str, ModuleType]) -> None:
    checkout = live["ordering_live_checkout"]
    q = quote(live)
    valid = checkout.parse_final_response(response("COMPLETED", q=q), quote=q)
    assert valid.succeeded and valid.terminal
    for field, value in (("basketId", "other"), ("total", 560001), ("currencyCode", "USD")):
        payload = response("COMPLETED", q=q)
        payload["checkout"]["order"][field] = value
        with pytest.raises(checkout.FinalCheckoutContractError):
            checkout.parse_final_response(payload, quote=q)


@pytest.mark.parametrize("bad", [True, 1, 2**80, float("nan")])
def test_parser_rejects_bool_as_int_overflow_and_malformed_total(live: dict[str, ModuleType], bad: Any) -> None:
    checkout = live["ordering_live_checkout"]
    q = quote(live)
    payload = response("COMPLETED", q=q)
    payload["checkout"]["order"]["total"] = bad
    with pytest.raises(checkout.FinalCheckoutContractError):
        checkout.parse_final_response(payload, quote=q)


@pytest.mark.parametrize("result", [TimeoutError(), ConnectionResetError(), RuntimeError(), {"malformed": True}])
def test_every_transport_or_malformed_failure_is_one_call_no_retry(live: dict[str, ModuleType], result: Any) -> None:
    checkout = live["ordering_live_checkout"]
    q = quote(live)
    api = live["api_session"]
    wrapped = result if isinstance(result, dict) else api.ApiSessionError(category="transport", endpoint_family="checkout", status=getattr(result, "status", None), purpose=api.MutationPurpose.FINAL_CHECKOUT)
    session = Session([wrapped])
    adapter = checkout.ProductionFinalCheckoutAdapter(session)
    with pytest.raises(checkout.FinalCheckoutAmbiguous):
        asyncio.run(adapter.async_submit(checkout.FinalCheckoutRequest.from_quote(q, now=100.0)))
    assert [(m, p) for m, p, _ in session.calls] == [("POST", "/v3/checkouts/order/1")]


def test_explicit_status_is_one_get_for_exact_known_id_and_no_completion(live: dict[str, ModuleType]) -> None:
    checkout = live["ordering_live_checkout"]
    q = quote(live)
    session = Session([response("COMPLETED", q=q)])
    adapter = checkout.ProductionFinalCheckoutAdapter(session)
    status = asyncio.run(adapter.async_status("checkout-fixture-1", q))
    assert status.succeeded
    with pytest.raises(checkout.FinalCheckoutUnsupported):
        asyncio.run(adapter.async_complete("checkout-fixture-1"))
    assert session.calls[0][:2] == ("GET", "/v3/checkouts/order/checkout-fixture-1")
    assert len(session.calls) == 1


def test_final_route_is_purpose_typed_and_completion_routes_absent(live: dict[str, ModuleType]) -> None:
    api = live["api_session"]
    assert api._MUTATION_ROUTES[api.MutationPurpose.FINAL_CHECKOUT][0] == "POST"
    assert api._MUTATION_ROUTES[api.MutationPurpose.FINAL_CHECKOUT][1].fullmatch("/v3/checkouts/order/1")
    source = (GLOVO_ROOT / "ordering_live_checkout.py").read_text()
    assert source.count("MutationPurpose.FINAL_CHECKOUT") == 1
    for forbidden in ("/complete", "/cancel", "/payments/"):
        assert forbidden not in source
