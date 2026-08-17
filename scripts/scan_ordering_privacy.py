#!/usr/bin/env python3
"""Fail closed on public ordering-data leaks and purchase capability seams.

Only public projections are scanned: shipped frontend JavaScript, deterministic
public fixture data, and string literals sent to Python loggers.  Python field
names and internal DTO declarations are intentionally not treated as public
output, preventing false positives merely because secure code names a token or
payment field.  Private captures remain outside the accepted fixture tree.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "glovo"
PUBLIC_FIXTURES = ROOT / "tests" / "fixtures"
FRONTEND = COMPONENT / "frontend"
BASKET_STORE_SOURCE = (
    "custom_components/glovo/ordering_basket_authority_store.py"
)
BASKET_DISCOVERY_SOURCE = (
    "custom_components/glovo/ordering_remote_basket_discovery.py"
)
BASKET_PARSER_SOURCE = "custom_components/glovo/ordering_remote_basket.py"
RUNTIME_SOURCE = "custom_components/glovo/__init__.py"
PUBLIC_SCHEMA_SOURCES = (
    "custom_components/glovo/ordering_ha.py",
    "custom_components/glovo/ordering_surface.py",
    "custom_components/glovo/ordering_live_api.py",
    "custom_components/glovo/frontend/glovo-ordering-panel.js",
)

_BASKET_STORE_FIELDS = frozenset(
    {
        "version",
        "generation",
        "knowledge",
        "account_digest",
        "store_digest",
        "intent_digest",
        "snapshot_digest",
        "observed_at",
    }
)
_PUBLIC_PROVIDER_KEYS = frozenset(
    {
        "providerid",
        "provider_id",
        "basketid",
        "basket_id",
        "basketversion",
        "basket_version",
        "customerid",
        "customer_id",
        "storeid",
        "store_id",
        "storeaddressid",
        "store_address_id",
        "storecategoryid",
        "store_category_id",
        "checkoutid",
        "checkout_id",
        "checkoutref",
        "checkout_ref",
        "checkoutsession",
        "checkout_session",
    }
)
_LOGGER_METHODS = frozenset(
    {"debug", "info", "warning", "error", "exception", "critical"}
)

# These patterns target values, not source-field names.  The test fixtures use
# literal "fixture" placeholders and explicitly masked card presentation only.
VALUE_DENYLIST: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("authorization bearer value", re.compile(r"(?i)bearer\\s+[a-z0-9._~+/-]{16,}")),
    ("JWT-like token", re.compile(r"\\beyJ[a-zA-Z0-9_-]{10,}\\.[a-zA-Z0-9_-]{10,}\\.[a-zA-Z0-9_-]{8,}\\b")),
    ("cookie assignment", re.compile(r"(?i)\\b(?:set-cookie|cookie)\\s*[:=].+")),
    ("unmasked payment-card number", re.compile(r"(?<![0-9])(?:[0-9]{4}[- ]?){3}[0-9]{4}(?![0-9])")),
    ("CVV/CVC value", re.compile(r"(?i)\\b(?:cvv|cvc)\\s*[:=]\\s*[0-9]{3,4}\\b")),
    ("full street address", re.compile(r"(?i)\\b\\d{1,5}\\s+[a-z][a-z .'-]{2,}\\s+(?:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln)\\b")),
    ("email address", re.compile(r"(?i)\\b[a-z0-9._%+-]+@[a-z0-9.-]+\\.[a-z]{2,}\\b")),
    ("phone number", re.compile(r"(?<![0-9])\\+?[0-9][0-9 ()-]{8,}[0-9](?![0-9])")),
)
SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "access_token",
        "refresh_token",
        "id_token",
        "cookie",
        "set-cookie",
        "payment_token",
        "payment_method_id",
        "card_number",
        "cvv",
        "cvc",
        "checkout_id",
        "checkoutid",
        "checkout_ref",
        "checkoutref",
        "checkout_session",
        "checkoutsession",
        "provider_id",
        "providerid",
    }
)
CAPABILITY_DENYLIST: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Home Assistant service registration", re.compile(r"(?:async_)?register_service|services\\.async_register")),
    ("final-purchase service name", re.compile(r"(?i)(?:glovo[._-])?(?:final_)?(?:checkout|purchase|place_order)[._-](?:submit|confirm|execute|complete)")),
    ("webhook registration", re.compile(r"(?:async_)?register_webhook")),
    ("final-purchase button entity", re.compile(r"(?is)(?:buttonentity|button entity).{0,160}(?:checkout|purchase|place_order)")),
    ("final-purchase event emission", re.compile(r"(?is)(?:async_fire|bus\\.async_fire).{0,160}(?:checkout|purchase|place_order)")),
    ("automation final-purchase seam", re.compile(r"(?is)(?:device_automation|automation).{0,160}(?:checkout|purchase|place_order)")),
    ("MQTT command seam", re.compile(r"(?i)mqtt(?:\\.|_).*(?:purchase|checkout|place_order)")),
    ("intent purchase seam", re.compile(r"(?i)(?:intent|conversation)(?:\\.|_).*(?:purchase|checkout|place_order)")),
)
# Public operation names such as ``live/checkout_status`` are intentionally not
# private references.  Scan only identifier-shaped checkout/provider handles.
PRIVATE_REFERENCE_PATTERN = re.compile(
    r"(?i)\b(?:checkout(?:[_-]?(?:id|ref|session(?:[_-]?id)?))|provider[_-]?id)\b"
)


def iter_public_files() -> list[Path]:
    files: list[Path] = []
    if PUBLIC_FIXTURES.exists():
        files.extend(path for path in PUBLIC_FIXTURES.rglob("*") if path.is_file() and "private" not in path.parts)
    if FRONTEND.exists():
        files.extend(path for path in FRONTEND.rglob("*.js") if path.is_file())
    return sorted(files)


def inspect_value(value: Any, location: str, findings: list[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and key.lower() in SENSITIVE_KEYS:
                # Sanitized offline fixtures may model a response key only when
                # its value is visibly synthetic; raw checkout handles remain
                # forbidden in all public fixture data.
                fixture_checkout = (
                    key.lower() in {"checkout_id", "checkoutid"}
                    and isinstance(child, str)
                    and "fixture" in child.lower()
                )
                if not fixture_checkout:
                    findings.append(f"{location}: sensitive public key {key!r}")
            inspect_value(child, location, findings)
    elif isinstance(value, list):
        for child in value:
            inspect_value(child, location, findings)
    elif isinstance(value, str):
        for name, pattern in VALUE_DENYLIST:
            if pattern.search(value):
                findings.append(f"{location}: {name}")


def scan_frontend_private_references(
    text: str, location: str, findings: list[str]
) -> None:
    """Reject leaked provider/checkout handles without flagging public routes."""
    if PRIVATE_REFERENCE_PATTERN.search(text):
        findings.append(f"{location}: private checkout/provider reference")


def scan_public_values(findings: list[str]) -> None:
    for path in iter_public_files():
        relative = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            try:
                inspect_value(json.loads(text), relative, findings)
            except json.JSONDecodeError as error:
                findings.append(f"{relative}: invalid public JSON ({error.msg})")
        else:
            inspect_value(text, relative, findings)
            if path.suffix == ".js":
                scan_frontend_private_references(text, relative, findings)


def scan_logger_literals(findings: list[str]) -> None:
    for path in COMPONENT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"debug", "info", "warning", "error", "exception", "critical"}:
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                continue
            value = node.args[0].value
            for name, pattern in VALUE_DENYLIST:
                if pattern.search(value):
                    findings.append(f"{path.relative_to(ROOT)}:{node.lineno}: logger literal has {name}")


def load_basket_authority_sources() -> dict[str, str]:
    """Load the release-critical basket authority and public boundary sources."""

    names = (
        BASKET_STORE_SOURCE,
        BASKET_DISCOVERY_SOURCE,
        BASKET_PARSER_SOURCE,
        RUNTIME_SOURCE,
        *PUBLIC_SCHEMA_SOURCES,
    )
    return {name: (ROOT / name).read_text(encoding="utf-8") for name in names}


def _assigned_frozenset(source: str, name: str) -> frozenset[str] | None:
    """Read one literal ``frozenset({...})`` assignment without importing code."""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == name for target in targets):
            continue
        value = node.value
        if (
            not isinstance(value, ast.Call)
            or not isinstance(value.func, ast.Name)
            or value.func.id != "frozenset"
            or len(value.args) != 1
        ):
            return None
        try:
            literal = ast.literal_eval(value.args[0])
        except (ValueError, TypeError, SyntaxError):
            return None
        if not isinstance(literal, (set, tuple, list)) or not all(
            type(item) is str for item in literal
        ):
            return None
        return frozenset(literal)
    return None


def _has_logger_call(source: str) -> bool:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return True
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _LOGGER_METHODS
        for node in ast.walk(tree)
    )


def _method_returned_dict_keys(
    source: str, *, class_name: str, method_name: str
) -> frozenset[str] | None:
    """Return exact literal keys from one class method's sole dictionary return."""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    owner = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        ),
        None,
    )
    if owner is None:
        return None
    method = next(
        (
            node
            for node in owner.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == method_name
        ),
        None,
    )
    if method is None:
        return None
    returns = [node for node in ast.walk(method) if isinstance(node, ast.Return)]
    if len(returns) != 1 or not isinstance(returns[0].value, ast.Dict):
        return None
    keys = returns[0].value.keys
    result: set[str] = set()
    for key in keys:
        if not isinstance(key, ast.Constant) or type(key.value) is not str:
            return None
        result.add(key.value)
    return frozenset(result)


