"""Release gates for the default-off, fixture-only ordering foundation."""

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
    assert "ordering_live_checkout" not in runtime


def test_release_evidence_is_explicitly_fail_closed() -> None:
    evidence = (ROOT / "docs" / "live-ordering-protocol-evidence.md").read_text(encoding="utf-8")
    assert "productionFinalCheckoutSupported: false" in evidence
    for missing in ("exact request JSON", "success", "rejection", "status endpoint", "completion"):
        assert missing in evidence


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
