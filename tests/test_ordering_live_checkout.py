"""Offline regressions for cancellation-safe transport and final checkout fixtures."""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib.util
import json
import socket
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]
GLOVO_ROOT = ROOT / "custom_components" / "glovo"
FIXTURES = ROOT / "tests" / "fixtures" / "glovo_ordering" / "checkout"
MODULES = (
    "ordering_models",
    "api_session",
    "ordering_contracts",
    "ordering_remote_basket",
    "ordering_live_quote",
    "ordering_live_checkout",
)


@pytest.fixture(autouse=True)
def socket_guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def blocked(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("checkout fixture test attempted outbound network access")

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


def quote(
    live: dict[str, ModuleType], *, received_at: float = 100.0, **overrides: Any
) -> Any:
    remote = live["ordering_remote_basket"]
    quote_module = live["ordering_live_quote"]
    product = remote.RemoteBasketProduct(
        product_id="product-fixture-1",
        external_id="external-fixture-1",
        legacy_id=None,
        store_product_id="store-product-fixture-1",
        basket_product_id="basket-product-fixture-1",
        quantity=2,
        quantity_limit=10,
        customizations=(),
    )
    values = {
        "checkout_session_id": "checkout-session-fixture-1",
        "version_id": 3,
        "template_id": 9,
        "basket_id": "basket-fixture-1",
        "basket_version": "basket-version-fixture-1",
        "customer_id": 101,
        "store_id": 71,
        "store_address_id": 81,
        "store_category_id": 201,
        "city_code": "city-fixture-1",
        "handling_strategy": "DELIVERY",
        "exact_products": (product,),
        "address_fingerprint": "a" * 64,
        "payment_fingerprint": "b" * 64,
        "capability_fingerprint": "c" * 64,
        "owner_key": "admin-fixture",
        "generation": 7,
        "intent_key": "intent-fixture-1",
        "total": live["ordering_models"].Money(560000, "AMD"),
        "price_lines": (quote_module.ProviderPriceLine("Total", "fixture", "TOTAL"),),
        "eta": None,
        "received_at": received_at,
        "store_display_name": "Fixture Kitchen",
        "masked_address": "Saved Home ••••",
        "masked_payment": "Saved card •••• 4242",
        "name": "Fixture checkout",
    }
    values.update(overrides)
    return quote_module.AuthoritativeQuote(**values)


class FixtureTransport:
    __glovo_fixture_only__ = True

    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    async def __call__(self, method: str, path: str, body: dict[str, Any] | None) -> Any:
        self.calls.append((method, path, body))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class StatusError(RuntimeError):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__("private provider detail")


def test_executor_cancellation_retains_mutation_authority_until_real_thread_finishes(
    live: dict[str, ModuleType],
) -> None:
    """A barrier-driven real-thread regression: no second write overlaps the first."""
    api = live["api_session"]

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        entered = threading.Barrier(2)
        release: concurrent.futures.Future[None] = concurrent.futures.Future()
        calls: list[str] = []

        def mutation(method: str, token: str, path: str, query: dict[str, str], body: Any) -> Any:
            del method, token, path, query, body
            calls.append("write")
            if len(calls) == 1:
                entered.wait()
                release.result()
            return {"ok": True}

        async def executor(function: Any) -> Any:
            return await loop.run_in_executor(None, function)

        client = api.SerializedApiSession(
            token_source=lambda: "token-fixture",
            persist_token=lambda value: None,
            ensure_token=lambda value: ("access-fixture", value),
            transport=lambda *args: {"read": True},
            mutation_transport=mutation,
            executor=executor,
        )
        first = asyncio.create_task(
            client.async_mutate(
                api.MutationPurpose.CREATE_BASKET,
                "POST",
                "/v1/authenticated/customers/42/baskets",
                {},
            )
        )
        await asyncio.to_thread(entered.wait)
        first.cancel()
        # Yield only through a completed executor Future; no timing sleeps.
        await asyncio.to_thread(lambda: None)
        assert client.lock.locked()
        second = asyncio.create_task(
            client.async_mutate(
                api.MutationPurpose.CREATE_BASKET,
                "POST",
                "/v1/authenticated/customers/42/baskets",
                {},
            )
        )
        await asyncio.to_thread(lambda: None)
        assert calls == ["write"]
        release.set_result(None)
        with pytest.raises(api.MutationDispatchUncertain):
            await first
        assert await second == {"ok": True}
        assert calls == ["write", "write"]

    asyncio.run(scenario())


def test_phase_mutation_allowlists_are_purpose_path_method_and_empty_query_parity(
    live: dict[str, ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    api = live["api_session"]
    glovo_spec = importlib.util.spec_from_file_location("glovo_phase_under_test", GLOVO_ROOT / "glovo.py")
    assert glovo_spec is not None and glovo_spec.loader is not None
    glovo = importlib.util.module_from_spec(glovo_spec)
    glovo_spec.loader.exec_module(glovo)
    assert api._PHASE_MUTATION_ALLOWLIST == glovo._PHASE_MUTATION_ALLOWLIST
    assert set(api._MUTATION_QUERY_CONTRACT) == {row[0] for row in glovo._PHASE_MUTATION_ALLOWLIST}
    assert all(value == frozenset() for value in api._MUTATION_QUERY_CONTRACT.values())

    sent: list[tuple[str, str, Any]] = []
    monkeypatch.setattr(glovo, "_request_json", lambda method, url, **kwargs: sent.append((method, url, kwargs.get("body"))))
    for purpose, method, pattern in glovo._PHASE_MUTATION_ALLOWLIST:
        assert api._MUTATION_ROUTES[api.MutationPurpose(purpose)][0] == method
        assert api._MUTATION_ROUTES[api.MutationPurpose(purpose)][1].pattern == pattern
        sample = {
            "create_basket": "/v1/authenticated/customers/42/baskets",
            "replace_basket_products": "/v1/authenticated/customers/42/baskets/basket-1/products",
            "change_basket_quantity": "/v1/authenticated/customers/42/baskets/basket-1/products/quantity",
            "delete_basket": "/v1/authenticated/customers/42/baskets/basket-1",
            "create_quote_template": "/v3/checkouts/order/1/template",
        }[purpose]
        glovo.single_attempt_authed_phase_mutation(method, "access-fixture", sample, {}, {"fixture": True})
    with pytest.raises(RuntimeError):
        glovo.single_attempt_authed_phase_mutation("POST", "access-fixture", "/v3/checkouts/order/1/template", {"x": "1"}, {})
    assert len(sent) == len(glovo._PHASE_MUTATION_ALLOWLIST)


def test_final_checkout_request_is_private_exact_and_quote_bound(live: dict[str, ModuleType]) -> None:
    checkout = live["ordering_live_checkout"]
    request = checkout.FinalCheckoutRequest.from_quote(quote(live), now=100.0)
    body = request.private_body()
    assert set(body) == {"checkout"}
    assert set(body["checkout"]) == {
        "checkoutSessionId", "versionId", "templateId", "basket", "address", "payment", "total", "authority",
    }
    assert body["checkout"]["total"] == {"minor": 560000, "currency": "AMD"}
    basket = body["checkout"]["basket"]
    assert set(basket) == {
        "id", "version", "customerId", "storeId", "storeAddressId",
        "storeCategoryId", "cityCode", "handlingStrategy", "products",
    }
    assert {key: basket[key] for key in basket if key != "products"} == {
        "id": "basket-fixture-1",
        "version": "basket-version-fixture-1",
        "customerId": 101,
        "storeId": 71,
        "storeAddressId": 81,
        "storeCategoryId": 201,
        "cityCode": "city-fixture-1",
        "handlingStrategy": "DELIVERY",
    }
    assert basket["products"][0]["quantity"] == 2
    assert "560000" not in repr(request)
    for bad_now in (True, float("nan"), 145.0):
        with pytest.raises(checkout.FinalCheckoutContractError):
            checkout.FinalCheckoutRequest.from_quote(quote(live), now=bad_now)


@pytest.mark.parametrize(
    ("field", "changed_value", "body_key"),
    [
        ("customer_id", 102, "customerId"),
        ("store_category_id", 202, "storeCategoryId"),
        ("city_code", "city-fixture-2", "cityCode"),
    ],
)
def test_final_checkout_binds_each_complete_quote_context_value(
    live: dict[str, ModuleType], field: str, changed_value: Any, body_key: str
) -> None:
    checkout = live["ordering_live_checkout"]
    baseline = quote(live)
    changed = quote(live, **{field: changed_value})
    request = checkout.FinalCheckoutRequest.from_quote(changed, now=100.0)

    assert request.quote_fingerprint != baseline.fingerprint
    assert request.private_body()["checkout"]["basket"][body_key] == changed_value


@pytest.mark.parametrize(
    ("field", "bypassed_value"),
    [
        ("customer_id", 102),
        ("store_category_id", 202),
        ("city_code", "city-fixture-2"),
        ("handling_strategy", "PICKUP"),
    ],
)
def test_final_checkout_rejects_bypassed_complete_quote_context(
    live: dict[str, ModuleType], field: str, bypassed_value: Any
) -> None:
    checkout = live["ordering_live_checkout"]
    request = checkout.FinalCheckoutRequest.from_quote(quote(live), now=100.0)

    # A bypassed frozen quote cannot cause a body that disagrees with authority.
    object.__setattr__(request.quote, field, bypassed_value)
    with pytest.raises(checkout.FinalCheckoutContractError) as raised:
        request.private_body()
    assert raised.value.category == "mismatch"


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("customer_id", True),
        ("customer_id", 0),
        ("store_category_id", True),
        ("store_category_id", 0),
        ("city_code", True),
        ("city_code", ""),
        ("handling_strategy", True),
        ("handling_strategy", ""),
        ("handling_strategy", "PICKUP"),
    ],
)
def test_final_checkout_rejects_invalid_complete_quote_context(
    live: dict[str, ModuleType], field: str, invalid_value: Any
) -> None:
    checkout = live["ordering_live_checkout"]
    invalid_quote = quote(live, **{field: invalid_value})

    with pytest.raises(checkout.FinalCheckoutContractError) as raised:
        checkout.FinalCheckoutRequest(
            quote=invalid_quote, quote_fingerprint=invalid_quote.fingerprint
        )
    assert raised.value.category == "mismatch"


@pytest.mark.parametrize(
    "response",
    [
        TimeoutError("private"), ConnectionResetError("private"), StatusError(408), StatusError(409),
        StatusError(425), StatusError(500), StatusError(503), {"checkout": {"state": "SUBMITTED"}},
        {"checkout": {"checkoutId": "checkout-fixture-1", "state": "COMPLETED"}},
    ],
)
def test_final_submit_all_ambiguous_outcomes_make_exactly_one_call(
    live: dict[str, ModuleType], response: Any
) -> None:
    checkout = live["ordering_live_checkout"]
    transport = FixtureTransport([response])
    adapter = checkout.FixtureOnlyFinalCheckoutAdapter(transport)
    with pytest.raises(checkout.FinalCheckoutAmbiguous):
        asyncio.run(adapter.async_submit(checkout.FinalCheckoutRequest.from_quote(quote(live), now=100.0)))
    assert [(method, path) for method, path, _ in transport.calls] == [("POST", checkout.FINAL_CHECKOUT_PATH)]


def test_final_submit_deterministic_rejection_is_one_call(live: dict[str, ModuleType]) -> None:
    checkout = live["ordering_live_checkout"]
    transport = FixtureTransport([StatusError(400)])
    adapter = checkout.FixtureOnlyFinalCheckoutAdapter(transport)
    with pytest.raises(checkout.FinalCheckoutRejected) as raised:
        asyncio.run(adapter.async_submit(checkout.FinalCheckoutRequest.from_quote(quote(live), now=100.0)))
    assert raised.value.status == 400
    assert [(method, path) for method, path, _ in transport.calls] == [("POST", checkout.FINAL_CHECKOUT_PATH)]


def test_final_submit_cancellation_is_ambiguous_exactly_once(live: dict[str, ModuleType]) -> None:
    checkout = live["ordering_live_checkout"]

    class CancellableFixture(FixtureTransport):
        async def __call__(self, method: str, path: str, body: dict[str, Any] | None) -> Any:
            self.calls.append((method, path, body))
            await asyncio.Event().wait()
            raise AssertionError

    async def scenario() -> None:
        transport = CancellableFixture([])
        adapter = checkout.FixtureOnlyFinalCheckoutAdapter(transport)
        task = asyncio.create_task(adapter.async_submit(checkout.FinalCheckoutRequest.from_quote(quote(live), now=100.0)))
        await asyncio.to_thread(lambda: None)
        task.cancel()
        with pytest.raises(checkout.FinalCheckoutAmbiguous):
            await task
        assert len(transport.calls) == 1

    asyncio.run(scenario())


def test_final_validation_and_fixture_gate_have_zero_submit_calls(live: dict[str, ModuleType]) -> None:
    checkout = live["ordering_live_checkout"]
    transport = FixtureTransport([fixture("submit-success.json")])
    adapter = checkout.FixtureOnlyFinalCheckoutAdapter(transport)
    with pytest.raises(checkout.FinalCheckoutContractError):
        asyncio.run(adapter.async_submit(object()))
    with pytest.raises(checkout.FinalCheckoutUnsupported):
        checkout.FixtureOnlyFinalCheckoutAdapter(lambda *args: None)
    assert transport.calls == []


def test_final_success_known_id_permits_one_explicit_read_only_status_and_no_completion(
    live: dict[str, ModuleType]
) -> None:
    checkout = live["ordering_live_checkout"]
    transport = FixtureTransport([fixture("submit-success.json"), fixture("status-completed.json")])
    adapter = checkout.FixtureOnlyFinalCheckoutAdapter(transport)
    submitted = asyncio.run(adapter.async_submit(checkout.FinalCheckoutRequest.from_quote(quote(live), now=100.0)))
    assert submitted.state == "SUBMITTED" and submitted.requires_manual_completion
    status = asyncio.run(adapter.async_status(submitted.checkout_id))
    assert status.state == "COMPLETED" and not status.requires_manual_completion
    with pytest.raises(checkout.FinalCheckoutUnsupported):
        asyncio.run(adapter.async_complete(submitted.checkout_id))
    assert [(method, path) for method, path, _ in transport.calls] == [
        ("POST", "/v3/checkouts/order/1"),
        ("GET", "/v3/checkouts/order/1/checkout-fixture-1"),
    ]


@pytest.mark.parametrize("response", [TimeoutError("private"), StatusError(500), {"checkout": {"checkoutId": "other", "state": "PENDING"}}])
def test_known_id_status_ambiguity_is_one_read_only_call(live: dict[str, ModuleType], response: Any) -> None:
    checkout = live["ordering_live_checkout"]
    transport = FixtureTransport([response])
    adapter = checkout.FixtureOnlyFinalCheckoutAdapter(transport)
    with pytest.raises(checkout.FinalCheckoutAmbiguous):
        asyncio.run(adapter.async_status("checkout-fixture-1"))
    assert [(method, path) for method, path, _ in transport.calls] == [
        ("GET", "/v3/checkouts/order/1/checkout-fixture-1")
    ]


def test_adapter_is_private_unwired_and_has_one_submit_call_site() -> None:
    source = (GLOVO_ROOT / "ordering_live_checkout.py").read_text(encoding="utf-8")
    assert source.count('self._fixture_transport("POST", FINAL_CHECKOUT_PATH, body)') == 1
    assert source.count('self._fixture_transport("GET", FINAL_STATUS_PATH_PREFIX + known_id, None)') == 1
    for runtime_file in ("__init__.py", "coordinator.py", "ordering.py"):
        path = GLOVO_ROOT / runtime_file
        if path.exists():
            assert "ordering_live_checkout" not in path.read_text(encoding="utf-8")
