"""Static contract tests for the no-persistence phone-first ordering panel."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
PANEL = ROOT / "custom_components" / "glovo" / "frontend" / "glovo-ordering-panel.js"


def test_frontend_uses_every_frozen_operation_with_exact_public_request_keys() -> None:
    source = PANEL.read_text(encoding="utf-8")
    operations = (
        "state", "live/addresses", "live/stores", "live/store_menu", "live/payment_methods",
        "live/basket", "live/basket_set", "live/basket_clear", "live/basket_reconcile",
        "live/create_quote", "live/prepare_confirmation", "live/execute_checkout", "live/checkout_status",
    )
    for operation in operations:
        assert f'"{operation}"' in source
    assert 'this._request("state")' in source
    assert 'this._request("state", this._withGeneration())' not in source
    assert "storeSlug" in source and "storeHandle" in source
    assert "storeSlug, addressHandle" in source
    assert 'Select a saved address before looking up a store.' in source
    assert "expectedRevision" in source and "addressHandle" in source and "paymentHandle" in source
    assert "challenge, acknowledged: true" in source


def test_frontend_requires_typed_exact_total_and_single_submit_without_retry() -> None:
    source = PANEL.read_text(encoding="utf-8")
    assert "Exact total: ${quote.purchaseTotalCents} ${quote.currencyCode}" in source
    assert "this._ackText = `ACK ${exact}`" in source
    assert 'value !== this._ackText' in source
    assert "this._checkoutSubmitting" in source
    assert "button.disabled = true" in source
    assert "MANUAL_CHECK_REQUIRED — no retry or re-submit is available." in source
    assert "Do not submit again" in source


def test_frontend_invalidates_memory_only_confirmation_on_each_boundary() -> None:
    source = PANEL.read_text(encoding="utf-8")
    assert "disconnectedCallback()" in source
    assert "_invalidateAuthority" in source
    for boundary in ("_loadAddresses", "#address", "_selectStore", "_saveBasket", "_clearBasket", "_loadPayments", "#payment", "Quote expired"):
        assert boundary in source
    for persistence in ("localStorage", "sessionStorage", "indexedDB", "document.cookie"):
        assert persistence not in source


def test_panel_has_no_provider_ids_private_refs_or_raw_log_bodies() -> None:
    source = PANEL.read_text(encoding="utf-8")
    for forbidden in ("providerId", "checkoutId", "checkoutSession", "fingerprint", "Authorization", "console.log", "console.error"):
        assert forbidden not in source


def test_panel_javascript_parses_when_node_is_available() -> None:
    node = shutil.which("node")
    if node is None:
        return
    result = subprocess.run([node, "--check", str(PANEL)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


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
