"""Deterministic tests for durable address-independent package recipes."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from collections.abc import Generator
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]
GLOVO_ROOT = ROOT / "custom_components" / "glovo"


@pytest.fixture()
def modules() -> Generator[dict[str, ModuleType], None, None]:
    package_name = "glovo_packages_under_test"
    package = ModuleType(package_name)
    package.__path__ = [str(GLOVO_ROOT)]  # type: ignore[attr-defined]
    sys.modules[package_name] = package
    loaded: dict[str, ModuleType] = {}
    try:
        for name in (
            "ordering_models",
            "ordering_contracts",
            "api_session",
            "ordering_remote_basket",
            "ordering_remote_basket_discovery",
            "ordering_live_quote",
            "ordering_account",
            "ordering_live_catalog",
            "ordering_live_selection",
            "ordering_packages",
            "ordering_live_api",
        ):
            spec = importlib.util.spec_from_file_location(
                f"{package_name}.{name}", GLOVO_ROOT / f"{name}.py"
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


def domain_objects(contracts: ModuleType) -> dict[str, Any]:
    money = contracts.ExactMoney(500, "AMD")
    option = contracts.CatalogOption(
        "option-private", "option-external-private", "Standard", money, True
    )
    group = contracts.CatalogOptionGroup(
        "group-private",
        "group-external-private",
        "Preparation",
        1,
        1,
        0,
        False,
        False,
        (option,),
    )
    product = contracts.CatalogProduct(
        "product-private",
        "product-external-private",
        "store-product-private",
        "Meal",
        money,
        False,
        (group,),
        (),
    )
    store = contracts.LiveStore(
        11,
        "Kitchen",
        "kitchen-private-slug",
        12,
        "YRV",
        None,
        13,
        "RESTAURANT",
        False,
        "image-private",
        "LIST_VIEW",
        (),
        False,
        None,
        money,
        money,
        "FOOD",
    )
    address = contracts.AddressSnapshot(
        17,
        "Private Street 10",
        "Private details",
        40.177,
        44.513,
        "AM",
        "YRV",
        "Yerevan",
        "APARTMENT",
        "Home private tag",
        (contracts.AddressField("FLOOR_NUMBER", "private-floor"),),
        is_live_saved_address=True,
    )
    return {
        "customer": contracts.CustomerIdentity(99),
        "address": address,
        "store": store,
        "product": product,
        "group": group,
        "option": option,
    }


async def populated_library(modules: dict[str, ModuleType]) -> tuple[Any, Any, dict[str, Any], Any]:
    packages = modules["ordering_packages"]
    objects = domain_objects(modules["ordering_contracts"])
    storage = packages.MemoryPackageLibraryStorage()
    library = packages.PackageLibrary(
        storage, ref_source=lambda _prefix: "pkg-" + "b" * 32
    )
    await library.async_load()
    account = packages.account_digest(objects["customer"])
    items = packages.recipe_items_from_capture(
        (
            (
                objects["product"],
                2,
                ((objects["group"], (objects["option"],)),),
            ),
        )
    )
    package = await library.async_save_package(
        expected_store_revision=0,
        package_ref="",
        expected_revision=0,
        name="KFC Lunch",
        aliases=("usual_kfc", "work meal"),
        store=objects["store"],
        items=items,
        current_account_digest=account,
    )
    return library, storage, objects, package


def test_round_trip_cas_alias_lookup_and_privacy(modules: dict[str, ModuleType]) -> None:
    async def scenario() -> None:
        packages = modules["ordering_packages"]
        library, storage, _objects, package = await populated_library(modules)

        public = await library.async_list()
        assert public["storeRevision"] == 1
        assert set(public) == {"storeRevision", "packages"}
        assert public["packages"][0]["name"] == "KFC Lunch"
        assert public["packages"][0]["aliases"] == ["usual_kfc", "work meal"]
        assert public["packages"][0]["items"] == [
            {"label": "Meal", "quantity": 2, "options": ["Standard"]}
        ]
        assert not ({"addressRef", "addressRevision", "addressName"} & public["packages"][0].keys())

        public_text = json.dumps(public, sort_keys=True)
        for forbidden in (
            "private-slug", "product-private", "option-private", "Private Street",
            "40.177", "44.513", "match_digest", "store_digest",
        ):
            assert forbidden not in public_text

        private_text = json.dumps(storage.value, sort_keys=True)
        assert storage.value["version"] == 1
        assert storage.value["schema_version"] == 2
        assert "kitchen-private-slug" in private_text
        assert "address" not in private_text.casefold()
        for forbidden in (
            "product-private", "product-external-private", "store-product-private",
            "option-private", "option-external-private", "group-private",
            "Private Street 10", "Private details", "Home private tag", "private-floor",
            "40.177", "44.513", '"generation"', '"price"', '"basket"',
        ):
            assert forbidden not in private_text

        reloaded = packages.PackageLibrary(storage)
        await reloaded.async_load()
        assert await reloaded.async_list() == public
        found_package, revision = await reloaded.async_prepare_records(
            package_key="USUAL KFC"
        )
        assert found_package.package_ref == package.package_ref
        assert revision == 1

        with pytest.raises(packages.PackageLibraryUnavailable):
            await reloaded.async_delete_package(
                expected_store_revision=0,
                package_ref=package.package_ref,
                expected_revision=package.revision,
            )

    asyncio.run(scenario())


def test_package_has_no_address_binding_or_alias_dependency(modules: dict[str, ModuleType]) -> None:
    async def scenario() -> None:
        library, storage, _objects, package = await populated_library(modules)
        prepared, revision = await library.async_prepare_records(
            package_key=package.package_ref
        )
        assert prepared == package
        assert revision == 1
        assert not hasattr(package, "address_ref")
        assert "address" not in json.dumps(storage.value, sort_keys=True).casefold()

    asyncio.run(scenario())


def test_exact_recipe_reconciliation_and_closed_drift(modules: dict[str, ModuleType]) -> None:
    packages = modules["ordering_packages"]
    objects = domain_objects(modules["ordering_contracts"])
    items = packages.recipe_items_from_capture(
        ((objects["product"], 2, ((objects["group"], (objects["option"],)),)),)
    )
    package = packages.PackageRecord(
        "pkg-" + "c" * 32,
        1,
        "KFC Lunch",
        ("usual",),
        objects["store"].slug,
        packages.store_digest(objects["store"]),
        objects["store"].name,
        items,
    )
    reconciled = packages.reconcile_recipe(
        package, objects["store"], (objects["product"],)
    )
    assert reconciled[0][0] is objects["product"]
    assert reconciled[0][1] == 2

    changed_product = replace(objects["product"], product_id="other-private")
    with pytest.raises(packages.PackageStale, match="unavailable or invalid") as err:
        packages.reconcile_recipe(package, objects["store"], (changed_product,))
    assert err.value.reason == "product_missing_or_changed"

    changed_option = replace(objects["option"], key="other-option")
    changed_group = replace(objects["group"], options=(changed_option,))
    with pytest.raises(packages.PackageStale) as err:
        packages.reconcile_recipe(
            package,
            objects["store"],
            (replace(objects["product"], option_groups=(changed_group,)),),
        )
    assert err.value.reason == "option_missing_or_changed"

    changed_option_identity = replace(
        objects["option"], external_id="other-option-external"
    )
    group_with_changed_option_identity = replace(
        objects["group"], options=(changed_option_identity,)
    )
    with pytest.raises(packages.PackageStale) as err:
        packages.reconcile_recipe(
            package,
            objects["store"],
            (
                replace(
                    objects["product"],
                    option_groups=(group_with_changed_option_identity,),
                ),
            ),
        )
    assert err.value.reason == "option_missing_or_changed"

    changed_group_identity = replace(
        objects["group"], external_id="other-group-external"
    )
    with pytest.raises(packages.PackageStale) as err:
        packages.reconcile_recipe(
            package,
            objects["store"],
            (replace(objects["product"], option_groups=(changed_group_identity,)),),
        )
    assert err.value.reason == "option_missing_or_changed"

    newly_required = modules["ordering_contracts"].CatalogOptionGroup(
        "new-group",
        "new-group-external",
        "New required choice",
        1,
        1,
        2,
        False,
        False,
        (objects["option"],),
    )
    with pytest.raises(packages.PackageStale) as err:
        packages.reconcile_recipe(
            package,
            objects["store"],
            (replace(objects["product"], option_groups=(objects["group"], newly_required)),),
        )
    assert err.value.reason == "selection_constraints_changed"


def test_package_names_are_unicode_display_text_with_stable_lookup_keys(
    modules: dict[str, ModuleType],
) -> None:
    packages = modules["ordering_packages"]
    assert packages.normalize_package_name("  Ընտանեկան ընթրիք!  ") == "Ընտանեկան ընթրիք!"
    assert packages.normalize_lookup_key("Ընտանեկան ընթրիք!") == "ընտանեկան-ընթրիք!"
    assert packages.normalize_lookup_key("KFC_lunch") == packages.normalize_lookup_key(
        "KFC-lunch"
    )
    assert packages.normalize_lookup_key("KFC lunch") == packages.normalize_lookup_key(
        "KFC-lunch"
    )
    with pytest.raises(packages.PackageLibraryError):
        packages.normalize_package_name("x" * 49)
    with pytest.raises(packages.PackageLibraryError):
        packages.normalize_package_name("bad\u0000name")


def test_strict_parser_and_storage_failure_is_library_local(modules: dict[str, ModuleType]) -> None:
    async def scenario() -> None:
        packages = modules["ordering_packages"]
        library, storage, objects, _package = await populated_library(modules)
        raw = json.loads(json.dumps(storage.value))
        raw["unknown"] = True
        with pytest.raises(packages.PackageLibraryError):
            packages.parse_library_image(raw)
        raw = json.loads(json.dumps(storage.value))
        raw["revision"] = True
        with pytest.raises(packages.PackageLibraryError):
            packages.parse_library_image(raw)

        class FailingStorage:
            async def async_load(self) -> Any:
                return storage.value

            async def async_save(self, _value: dict[str, Any]) -> None:
                raise RuntimeError("private provider payload must not escape")

        failing = packages.PackageLibrary(FailingStorage())
        await failing.async_load()
        with pytest.raises(packages.PackageLibraryUnavailable, match="unavailable or invalid"):
            await failing.async_delete_package(
                expected_store_revision=1,
                package_ref="pkg-" + "b" * 32,
                expected_revision=1,
            )
        assert failing.loaded is True
        assert failing.writable is False
        assert (await failing.async_list())["packages"]
        assert objects["address"].address_line not in str(packages.PackageLibraryUnavailable())

    asyncio.run(scenario())


def test_v1_migrates_bundle_losslessly_and_discards_obsolete_addresses(
    modules: dict[str, ModuleType],
) -> None:
    async def scenario() -> None:
        packages = modules["ordering_packages"]
        _library, storage, _objects, package = await populated_library(modules)
        legacy = json.loads(json.dumps(storage.value))
        legacy.pop("schema_version")
        legacy["address_aliases"] = [
            {
                "alias_ref": "addr-" + "a" * 32,
                "revision": 7,
                "name": "Synthetic destination",
                "match_digest": "d" * 64,
            }
        ]
        legacy["packages"][0]["address_ref"] = "addr-" + "a" * 32
        legacy["packages"][0]["address_revision"] = 7

        migrated_storage = packages.MemoryPackageLibraryStorage(legacy)
        migrated = packages.PackageLibrary(migrated_storage)
        await migrated.async_load()
        public = await migrated.async_list()
        assert public["packages"][0]["packageRef"] == package.package_ref
        assert public["packages"][0]["items"] == [
            {"label": "Meal", "quantity": 2, "options": ["Standard"]}
        ]
        assert migrated_storage.value["schema_version"] == 2
        assert "address_aliases" not in migrated_storage.value
        assert "address_ref" not in migrated_storage.value["packages"][0]

    asyncio.run(scenario())


def test_malformed_v1_never_partially_migrates_useful_packages(
    modules: dict[str, ModuleType],
) -> None:
    async def scenario() -> None:
        packages = modules["ordering_packages"]
        _library, storage, _objects, _package = await populated_library(modules)
        valid = json.loads(json.dumps(storage.value))
        valid.pop("schema_version")
        valid["address_aliases"] = [
            {
                "alias_ref": "addr-" + "a" * 32,
                "revision": 1,
                "name": "Synthetic destination",
                "match_digest": "d" * 64,
            }
        ]
        valid["packages"][0].update(
            address_ref="addr-" + "a" * 32,
            address_revision=1,
        )
        malformed_images = []
        missing_address = json.loads(json.dumps(valid))
        missing_address["packages"][0]["address_ref"] = "addr-" + "f" * 32
        malformed_images.append(missing_address)
        duplicate_address = json.loads(json.dumps(valid))
        duplicate_address["address_aliases"].append(
            json.loads(json.dumps(duplicate_address["address_aliases"][0]))
        )
        malformed_images.append(duplicate_address)
        malformed_alias = json.loads(json.dumps(valid))
        malformed_alias["address_aliases"][0]["unknown"] = True
        malformed_images.append(malformed_alias)
        for raw in malformed_images:
            target = packages.MemoryPackageLibraryStorage(raw)
            library = packages.PackageLibrary(target)
            with pytest.raises(packages.PackageLibraryUnavailable):
                await library.async_load()
            assert library.loaded is False
            assert library.writable is False
            assert target.value == raw

    asyncio.run(scenario())


def test_unknown_or_malformed_v2_schema_fails_closed(modules: dict[str, ModuleType]) -> None:
    packages = modules["ordering_packages"]
    valid = {
        "version": 1,
        "schema_version": 2,
        "revision": 0,
        "account_digest": None,
        "packages": [],
    }
    assert packages.parse_library_image(valid).revision == 0
    for mutate in (
        lambda value: value.update(schema_version=3),
        lambda value: value.update(schema_version=True),
        lambda value: value.update(address_aliases=[]),
        lambda value: value.update(packages={}),
    ):
        malformed = json.loads(json.dumps(valid))
        mutate(malformed)
        with pytest.raises(packages.PackageLibraryError):
            packages.parse_library_image(malformed)


def test_home_assistant_store_envelope_stays_v1_while_payload_is_schema_v2(
    modules: dict[str, ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    packages = modules["ordering_packages"]
    calls: dict[str, Any] = {}

    class FakeStore:
        def __init__(self, hass: Any, version: int, key: str) -> None:
            calls.update(hass=hass, version=version, key=key)

        async def async_load(self) -> dict[str, Any]:
            return {
                "version": 1,
                "schema_version": 2,
                "revision": 0,
                "account_digest": None,
                "packages": [],
            }

        async def async_save(self, value: dict[str, Any]) -> None:
            calls["saved"] = value

    homeassistant = ModuleType("homeassistant")
    helpers = ModuleType("homeassistant.helpers")
    storage_module = ModuleType("homeassistant.helpers.storage")
    setattr(storage_module, "Store", FakeStore)
    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.storage", storage_module)

    hass = object()
    storage = packages.HomeAssistantPackageLibraryStorage(hass, "entry-synthetic")
    assert calls == {
        "hass": hass,
        "version": 1,
        "key": "glovo.ordering_packages_v1.entry-synthetic",
    }
    loaded = asyncio.run(storage.async_load())
    assert loaded["schema_version"] == 2
    asyncio.run(storage.async_save(loaded))
    assert calls["saved"] == loaded


def test_cancelled_save_finishes_persistence_before_publish(modules: dict[str, ModuleType]) -> None:
    async def scenario() -> None:
        packages = modules["ordering_packages"]
        objects = domain_objects(modules["ordering_contracts"])

        class BarrierStorage(packages.MemoryPackageLibraryStorage):
            def __init__(self) -> None:
                super().__init__()
                self.started = asyncio.Event()
                self.release = asyncio.Event()
                self.block = False

            async def async_save(self, value: dict[str, Any]) -> None:
                if self.block:
                    self.started.set()
                    await self.release.wait()
                await super().async_save(value)

        storage = BarrierStorage()
        library = packages.PackageLibrary(
            storage, ref_source=lambda _prefix: "pkg-" + "e" * 32
        )
        await library.async_load()
        storage.block = True
        task = asyncio.create_task(
            library.async_save_package(
                expected_store_revision=0,
                package_ref="",
                expected_revision=0,
                name="Saved bundle",
                aliases=(),
                store=objects["store"],
                items=packages.recipe_items_from_capture(
                    ((objects["product"], 1, ()),)
                ),
                current_account_digest=packages.account_digest(objects["customer"]),
            )
        )
        await storage.started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert task.done() is False
        storage.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert library.revision == 1
        assert storage.value["revision"] == 1
        assert (await library.async_list())["packages"][0]["name"] == "Saved bundle"

    asyncio.run(scenario())


def test_cancelled_initial_load_never_publishes_empty_writable_state(
    modules: dict[str, ModuleType],
) -> None:
    async def scenario() -> None:
        packages = modules["ordering_packages"]

        class BlockingLoadStorage:
            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.release = asyncio.Event()
                self.saved = False

            async def async_load(self) -> dict[str, Any]:
                self.started.set()
                await self.release.wait()
                return {
                    "version": 1,
                    "revision": 7,
                    "account_digest": None,
                    "addresses": [],
                    "packages": [],
                }

            async def async_save(self, _value: dict[str, Any]) -> None:
                self.saved = True

        storage = BlockingLoadStorage()
        library = packages.PackageLibrary(storage)
        task = asyncio.create_task(library.async_load())
        await storage.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert library.loaded is False
        assert library.writable is False
        assert storage.saved is False
        with pytest.raises(packages.PackageLibraryUnavailable):
            await library.async_list()

    asyncio.run(scenario())


def test_package_save_accepts_owner_bound_draft_after_live_mutation_lease_expires(
    modules: dict[str, ModuleType],
) -> None:
    async def scenario() -> None:
        api = modules["ordering_live_api"]
        packages = modules["ordering_packages"]
        selections_module = modules["ordering_live_selection"]
        objects = domain_objects(modules["ordering_contracts"])

        class Clock:
            value = 1.0

            def __call__(self) -> float:
                return self.value

        clock = Clock()
        selections = selections_module.LiveSelectionRegistry(
            clock=clock,
            handle_source=iter(
                ("store-local", "product-local", "group-local", "option-local")
            ).__next__,
        )
        store_public = selections.issue_store(
            objects["store"], owner="admin-owner", generation=1
        )
        menu_public = selections.issue_menu(
            store_public["storeHandle"],
            modules["ordering_contracts"].CatalogMenu(
                "LIST_VIEW_LAYOUT", objects["store"].address_id, (objects["product"],)
            ),
            owner="admin-owner",
            generation=1,
        )
        product_handle = menu_public["products"][0]["productHandle"]

        library = packages.PackageLibrary(
            packages.MemoryPackageLibraryStorage(),
            ref_source=lambda _prefix: "pkg-" + "c" * 32,
        )
        await library.async_load()

        class Account:
            async def async_customer(self) -> Any:
                return objects["customer"]

        class Forbidden:
            def __getattr__(self, name: str) -> Any:
                raise AssertionError(f"local package save reached forbidden client: {name}")

        facade = api.LiveOrderingFacade(
            account=Account(),
            catalog=Forbidden(),
            selections=selections,
            baskets=Forbidden(),
            quotes=Forbidden(),
            confirmations=Forbidden(),
            preparation_authority=Forbidden(),
            package_library=library,
        )

        clock.value += selections.selection_ttl_seconds
        with pytest.raises(selections_module.LiveSelectionError):
            selections.compile_intent(
                owner="admin-owner",
                generation=1,
                customer_id=objects["customer"].customer_id,
                store_handle=store_public["storeHandle"],
                selections=selections_module.parse_selected_products(
                    [
                        {
                            "productHandle": product_handle,
                            "quantity": 2,
                            "options": [],
                        }
                    ]
                ),
            )

        saved = await facade.async_dispatch(
            owner="admin-owner",
            operation="library/package_save",
            request={
                "generation": 1,
                "expectedStoreRevision": 0,
                "packageRef": "",
                "expectedRevision": 0,
                "name": "Late local draft",
                "aliases": [],
                "storeHandle": store_public["storeHandle"],
                "products": [
                    {
                        "productHandle": product_handle,
                        "quantity": 2,
                        "options": [],
                    }
                ],
            },
        )
        assert saved["package"]["name"] == "Late local draft"
        assert saved["package"]["itemCount"] == 2
        assert "address" not in json.dumps(saved).casefold()

    asyncio.run(scenario())


def test_package_prepare_is_get_only_and_rematerializes_fresh_handles(
    modules: dict[str, ModuleType],
) -> None:
    async def scenario() -> None:
        contracts = modules["ordering_contracts"]
        api = modules["ordering_live_api"]
        library, _storage, objects, package = await populated_library(modules)
        calls: list[str] = []

        class Account:
            async def async_customer(self) -> Any:
                calls.append("customer_get")
                return objects["customer"]

            def resolve_address(self, handle: str, **kwargs: Any) -> Any:
                calls.append("address_handle_resolve")
                assert handle == "fresh-address-handle"
                assert kwargs == {"owner_key": "admin-owner", "generation": 1}
                return objects["address"]

        class Catalog:
            async def async_store(self, slug: str, address: Any) -> Any:
                calls.append("store_get")
                assert slug == "kitchen-private-slug"
                assert address is objects["address"]
                return objects["store"]

            async def async_menu(self, store: Any, address: Any) -> Any:
                calls.append("menu_get")
                assert store is objects["store"]
                assert address is objects["address"]
                return contracts.CatalogMenu(
                    "LIST_VIEW_LAYOUT", store.address_id, (objects["product"],)
                )

        class Selections:
            def issue_reconciled_selection(self, **kwargs: Any) -> tuple[Any, Any, Any]:
                calls.append("fresh_handle_issue")
                assert kwargs["address_handle"] == "fresh-address-handle"
                return (
                    {"storeHandle": "fresh-store", "label": "Kitchen"},
                    {"storeHandle": "fresh-store", "products": []},
                    {
                        "products": [
                            {
                                "productHandle": "fresh-product",
                                "quantity": 2,
                                "options": [],
                            }
                        ]
                    },
                )

            def invalidate(self) -> None:
                raise AssertionError("prepare must not invalidate selections")

        class ForbiddenMutationClient:
            def __getattr__(self, name: str) -> Any:
                raise AssertionError(
                    f"package prepare reached forbidden mutation client: {name}"
                )

        class Confirmations:
            def invalidate_all(self) -> None:
                raise AssertionError("package prepare must not touch confirmations")

        facade = api.LiveOrderingFacade(
            account=Account(),
            catalog=Catalog(),
            selections=Selections(),
            baskets=ForbiddenMutationClient(),
            quotes=ForbiddenMutationClient(),
            confirmations=Confirmations(),
            preparation_authority=ForbiddenMutationClient(),
            package_library=library,
        )
        result = await facade.async_dispatch(
            owner="admin-owner",
            operation="library/package_prepare",
            request={
                "generation": 1,
                "packageKey": package.package_ref,
                "addressHandle": "fresh-address-handle",
            },
        )
        assert result["status"] == "ready"
        assert result["selectionComplete"] is True
        assert result["selection"]["products"][0]["productHandle"] == "fresh-product"
        assert result["address"]["fullAddress"] == "Private Street 10"
        assert calls == [
            "customer_get",
            "address_handle_resolve",
            "store_get",
            "menu_get",
            "fresh_handle_issue",
        ]

        calls.clear()

        class MissingCatalog(Catalog):
            async def async_store(self, slug: str, address: Any) -> Any:
                calls.append("store_get")
                assert slug == "kitchen-private-slug"
                assert address is objects["address"]
                raise modules["api_session"].ApiSessionError(
                    category="http", endpoint_family="catalog", status=404
                )

        missing_facade = api.LiveOrderingFacade(
            account=Account(),
            catalog=MissingCatalog(),
            selections=Selections(),
            baskets=ForbiddenMutationClient(),
            quotes=ForbiddenMutationClient(),
            confirmations=Confirmations(),
            preparation_authority=ForbiddenMutationClient(),
            package_library=library,
        )
        missing = await missing_facade.async_dispatch(
            owner="admin-owner",
            operation="library/package_prepare",
            request={
                "generation": 1,
                "packageKey": package.package_ref,
                "addressHandle": "fresh-address-handle",
            },
        )
        assert missing == {
            "status": "stale",
            "packageRef": package.package_ref,
            "packageRevision": package.revision,
            "reason": "store_missing_or_changed",
        }
        assert calls == ["customer_get", "address_handle_resolve", "store_get"]

        calls.clear()

        class ClosedCatalog(Catalog):
            async def async_store(self, slug: str, address: Any) -> Any:
                calls.append("store_get")
                assert slug == "kitchen-private-slug"
                assert address is objects["address"]
                return replace(objects["store"], is_open=False)

            async def async_menu(self, _store: Any, _address: Any) -> Any:
                raise AssertionError("closed package preparation must stop before menu")

        closed_facade = api.LiveOrderingFacade(
            account=Account(),
            catalog=ClosedCatalog(),
            selections=Selections(),
            baskets=ForbiddenMutationClient(),
            quotes=ForbiddenMutationClient(),
            confirmations=Confirmations(),
            preparation_authority=ForbiddenMutationClient(),
            package_library=library,
        )
        closed = await closed_facade.async_dispatch(
            owner="admin-owner",
            operation="library/package_prepare",
            request={
                "generation": 1,
                "packageKey": package.package_ref,
                "addressHandle": "fresh-address-handle",
            },
        )
        assert closed == {
            "status": "blocked",
            "packageRef": package.package_ref,
            "packageRevision": package.revision,
            "reason": "store_closed",
        }
        assert calls == ["customer_get", "address_handle_resolve", "store_get"]

    asyncio.run(scenario())


def test_basket_replace_accepts_fresh_handles_for_same_private_store_and_address(
    modules: dict[str, ModuleType],
) -> None:
    async def scenario() -> None:
        api = modules["ordering_live_api"]
        discovery = modules["ordering_remote_basket_discovery"]
        remote = modules["ordering_remote_basket"]
        objects = domain_objects(modules["ordering_contracts"])
        create_calls = 0
        replace_calls = 0

        from test_ordering_remote_basket_quotes import basket_payload, intent_payload

        def intent(quantity: int = 1) -> Any:
            intent_data = intent_payload()
            intent_data.update(
                customerId=99,
                storeId=11,
                storeAddressId=12,
                storeCategoryId=13,
            )
            intent_data["products"][0]["quantity"]["increments"] = quantity
            return remote.parse_basket_intent(intent_data)

        basket_intent = intent()

        class Account:
            def resolve_address(self, handle: str, **_kwargs: Any) -> Any:
                assert handle in {"address-first", "address-fresh"}
                return objects["address"]

            async def async_customer(self) -> Any:
                return objects["customer"]

            async def async_saved_payments(self, **_kwargs: Any) -> Any:
                raise AssertionError("closed store must not issue payment handles")

            def invalidate(self) -> None:
                return None

        class Selections:
            def resolve_store(self, handle: str, **_kwargs: Any) -> Any:
                assert handle in {"store-first", "store-fresh"}
                return objects["store"]

            def capture_selection(self, **kwargs: Any) -> tuple[Any, ...]:
                return ((objects["product"], kwargs["selections"][0].quantity, ()),)

            def safe_lines(
                self, captured: Any, product_handles: list[str]
            ) -> list[dict[str, Any]]:
                return [
                    {
                        "productHandle": product_handles[0],
                        "label": "Meal",
                        "quantity": captured[0][1],
                        "unitPriceMinor": 500,
                        "currency": "AMD",
                        "options": [],
                    }
                ]

            def compile_intent(self, **kwargs: Any) -> Any:
                return intent(kwargs["selections"][0].quantity)

            def product_currency(self, *_args: Any, **_kwargs: Any) -> str:
                return "AMD"

            def invalidate(self) -> None:
                return None

        def snapshot(total: int | None, *, version: str, quantity: int = 1) -> Any:
            payload = basket_payload(version=version, quantity=quantity)
            payload.update(
                customerId=99,
                storeId=11,
                storeAddressId=12,
                storeCategoryId=13,
            )
            payload["basketPrice"]["final"]["minor"] = total
            return remote.parse_remote_basket(payload, intent(quantity))

        class Discovery:
            calls = 0

            async def async_discover(
                self, selected: Any, delivery_location: Any
            ) -> Any:
                self.calls += 1
                assert selected == basket_intent
                assert delivery_location.transport_context() == {
                    "countryCode": "AM",
                    "cityCode": "YRV",
                    "latitude": "40.177",
                    "longitude": "44.513",
                }
                return discovery.RemoteBasketDiscoveryResult(
                    discovery.RemoteBasketDiscoveryStatus.ABSENT_VERIFIED
                )

        discovery_client = Discovery()

        class Baskets:
            async def async_create(self, _intent: Any, delivery_location: Any) -> Any:
                nonlocal create_calls
                assert delivery_location.transport_context() == {
                    "countryCode": "AM",
                    "cityCode": "YRV",
                    "latitude": "40.177",
                    "longitude": "44.513",
                }
                create_calls += 1
                return snapshot(500, version="basket-v1")

            async def async_replace(
                self, previous: Any, products: Any, delivery_location: Any
            ) -> Any:
                nonlocal replace_calls
                assert delivery_location.transport_context() == {
                    "countryCode": "AM",
                    "cityCode": "YRV",
                    "latitude": "40.177",
                    "longitude": "44.513",
                }
                replace_calls += 1
                assert previous.basket_price.minor == 500
                assert len(products) == 1
                return snapshot(
                    None,
                    version="basket-v2",
                    quantity=products[0].quantity,
                )

        class Authority:
            loaded = True
            unresolved: tuple[Any, ...] = ()

            async def async_acquire(self, **_kwargs: Any) -> Any:
                return SimpleNamespace(attempt_id="prep-safe", record_revision=1)

            async def async_record_outcome(self, **_kwargs: Any) -> None:
                return None

        class Confirmations:
            def invalidate(self, _owner: str) -> None:
                return None

            def invalidate_all(self) -> None:
                return None

        class Catalog:
            def __init__(self) -> None:
                self.closed = False

            async def async_store(self, slug: str, address: Any) -> Any:
                assert slug == objects["store"].slug
                assert address is objects["address"]
                return replace(objects["store"], is_open=not self.closed)

        catalog = Catalog()

        facade = api.LiveOrderingFacade(
            account=Account(),
            catalog=catalog,
            selections=Selections(),
            baskets=Baskets(),
            quotes=SimpleNamespace(),
            confirmations=Confirmations(),
            preparation_authority=Authority(),
            preparation_attempt_source=lambda: "prep-" + "a" * 32,
            discovery_client=discovery_client,
        )
        empty = await facade.async_dispatch(
            owner="admin-owner",
            operation="live/basket",
            request={"generation": 1},
        )
        assert empty == {"status": "unknown"}
        adopted_absence = await facade.async_dispatch(
            owner="admin-owner",
            operation="live/basket_adopt",
            request={
                "generation": 1,
                "storeHandle": "store-first",
                "addressHandle": "address-first",
                "products": [
                    {
                        "productHandle": "product-first",
                        "quantity": 1,
                        "options": [],
                    }
                ],
            },
        )
        assert adopted_absence["status"] == "absent_verified"
        assert discovery_client.calls == 1
        assert create_calls == 0
        first = await facade.async_dispatch(
            owner="admin-owner",
            operation="live/basket_set",
            request={
                "generation": 1,
                "expectedRevision": 0,
                "storeHandle": "store-first",
                "addressHandle": "address-first",
                "products": [
                    {"productHandle": "product-first", "quantity": 1, "options": []}
                ],
            },
        )
        second = await facade.async_dispatch(
            owner="admin-owner",
            operation="live/basket_set",
            request={
                "generation": 1,
                "expectedRevision": 1,
                "storeHandle": "store-fresh",
                "addressHandle": "address-fresh",
                "products": [
                    {"productHandle": "product-fresh", "quantity": 2, "options": []}
                ],
            },
        )
        assert first["providerTotal"] == 500
        assert first["status"] == "adopted"
        assert second["providerTotal"] is None
        assert second["status"] == "adopted"
        assert second["revision"] == 2
        assert second["storeHandle"] == "store-fresh"
        assert second["lines"][0]["productHandle"] == "product-fresh"
        assert create_calls == 1
        assert replace_calls == 1
        catalog.closed = True
        with pytest.raises(api.PublicContractError):
            await facade.async_dispatch(
                owner="admin-owner",
                operation="live/payment_methods",
                request={"generation": 1},
            )
        with pytest.raises(api.PublicContractError):
            await facade.async_dispatch(
                owner="admin-owner",
                operation="live/basket_set",
                request={
                    "generation": 1,
                    "expectedRevision": 2,
                    "storeHandle": "store-fresh",
                    "addressHandle": "address-fresh",
                    "products": [
                        {
                            "productHandle": "product-fresh",
                            "quantity": 3,
                            "options": [],
                        }
                    ],
                },
            )
        assert create_calls == 1
        assert replace_calls == 1
        current = await facade.async_dispatch(
            owner="admin-owner",
            operation="live/basket",
            request={"generation": 1},
        )
        assert current["revision"] == 2
        assert current["providerTotal"] is None

    asyncio.run(scenario())
