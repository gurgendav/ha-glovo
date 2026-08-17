"""Home.15 production-shaped final checkout outcome and replay-denial matrix."""
from __future__ import annotations

import asyncio
import importlib.util
import socket
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from test_ordering_live_checkout import quote as build_quote
from test_ordering_live_checkout import response as build_response

ROOT = Path(__file__).parents[1]
GLOVO_ROOT = ROOT / "custom_components" / "glovo"
MODULES = (
    "ordering_models",
    "api_session",
    "ordering_contracts",
    "ordering_remote_basket",
    "ordering_live_quote",
    "ordering_live_checkout",
    "ordering_basket",
    "ordering_quote",
    "ordering_catalog",
    "ordering_journal",
    "ordering_state",
    "ordering_adapter",
    "ordering_manager",
)


@pytest.fixture(autouse=True)
def socket_guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Prove the production-shaped transport stays fixture-only and offline."""

    def blocked(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("Home.15 final checkout fixture attempted outbound network access")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    yield


@pytest.fixture()
def home15() -> Iterator[dict[str, ModuleType]]:
    """Load the exact production modules without importing Home Assistant."""
    package_name = "glovo_home15_final_path"
    package = ModuleType(package_name)
    package.__path__ = [str(GLOVO_ROOT)]  # type: ignore[attr-defined]
    sys.modules[package_name] = package
    loaded: dict[str, ModuleType] = {}
    try:
        for name in MODULES:
            module_name = f"{package_name}.{name}"
            spec = importlib.util.spec_from_file_location(
                module_name, GLOVO_ROOT / f"{name}.py"
            )
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


class ProviderHttpError(RuntimeError):
    """Low-level provider error normalized by the real serialized session."""

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__("private provider response is deliberately unavailable")


class FinalTransport:
    """One-call fixture transport that asserts durable pre-dispatch state."""

    def __init__(self, result: Any, journal: Any) -> None:
        self.result = result
        self.journal = journal
        self.calls: list[tuple[str, str, dict[str, str], dict[str, Any], dict[str, str]]] = []

    async def __call__(
        self,
        method: str,
        access_token: str,
        path: str,
        query: dict[str, str],
        body: dict[str, Any],
        context: dict[str, str],
    ) -> Any:
        assert access_token == "access-fixture"
        assert self.journal.records[-1].state.value == "DISPATCHING"
        self.calls.append((method, path, query, body, context))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


async def _scenario(
    modules: dict[str, ModuleType], *, outcome: str
) -> tuple[Any, Any, FinalTransport, Any]:
    journal_module = modules["ordering_journal"]
    state_module = modules["ordering_state"]
    checkout_module = modules["ordering_live_checkout"]
    manager_module = modules["ordering_manager"]

    class TerminalSaveFailure(journal_module.MemoryJournalStorage):
        failed = False

        async def async_save(self, data: dict[str, Any]) -> None:
            terminal = any(
                record["state"] in {"CONFIRMED_SUCCEEDED", "CONFIRMED_FAILED"}
                for record in data["records"]
            )
            if outcome == "save_failure" and terminal and not self.failed:
                self.failed = True
                raise OSError("private injected terminal save failure")
            await super().async_save(data)

    journal_storage = TerminalSaveFailure()
    journal = journal_module.AttemptJournal(journal_storage, clock=lambda: 100.0)
    durable_storage = state_module.MemoryOrderingStateStorage()
    durable_state = state_module.DurableOrderingState(durable_storage)
    options: dict[str, object] = {
        "allow_ordering": True,
        "ordering_acknowledged": True,
        "allow_live_checkout": True,
        "live_checkout_acknowledged": True,
    }
    manager = manager_module.OrderingManager(
        allow_ordering=True,
        ordering_acknowledged=True,
        allow_live_checkout=True,
        live_checkout_acknowledged=True,
        live_options=lambda: options,
        catalog=None,
        journal=journal,
        checkout_adapter=None,
        clock=lambda: 100.0,
        attempt_source=lambda: f"home15-{outcome}",
        durable_state=durable_state,
        execution_mode="live",
    )
    await manager.async_initialize()
    quote = build_quote(
        modules,
        generation=manager.generation,
        owner_key="admin-fixture",
    )

    if outcome in {"success", "save_failure"}:
        result: Any = build_response("COMPLETED", q=quote)
    elif outcome == "rejected_422":
        result = ProviderHttpError(422)
    elif outcome == "unallowlisted_418":
        result = ProviderHttpError(418)
    elif outcome == "http_500":
        result = ProviderHttpError(500)
    elif outcome == "timeout":
        result = TimeoutError("private timeout")
    elif outcome == "reset":
        result = ConnectionResetError("private reset")
    elif outcome == "malformed":
        result = {"privateMalformed": "not a reviewed checkout response"}
    elif outcome == "cancelled":
        result = asyncio.CancelledError()
    else:  # pragma: no cover - closed test matrix
        raise AssertionError(outcome)

    transport = FinalTransport(result, journal)
    session = modules["api_session"].SerializedApiSession(
        token_source=lambda: "stored-fixture-token",
        persist_token=lambda _value: None,
        ensure_token=lambda _value: ("access-fixture", "stored-fixture-token"),
        transport=lambda *_args: None,
        mutation_transport=transport,
    )
    adapter = checkout_module.ProductionFinalCheckoutAdapter(session)
    request = checkout_module.FinalCheckoutRequest.from_quote(quote, now=100.0)
    user = manager_module.OrderingUser("admin-fixture", True)

    try:
        response = await manager.async_execute_live_final(
            user, manager.generation, quote, request, adapter
        )
    except BaseException as error:  # cancellation is an expected matrix outcome
        response = error

    assert len(transport.calls) == 1
    method, path, query, body, context = transport.calls[0]
    assert (method, path, query) == ("POST", "/v3/checkouts/order/1", {})
    assert body == request.private_body()
    assert context == quote.delivery_location.transport_context()

    # Every terminal or ambiguous result consumes authority. A second direct call
    # is rejected before transport, proving no replay even outside the UI/flow.
    with pytest.raises(BaseException):
        await manager.async_execute_live_final(
            user, quote.generation, quote, request, adapter
        )
    assert len(transport.calls) == 1
    return manager, journal_storage, transport, response


@pytest.mark.parametrize(
    ("outcome", "expected_state", "manual", "response_kind"),
    [
        ("success", "CONFIRMED_SUCCEEDED", False, "succeeded"),
        ("rejected_422", "CONFIRMED_FAILED", False, "failed"),
        ("unallowlisted_418", "MANUAL_CHECK_REQUIRED", True, "error"),
        ("http_500", "MANUAL_CHECK_REQUIRED", True, "error"),
        ("timeout", "MANUAL_CHECK_REQUIRED", True, "error"),
        ("reset", "MANUAL_CHECK_REQUIRED", True, "error"),
        ("malformed", "MANUAL_CHECK_REQUIRED", True, "error"),
        ("cancelled", "MANUAL_CHECK_REQUIRED", True, "cancelled"),
        ("save_failure", "MANUAL_CHECK_REQUIRED", True, "error"),
    ],
)
def test_home15_full_final_path_is_one_call_no_replay_and_durably_conservative(
    home15: dict[str, ModuleType],
    outcome: str,
    expected_state: str,
    manual: bool,
    response_kind: str,
) -> None:
    manager, storage, _transport, response = asyncio.run(
        _scenario(home15, outcome=outcome)
    )
    record = storage.data["records"][-1]
    assert record["state"] == expected_state
    assert manager.manual_check_required is manual
    if response_kind in {"succeeded", "failed"}:
        assert response == {
            "status": response_kind,
            "manualCheckRequired": False,
        }
    elif response_kind == "cancelled":
        assert isinstance(response, asyncio.CancelledError)
    else:
        assert isinstance(response, home15["ordering_manager"].OrderingError)

    # Durable images contain only hashes/closed evidence, never request/provider data.
    exposed = repr(storage.data) + repr(manager._durable_state._storage.data)  # noqa: SLF001
    for forbidden in (
        "stored-fixture-token",
        "access-fixture",
        "checkout-session-fixture-1",
        "basket-fixture-1",
        "40.177",
        "44.513",
        "Fixture delivery address",
    ):
        assert forbidden not in exposed