def _python_public_provider_keys(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {"invalid_python_schema"}
    return {
        node.value.casefold()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and type(node.value) is str
        and node.value.casefold() in _PUBLIC_PROVIDER_KEYS
    }


def scan_basket_authority_contracts(
    findings: list[str], *, sources: Mapping[str, str] | None = None
) -> None:
    """Enforce Home.21's private, read-only basket authority release contract."""

    loaded = dict(sources) if sources is not None else load_basket_authority_sources()
    required_names = {
        BASKET_STORE_SOURCE,
        BASKET_DISCOVERY_SOURCE,
        BASKET_PARSER_SOURCE,
        RUNTIME_SOURCE,
        *PUBLIC_SCHEMA_SOURCES,
    }
    if set(loaded) != required_names or not all(
        isinstance(loaded.get(name), str) for name in required_names
    ):
        findings.append("basket authority scanner source inventory is incomplete")
        return

    store = loaded[BASKET_STORE_SOURCE]
    discovery = loaded[BASKET_DISCOVERY_SOURCE]
    parser = loaded[BASKET_PARSER_SOURCE]
    runtime = loaded[RUNTIME_SOURCE]

    if _assigned_frozenset(store, "_STORAGE_KEYS") != _BASKET_STORE_FIELDS:
        findings.append(
            f"{BASKET_STORE_SOURCE}: hash-only basket Store schema changed"
        )
    if _method_returned_dict_keys(
        store,
        class_name="BasketAuthoritySnapshot",
        method_name="to_raw",
    ) != _BASKET_STORE_FIELDS or (
        "await self._storage.async_save(candidate.to_raw())" not in store
    ):
        findings.append(
            f"{BASKET_STORE_SOURCE}: serialized hash-only basket Store image changed"
        )
    if (
        "from homeassistant.helpers.storage import Store" not in store
        or 'f"glovo.ordering_basket_authority_v1.{entry_key}"' not in store
    ):
        findings.append(f"{BASKET_STORE_SOURCE}: private basket Store key is absent")
    if _has_logger_call(store) or _has_logger_call(discovery) or _has_logger_call(parser):
        findings.append("basket authority source logging is forbidden")

    bounded_payload = "_bounded_payload(payload)"
    opaque_store_display_contract = (
        '"storeInfo",' in parser
        and bounded_payload in parser
        and "Provider-owned lifecycle/store display extensions are bounded" in parser
        and "whole payload preflight and discarded as complete opaque values" in parser
        and '"storeInfo":' not in parser
    )
    if not opaque_store_display_contract:
        findings.append(
            f"{BASKET_PARSER_SOURCE}: bounded opaque store display contract changed"
        )

    wiring = (
        "HomeAssistantBasketAuthorityStorage(hass, entry.entry_id)",
        "await basket_authority.async_load()",
        "discovery_client=RemoteBasketDiscoveryClient(api_session),",
        "basket_authority=basket_authority,",
    )
    if not all(token in runtime for token in wiring) or runtime.count(
        "basket_authority=basket_authority,"
    ) < 2:
        findings.append(f"{RUNTIME_SOURCE}: basket authority runtime wiring is incomplete")

    exact_routes = (
        'root = f"/v1/authenticated/customers/{intent.customer_id}/baskets"',
        "collection_payload = await self._async_get(",
        'root, delivery_location, stage="collection_get"',
        'f"{root}/stores/{intent.store_id}"',
        'stage="full_get"',
        '"basket", path, delivery_location=delivery_location',
    )
    if not all(token in discovery for token in exact_routes):
        findings.append(
            f"{BASKET_DISCOVERY_SOURCE}: exact basket GET allowlist changed"
        )

    try:
        discovery_tree = ast.parse(discovery)
    except SyntaxError:
        discovery_tree = None
    discover_method = None
    if discovery_tree is not None:
        discover_method = next(
            (
                node
                for node in ast.walk(discovery_tree)
                if isinstance(node, ast.AsyncFunctionDef)
                and node.name == "async_discover"
            ),
            None,
        )
    get_calls = []
    if discover_method is not None:
        get_calls = [
            node
            for node in ast.walk(discover_method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
            and node.func.attr == "_async_get"
        ]
    if (
        discover_method is None
        or len(get_calls) != 2
        or any(isinstance(node, (ast.For, ast.AsyncFor, ast.While)) for node in ast.walk(discover_method))
        or any(
            token in discovery
            for token in (
                "asyncio.sleep",
                "asyncio.gather",
                ".async_post(",
                ".async_put(",
                ".async_patch(",
                ".async_delete(",
            )
        )
    ):
        findings.append(
            f"{BASKET_DISCOVERY_SOURCE}: basket discovery retry/polling construct or mutation exists"
        )

    js_private_pattern = re.compile(
        r"(?i)\b(?:provider|basket|customer|store(?:address|category)?|checkout)"
        r"(?:Id|_id|Version|_version|Ref|_ref|Session|_session)\b"
    )
    for name in PUBLIC_SCHEMA_SOURCES:
        source = loaded[name]
        leaked = (
            bool(js_private_pattern.search(source))
            if name.endswith(".js")
            else bool(_python_public_provider_keys(source))
        )
        if leaked:
            findings.append(f"{name}: public schema exposes provider identifier")


def scan_capabilities(findings: list[str]) -> None:
    for path in sorted(COMPONENT.rglob("*")):
        if path.suffix not in {".py", ".js"} or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for name, pattern in CAPABILITY_DENYLIST:
            if pattern.search(text):
                findings.append(f"{path.relative_to(ROOT)}: {name}")
    scan_basket_authority_contracts(findings)
    # Home.21 must compose the exact reviewed adapter and request factory while
    # retaining the durable final coordinator. Runtime tests prove gate absence.
    runtime_text = {
        path.name: path.read_text(encoding="utf-8")
        for path in (COMPONENT / "__init__.py", COMPONENT / "ordering_manager.py")
    }
    for required in ("ProductionFinalCheckoutAdapter", "FinalCheckoutRequest.from_quote"):
        if required not in runtime_text["__init__.py"]:
            findings.append(
                "custom_components/glovo/__init__.py: guarded final composition is absent"
            )
    if "async_execute_live_final" not in runtime_text["ordering_manager.py"]:
        findings.append("custom_components/glovo/ordering_manager.py: durable final coordinator is absent")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capabilities-only", action="store_true")
    args = parser.parse_args()
    findings: list[str] = []
    if not args.capabilities_only:
        scan_public_values(findings)
        scan_logger_literals(findings)
    scan_capabilities(findings)
    if findings:
        print("ordering safety scan failed:", *sorted(findings), sep="\n- ", file=sys.stderr)
        return 1
    scope = "capability" if args.capabilities_only else "privacy and capability"
    print(f"ordering {scope} scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
