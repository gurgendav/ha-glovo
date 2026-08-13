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
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "glovo"
PUBLIC_FIXTURES = ROOT / "tests" / "fixtures"
FRONTEND = COMPONENT / "frontend"

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


def scan_capabilities(findings: list[str]) -> None:
    for path in sorted(COMPONENT.rglob("*")):
        if path.suffix not in {".py", ".js"} or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for name, pattern in CAPABILITY_DENYLIST:
            if pattern.search(text):
                findings.append(f"{path.relative_to(ROOT)}: {name}")
    # Production final checkout is permitted only through the reviewed adapter,
    # purpose-typed serialized session, and durable manager coordinator.
    runtime_text = {
        path.name: path.read_text(encoding="utf-8")
        for path in (COMPONENT / "__init__.py", COMPONENT / "ordering_manager.py")
    }
    if "ProductionFinalCheckoutAdapter" not in runtime_text["__init__.py"]:
        findings.append("custom_components/glovo/__init__.py: reviewed final adapter is not composed")
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
