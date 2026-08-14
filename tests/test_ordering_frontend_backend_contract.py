"""Cross-layer contract for the runtime frontend basket-adoption payload."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
import voluptuous as vol

from test_ordering_basket_authority import Harness, modules as authority_modules_fixture
from test_ordering_ha_lifecycle import ha_runtime as ha_runtime_fixture

ROOT = Path(__file__).parents[1]
PANEL = ROOT / "custom_components" / "glovo" / "frontend" / "glovo-ordering-panel.js"
HARNESS = ROOT / "tests" / "frontend_ordering_harness.mjs"


@pytest.fixture()
def authority_modules() -> Iterator[dict[str, ModuleType]]:
    """Drive the existing transport-free production-module loader directly."""
    loaded = getattr(authority_modules_fixture, "__wrapped__")()
    modules = next(loaded)
    try:
        yield modules
    finally:
        with pytest.raises(StopIteration):
            next(loaded)


def test_runtime_adoption_payload_crosses_exact_ha_and_facade_contracts(
    monkeypatch: pytest.MonkeyPatch,
    authority_modules: dict[str, ModuleType],
) -> None:
    """The payload emitted by JS must survive every real public boundary unchanged."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute the production frontend payload builder")

    emitted = subprocess.run(
        [node, str(HARNESS), str(PANEL), "--emit-adoption-payload"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert emitted.returncode == 0, emitted.stdout + emitted.stderr
    payload = json.loads(emitted.stdout)
    assert payload == {
        "generation": 7,
        "addressHandle": "address-admin-a",
        "storeHandle": "store-admin-a",
        "products": [
            {"productHandle": "product-admin-a", "quantity": 2, "options": []}
        ],
    }

    runtime = getattr(ha_runtime_fixture, "__wrapped__")(monkeypatch)
    ordering_ha = sys.modules[f"{runtime.prefix}.ordering_ha"]
    command = "glovo/ordering/live/basket_adopt"
    websocket_schema = vol.Schema(
        {vol.Required("id"): int, **ordering_ha._COMMAND_SCHEMAS[command]}
    )
    websocket_message = {"id": 1, "type": command, **payload}
    assert websocket_schema(websocket_message) == websocket_message

    api = authority_modules["ordering_live_api"]
    operation, validated, generation = api.validate_public_request(
        "live/basket_adopt", payload
    )
    assert operation == "live/basket_adopt"
    assert validated == payload
    assert generation == 7
    assert set(payload) == api.OPERATION_REQUEST_FIELDS[operation]

    drifted = dict(payload)
    drifted["addressKey"] = drifted.pop("addressHandle")
    with pytest.raises(vol.Invalid):
        websocket_schema({"id": 2, "type": command, **drifted})
    with pytest.raises(api.PublicContractError):
        api.validate_public_request("live/basket_adopt", drifted)

    facade = Harness(authority_modules)
    adopted = asyncio.run(
        facade.facade.async_dispatch(
            owner="admin-a", operation="live/basket_adopt", request=validated
        )
    )
    assert adopted["status"] == "absent_verified"
    assert facade.account_calls == 1
    assert len(facade.discovery_calls) == 1
