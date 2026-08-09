"""Offline regression coverage for local live-selection handles and compiler."""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[1]
GLOVO_ROOT = ROOT / "custom_components" / "glovo"
MODULES = (
    "ordering_models", "ordering_contracts", "api_session", "ordering_remote_basket",
    "ordering_live_quote", "ordering_account", "ordering_live_catalog", "ordering_live_selection", "ordering_live_api",
)


@pytest.fixture()
def live() -> dict[str, ModuleType]:
    package_name = "glovo_live_selection_under_test"
    package = ModuleType(package_name)
    package.__path__ = [str(GLOVO_ROOT)]  # type: ignore[attr-defined]
    sys.modules[package_name] = package
    loaded: dict[str, ModuleType] = {}
    try:
        for name in MODULES:
            spec = importlib.util.spec_from_file_location(f"{package_name}.{name}", GLOVO_ROOT / f"{name}.py")
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
    value = 1.0

    def __call__(self) -> float:
        return self.value


def fixtures(contracts: ModuleType) -> tuple[object, object]:
    money = contracts.ExactMoney(500, "AMD")
    options = (
        contracts.CatalogOption("option-provider", "option-external", "Standard", contracts.ExactMoney(0, "AMD"), True),
        contracts.CatalogOption("option-provider-2", "option-external-2", "Extra", contracts.ExactMoney(100, "AMD"), False),
    )
    group = contracts.CatalogOptionGroup("group-provider", "group-external", "Preparation", 1, 1, 0, False, False, options)
    product = contracts.CatalogProduct("product-provider", "product-external", "store-product-private", "Meal", money, False, (group,), ())
    menu = contracts.CatalogMenu("LIST_VIEW_LAYOUT", 12, (product,))
    store = contracts.LiveStore(11, "Kitchen", "kitchen", 12, "YRV", None, 13, "RESTAURANT", False, "image", "LIST_VIEW", (), False, None, money, money, "FOOD")
    return store, menu


def test_handles_are_owner_generation_ttl_bound_and_provider_ids_never_projected(live: dict[str, ModuleType]) -> None:
    clock = Clock()
    registry = live["ordering_live_selection"].LiveSelectionRegistry(clock=clock, handle_source=iter(("store-local", "product-local", "group-local", "option-local", "option-local-2")).__next__)
    store, menu = fixtures(live["ordering_contracts"])
    public_store = registry.issue_store(
        store, owner="owner-a", generation=1, address_handle="address-a"
    )
    public_menu = registry.issue_menu(public_store["storeHandle"], menu, owner="owner-a", generation=1)
    encoded = repr((public_store, public_menu))
    for private in ("product-provider", "product-external", "group-provider", "group-external", "option-provider", "option-external", "store-product-private"):
        assert private not in encoded
    with pytest.raises(live["ordering_live_selection"].LiveSelectionError):
        registry.resolve_store(
            public_store["storeHandle"],
            owner="owner-a",
            generation=1,
            address_handle="address-b",
        )
    with pytest.raises(live["ordering_live_selection"].LiveSelectionError):
        registry.resolve_store(public_store["storeHandle"], owner="owner-b", generation=1)
    with pytest.raises(live["ordering_live_selection"].LiveSelectionError):
        registry.resolve_store(public_store["storeHandle"], owner="owner-a", generation=2)
    clock.value += registry.selection_ttl_seconds
    with pytest.raises(live["ordering_live_selection"].LiveSelectionError):
        registry.resolve_store(public_store["storeHandle"], owner="owner-a", generation=1)


def test_store_handle_omits_display_fees_when_current_schema_has_no_currency(
    live: dict[str, ModuleType],
) -> None:
    store, _menu = fixtures(live["ordering_contracts"])
    current_store = replace(store, delivery_fee=None, service_fee=None)
    registry = live["ordering_live_selection"].LiveSelectionRegistry(
        handle_source=iter(("store-local",)).__next__
    )
    public = registry.issue_store(
        current_store,
        owner="owner-a",
        generation=1,
        address_handle="address-a",
    )
    assert public == {
        "storeHandle": "store-local",
        "label": "Kitchen",
        "category": "RESTAURANT",
    }
    assert registry.resolve_store(
        "store-local",
        owner="owner-a",
        generation=1,
        address_handle="address-a",
    ) is current_store


def test_compiler_uses_default_or_exact_choice_and_rejects_duplicate_and_constraints(live: dict[str, ModuleType]) -> None:
    selections = live["ordering_live_selection"]
    registry = selections.LiveSelectionRegistry(handle_source=iter(("store-local", "product-local", "group-local", "option-local", "option-local-2")).__next__)
    store, menu = fixtures(live["ordering_contracts"])
    store_handle = registry.issue_store(store, owner="owner-a", generation=1)["storeHandle"]
    product = registry.issue_menu(store_handle, menu, owner="owner-a", generation=1)["products"][0]
    intent = registry.compile_intent(owner="owner-a", generation=1, customer_id=7, store_handle=store_handle, selections=selections.parse_selected_products([{"productHandle": product["productHandle"], "quantity": 2, "options": []}]))
    assert intent.products[0].customizations[0].attribute_id == "option-provider"
    bad = {"productHandle": product["productHandle"], "quantity": 1, "options": [{"groupHandle": product["optionGroups"][0]["groupHandle"], "optionHandles": []}]}
    with pytest.raises(selections.LiveSelectionError):
        registry.compile_intent(owner="owner-a", generation=1, customer_id=7, store_handle=store_handle, selections=selections.parse_selected_products([bad]))
    with pytest.raises(selections.LiveSelectionError):
        selections.parse_selected_products([{"productHandle": product["productHandle"], "quantity": 1, "options": []}, {"productHandle": product["productHandle"], "quantity": 1, "options": []}])



def test_private_dtos_revalidate_direct_fabrication_and_duplicate_identity(live: dict[str, ModuleType]) -> None:
    contracts = live["ordering_contracts"]
    remote = live["ordering_remote_basket"]
    with pytest.raises(contracts.ContractError):
        contracts.AddressSnapshot(1, "line", "", float("nan"), 1.0, "AM", "YRV", "Yerevan", "APARTMENT", None, ())
    customization = remote.RemoteCustomization("g", "Group", 0, "o", "Option", 1)
    product = remote.RemoteBasketProduct("p", quantity=1, customizations=(customization,))
    with pytest.raises(remote.BasketContractError):
        remote.BasketIntent(1, 2, 3, 4, "DELIVERY", (product, product))

    api = live["ordering_live_api"]
    assert tuple(api.PUBLIC_OPERATIONS) == ("state", "live/addresses", "live/stores", "live/store_menu", "live/payment_methods", "live/basket", "live/basket_set", "live/basket_clear", "live/basket_reconcile", "live/create_quote", "live/prepare_confirmation", "live/execute_checkout", "live/checkout_status")
    assert api.validate_public_request("live/execute_checkout", {"generation": 1, "challenge": "local", "acknowledged": True})[2] == 1
    for request in ({"generation": 0}, {"generation": True}, {"generation": 1, "challenge": "x", "acknowledged": 1}, {"generation": 1, "challenge": "x", "acknowledged": True, "total": 1}):
        with pytest.raises(api.PublicContractError):
            api.validate_public_request("live/execute_checkout", request)
