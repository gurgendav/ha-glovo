"""Tests for the default-off, fixture-only ordering safety foundation."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]
ORDERING_MODULES = (
    "ordering_models",
    "ordering_basket",
    "ordering_quote",
    "ordering_catalog",
    "ordering_journal",
    "ordering_state",
    "ordering_adapter",
    "ordering_manager",
    "ordering_surface",
)


@pytest.fixture()
def ordering() -> dict[str, ModuleType]:
    """Load pure ordering modules without importing Home Assistant."""
    package_name = "glovo_ordering_under_test"
    package = ModuleType(package_name)
    package.__path__ = []  # type: ignore[attr-defined]
    sys.modules[package_name] = package
    loaded: dict[str, ModuleType] = {}
    root = ROOT / "custom_components" / "glovo"
    try:
        for name in ORDERING_MODULES:
            module_name = f"{package_name}.{name}"
            spec = importlib.util.spec_from_file_location(module_name, root / f"{name}.py")
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


class FakeClock:
    def __init__(self, value: float = 1_800_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class ChallengeSource:
    def __init__(self) -> None:
        self.number = 0

    def __call__(self) -> str:
        self.number += 1
        return f"synthetic-challenge-{self.number:04d}-xxxxxxxxxxxxxxxx"


async def make_manager(
    ordering: dict[str, ModuleType],
    *,
    enabled: object = True,
    acknowledged: object,
    clock: FakeClock | None = None,
    storage_data: Any = None,
) -> tuple[Any, Any, Any, FakeClock]:
    clock = clock or FakeClock()
    catalog = ordering["ordering_catalog"].SyntheticCatalogProvider(clock=clock)
    storage = ordering["ordering_journal"].MemoryJournalStorage(storage_data)
    journal = ordering["ordering_journal"].AttemptJournal(storage, clock=clock)
    adapter = ordering["ordering_adapter"].MockCheckoutAdapter()
    manager = ordering["ordering_manager"].OrderingManager(
        allow_ordering=enabled,
        ordering_acknowledged=acknowledged,
        catalog=catalog,
        journal=journal,
        checkout_adapter=adapter,
        clock=clock,
        challenge_source=ChallengeSource(),
    )
    await manager.async_initialize()
    return manager, storage, adapter, clock


def admin(ordering: dict[str, ModuleType], user_id: str = "admin-a") -> Any:
    return ordering["ordering_manager"].OrderingUser(user_id=user_id, is_admin=True)


def non_admin(ordering: dict[str, ModuleType]) -> Any:
    return ordering["ordering_manager"].OrderingUser(user_id="ordinary-user", is_admin=False)


async def add_default_item(ordering: dict[str, ModuleType], manager: Any, user: Any) -> int:
    generation = manager.generation
    await manager.async_add_item(
        user,
        generation,
        store_key="fixture-store",
        product_key="fixture-meal",
        variant_key="standard",
        modifier_keys=("no-change",),
        quantity=1,
    )
    return generation


async def quote_default(ordering: dict[str, ModuleType], manager: Any, user: Any) -> dict[str, Any]:
    generation = manager.generation
    return await manager.async_quote(
        user,
        generation,
        address_key="masked-destination",
        payment_key="masked-payment",
    )


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_money_currency_and_reference_models_are_strict_and_redaction_safe(
    ordering: dict[str, ModuleType],
) -> None:
    models = ordering["ordering_models"]
    assert models.Money(624000, "AMD").action_amount == "6,240.00 AMD"
    for bad_amount in (True, -1, 1.5, "1"):
        with pytest.raises((TypeError, ValueError)):
            models.Money(bad_amount, "AMD")
    for bad_currency in ("amd", "US", "EURO", "12A"):
        with pytest.raises(ValueError):
            models.Money(1, bad_currency)

    payment = models.MaskedPaymentSummary("local-choice", "Test card •••• 4242")
    assert "local-choice" not in repr(payment)
    assert payment.public_dict() == {"key": "local-choice", "label": "Test card •••• 4242"}
    with pytest.raises(ValueError):
        models.MaskedPaymentSummary("choice", "Unmasked payment digits 12345")

    address = models.SavedAddressSummary("local-destination", "Saved destination ••••")
    assert address.public_dict() == {
        "key": "local-destination",
        "label": "Saved destination ••••",
    }
    for raw_label in (
        "123 Synthetic Street",
        "123 Synthetic Street ••••",
        "Saved destination",
        "Saved\ndestination ••••",
        f"{'A' * 41} ••••",
    ):
        with pytest.raises(ValueError):
            models.SavedAddressSummary("raw-destination", raw_label)

    secret_data = {
        "safeTopLevel": "ok",
        "nested": {
            "Authorization": "synthetic-non-secret",
            "auth-header": "synthetic-non-secret",
            "PASSWORD": "synthetic-non-secret",
            "pass_phrase": "synthetic-non-secret",
            "Api-Key": "synthetic-non-secret",
            "set_cookie": "synthetic-non-secret",
            "access-token": "synthetic-non-secret",
            "Refresh_Token": "synthetic-non-secret",
            "sessionCredential": "synthetic-non-secret",
            "session-secret": "synthetic-non-secret",
            "Bearer": "synthetic-non-secret",
            "safe": "ok",
            "purchaseTotalCents": 624000,
            "checkoutSessionId": "fixture-session",
            "versionId": "fixture-v1",
        },
    }
    redacted = models.redact_mapping(secret_data)
    assert json.dumps(redacted).count("synthetic-non-secret") == 0
    assert redacted["nested"]["safe"] == "ok"
    assert redacted["nested"]["purchaseTotalCents"] == 624000
    assert redacted["nested"]["checkoutSessionId"] == "fixture-session"
    assert redacted["nested"]["versionId"] == "fixture-v1"


@pytest.mark.parametrize("enabled", [None, False, 0, 1, "true", {}, []])
def test_ordering_is_default_off_unless_option_is_literal_true(
    ordering: dict[str, ModuleType], enabled: object
) -> None:
    manager, _, _, _ = run(make_manager(ordering, enabled=enabled, acknowledged=True))
    assert manager.enabled is False
    with pytest.raises(ordering["ordering_manager"].OrderingDisabled):
        run(manager.async_state(admin(ordering)))


@pytest.mark.parametrize("acknowledged", [pytest.param("omitted"), None, False])
def test_omitted_none_or_false_acknowledgement_cannot_initialize_quote_or_execute(
    ordering: dict[str, ModuleType], acknowledged: object
) -> None:
    async def build() -> tuple[Any, Any]:
        clock = FakeClock()
        adapter = ordering["ordering_adapter"].MockCheckoutAdapter()
        kwargs = (
            {}
            if acknowledged == "omitted"
            else {"ordering_acknowledged": acknowledged}
        )
        manager = ordering["ordering_manager"].OrderingManager(
            allow_ordering=True,
            catalog=ordering["ordering_catalog"].SyntheticCatalogProvider(clock=clock),
            journal=ordering["ordering_journal"].AttemptJournal(
                ordering["ordering_journal"].MemoryJournalStorage(), clock=clock
            ),
            checkout_adapter=adapter,
            clock=clock,
            **kwargs,
        )
        await manager.async_initialize()
        return manager, adapter

    manager, adapter = run(build())
    user = admin(ordering)
    assert manager.enabled is False
    with pytest.raises(ordering["ordering_manager"].OrderingDisabled):
        run(
            manager.async_quote(
                user,
                manager.generation,
                address_key="masked-destination",
                payment_key="masked-payment",
            )
        )
    with pytest.raises(ordering["ordering_manager"].OrderingDisabled):
        run(
            manager.async_execute_mock_checkout(
                user, manager.generation, "synthetic-challenge", "f" * 64
            )
        )
    assert adapter.execution_count == 0


def test_enable_requires_explicit_literal_true_acknowledgement(
    ordering: dict[str, ModuleType],
) -> None:
    manager, _, adapter, _ = run(
        make_manager(ordering, enabled=True, acknowledged=True)
    )
    user = admin(ordering)
    generation = run(add_default_item(ordering, manager, user))
    quote = run(quote_default(ordering, manager, user))
    confirmation = run(
        manager.async_prepare_mock_confirmation(user, generation, quote["fingerprint"])
    )
    run(manager.async_set_enabled(False))

    for acknowledged in ("omitted", None, False):
        if acknowledged == "omitted":
            run(manager.async_set_enabled(True))
        else:
            run(manager.async_set_enabled(True, acknowledged=acknowledged))
        assert manager.enabled is False
        with pytest.raises(ordering["ordering_manager"].OrderingDisabled):
            run(
                manager.async_quote(
                    user,
                    manager.generation,
                    address_key="masked-destination",
                    payment_key="masked-payment",
                )
            )
        with pytest.raises(ordering["ordering_manager"].OrderingDisabled):
            run(
                manager.async_execute_mock_checkout(
                    user,
                    manager.generation,
                    confirmation["challenge"],
                    confirmation["fingerprint"],
                )
            )
    assert adapter.execution_count == 0


def test_enable_disable_bumps_generation_and_invalidates_all_ephemeral_state(
    ordering: dict[str, ModuleType],
) -> None:
    manager, _, _, _ = run(make_manager(ordering, enabled=True, acknowledged=True))
    user = admin(ordering)
    old_generation = run(add_default_item(ordering, manager, user))
    quote = run(quote_default(ordering, manager, user))
    confirmation = run(
        manager.async_prepare_mock_confirmation(user, old_generation, quote["fingerprint"])
    )

    run(manager.async_set_enabled(False))
    assert manager.enabled is False
    assert manager.generation > old_generation
    run(manager.async_set_enabled(True, acknowledged=True))
    assert manager.enabled is True
    assert manager.generation > old_generation
    with pytest.raises(ordering["ordering_manager"].StaleOrderingGeneration):
        run(manager.async_get_basket(user, old_generation))
    with pytest.raises(ordering["ordering_manager"].InvalidConfirmation):
        run(
            manager.async_execute_mock_checkout(
                user,
                manager.generation,
                confirmation["challenge"],
                confirmation["fingerprint"],
            )
        )
    assert run(manager.async_get_basket(user, manager.generation))["items"] == []


def test_non_admin_is_rejected_from_every_surface(ordering: dict[str, ModuleType]) -> None:
    manager, _, _, _ = run(make_manager(ordering, acknowledged=True))
    user = non_admin(ordering)
    methods = (
        manager.async_state(user),
        manager.async_catalog(user, manager.generation),
        manager.async_get_basket(user, manager.generation),
    )
    for method in methods:
        with pytest.raises(ordering["ordering_manager"].OrderingAdminRequired):
            run(method)


def test_basket_is_user_owned_one_store_bounded_and_ttl_bound(
    ordering: dict[str, ModuleType],
) -> None:
    clock = FakeClock()
    manager, _, _, _ = run(make_manager(ordering, acknowledged=True, clock=clock))
    first = admin(ordering, "admin-a")
    second = admin(ordering, "admin-b")
    generation = run(add_default_item(ordering, manager, first))
    assert run(manager.async_get_basket(second, generation))["items"] == []
    with pytest.raises(ordering["ordering_basket"].BasketStoreMismatch):
        run(
            manager.async_add_item(
                first,
                generation,
                store_key="different-fixture-store",
                product_key="fixture-meal",
                variant_key="standard",
                modifier_keys=(),
                quantity=1,
            )
        )
    with pytest.raises(ordering["ordering_basket"].BasketLimitExceeded):
        run(
            manager.async_add_item(
                first,
                generation,
                store_key="fixture-store",
                product_key="fixture-meal",
                variant_key="standard",
                modifier_keys=(),
                quantity=999,
            )
        )
    clock.advance(manager.basket_ttl_seconds + 1)
    assert run(manager.async_get_basket(first, generation))["items"] == []


def test_catalog_and_checkout_summaries_are_synthetic_read_only_and_masked(
    ordering: dict[str, ModuleType],
) -> None:
    manager, _, _, _ = run(make_manager(ordering, acknowledged=True))
    payload = run(manager.async_catalog(admin(ordering), manager.generation))
    assert payload["provider"] == "synthetic_fixture"
    assert payload["stores"][0]["key"] == "fixture-store"
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "https://" not in encoded
    assert "latitude" not in encoded
    assert "longitude" not in encoded
    assert "••••" in encoded


def test_quote_contains_complete_authoritative_fixture_and_exact_action_text(
    ordering: dict[str, ModuleType],
) -> None:
    manager, _, _, _ = run(make_manager(ordering, acknowledged=True))
    user = admin(ordering)
    run(add_default_item(ordering, manager, user))
    quote = run(quote_default(ordering, manager, user))
    assert quote["purchaseTotalCents"] == 624000
    assert quote["currencyCode"] == "AMD"
    assert quote["actionText"] == "Run mock checkout — 6,240.00 AMD"
    assert {line["kind"] for line in quote["priceLines"]} == {
        "items",
        "delivery_fee",
        "service_fee",
        "discount",
    }
    assert {line["currencyCode"] for line in quote["priceLines"]} == {"AMD"}
    assert quote["checkoutSessionId"].startswith("fixture-")
    assert quote["expiresAt"] > 1_800_000_000


def test_fixture_parser_fails_closed_on_schema_drift_bad_total_and_expiry(
    ordering: dict[str, ModuleType],
) -> None:
    quote_module = ordering["ordering_quote"]
    payload = {
        "checkoutSessionId": "fixture-session",
        "versionId": "fixture-v1",
        "templateId": "fixture-template",
        "basketVersion": 1,
        "purchaseTotalCents": 100,
        "currencyCode": "AMD",
        "priceLines": [{"kind": "items", "label": "Items", "amountCents": 100}],
        "eta": "20–30 min",
        "expiresAt": 1_800_000_100.0,
    }
    parsed = quote_module.CheckoutFixture.from_mapping(payload, now=1_800_000_000.0)
    assert parsed.purchase_total.amount_minor == 100
    for mutation in (
        {**payload, "unexpected": "schema-drift"},
        {**payload, "purchaseTotalCents": 101},
        {**payload, "expiresAt": 1_799_999_999.0},
        {**payload, "basketVersion": True},
    ):
        with pytest.raises(quote_module.InvalidCheckoutFixture):
            quote_module.CheckoutFixture.from_mapping(mutation, now=1_800_000_000.0)


def test_confirmation_rejects_stale_quote_and_every_bound_change(
    ordering: dict[str, ModuleType],
) -> None:
    manager, _, _, _ = run(make_manager(ordering, acknowledged=True))
    user = admin(ordering)
    generation = run(add_default_item(ordering, manager, user))
    first_quote = run(quote_default(ordering, manager, user))
    confirmation = run(
        manager.async_prepare_mock_confirmation(user, generation, first_quote["fingerprint"])
    )

    # Re-quote with another masked destination invalidates the old confirmation.
    changed_quote = run(
        manager.async_quote(
            user,
            generation,
            address_key="masked-destination-alt",
            payment_key="masked-payment",
        )
    )
    assert changed_quote["fingerprint"] != confirmation["fingerprint"]
    with pytest.raises(ordering["ordering_manager"].InvalidConfirmation):
        run(
            manager.async_execute_mock_checkout(
                user,
                generation,
                confirmation["challenge"],
                confirmation["fingerprint"],
            )
        )

    # Payment and amount/line differences are fingerprinted too.
    changed_payment = run(
        manager.async_quote(
            user,
            generation,
            address_key="masked-destination",
            payment_key="masked-payment-alt",
        )
    )
    assert changed_payment["fingerprint"] not in {
        first_quote["fingerprint"],
        changed_quote["fingerprint"],
    }
    with pytest.raises(ordering["ordering_manager"].InvalidConfirmation):
        run(
            manager.async_prepare_mock_confirmation(
                user, generation, "0" * len(changed_payment["fingerprint"])
            )
        )


def test_basket_mutation_and_expiry_invalidate_confirmation(
    ordering: dict[str, ModuleType],
) -> None:
    clock = FakeClock()
    manager, _, _, _ = run(make_manager(ordering, acknowledged=True, clock=clock))
    user = admin(ordering)
    generation = run(add_default_item(ordering, manager, user))
    quote = run(quote_default(ordering, manager, user))
    confirmation = run(
        manager.async_prepare_mock_confirmation(user, generation, quote["fingerprint"])
    )
    run(
        manager.async_add_item(
            user,
            generation,
            store_key="fixture-store",
            product_key="fixture-meal",
            variant_key="standard",
            modifier_keys=("no-change",),
            quantity=1,
        )
    )
    with pytest.raises(ordering["ordering_manager"].InvalidConfirmation):
        run(
            manager.async_execute_mock_checkout(
                user,
                generation,
                confirmation["challenge"],
                confirmation["fingerprint"],
            )
        )

    quote = run(quote_default(ordering, manager, user))
    confirmation = run(
        manager.async_prepare_mock_confirmation(user, generation, quote["fingerprint"])
    )
    clock.advance(manager.confirmation_ttl_seconds + 1)
    with pytest.raises(ordering["ordering_manager"].InvalidConfirmation):
        run(
            manager.async_execute_mock_checkout(
                user,
                generation,
                confirmation["challenge"],
                confirmation["fingerprint"],
            )
        )


def test_confirmation_is_same_admin_single_use_and_mock_result_is_clear(
    ordering: dict[str, ModuleType],
) -> None:
    manager, _, adapter, _ = run(make_manager(ordering, acknowledged=True))
    owner = admin(ordering, "admin-owner")
    other = admin(ordering, "admin-other")
    generation = run(add_default_item(ordering, manager, owner))
    quote = run(quote_default(ordering, manager, owner))
    confirmation = run(
        manager.async_prepare_mock_confirmation(owner, generation, quote["fingerprint"])
    )
    with pytest.raises(ordering["ordering_manager"].InvalidConfirmation):
        run(
            manager.async_execute_mock_checkout(
                other,
                generation,
                confirmation["challenge"],
                confirmation["fingerprint"],
            )
        )
    result = run(
        manager.async_execute_mock_checkout(
            owner,
            generation,
            confirmation["challenge"],
            confirmation["fingerprint"],
        )
    )
    assert result == {
        "mock": True,
        "outcome": "synthetic_success",
        "message": "Mock checkout completed; no order was sent.",
    }
    assert adapter.execution_count == 1
    with pytest.raises(ordering["ordering_manager"].InvalidConfirmation):
        run(
            manager.async_execute_mock_checkout(
                owner,
                generation,
                confirmation["challenge"],
                confirmation["fingerprint"],
            )
        )


def test_concurrent_confirmation_attempts_serialize_to_exactly_one_success(
    ordering: dict[str, ModuleType],
) -> None:
    async def scenario() -> tuple[list[Any], Any]:
        manager, _, adapter, _ = await make_manager(ordering, acknowledged=True)
        user = admin(ordering)
        generation = await add_default_item(ordering, manager, user)
        quote = await quote_default(ordering, manager, user)
        confirmation = await manager.async_prepare_mock_confirmation(
            user, generation, quote["fingerprint"]
        )
        results = await asyncio.gather(
            *(
                manager.async_execute_mock_checkout(
                    user,
                    generation,
                    confirmation["challenge"],
                    confirmation["fingerprint"],
                )
                for _ in range(2)
            ),
            return_exceptions=True,
        )
        return results, adapter

    results, adapter = run(scenario())
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(
        isinstance(result, ordering["ordering_manager"].InvalidConfirmation)
        for result in results
    ) == 1
    assert adapter.execution_count == 1


def test_live_adapter_mode_fails_before_any_journal_attempt(ordering: dict[str, ModuleType]) -> None:
    adapter_module = ordering["ordering_adapter"]
    adapter = adapter_module.MockCheckoutAdapter()
    with pytest.raises(adapter_module.LiveOrderingUnavailable):
        run(adapter.async_execute(mode="live", fingerprint="synthetic"))
    assert adapter.execution_count == 0


def test_journal_restart_recovery_corruption_and_uncertain_preservation(
    ordering: dict[str, ModuleType],
) -> None:
    journal_module = ordering["ordering_journal"]
    clock = FakeClock()
    storage = journal_module.MemoryJournalStorage()
    journal = journal_module.AttemptJournal(storage, clock=clock)
    run(journal.async_load())
    record = run(journal.async_create("attempt-synthetic", 3, "f" * 64))
    run(journal.async_transition(record.attempt_id, journal_module.JournalState.QUOTED))
    run(journal.async_transition(record.attempt_id, journal_module.JournalState.DISPATCHING))

    restarted = journal_module.AttemptJournal(storage, clock=clock)
    records = run(restarted.async_load())
    assert records[0].state is journal_module.JournalState.LEGACY_MOCK_NO_REMOTE_EFFECT
    manager, _, _, _ = run(
        make_manager(
            ordering,
            enabled=True,
            acknowledged=True,
            clock=clock,
            storage_data=storage.data,
        )
    )
    run(manager.async_set_enabled(False))
    assert any(
        item.state is journal_module.JournalState.LEGACY_MOCK_NO_REMOTE_EFFECT
        for item in manager.journal.records
    )

    corrupted_manager, _, _, _ = run(
        make_manager(
            ordering,
            enabled=True,
            acknowledged=True,
            storage_data={"version": 1, "records": "bad"},
        )
    )
    assert corrupted_manager.enabled is False
    assert corrupted_manager.security_fault is True


def test_journal_contains_no_basket_or_secret_bearing_data(ordering: dict[str, ModuleType]) -> None:
    manager, storage, _, _ = run(make_manager(ordering, acknowledged=True))
    user = admin(ordering)
    generation = run(add_default_item(ordering, manager, user))
    quote = run(quote_default(ordering, manager, user))
    confirmation = run(
        manager.async_prepare_mock_confirmation(user, generation, quote["fingerprint"])
    )
    run(
        manager.async_execute_mock_checkout(
            user,
            generation,
            confirmation["challenge"],
            confirmation["fingerprint"],
        )
    )
    encoded = json.dumps(storage.data).lower()
    for forbidden in (
        "fixture-meal",
        "masked-destination",
        "masked-payment",
        "challenge",
        "token",
        "cookie",
        "cvv",
        "latitude",
        "longitude",
    ):
        assert forbidden not in encoded


class FakeSurfaceAdapter:
    def __init__(self) -> None:
        self.panel_registration: dict[str, Any] | None = None
        self.handlers: dict[str, Any] = {}
        self.unregistered = False

    async def async_register_panel(self, **kwargs: Any) -> None:
        self.panel_registration = kwargs

    async def async_remove_panel(self) -> None:
        self.unregistered = True
        self.panel_registration = None

    async def async_register_handler(self, name: str, handler: Any) -> None:
        self.handlers[name] = handler

    async def async_remove_handlers(self) -> None:
        self.handlers.clear()


def test_panel_and_handlers_register_only_enabled_and_are_admin_only_semantic(
    ordering: dict[str, ModuleType],
) -> None:
    async def scenario(enabled: object) -> FakeSurfaceAdapter:
        manager, _, _, _ = await make_manager(ordering, enabled=enabled, acknowledged=True)
        adapter = FakeSurfaceAdapter()
        surface = ordering["ordering_surface"].OrderingSurface(manager, adapter)
        await surface.async_setup()
        if manager.enabled:
            assert adapter.panel_registration == {
                "url_path": "glovo-ordering",
                "title": "Glovo Mock Ordering",
                "icon": "mdi:cart-outline",
                "require_admin": True,
            }
            assert "glovo/ordering/execute_mock_checkout" in adapter.handlers
            assert all("place_order" not in name for name in adapter.handlers)
            await surface.async_unload()
            assert adapter.unregistered is True
            assert adapter.handlers == {}
        return adapter

    disabled = run(scenario(False))
    assert disabled.panel_registration is None
    run(scenario(True))


def test_both_literal_runtime_gates_are_required_for_every_handler(
    ordering: dict[str, ModuleType],
) -> None:
    options: dict[str, object] = {
        "allow_ordering": True,
        "ordering_acknowledged": False,
    }
    manager, _, _, _ = run(make_manager(ordering, enabled=True, acknowledged=True))
    manager._live_options = lambda: options
    with pytest.raises(ordering["ordering_manager"].OrderingDisabled):
        run(manager.async_state(admin(ordering)))
    options["ordering_acknowledged"] = True
    assert run(manager.async_state(admin(ordering)))["mockOnly"] is True
    options.pop("allow_ordering")
    with pytest.raises(ordering["ordering_manager"].OrderingDisabled):
        run(manager.async_catalog(admin(ordering), manager.generation))


def test_no_purchase_endpoint_or_network_capability_exists_in_ordering_modules(
    ordering: dict[str, ModuleType],
) -> None:
    root = ROOT / "custom_components/glovo"
    combined = "\n".join((root / f"{name}.py").read_text().lower() for name in ORDERING_MODULES)
    forbidden = (
        "requests.",
        "aiohttp",
        "urllib",
        "httpx",
        "api.glovoapp.com",
        "place_order",
        "payment_intent",
        "checkout/submit",
        "method=\"post\"",
        "method='post'",
    )
    assert not [value for value in forbidden if value in combined]
    source = inspect.getsource(ordering["ordering_adapter"].MockCheckoutAdapter)
    assert "LiveOrderingUnavailable" in source
    assert "mode != MOCK_MODE" in source


def test_documentation_calls_feature_mock_only_default_off_admin_only() -> None:
    readme = (ROOT / "README.md").read_text().lower()
    for phrase in ("mock-only", "default-off", "admin-only", "incapable of live purchases"):
        assert phrase in readme


def test_manifest_version_and_trust_identity_are_unchanged() -> None:
    manifest = json.loads((ROOT / "custom_components/glovo/manifest.json").read_text())
    const_source = (ROOT / "custom_components/glovo/const.py").read_text()
    assert manifest["version"] == "1.1.0+home.2"
    assert "d65db609f53b5f93092f368b60398d4b231ee5aa8a3d506b25103081aa2b50da" in const_source
    assert datetime.now(UTC).tzinfo is UTC


async def build_with_durable_state(
    ordering: dict[str, ModuleType],
    state_storage: Any,
    *,
    options: dict[str, object] | None = None,
    journal_storage: Any | None = None,
    clock: FakeClock | None = None,
) -> tuple[Any, Any, Any]:
    clock = clock or FakeClock()
    options = options or {
        "allow_ordering": True,
        "ordering_acknowledged": True,
    }
    durable = ordering["ordering_state"].DurableOrderingState(state_storage)
    await durable.async_load()
    journal_storage = journal_storage or ordering["ordering_journal"].MemoryJournalStorage()
    journal = ordering["ordering_journal"].AttemptJournal(journal_storage, clock=clock)
    adapter = ordering["ordering_adapter"].MockCheckoutAdapter()
    manager = ordering["ordering_manager"].OrderingManager(
        allow_ordering=options.get("allow_ordering"),
        ordering_acknowledged=options.get("ordering_acknowledged"),
        live_options=lambda: options,
        catalog=ordering["ordering_catalog"].SyntheticCatalogProvider(clock=clock),
        journal=journal,
        checkout_adapter=adapter,
        clock=clock,
        challenge_source=ChallengeSource(),
        durable_state=durable,
    )
    await manager.async_initialize()
    return manager, adapter, journal_storage


def test_generation_is_durable_monotonic_across_disable_unload_and_rebuild(
    ordering: dict[str, ModuleType],
) -> None:
    state_storage = ordering["ordering_state"].MemoryOrderingStateStorage(
        {"version": 2, "generation": 9, "manual_check_required": False, "integrity_fault": False}
    )
    options = {"allow_ordering": True, "ordering_acknowledged": True}
    manager, _, journal_storage = run(
        build_with_durable_state(ordering, state_storage, options=options)
    )
    user = admin(ordering)
    stale = run(add_default_item(ordering, manager, user))
    assert stale == 9
    options["allow_ordering"] = False
    run(manager.async_set_enabled(False))
    assert state_storage.data["generation"] == 10

    options.update(allow_ordering=True, ordering_acknowledged=True)
    rebuilt, _, _ = run(
        build_with_durable_state(
            ordering,
            state_storage,
            options=options,
            journal_storage=journal_storage,
        )
    )
    assert rebuilt.generation == 10
    with pytest.raises(ordering["ordering_manager"].StaleOrderingGeneration):
        run(rebuilt.async_get_basket(user, stale))


def test_live_option_race_is_rechecked_at_final_mock_boundary(
    ordering: dict[str, ModuleType],
) -> None:
    state_storage = ordering["ordering_state"].MemoryOrderingStateStorage()
    options = {"allow_ordering": True, "ordering_acknowledged": True}
    manager, adapter, _ = run(
        build_with_durable_state(ordering, state_storage, options=options)
    )
    user = admin(ordering)
    generation = run(add_default_item(ordering, manager, user))
    quote = run(quote_default(ordering, manager, user))
    confirmation = run(
        manager.async_prepare_mock_confirmation(user, generation, quote["fingerprint"])
    )
    reads = 0

    def racing_options() -> dict[str, object]:
        nonlocal reads
        reads += 1
        if reads >= 2:
            options["allow_ordering"] = False
        return options

    manager._live_options = racing_options
    with pytest.raises(ordering["ordering_manager"].OrderingSecurityFault):
        run(
            manager.async_execute_mock_checkout(
                user,
                generation,
                confirmation["challenge"],
                confirmation["fingerprint"],
            )
        )
    assert adapter.execution_count == 0
    assert manager.security_fault is True


def test_durable_generation_race_is_rechecked_before_mock_adapter(
    ordering: dict[str, ModuleType],
) -> None:
    state_storage = ordering["ordering_state"].MemoryOrderingStateStorage()
    manager, adapter, _ = run(build_with_durable_state(ordering, state_storage))
    user = admin(ordering)
    generation = run(add_default_item(ordering, manager, user))
    quote = run(quote_default(ordering, manager, user))
    confirmation = run(
        manager.async_prepare_mock_confirmation(user, generation, quote["fingerprint"])
    )
    state_storage.data = {
        "version": 2,
        "generation": generation + 1,
        "manual_check_required": False,
        "integrity_fault": False,
    }
    with pytest.raises(ordering["ordering_manager"].OrderingSecurityFault):
        run(
            manager.async_execute_mock_checkout(
                user,
                generation,
                confirmation["challenge"],
                confirmation["fingerprint"],
            )
        )
    assert adapter.execution_count == 0
    assert state_storage.data["generation"] >= generation + 2
    assert state_storage.data["integrity_fault"] is True


class FailingSaveStorage:
    def __init__(self, base: Any, fail_on: int) -> None:
        self.base = base
        self.fail_on = fail_on
        self.saves = 0

    @property
    def data(self) -> Any:
        return self.base.data

    async def async_load(self) -> Any:
        return await self.base.async_load()

    async def async_save(self, data: dict[str, Any]) -> None:
        self.saves += 1
        if self.saves == self.fail_on:
            raise OSError("injected journal write failure")
        await self.base.async_save(data)


class CancellingFinalSaveStorage:
    """Cancel final outcome persistence and optionally obstruct recovery."""

    def __init__(
        self,
        base: Any,
        *,
        block_recovery: bool = False,
        fail_recovery: bool = False,
    ) -> None:
        self.base = base
        self.block_recovery = block_recovery
        self.fail_recovery = fail_recovery
        self.saves = 0
        self.recovery_started = asyncio.Event()
        self.release_recovery = asyncio.Event()

    @property
    def data(self) -> Any:
        return self.base.data

    async def async_load(self) -> Any:
        return await self.base.async_load()

    async def async_save(self, data: dict[str, Any]) -> None:
        self.saves += 1
        if self.saves == 4:
            raise asyncio.CancelledError
        if self.saves == 5:
            self.recovery_started.set()
            if self.block_recovery:
                await self.release_recovery.wait()
            if self.fail_recovery:
                raise OSError("injected uncertain recovery write failure")
        await self.base.async_save(data)


async def prepare_checkout(
    ordering: dict[str, ModuleType], manager: Any, user: Any
) -> tuple[int, dict[str, Any]]:
    generation = await add_default_item(ordering, manager, user)
    quote = await quote_default(ordering, manager, user)
    confirmation = await manager.async_prepare_mock_confirmation(
        user, generation, quote["fingerprint"]
    )
    return generation, confirmation


def test_post_execution_cancellation_recovers_uncertain_latches_and_blocks_rebuild(
    ordering: dict[str, ModuleType],
) -> None:
    async def scenario() -> None:
        journal_module = ordering["ordering_journal"]
        manager_module = ordering["ordering_manager"]
        journal_base = journal_module.MemoryJournalStorage()
        cancelling = CancellingFinalSaveStorage(journal_base)
        state_storage = ordering["ordering_state"].MemoryOrderingStateStorage()
        manager, adapter, _ = await build_with_durable_state(
            ordering, state_storage, journal_storage=cancelling
        )
        user = admin(ordering)
        generation, confirmation = await prepare_checkout(ordering, manager, user)

        with pytest.raises(asyncio.CancelledError):
            await manager.async_execute_mock_checkout(
                user,
                generation,
                confirmation["challenge"],
                confirmation["fingerprint"],
            )

        assert adapter.execution_count == 1
        assert manager.enabled is False
        assert manager.security_fault is True
        assert manager._baskets._baskets == {}
        assert manager._quotes == {}
        assert manager._confirmations == {}
        assert state_storage.data == {
            "version": 2,
            "generation": generation + 1,
            "manual_check_required": False,
            "integrity_fault": True,
        }
        assert journal_base.data["records"][-1]["state"] == "LEGACY_MOCK_NO_REMOTE_EFFECT"

        with pytest.raises(manager_module.OrderingSecurityFault):
            await manager.async_prepare_mock_confirmation(
                user, generation, confirmation["fingerprint"]
            )
        with pytest.raises(manager_module.OrderingSecurityFault):
            await manager.async_execute_mock_checkout(
                user,
                generation,
                confirmation["challenge"],
                confirmation["fingerprint"],
            )
        assert adapter.execution_count == 1

        rebuilt, rebuilt_adapter, _ = await build_with_durable_state(
            ordering, state_storage, journal_storage=journal_base
        )
        assert rebuilt.enabled is False
        assert rebuilt.security_fault is True
        assert rebuilt.generation == generation + 1
        with pytest.raises(manager_module.OrderingSecurityFault):
            await rebuilt.async_get_basket(user, generation)
        assert rebuilt_adapter.execution_count == 0

    run(scenario())


def test_repeated_caller_cancellation_cannot_interrupt_uncertain_recovery(
    ordering: dict[str, ModuleType],
) -> None:
    async def scenario() -> None:
        journal_module = ordering["ordering_journal"]
        journal_base = journal_module.MemoryJournalStorage()
        cancelling = CancellingFinalSaveStorage(journal_base, block_recovery=True)
        state_storage = ordering["ordering_state"].MemoryOrderingStateStorage()
        manager, adapter, _ = await build_with_durable_state(
            ordering, state_storage, journal_storage=cancelling
        )
        user = admin(ordering)
        generation, confirmation = await prepare_checkout(ordering, manager, user)
        execution = asyncio.create_task(
            manager.async_execute_mock_checkout(
                user,
                generation,
                confirmation["challenge"],
                confirmation["fingerprint"],
            )
        )

        await asyncio.wait_for(cancelling.recovery_started.wait(), timeout=1)
        execution.cancel()
        await asyncio.sleep(0)
        assert execution.done() is False
        assert adapter.execution_count == 1

        cancelling.release_recovery.set()
        with pytest.raises(asyncio.CancelledError):
            await execution
        assert journal_base.data["records"][-1]["state"] == "LEGACY_MOCK_NO_REMOTE_EFFECT"
        assert state_storage.data["generation"] == generation + 1
        assert state_storage.data["integrity_fault"] is True
        assert manager.enabled is False
        assert manager.security_fault is True

    run(scenario())


def test_cancelled_final_save_with_failed_uncertain_write_still_latches_safety_store(
    ordering: dict[str, ModuleType],
) -> None:
    async def scenario() -> None:
        journal_module = ordering["ordering_journal"]
        manager_module = ordering["ordering_manager"]
        journal_base = journal_module.MemoryJournalStorage()
        cancelling = CancellingFinalSaveStorage(journal_base, fail_recovery=True)
        state_storage = ordering["ordering_state"].MemoryOrderingStateStorage()
        manager, adapter, _ = await build_with_durable_state(
            ordering, state_storage, journal_storage=cancelling
        )
        user = admin(ordering)
        generation, confirmation = await prepare_checkout(ordering, manager, user)

        with pytest.raises(asyncio.CancelledError):
            await manager.async_execute_mock_checkout(
                user,
                generation,
                confirmation["challenge"],
                confirmation["fingerprint"],
            )
        assert adapter.execution_count == 1
        assert manager.enabled is False
        assert manager.security_fault is True
        assert state_storage.data["generation"] == generation + 1
        assert state_storage.data["integrity_fault"] is True
        assert journal_base.data["records"][-1]["state"] == "DISPATCHING"

        rebuilt, rebuilt_adapter, _ = await build_with_durable_state(
            ordering, state_storage, journal_storage=journal_base
        )
        assert rebuilt.enabled is False
        assert rebuilt.security_fault is True
        assert rebuilt_adapter.execution_count == 0
        assert journal_base.data["records"][-1]["state"] == "LEGACY_MOCK_NO_REMOTE_EFFECT"
        with pytest.raises(manager_module.OrderingSecurityFault):
            await rebuilt.async_get_basket(user, generation)

    run(scenario())


def test_generation_storage_failure_never_publishes_advance_and_rebuild_stays_blocked(
    ordering: dict[str, ModuleType],
) -> None:
    state_base = ordering["ordering_state"].MemoryOrderingStateStorage(
        {"version": 2, "generation": 1, "manual_check_required": False, "integrity_fault": False}
    )
    failing_state = FailingSaveStorage(state_base, 1)
    journal_storage = ordering["ordering_journal"].MemoryJournalStorage()
    manager, _, _ = run(
        build_with_durable_state(
            ordering,
            failing_state,
            journal_storage=journal_storage,
        )
    )
    generation = manager.generation
    run(manager.async_set_enabled(False))
    assert manager.generation == generation
    assert manager.enabled is False
    assert any(
        record.state is ordering["ordering_journal"].JournalState.SECURITY_FAULT
        for record in manager.journal.records
    )

    rebuilt, _, _ = run(
        build_with_durable_state(
            ordering,
            state_base,
            journal_storage=journal_storage,
        )
    )
    assert rebuilt.enabled is False
    assert rebuilt.security_fault is True
    assert state_base.data["integrity_fault"] is True


@pytest.mark.parametrize("fail_on,expected_executions", [(1, 0), (2, 0), (3, 0), (4, 1)])
def test_every_journal_write_failure_latches_and_blocks_retry_and_rebuild(
    ordering: dict[str, ModuleType], fail_on: int, expected_executions: int
) -> None:
    journal_module = ordering["ordering_journal"]
    base = journal_module.MemoryJournalStorage()
    failing = FailingSaveStorage(base, fail_on)
    state_storage = ordering["ordering_state"].MemoryOrderingStateStorage()
    manager, adapter, _ = run(
        build_with_durable_state(
            ordering, state_storage, journal_storage=failing
        )
    )
    user = admin(ordering)
    generation = run(add_default_item(ordering, manager, user))
    quote = run(quote_default(ordering, manager, user))
    confirmation = run(
        manager.async_prepare_mock_confirmation(user, generation, quote["fingerprint"])
    )
    with pytest.raises(ordering["ordering_manager"].OrderingSecurityFault):
        run(
            manager.async_execute_mock_checkout(
                user,
                generation,
                confirmation["challenge"],
                confirmation["fingerprint"],
            )
        )
    assert adapter.execution_count == expected_executions
    assert manager.enabled is False
    assert state_storage.data["integrity_fault"] is True
    assert run(manager.async_state(user))["integrityFault"] is True

    rebuilt, rebuilt_adapter, _ = run(
        build_with_durable_state(
            ordering, state_storage, journal_storage=base
        )
    )
    assert rebuilt.enabled is False
    assert rebuilt.security_fault is True
    assert rebuilt_adapter.execution_count == 0
    if expected_executions:
        assert any(
            record.state is journal_module.JournalState.LEGACY_MOCK_NO_REMOTE_EFFECT
            for record in rebuilt.journal.records
        )


@pytest.mark.parametrize("bad_time", [float("nan"), float("inf"), float("-inf")])
def test_all_nonfinite_authorization_and_persistence_times_fail_closed(
    ordering: dict[str, ModuleType], bad_time: float
) -> None:
    quote_module = ordering["ordering_quote"]
    payload = {
        "checkoutSessionId": "fixture-session",
        "versionId": "fixture-v1",
        "templateId": "fixture-template",
        "basketVersion": 1,
        "purchaseTotalCents": 100,
        "currencyCode": "AMD",
        "priceLines": [{"kind": "items", "label": "Items", "amountCents": 100}],
        "eta": "20 min",
        "expiresAt": 1_800_000_100.0,
    }
    with pytest.raises(quote_module.InvalidCheckoutFixture):
        quote_module.CheckoutFixture.from_mapping(payload, now=bad_time)
    with pytest.raises(quote_module.InvalidCheckoutFixture):
        quote_module.CheckoutFixture.from_mapping(
            {**payload, "expiresAt": bad_time}, now=1_800_000_000.0
        )

    clock = FakeClock(bad_time)
    manager, _, _, _ = run(make_manager(ordering, acknowledged=True, clock=clock))
    with pytest.raises((ValueError, ordering["ordering_manager"].OrderingSecurityFault)):
        run(add_default_item(ordering, manager, admin(ordering)))

    record = {
        "attempt_id": "attempt-time",
        "state": "DRAFT",
        "created_at": bad_time,
        "updated_at": 1_800_000_000.0,
        "generation": 1,
        "quote_fingerprint": "f" * 64,
        "outcome": None,
    }
    journal = ordering["ordering_journal"].AttemptJournal(
        ordering["ordering_journal"].MemoryJournalStorage(
            {"version": 1, "records": [record]}
        ),
        clock=FakeClock(),
    )
    with pytest.raises(ordering["ordering_journal"].JournalCorrupt):
        run(journal.async_load())


def test_currency_exponents_complete_lines_and_amount_mutations_are_bound(
    ordering: dict[str, ModuleType],
) -> None:
    models = ordering["ordering_models"]
    assert models.Money(624000, "AMD").action_amount == "6,240.00 AMD"
    assert models.Money(6240, "AMD").action_amount == "62.40 AMD"
    assert models.Money(6240, "JPY").action_amount == "6,240 JPY"
    assert models.Money(6240, "KWD").action_amount == "6.240 KWD"
    for value in (True, 1.0):
        with pytest.raises(TypeError):
            models.Money(value, "AMD")
    with pytest.raises(ValueError):
        models.Money(1, "ZZZ")

    quote_module = ordering["ordering_quote"]
    kinds = ("items", "tip", "tax", "credit", "surcharge")
    amounts = (1000, 100, 200, -50, 75)
    payload = {
        "checkoutSessionId": "fixture-complete",
        "versionId": "fixture-v1",
        "templateId": "fixture-template",
        "basketVersion": 1,
        "purchaseTotalCents": sum(amounts),
        "currencyCode": "AMD",
        "priceLines": [
            {"kind": kind, "label": kind.title(), "amountCents": amount}
            for kind, amount in zip(kinds, amounts, strict=True)
        ],
        "eta": "20 min",
        "expiresAt": 1_800_000_100.0,
    }
    fixture = quote_module.CheckoutFixture.from_mapping(payload, now=1_800_000_000.0)
    assert {line.currency for line in fixture.price_lines} == {"AMD"}
    assert {line.kind for line in fixture.price_lines} == set(kinds)

    manager, _, adapter, _ = run(make_manager(ordering, acknowledged=True))
    user = admin(ordering)
    generation = run(add_default_item(ordering, manager, user))
    quote = run(quote_default(ordering, manager, user))
    confirmation = run(
        manager.async_prepare_mock_confirmation(user, generation, quote["fingerprint"])
    )
    context = manager._quotes[user.user_id]
    object.__setattr__(context.fixture.purchase_total, "amount_minor", 1)
    with pytest.raises(ordering["ordering_manager"].InvalidConfirmation):
        run(
            manager.async_execute_mock_checkout(
                user,
                generation,
                confirmation["challenge"],
                confirmation["fingerprint"],
            )
        )
    assert adapter.execution_count == 0

    manager, _, adapter, _ = run(make_manager(ordering, acknowledged=True))
    generation = run(add_default_item(ordering, manager, user))
    quote = run(quote_default(ordering, manager, user))
    confirmation = run(
        manager.async_prepare_mock_confirmation(user, generation, quote["fingerprint"])
    )
    context = manager._quotes[user.user_id]
    object.__setattr__(context.fixture.price_lines[0], "amount_minor", 1)
    with pytest.raises(ordering["ordering_manager"].InvalidConfirmation):
        run(
            manager.async_execute_mock_checkout(
                user,
                generation,
                confirmation["challenge"],
                confirmation["fingerprint"],
            )
        )
    assert adapter.execution_count == 0


def test_distinct_item_total_quantity_and_independent_quote_expiry_bounds(
    ordering: dict[str, ModuleType],
) -> None:
    basket_module = ordering["ordering_basket"]
    models = ordering["ordering_models"]
    clock = FakeClock()
    baskets = basket_module.BasketManager(clock=clock)
    for index in range(basket_module.MAX_DISTINCT_ITEMS):
        baskets.add_item(
            owner_key="admin-a",
            generation=1,
            store_key="fixture-store",
            store_label="Fixture",
            item=models.CanonicalItem(
                f"item-{index}", "Item", "standard", "Standard", (), 1
            ),
        )
    with pytest.raises(basket_module.BasketLimitExceeded):
        baskets.add_item(
            owner_key="admin-a",
            generation=1,
            store_key="fixture-store",
            store_label="Fixture",
            item=models.CanonicalItem("item-extra", "Item", "standard", "Standard", (), 1),
        )

    totals = basket_module.BasketManager(clock=clock)
    for index, quantity in enumerate((20, 20, 10)):
        totals.add_item(
            owner_key="admin-b",
            generation=1,
            store_key="fixture-store",
            store_label="Fixture",
            item=models.CanonicalItem(
                f"total-{index}", "Item", "standard", "Standard", (), quantity
            ),
        )
    with pytest.raises(basket_module.BasketLimitExceeded):
        totals.add_item(
            owner_key="admin-b",
            generation=1,
            store_key="fixture-store",
            store_label="Fixture",
            item=models.CanonicalItem("total-extra", "Item", "standard", "Standard", (), 1),
        )

    manager, _, _, clock = run(make_manager(ordering, acknowledged=True, clock=clock))
    user = admin(ordering)
    generation = run(add_default_item(ordering, manager, user))
    quote = run(quote_default(ordering, manager, user))
    clock.advance(121)
    with pytest.raises(ordering["ordering_manager"].InvalidConfirmation):
        run(manager.async_prepare_mock_confirmation(user, generation, quote["fingerprint"]))


def test_full_added_ordering_source_has_no_network_or_remote_mutation_capability() -> None:
    root = ROOT / "custom_components/glovo"
    added = [
        path
        for path in root.rglob("ordering_*")
        if path.suffix in {".py", ".js"}
    ] + list((root / "frontend").glob("*"))
    combined = "\n".join(path.read_text(errors="ignore").casefold() for path in added)
    forbidden = (
        "aiohttp",
        "requests.",
        "httpx",
        "urllib",
        "socket.",
        "api.glovoapp.com",
        "checkout/submit",
        "payment_intent",
        "place_order",
        'method: "post"',
        "method: 'post'",
        "method=\"post\"",
        "method='post'",
        "fetch(",
        "xmlhttprequest",
    )
    assert not [needle for needle in forbidden if needle in combined]


def _live_manual_record() -> dict[str, Any]:
    return {
        "attempt_id": "attempt-live-one",
        "state": "MANUAL_CHECK_REQUIRED",
        "created_at": 1_800_000_000.0,
        "updated_at": 1_800_000_010.0,
        "generation": 4,
        "amount_minor": 624000,
        "currency": "AMD",
        "execution_mode": "live",
        "request_fingerprint": "f" * 64,
        "provider_session_hash": "a" * 64,
        "checkout_id": None,
        "dispatch_started_at": 1_800_000_005.0,
        "failure_class": "connection_reset",
        "resolution": None,
        "evidence_source": None,
        "record_revision": 3,
        "store_display_name": "Fixture Kitchen",
        "item_count": 1,
        "item_summary": "1 item",
        "masked_payment_label": "Test card •••• 4242",
        "masked_address_alias": "Saved destination ••••",
        "last_reviewed_at": None,
    }


def _manual_state(
    record: dict[str, Any], *, generation: int = 4, resolution: str | None = None
) -> dict[str, Any]:
    return {
        "version": 2,
        "generation": generation,
        "manual_check_required": True,
        "integrity_fault": False,
        "manual_binding": {
            "attempt_id": record["attempt_id"],
            "record_revision": record["record_revision"],
            "generation": generation,
            "resolution": resolution,
        },
    }


def test_v2_restart_converts_only_live_inflight_to_manual_check(
    ordering: dict[str, ModuleType],
) -> None:
    journal_module = ordering["ordering_journal"]
    live = {
        **_live_manual_record(),
        "state": "DISPATCHING",
        "record_revision": 2,
        "failure_class": None,
    }
    mock = {
        **live,
        "attempt_id": "attempt-mock-one",
        "execution_mode": "mock",
        "state": "VERIFYING",
    }
    storage = journal_module.MemoryJournalStorage(
        {"version": 2, "records": [live, mock]}
    )
    journal = journal_module.AttemptJournal(
        storage, clock=FakeClock(1_800_000_100.0)
    )
    records = run(journal.async_load())
    assert records[0].state is journal_module.JournalState.MANUAL_CHECK_REQUIRED
    assert records[0].failure_class == "restart_inflight"
    assert records[1].state is journal_module.JournalState.LEGACY_MOCK_NO_REMOTE_EFFECT


def test_legacy_v1_migration_is_explicit_mock_only_and_preserves_integrity_fault(
    ordering: dict[str, ModuleType],
) -> None:
    journal_module = ordering["ordering_journal"]
    legacy = {
        "version": 1,
        "records": [
            {
                "attempt_id": "attempt-old-inflight",
                "state": "DISPATCHING",
                "created_at": 1_800_000_000.0,
                "updated_at": 1_800_000_001.0,
                "generation": 2,
                "quote_fingerprint": "b" * 64,
                "outcome": None,
            },
            {
                "attempt_id": "attempt-old-fault",
                "state": "SECURITY_FAULT",
                "created_at": 1_800_000_000.0,
                "updated_at": 1_800_000_001.0,
                "generation": 2,
                "quote_fingerprint": "c" * 64,
                "outcome": "authority_storage_failure",
            },
        ],
    }
    storage = journal_module.MemoryJournalStorage(legacy)
    journal = journal_module.AttemptJournal(
        storage, clock=FakeClock(1_800_000_100.0)
    )
    records = run(journal.async_load())
    assert journal.load_source == "v1"
    assert records[0].state is journal_module.JournalState.LEGACY_MOCK_NO_REMOTE_EFFECT
    assert records[0].resolution == "legacy_mock_no_remote_effect"
    assert records[1].state is journal_module.JournalState.INTEGRITY_FAULT
    assert storage.legacy_data == legacy
    assert storage.data["version"] == 2


def test_manual_resolution_is_challenged_generation_bumping_and_privacy_safe(
    ordering: dict[str, ModuleType],
) -> None:
    journal_module = ordering["ordering_journal"]
    state_module = ordering["ordering_state"]
    journal_storage = journal_module.MemoryJournalStorage(
        {"version": 2, "records": [_live_manual_record()]}
    )
    state_storage = state_module.MemoryOrderingStateStorage(
        _manual_state(_live_manual_record())
    )
    manager, adapter, _ = run(
        build_with_durable_state(
            ordering,
            state_storage,
            journal_storage=journal_storage,
            clock=FakeClock(1_800_000_100.0),
        )
    )
    user = admin(ordering)
    assert manager.manual_check_required is True
    with pytest.raises(ordering["ordering_manager"].OrderingManualCheckRequired):
        run(manager.async_add_item(
            user,
            manager.generation,
            store_key="fixture-store",
            product_key="fixture-meal",
            variant_key="standard",
            modifier_keys=(),
            quantity=1,
        ))
    checks = run(manager.async_list_manual_checks(user))
    assert len(checks["attempts"]) == 1
    public = checks["attempts"][0]
    assert set(public) == {
        "attemptRef", "state", "recordRevision", "submittedAt",
        "storeDisplayName", "amountMinor", "currency", "itemCount",
        "itemSummary", "maskedPaymentLabel", "maskedAddressAlias",
        "hasCheckoutId", "ambiguityReason", "lastReviewedAt",
    }
    forbidden = (
        "fingerprint", "generation", "session", "checkout_id", "selection",
        "coordinates", "token", "cookie", "idempotency",
    )
    encoded = json.dumps(public).casefold()
    assert not [value for value in forbidden if value in encoded]

    prepared = run(manager.async_prepare_manual_resolution(
        user,
        attempt_id="attempt-live-one",
        expected_revision=3,
        expected_state="MANUAL_CHECK_REQUIRED",
        resolution="found_failed_or_cancelled",
    ))
    old_generation = manager.generation
    result = run(manager.async_resolve_manual_check(
        user,
        attempt_id="attempt-live-one",
        expected_revision=3,
        expected_state="MANUAL_CHECK_REQUIRED",
        resolution="found_failed_or_cancelled",
        challenge=prepared["challenge"],
        acknowledged=True,
    ))
    assert result["resolved"] is True
    assert manager.generation == old_generation + 1
    assert manager.manual_check_required is False
    assert state_storage.data["manual_check_required"] is False
    assert adapter.execution_count == 0
    with pytest.raises(ordering["ordering_manager"].InvalidManualResolution):
        run(manager.async_resolve_manual_check(
            user,
            attempt_id="attempt-live-one",
            expected_revision=3,
            expected_state="MANUAL_CHECK_REQUIRED",
            resolution="found_failed_or_cancelled",
            challenge=prepared["challenge"],
            acknowledged=True,
        ))


def test_still_unknown_keeps_manual_block_and_multiple_live_unknowns_are_integrity_fault(
    ordering: dict[str, ModuleType],
) -> None:
    journal_module = ordering["ordering_journal"]
    state_module = ordering["ordering_state"]
    record = _live_manual_record()
    journal_storage = journal_module.MemoryJournalStorage(
        {"version": 2, "records": [record]}
    )
    state_storage = state_module.MemoryOrderingStateStorage(_manual_state(record))
    manager, _, _ = run(build_with_durable_state(
        ordering,
        state_storage,
        journal_storage=journal_storage,
        clock=FakeClock(1_800_000_100.0),
    ))
    user = admin(ordering)
    prepared = run(manager.async_prepare_manual_resolution(
        user,
        attempt_id=record["attempt_id"],
        expected_revision=record["record_revision"],
        expected_state=record["state"],
        resolution="still_unknown",
    ))
    result = run(manager.async_resolve_manual_check(
        user,
        attempt_id=record["attempt_id"],
        expected_revision=record["record_revision"],
        expected_state=record["state"],
        resolution="still_unknown",
        challenge=prepared["challenge"],
        acknowledged=True,
    ))
    assert result["resolved"] is False
    assert manager.manual_check_required is True

    second = {**record, "attempt_id": "attempt-live-two"}
    bad_journal = journal_module.MemoryJournalStorage(
        {"version": 2, "records": [record, second]}
    )
    bad_state = state_module.MemoryOrderingStateStorage(_manual_state(record))
    bad_manager, _, _ = run(build_with_durable_state(
        ordering, bad_state, journal_storage=bad_journal
    ))
    assert bad_manager.integrity_fault is True
    with pytest.raises(ordering["ordering_manager"].OrderingSecurityFault):
        run(bad_manager.async_prepare_manual_resolution(
            user,
            attempt_id=record["attempt_id"],
            expected_revision=record["record_revision"],
            expected_state=record["state"],
            resolution="found_succeeded",
        ))
