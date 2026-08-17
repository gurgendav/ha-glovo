"""Release gates for the guarded production paid-checkout seam."""

from __future__ import annotations

import hashlib
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
    assert "ProductionFinalCheckoutAdapter" in init_source
    assert "FinalCheckoutRequest.from_quote" in init_source
    assert "async_execute_live_final" in runtime


def test_basket_authority_scanner_enforces_private_store_routes_wiring_and_projection() -> None:
    script = ROOT / "scripts" / "scan_ordering_privacy.py"
    spec = importlib.util.spec_from_file_location("ordering_privacy_basket_scan", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sources = module.load_basket_authority_sources()
    clean: list[str] = []
    module.scan_basket_authority_contracts(clean, sources=sources)
    assert clean == []

    raw_tampered = dict(sources)
    raw_tampered[module.BASKET_STORE_SOURCE] = raw_tampered[
        module.BASKET_STORE_SOURCE
    ].replace(
        '"observed_at": float(validated.observed_at),',
        '"observed_at": float(validated.observed_at),\n            "provider_id": "raw-private",',
        1,
    )
    raw_findings: list[str] = []
    module.scan_basket_authority_contracts(raw_findings, sources=raw_tampered)
    assert "serialized hash-only basket Store image" in "\n".join(raw_findings)

    logo_tampered = dict(sources)
    logo_tampered[module.BASKET_PARSER_SOURCE] = logo_tampered[
        module.BASKET_PARSER_SOURCE
    ].replace('if store_info["logo"] is None', 'if False', 1)
    logo_findings: list[str] = []
    module.scan_basket_authority_contracts(logo_findings, sources=logo_tampered)
    assert "nullable bounded basket logo contract" in "\n".join(logo_findings)

    tampered = dict(sources)
    tampered[module.BASKET_STORE_SOURCE] = tampered[module.BASKET_STORE_SOURCE].replace(
        '"snapshot_digest",', '"basket_id",', 1
    ).replace(
        'f"glovo.ordering_basket_authority_v1.{entry_key}"',
        'f"glovo.public_basket.{entry_key}"',
        1,
    )
    tampered[module.BASKET_DISCOVERY_SOURCE] = tampered[
        module.BASKET_DISCOVERY_SOURCE
    ].replace(
        'root = f"/v1/authenticated/customers/{intent.customer_id}/baskets"',
        'root = f"/v2/customers/{intent.customer_id}/carts"',
        1,
    ).replace(
        "collection_payload = await self._async_get(\n            root, delivery_location, stage=\"collection_get\"\n        )",
        "for _attempt in range(2):\n            collection_payload = await self._async_get(\n                root, delivery_location, stage=\"collection_get\"\n            )",
        1,
    ) + '\nimport logging\nlogging.getLogger(__name__).info("basket", "private")\n'
    tampered[module.RUNTIME_SOURCE] = tampered[module.RUNTIME_SOURCE].replace(
        "discovery_client=RemoteBasketDiscoveryClient(api_session),", "", 1
    )
    tampered[module.PUBLIC_SCHEMA_SOURCES[0]] += (
        '\nLEAK = {"providerId": "private"}\n'
    )
    findings: list[str] = []
    module.scan_basket_authority_contracts(findings, sources=tampered)
    encoded = "\n".join(findings)
    for required in (
        "hash-only basket Store schema",
        "private basket Store key",
        "exact basket GET allowlist",
        "basket discovery retry/polling construct",
        "basket authority source logging",
        "basket authority runtime wiring",
        "public schema exposes provider identifier",
    ):
        assert required in encoded


def test_release_evidence_composes_guarded_final_seam_without_idempotency_claim() -> None:
    evidence = (ROOT / "docs" / "live-ordering-protocol-evidence.md").read_text(encoding="utf-8")
    assert "productionFinalCheckoutSupported: true" in evidence
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


def test_home20_release_identity_basket_evidence_and_no_action_claims_are_frozen() -> None:
    descriptor = (
        "ha-glovo|upstream=0142e44c091f3ff594fe627499070d515598ac5a|"
        "version=1.1.0+home.20|profile=coordinator-source-provenance-v1"
    )
    expected = "4ac96cdf32647e80586cb2f48bb4a024be88c8bf34d5138034a49c0cd2afd339"
    manifest = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
    const = (COMPONENT / "const.py").read_text(encoding="utf-8")
    glovo = (COMPONENT / "glovo.py").read_text(encoding="utf-8")
    documents = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "README.md",
            ROOT / "docs/live-ordering-release-checklist.md",
            ROOT / "docs/live-ordering-operator-runbook.md",
            ROOT / "docs/live-ordering-protocol-evidence.md",
        )
    )

    assert hashlib.sha256(descriptor.encode()).hexdigest() == expected
    assert manifest["version"] == "1.1.0+home.20"
    assert descriptor in const and expected in const
    assert 'ORDERING_WEB_VERSION = "v1.2569.0"' in glovo
    assert "v1.2570.0" not in documents
    for required in (
        "Home.20",
        "UNKNOWN",
        "ABSENT_VERIFIED",
        "ADOPTED",
        "CONFLICT",
        "account-scoped",
        "hash-only",
        "fresh provider re-adoption",
        "cross-administrator",
        "one collection GET",
        "at most one",
        "read-only adoption",
        "fresh re-opt-in",
        "minor v13",
        "fully opted-in Home.19",
        "storeInfo.logo",
        "incrementsLimit",
        "root.storeInfo.logo",
        "exact quote",
        "No deployment",
        "no provider idempotency",
    ):
        assert required.casefold() in documents.casefold()


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
