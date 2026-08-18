"""Home.26 generic first-party web-header parity release tests."""

from __future__ import annotations

import hashlib
import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]
GLOVO = ROOT / "custom_components" / "glovo"

WEB_VERSION = "v1.2580.2"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)
SAFE_STATIC_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en",
    "Glovo-Api-Version": "14",
    "Glovo-App-Context": "web",
    "Glovo-App-Development-State": "prod",
    "Glovo-App-Platform": "web",
    "Glovo-App-Type": "customer",
    "Glovo-App-Version": WEB_VERSION,
    "Glovo-Client-Info": (
        f"web-customer-web-react/{WEB_VERSION} project:customer-web"
    ),
    "Glovo-Language-Code": "en",
    "Glovo-Perseus-Consent": "essential_functional_marketing",
    "Glovo-Delivery-Location-Accuracy": "0",
    "Glovo-Request-TTL": "7500",
    "Referer": "https://glovoapp.com/",
    "User-Agent": USER_AGENT,
    "sec-ch-ua-platform": '"macOS"',
    "sec-ch-ua": (
        '"Not=A?Brand";v="99", "Google Chrome";v="151", '
        '"Chromium";v="151"'
    ),
    "sec-ch-ua-mobile": "?0",
}
PRIVATE_PROCESS_HEADERS = {
    "Glovo-Device-Urn",
    "Glovo-Perseus-Client-Id",
    "Glovo-Perseus-Session-Id",
    "Glovo-Perseus-Session-Timestamp",
    "Glovo-Dynamic-Session-Id",
}
PRIVATE_LOCATION_HEADERS = {
    "Glovo-Location-Country-Code",
    "Glovo-Location-City-Code",
    "Glovo-Delivery-Location-Latitude",
    "Glovo-Delivery-Location-Longitude",
}
FRESH_HEADERS = {
    "Glovo-Delivery-Location-Timestamp",
    "Glovo-Request-Id",
}


