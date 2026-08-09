"""Static and deterministic interaction contracts for the ordering workspace."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
PANEL = ROOT / "custom_components" / "glovo" / "frontend" / "glovo-ordering-panel.js"
HARNESS = ROOT / "tests" / "frontend_ordering_harness.mjs"


def _source() -> str:
    return PANEL.read_text(encoding="utf-8")


def test_frontend_uses_open_shadow_dom_and_one_canonical_model() -> None:
    source = _source()
    assert 'attachShadow({ mode: "open" })' in source
    assert "this._model = {" in source
    for key in ("lifecycle", "capability", "context", "menu", "draft", "basket", "payments", "quote", "overlay", "library", "recovery"):
        assert f"{key}:" in source
    assert "set hass(hass)" in source
    assert "if (this._hass) return" not in source
    assert source.count(".innerHTML =") == 1
    assert "this.shadowRoot.innerHTML =" in source


def test_frontend_uses_frozen_live_and_library_operations_with_generation_rules() -> None:
    source = _source()
    operations = (
        "state", "live/addresses", "live/stores", "live/store_menu", "live/payment_methods",
        "live/basket", "live/basket_set", "live/basket_clear", "live/create_quote",
        "live/prepare_confirmation", "live/execute_checkout", "live/checkout_status",
        "library/list", "library/address_save", "library/address_delete", "library/package_save",
        "library/package_delete", "library/package_prepare",
    )
    for operation in operations:
        assert f'"{operation}"' in source
    assert 'this._request("state")' in source
    assert 'this._request("state", this._withGeneration())' not in source
    assert "live/basket_reconcile" not in source
    for field in (
        "generation", "expectedRevision", "expectedStoreRevision", "addressHandle", "paymentHandle",
        "storeHandle", "storeSlug", "addressRef", "addressRevision", "packageRef", "aliases", "products",
    ):
        assert field in source
    assert "challenge, acknowledged: true" in source


def test_frontend_has_latest_read_guards_single_flight_and_no_automatic_mutation_retry() -> None:
    source = _source()
    assert "_beginLatestRead" in source
    assert "_isLatestRead" in source
    assert "capturedGeneration" in source
    assert "_runSingleFlight" in source
    assert '"basket-sync"' in source
    assert '"quote"' in source
    assert '"checkout"' in source
    assert "outcome is unknown" in source
    assert "will not retry" in source
    assert "MANUAL_CHECK_REQUIRED" in source
    assert "_refreshStateAfterMutationFailure" in source


def test_frontend_invalidates_ephemeral_authority_on_generation_and_selection_boundaries() -> None:
    source = _source()
    assert "disconnectedCallback()" in source
    assert "_invalidateAuthority" in source
    assert "_resetEphemeralGeneration" in source
    assert "Ordering generation changed" in source
    for boundary in ("_loadAddresses", "address-select", "_loadStoreMenu", "_syncBasket", "_clearBasket", "payment-select", "Quote expired"):
        assert boundary in source


def test_frontend_menu_basket_and_customizer_accessibility_contract() -> None:
    source = _source()
    for token in (
        "All", "Customizable", "In basket", "No products match", "content-visibility: auto",
        "Estimated catalog subtotal", "non-authoritative", "Sync basket", "Provider-synced",
        "fieldset", "legend", 'type = group.max === 1 ? "radio" : "checkbox"',
        "Choose ${minimum}–${maximum}.", "quantity", "50", "showModal", "returnFocus", "aria-live",
    ):
        assert token in source
    assert "Intl.NumberFormat" in source
    assert "textContent" in source
    assert "option price" not in source.lower() or "priceMinor" in source


def test_closed_store_menu_is_prominently_read_only() -> None:
    source = _source()
    for token in (
        "Store closed — browsing only",
        "This menu is visible for reference, but this store is not accepting orders.",
        "Visible for reference while this store is closed.",
        "orderingAvailable",
        "closed-store-warning",
        "_storeAllowsOrdering",
    ):
        assert token in source
    assert 'setAttribute("role", "alert")' in source


def test_frontend_responsive_theme_and_input_mode_contract() -> None:
    source = _source()
    for token in (
        "--primary-color", "--primary-text-color", "--secondary-text-color", "--card-background-color",
        "--primary-background-color", "--secondary-background-color", "--divider-color", "--error-color",
        "--warning-color", "--success-color", "--ha-card-border-radius", "--ha-card-box-shadow",
        "prefers-reduced-motion", "forced-colors", "env(safe-area-inset-bottom)", "inset-inline",
        "min-block-size: 44px", "direction", "mobile-basket-bar", "basket-dialog", "customizer-dialog",
        "Selected full delivery address", ".selected-address", "overflow-wrap: anywhere",
    ):
        assert token in source


def test_frontend_package_and_address_alias_crud_is_backend_backed() -> None:
    source = _source()
    for token in (
        "Address aliases", "Create address alias", "Update alias", "Delete alias",
        "Package library", "Create package", "Update package", "Duplicate", "Delete package", "Load into draft",
        "storeRevision", "addressRevision", "packageKey", "addressKey", "selectionComplete",
    ):
        assert token in source
    for persistence in ("localStorage", "sessionStorage", "indexedDB", "document.cookie"):
        assert persistence not in source


def test_frontend_quotes_are_authoritative_and_paid_cta_is_capability_gated() -> None:
    source = _source()
    assert "Final total is available only from an authoritative quote." in source
    assert "Authoritative provider total" in source
    assert "this._ackText = `ACK ${exact}`" in source
    assert "typed !== this._ackText" in source
    assert "this._model.capability.liveCheckoutAvailable === true" in source
    assert "submit-checkout" in source
    assert "Paid checkout unsupported" not in source
    assert "button.disabled = true" in source


def test_panel_has_no_provider_ids_private_refs_raw_errors_analytics_or_logging() -> None:
    source = _source()
    for forbidden in (
        "providerId", "checkoutId", "checkoutSession", "fingerprint", "Authorization",
        "console.log", "console.error", "error.message", "error.stack", "fetch(", "sendBeacon",
    ):
        assert forbidden not in source


def test_manual_recovery_retains_exactly_three_approved_outcomes() -> None:
    source = _source()
    outcomes = ("found_succeeded", "found_failed_or_cancelled", "still_unknown")
    for outcome in outcomes:
        assert source.count(outcome) == 1
    assert "prepare_manual_resolution" in source
    assert "resolve_manual_check" in source
    assert "never re-submits" in source


def test_component_registration_remains_content_addressed() -> None:
    source = _source()
    assert 'new URL(import.meta.url).searchParams.get("component")' in source
    assert "if (!customElements.get(componentName)) customElements.define(componentName, GlovoOrderingPanel);" in source


def test_panel_javascript_parses_and_deterministic_harness_passes_when_node_is_available() -> None:
    node = shutil.which("node")
    if node is None:
        return
    syntax = subprocess.run([node, "--check", str(PANEL)], capture_output=True, text=True, check=False)
    assert syntax.returncode == 0, syntax.stderr
    result = subprocess.run([node, str(HARNESS), str(PANEL)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "frontend interaction harness: PASS" in result.stdout


def test_strings_and_every_translation_have_live_spending_copy_and_key_parity() -> None:
    base = json.loads((ROOT / "custom_components" / "glovo" / "strings.json").read_text(encoding="utf-8"))
    fields = base["options"]["step"]["init"]["data"]
    assert "allow_live_checkout" in fields
    assert "server basket/template" in base["options"]["step"]["init"]["description"]
    assert "no retry" in base["options"]["step"]["init"]["description"]

    def leaves(value: object, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
        if isinstance(value, dict):
            return set().union(*(leaves(child, prefix + (key,)) for key, child in value.items())) if value else set()
        return {prefix}

    expected = leaves(base)
    translations = sorted((ROOT / "custom_components" / "glovo" / "translations").glob("*.json"))
    assert len(translations) == 48
    for path in translations:
        assert leaves(json.loads(path.read_text(encoding="utf-8"))) == expected, path
