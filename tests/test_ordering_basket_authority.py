"""Transport-free account-scoped basket-authority regression tests."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from test_ordering_live_get_clients import store_payload
from test_ordering_remote_basket_quotes import address, basket_payload, intent_payload

ROOT = Path(__file__).parents[1]
GLOVO = ROOT / "custom_components" / "glovo"
MODULES = (
    "ordering_models",
    "ordering_contracts",
    "api_session",
    "ordering_remote_basket",
    "ordering_live_quote",
    "ordering_account",
    "ordering_live_catalog",
    "ordering_live_selection",
    "ordering_packages",
    "ordering_prep_authority",
    "ordering_live_api",
    "ordering_live_flow",
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture()
def modules() -> Iterator[dict[str, ModuleType]]:
    package_name = "glovo_basket_authority_under_test"
    package = ModuleType(package_name)
    package.__path__ = [str(GLOVO)]  # type: ignore[attr-defined]
    sys.modules[package_name] = package
    loaded: dict[str, ModuleType] = {}
    try:
        for name in MODULES:
            spec = importlib.util.spec_from_file_location(
                f"{package_name}.{name}", GLOVO / f"{name}.py"
            )
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            loaded[name] = module
        yield loaded
    finally:
        for name in tuple(sys.modules):
            if name == package_name or name.startswith(f"{package_name}."):
                sys.modules.pop(name, None)


class Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


class ProviderRejected(RuntimeError):
    category = "provider_rejection"
    status = 409


class Harness:
    def __init__(self, modules: dict[str, ModuleType]) -> None:
        self.modules = modules
        self.api = modules["ordering_live_api"]
        self.remote = modules["ordering_remote_basket"]
        self.contracts = modules["ordering_contracts"]
        self.clock = Clock()
        self.customer = self.contracts.CustomerIdentity(42)
        self.address = address(self.contracts)
        self.store = self.contracts.parse_store(store_payload())
        self.location = modules["api_session"].DeliveryLocation(
            "AM", "YRV", 40.177, 44.513
        )
        self.account_calls = 0
        self.create_calls = 0
        self.replace_calls = 0
        self.delete_calls = 0
        self.next_create: BaseException | None = None
        self.next_replace: BaseException | None = None
        self.next_delete: BaseException | None = None
        harness = self

        class Account:
            selection_ttl_seconds = 300.0

            async def async_customer(self) -> Any:
                harness.account_calls += 1
                return harness.customer

            def resolve_address(
                self, handle: str, *, owner_key: str, generation: int
            ) -> Any:
                if handle != f"address-{owner_key}" or generation != 7:
                    raise ValueError
                return harness.address

            def invalidate(self) -> None:
                return None

        class Selections:
            selection_ttl_seconds = 300.0

            def resolve_store(
                self,
                handle: str,
                *,
                owner: str,
                generation: int,
                address_handle: str | None = None,
            ) -> Any:
                if (
                    handle != f"store-{owner}"
                    or address_handle != f"address-{owner}"
                    or generation != 7
                ):
                    raise ValueError
                return harness.store

            def capture_selection(self, **kwargs: Any) -> tuple[Any, ...]:
                selected = kwargs["selections"][0]
                return ((SimpleNamespace(), selected.quantity, ()),)

            def safe_lines(
                self, captured: Any, product_handles: list[str]
            ) -> list[dict[str, Any]]:
                return [
                    {
                        "productHandle": product_handles[0],
                        "label": "Fixture item",
                        "quantity": captured[0][1],
                        "unitPriceMinor": 275000,
                        "currency": "AMD",
                        "options": [],
                    }
                ]

            def compile_intent(self, **kwargs: Any) -> Any:
                return harness.intent(kwargs["selections"][0].quantity)

            def product_currency(self, *_args: Any, **_kwargs: Any) -> str:
                return "AMD"

            def invalidate(self) -> None:
                return None

        class Catalog:
            async def async_store(self, _slug: str, _address: Any) -> Any:
                return harness.store

        class Baskets:
            async def async_create(self, intent: Any, _location: Any) -> Any:
                harness.create_calls += 1
                if harness.next_create is not None:
                    raise harness.next_create
                return harness.snapshot(intent.products[0].quantity)

            async def async_replace(
                self, _previous: Any, products: Any, _location: Any
            ) -> Any:
                harness.replace_calls += 1
                if harness.next_replace is not None:
                    raise harness.next_replace
                return harness.snapshot(products[0].quantity, version="basket-v2")

            async def async_delete(self, *_args: Any, **_kwargs: Any) -> None:
                harness.delete_calls += 1
                if harness.next_delete is not None:
                    raise harness.next_delete

        class PreparationAuthority:
            loaded = True
            unresolved: tuple[Any, ...] = ()

            async def async_acquire(self, **_kwargs: Any) -> Any:
                return SimpleNamespace(attempt_id="prep-authority", record_revision=1)

            async def async_record_outcome(self, **_kwargs: Any) -> None:
                return None

        class Confirmations:
            def invalidate(self, _owner: str) -> None:
                return None

            def invalidate_all(self) -> None:
                return None

        self.facade = self.api.LiveOrderingFacade(
            account=Account(),
            catalog=Catalog(),
            selections=Selections(),
            baskets=Baskets(),
            quotes=SimpleNamespace(),
            confirmations=Confirmations(),
            preparation_authority=PreparationAuthority(),
            preparation_attempt_source=lambda: "prep-" + "a" * 32,
            basket_authority_clock=self.clock,
        )

    def intent(self, quantity: int = 2) -> Any:
        payload = intent_payload()
        payload["products"][0]["quantity"]["increments"] = quantity
        return self.remote.parse_basket_intent(payload)

    def snapshot(self, quantity: int = 2, *, version: str = "basket-v1") -> Any:
        return self.remote.parse_remote_basket(
            basket_payload(version=version, quantity=quantity), self.intent(quantity)
        )

    def products(self, owner: str, quantity: int = 2) -> list[dict[str, Any]]:
        return [
            {
                "productHandle": f"product-{owner}",
                "quantity": quantity,
                "options": [],
            }
        ]

    def request(self, owner: str, quantity: int, revision: int) -> dict[str, Any]:
        return {
            "generation": 7,
            "expectedRevision": revision,
            "storeHandle": f"store-{owner}",
            "addressHandle": f"address-{owner}",
            "products": self.products(owner, quantity),
        }

    def state(self, owner: str = "admin-a", quantity: int = 2) -> Any:
        return self.api._BasketState(
            generation=7,
            revision=1,
            store_handle=f"store-{owner}",
            address_handle=f"address-{owner}",
            currency="AMD",
            store_label=self.store.name,
            lines=(
                {
                    "productHandle": f"product-{owner}",
                    "label": "Fixture item",
                    "quantity": quantity,
                    "unitPriceMinor": 275000,
                    "currency": "AMD",
                    "options": [],
                },
            ),
            snapshot=self.snapshot(quantity),
            store=self.store,
            address_fingerprint=self.address.canonical_fingerprint,
            delivery_location=self.location,
        )

    def verify_absence(self, owner: str = "admin-a", quantity: int = 2) -> None:
        self.facade._record_basket_absent_verified(
            generation=7,
            customer=self.customer,
            intent=self.intent(quantity),
            store_handle=f"store-{owner}",
            address_handle=f"address-{owner}",
            products=self.products(owner, quantity),
        )

    def adopt(self, owner: str = "admin-a", quantity: int = 2) -> None:
        self.facade._adopt_basket_snapshot(
            generation=7,
            customer=self.customer,
            intent=self.intent(quantity),
            state=self.state(owner, quantity),
            products=self.products(owner, quantity),
        )


@pytest.fixture()
def harness(modules: dict[str, ModuleType]) -> Harness:
    return Harness(modules)


def test_fresh_facade_is_unknown_and_all_spend_prerequisites_fail_closed(
    harness: Harness,
) -> None:
    async def scenario() -> None:
        assert await harness.facade.async_dispatch(
            owner="admin-a", operation="live/basket", request={"generation": 7}
        ) == {"status": "unknown"}
        requests = (
            ("live/basket_set", harness.request("admin-a", 2, 0)),
            ("live/payment_methods", {"generation": 7}),
            (
                "live/create_quote",
                {
                    "generation": 7,
                    "addressHandle": "address-admin-a",
                    "paymentHandle": "payment-admin-a",
                },
            ),
            ("live/prepare_confirmation", {"generation": 7}),
        )
        for operation, request in requests:
            with pytest.raises(harness.api.PublicContractError):
                await harness.facade.async_dispatch(
                    owner="admin-a", operation=operation, request=request
                )
        harness.facade._quote["admin-a"] = SimpleNamespace(
            generation=7, quote=object()
        )
        with pytest.raises(harness.api.PublicContractError):
            harness.facade.async_consume_final_quote(
                owner="admin-a", generation=7, challenge="challenge"
            )
        assert (harness.create_calls, harness.replace_calls, harness.delete_calls) == (
            0,
            0,
            0,
        )

    run(scenario())


def test_verified_absence_is_exact_and_create_is_one_call_then_adopted(
    harness: Harness,
) -> None:
    async def scenario() -> None:
        harness.verify_absence()
        with pytest.raises(harness.api.PublicContractError):
            await harness.facade.async_dispatch(
                owner="admin-a",
                operation="live/basket_set",
                request=harness.request("admin-a", 3, 0),
            )
        assert harness.create_calls == 0
        created = await harness.facade.async_dispatch(
            owner="admin-a",
            operation="live/basket_set",
            request=harness.request("admin-a", 2, 0),
        )
        assert created["revision"] == 1
        assert harness.create_calls == 1
        same = await harness.facade.async_dispatch(
            owner="admin-a",
            operation="live/basket_set",
            request=harness.request("admin-a", 2, 1),
        )
        assert same == created
        assert (harness.create_calls, harness.replace_calls) == (1, 0)

    run(scenario())


def test_account_scope_is_shared_across_two_admins_but_handles_are_revalidated(
    harness: Harness,
) -> None:
    async def scenario() -> None:
        harness.adopt("admin-a")
        before = await harness.facade.async_dispatch(
            owner="admin-b", operation="live/basket", request={"generation": 7}
        )
        assert before["revision"] == 1
        same = await harness.facade.async_dispatch(
            owner="admin-b",
            operation="live/basket_set",
            request=harness.request("admin-b", 2, 1),
        )
        assert same["storeHandle"] == "store-admin-b"
        assert same["lines"][0]["productHandle"] == "product-admin-b"
        assert (harness.create_calls, harness.replace_calls) == (0, 0)
        replaced = await harness.facade.async_dispatch(
            owner="admin-b",
            operation="live/basket_set",
            request=harness.request("admin-b", 3, 1),
        )
        assert replaced["revision"] == 2
        assert harness.replace_calls == 1
        after = await harness.facade.async_dispatch(
            owner="admin-a", operation="live/basket", request={"generation": 7}
        )
        assert after["revision"] == 2

    run(scenario())


def test_reload_invalidation_generation_conflict_and_stale_lease_are_closed(
    modules: dict[str, ModuleType], harness: Harness
) -> None:
    harness.adopt()
    harness.facade.invalidate_all()
    assert harness.facade._basket_authority.mode is harness.api._BasketAuthorityMode.UNKNOWN
    with pytest.raises(harness.api.PublicContractError):
        run(
            harness.facade.async_dispatch(
                owner="admin-a",
                operation="live/basket_set",
                request=harness.request("admin-a", 2, 1),
            )
        )

    reloaded = Harness(modules)
    assert reloaded.facade._basket_authority.mode is reloaded.api._BasketAuthorityMode.UNKNOWN
    reloaded.adopt()
    with pytest.raises(reloaded.api.PublicContractError):
        run(
            reloaded.facade.async_dispatch(
                owner="admin-a", operation="live/basket", request={"generation": 8}
            )
        )
    assert reloaded.facade._basket_authority.mode is reloaded.api._BasketAuthorityMode.UNKNOWN

    conflict = Harness(modules)
    conflict.facade._record_basket_conflict(
        generation=7, customer=conflict.customer
    )
    with pytest.raises(conflict.api.PublicContractError):
        run(
            conflict.facade.async_dispatch(
                owner="admin-a",
                operation="live/basket_set",
                request=conflict.request("admin-a", 2, 0),
            )
        )
    assert conflict.create_calls == 0

    stale = Harness(modules)
    stale.adopt()
    stale.clock.value += stale.api.BASKET_AUTHORITY_LEASE_SECONDS
    with pytest.raises(stale.api.PublicContractError):
        run(
            stale.facade.async_dispatch(
                owner="admin-a", operation="live/payment_methods", request={"generation": 7}
            )
        )
    assert stale.facade._basket_authority.mode is stale.api._BasketAuthorityMode.UNKNOWN


@pytest.mark.parametrize("operation", ["replace", "delete"])
def test_deterministic_rejection_retains_adopted_snapshot_and_never_enables_create(
    harness: Harness, operation: str
) -> None:
    async def scenario() -> None:
        harness.adopt()
        if operation == "replace":
            harness.next_replace = ProviderRejected()
            request = harness.request("admin-a", 3, 1)
            public_operation = "live/basket_set"
        else:
            harness.next_delete = ProviderRejected()
            request = {"generation": 7, "expectedRevision": 1}
            public_operation = "live/basket_clear"
        with pytest.raises(harness.api.PreparationMutationRejected):
            await harness.facade.async_dispatch(
                owner="admin-a", operation=public_operation, request=request
            )
        current = await harness.facade.async_dispatch(
            owner="admin-b", operation="live/basket", request={"generation": 7}
        )
        assert current["revision"] == 1
        assert harness.facade._basket_authority.mode is harness.api._BasketAuthorityMode.ADOPTED
        assert harness.create_calls == 0
        assert (harness.replace_calls if operation == "replace" else harness.delete_calls) == 1

    run(scenario())


@pytest.mark.parametrize("failure", [RuntimeError("ambiguous"), asyncio.CancelledError()])
def test_ambiguous_and_cancelled_replace_mark_unknown_and_never_retry(
    harness: Harness, failure: BaseException
) -> None:
    async def scenario() -> None:
        harness.adopt()
        harness.next_replace = failure
        with pytest.raises(type(failure)) if isinstance(
            failure, asyncio.CancelledError
        ) else pytest.raises(harness.api.PublicContractError):
            await harness.facade.async_dispatch(
                owner="admin-a",
                operation="live/basket_set",
                request=harness.request("admin-a", 3, 1),
            )
        assert harness.facade._basket_authority.mode is harness.api._BasketAuthorityMode.UNKNOWN
        assert harness.replace_calls == 1
        with pytest.raises(harness.api.PublicContractError):
            await harness.facade.async_dispatch(
                owner="admin-a",
                operation="live/basket_set",
                request=harness.request("admin-a", 3, 1),
            )
        assert harness.replace_calls == 1

    run(scenario())


def test_successful_delete_becomes_unknown_until_fresh_absence_verification(
    harness: Harness,
) -> None:
    async def scenario() -> None:
        harness.adopt()
        result = await harness.facade.async_dispatch(
            owner="admin-a",
            operation="live/basket_clear",
            request={"generation": 7, "expectedRevision": 1},
        )
        assert result == {"cleared": True, "revision": 2}
        assert harness.delete_calls == 1
        assert harness.facade._basket_authority.mode is harness.api._BasketAuthorityMode.UNKNOWN
        with pytest.raises(harness.api.PublicContractError):
            await harness.facade.async_dispatch(
                owner="admin-a",
                operation="live/basket_set",
                request=harness.request("admin-a", 2, 0),
            )
        assert harness.create_calls == 0

    run(scenario())


def test_live_flow_serializes_transport_free_hooks_and_invalidation(
    modules: dict[str, ModuleType], harness: Harness
) -> None:
    async def scenario() -> None:
        prep = modules["ordering_prep_authority"]
        authority = prep.PreparationMutationAuthority(
            prep.MemoryPreparationStorage(), clock=harness.clock
        )
        flow = modules["ordering_live_flow"].OrderingLiveFlow(
            facade=harness.facade,
            preparation_authority=authority,
            live_options=lambda: {
                "allow_ordering": True,
                "ordering_acknowledged": True,
            },
        )
        await flow.async_initialize()
        await flow._async_record_basket_absent_verified(
            generation=7,
            customer=harness.customer,
            intent=harness.intent(),
            store_handle="store-admin-a",
            address_handle="address-admin-a",
            products=harness.products("admin-a"),
        )
        assert (
            harness.facade._basket_authority.mode
            is harness.api._BasketAuthorityMode.ABSENT_VERIFIED
        )
        await flow._async_adopt_basket_snapshot(
            generation=7,
            customer=harness.customer,
            intent=harness.intent(),
            state=harness.state(),
            products=harness.products("admin-a"),
        )
        assert (
            harness.facade._basket_authority.mode
            is harness.api._BasketAuthorityMode.ADOPTED
        )
        await flow.async_invalidate(8)
        assert (
            harness.facade._basket_authority.mode
            is harness.api._BasketAuthorityMode.UNKNOWN
        )
        with pytest.raises(modules["ordering_live_flow"].LiveFlowUnavailable):
            await flow._async_record_basket_conflict(
                generation=7, customer=harness.customer
            )

    run(scenario())