def _load_glovo() -> Any:
    spec = importlib.util.spec_from_file_location(
        "glovo_home26_transport_under_test", GLOVO / "glovo.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _context() -> dict[str, str]:
    return {
        "countryCode": "AM",
        "cityCode": "YRV",
        "latitude": "40.177",
        "longitude": "44.513",
    }


def _assert_common_headers(headers: dict[str, str]) -> None:
    expected_keys = (
        set(SAFE_STATIC_HEADERS)
        | PRIVATE_PROCESS_HEADERS
        | PRIVATE_LOCATION_HEADERS
        | FRESH_HEADERS
    )
    assert set(headers) == expected_keys
    assert {key: headers[key] for key in SAFE_STATIC_HEADERS} == SAFE_STATIC_HEADERS
    assert headers["Glovo-Location-Country-Code"] == "AM"
    assert headers["Glovo-Location-City-Code"] == "YRV"
    assert headers["Glovo-Delivery-Location-Latitude"] == "40.177"
    assert headers["Glovo-Delivery-Location-Longitude"] == "44.513"
    assert re.fullmatch(r"glv:device:[0-9a-f-]{36}", headers["Glovo-Device-Urn"])
    assert re.fullmatch(r"[0-9a-f-]{36}", headers["Glovo-Perseus-Client-Id"])
    assert re.fullmatch(r"[0-9a-f-]{36}", headers["Glovo-Perseus-Session-Id"])
    assert headers["Glovo-Perseus-Client-Id"] != headers["Glovo-Perseus-Session-Id"]
    assert headers["Glovo-Dynamic-Session-Id"] == headers[
        "Glovo-Perseus-Session-Id"
    ]
    assert headers["Glovo-Perseus-Session-Timestamp"].isdigit()
    assert headers["Glovo-Delivery-Location-Timestamp"].isdigit()
    assert headers["Glovo-Request-Id"]
    assert "Origin" not in headers
    assert "Content-Type" not in headers


def test_home26_common_header_builder_has_complete_safe_browser_parity() -> None:
    glovo = _load_glovo()

    assert glovo.ORDERING_WEB_VERSION == WEB_VERSION
    _assert_common_headers(glovo._delivery_headers(_context()))


def test_home26_production_transport_ledger_uses_one_generic_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover bootstrap/priced payment, basket adoption, template, and final paths."""
    glovo = _load_glovo()
    ledger: list[dict[str, Any]] = []

    def request(method: str, url: str, **kwargs: Any) -> dict[str, bool]:
        ledger.append({"method": method, "url": url, **kwargs})
        return {"ok": True}

    monkeypatch.setattr(glovo, "_request_json", request)
    timestamps = iter(
        (1_800_000_000.001, 1_800_000_000.002, 1_800_000_000.003,
         1_800_000_000.004, 1_800_000_000.005, 1_800_000_000.006)
    )
    request_ids = iter(f"request-{index}" for index in range(1, 7))
    monkeypatch.setattr(glovo.time, "time", lambda: next(timestamps))
    monkeypatch.setattr(glovo.uuid, "uuid4", lambda: next(request_ids))
    access = "synthetic-access-authority"
    context = _context()

    payment_queries = (
        {
            "storeAddressId": "81",
            "clientSupports": "GooglePay",
            "clientReady": "",
            "context": "checkout",
        },
        {
            "amount": "5600",
            "currency": "AMD",
            "checkoutSessionId": "synthetic-checkout-session",
            "storeAddressId": "81",
            "clientSupports": "GooglePay",
            "clientReady": "",
            "context": "checkout",
        },
    )
    for query in payment_queries:
        glovo.single_attempt_authed_location_get(
            "GET", access, "/v4/payment_methods", query, context
        )
    for path in (
        "/v1/authenticated/customers/42/baskets",
        "/v1/authenticated/customers/42/baskets/stores/71",
    ):
        glovo.single_attempt_authed_location_get("GET", access, path, {}, context)
    for path in ("/v3/checkouts/order/1/template", "/v3/checkouts/order/1"):
        glovo.single_attempt_authed_phase_mutation(
            "POST", access, path, {}, {"synthetic": True}, context
        )

    assert [(item["method"], item["url"].split("?", 1)[0]) for item in ledger] == [
        ("GET", "https://api.glovoapp.com/v4/payment_methods"),
        ("GET", "https://api.glovoapp.com/v4/payment_methods"),
        ("GET", "https://api.glovoapp.com/v1/authenticated/customers/42/baskets"),
        (
            "GET",
            "https://api.glovoapp.com/v1/authenticated/customers/42/baskets/stores/71",
        ),
        ("POST", "https://api.glovoapp.com/v3/checkouts/order/1/template"),
        ("POST", "https://api.glovoapp.com/v3/checkouts/order/1"),
    ]
    for item in ledger:
        assert item["access_token"] == access
        _assert_common_headers(item["extra_headers"])
    assert len({item["extra_headers"]["Glovo-Request-Id"] for item in ledger}) == 6
    assert len(
        {
            item["extra_headers"]["Glovo-Delivery-Location-Timestamp"]
            for item in ledger
        }
    ) == 6
    for key in PRIVATE_PROCESS_HEADERS:
        assert len({item["extra_headers"][key] for item in ledger}) == 1


def test_home26_release_identity_and_fresh_consent_migration_are_coupled() -> None:
    descriptor = (
        "ha-glovo|upstream=0142e44c091f3ff594fe627499070d515598ac5a|"
        "version=1.1.0+home.26|profile=coordinator-source-provenance-v1"
    )
    digest = "c711950a2891a32d2f1a1c790364e39b7b937a11bc2642f3c4e865af421e2541"
    manifest = (GLOVO / "manifest.json").read_text(encoding="utf-8")
    const = (GLOVO / "const.py").read_text(encoding="utf-8")
    integration = (GLOVO / "__init__.py").read_text(encoding="utf-8")
    flow = (GLOVO / "config_flow.py").read_text(encoding="utf-8")

    assert hashlib.sha256(descriptor.encode()).hexdigest() == digest
    assert '"version": "1.1.0+home.26"' in manifest
    assert descriptor in const and digest in const
    assert "CONFIG_ENTRY_MINOR_VERSION = 19" in integration
    assert "MINOR_VERSION = 19" in flow
