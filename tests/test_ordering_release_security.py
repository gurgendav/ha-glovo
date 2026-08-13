"""Release gates for the guarded production paid-checkout seam."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
COMPONENT = ROOT / "custom_components" / "glovo"


def run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, cwd=ROOT, text=True, capture_output=True, check=False)


def test_privacy_and_capability_scan_passes() -> None:
    result = run(sys.executable, "scripts/scan_ordering_privacy.py")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "privacy and capability scan passed" in result.stdout


def test_privacy_scanner_allows_public_checkout_operation_but_catches_private_refs() -> None:
    script = ROOT / "scripts" / "scan_ordering_privacy.py"
    spec = importlib.util.spec_from_file_location("ordering_privacy_scan", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    safe: list[str] = []
    module.scan_frontend_private_references(
        'this._request("live/checkout_status")', "panel.js", safe
    )
    assert safe == []
    leaked: list[str] = []
    module.scan_frontend_private_references(
        'const checkoutRef = "private-checkout"; const providerId = "private-provider";',
        "panel.js",
        leaked,
    )
    assert leaked == ["panel.js: private checkout/provider reference"]


    result = run(sys.executable, "scripts/scan_ordering_privacy.py", "--capabilities-only")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "capability scan passed" in result.stdout
    runtime = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (COMPONENT / "__init__.py", COMPONENT / "coordinator.py", COMPONENT / "ordering_manager.py")
    )
    init_source = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
    assert "ProductionFinalCheckoutAdapter" not in init_source
    assert "FinalCheckoutRequest" not in init_source
    assert "async_execute_live_final" in runtime


def test_release_evidence_keeps_reviewed_final_seam_uncomposed_without_idempotency_claim() -> None:
    evidence = (ROOT / "docs" / "live-ordering-protocol-evidence.md").read_text(encoding="utf-8")
    assert "productionFinalCheckoutSupported: false" in evidence
    for required in (
        "POST /v3/checkouts/order/1",
        "GET /v3/checkouts/order/{checkoutId}",
        "Preserve the server template projection exactly",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "Saved card only",
        "No polling",
        "does **not** establish provider idempotency",
    ):
        assert required in evidence


def test_reviewed_final_path_has_no_completion_cancel_payment_mutation_or_retry() -> None:
    final_source = (COMPONENT / "ordering_live_checkout.py").read_text(encoding="utf-8")
    session_source = (COMPONENT / "api_session.py").read_text(encoding="utf-8")
    manager_source = (COMPONENT / "ordering_manager.py").read_text(encoding="utf-8")
    assert final_source.count("MutationPurpose.FINAL_CHECKOUT") == 1
    assert 'delivery_location=request.quote.delivery_location' in final_source
    assert 'FINAL_CHECKOUT_PATH: Final = "/v3/checkouts/order/1"' in final_source
    assert "async_final_status" in final_source and "FINAL_STATUS_PATH_PREFIX + known" in final_source
    for prohibited in ("/complete", "/cancel", "/payments/", "async_complete_checkout"):
        assert prohibited not in final_source
        assert prohibited not in manager_source
    assert "No branch below retries" in session_source
    assert "refreshes, replays, compensates" in session_source
    assert "while attempt" not in final_source


def test_manifest_does_not_claim_unevidenced_minimum_home_assistant() -> None:
    manifest = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
    assert "homeassistant" not in manifest.get("requirements", [])
    assert "min_homeassistant_version" not in manifest


def test_clean_checkout_verifier_uses_git_archive_and_runs_pytest() -> None:
    source = (ROOT / "scripts" / "verify_clean_checkout.py").read_text(encoding="utf-8")
    assert '"git", "archive", "--format=tar", "HEAD"' in source
    assert '"-m", "pytest", "-q"' in source
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "tests/\n" not in ignored
    assert "tests/private/" in ignored
