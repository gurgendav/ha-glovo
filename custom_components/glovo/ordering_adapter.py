"""Fixture-only checkout adapter with no live transport capability."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

MOCK_MODE: Final = "mock"


class LiveOrderingUnavailable(RuntimeError):
    """Raised before any attempt when execution is not explicitly fixture-only."""


class FixtureHTTP5xx(RuntimeError):
    """Mock-only stand-in for an ambiguous provider 5xx after dispatch."""


class FixtureMalformedResponse(RuntimeError):
    """Mock-only stand-in for a malformed post-dispatch provider response."""


@dataclass(frozen=True, slots=True)
class MockCheckoutResult:
    """Clearly synthetic deterministic result."""

    mock: bool = True
    outcome: str = "synthetic_success"
    message: str = "Mock checkout completed; no order was sent."

    def public_dict(self) -> dict[str, object]:
        return {"mock": self.mock, "outcome": self.outcome, "message": self.message}


class MockCheckoutAdapter:
    """In-memory adapter deliberately incapable of HTTP or remote mutations."""

    def __init__(self, *, outcome: str = "synthetic_success") -> None:
        if outcome not in {"synthetic_success", "synthetic_failure"}:
            raise ValueError("unsupported synthetic outcome")
        self._outcome = outcome
        self.execution_count = 0

    async def async_execute(self, *, mode: str, fingerprint: str) -> MockCheckoutResult:
        """Record a fixture execution only; reject every other mode immediately."""
        if mode != MOCK_MODE:
            raise LiveOrderingUnavailable("live ordering is unavailable in this integration")
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise ValueError("mock checkout fingerprint is invalid")
        self.execution_count += 1
        if self._outcome == "synthetic_failure":
            return MockCheckoutResult(
                outcome="synthetic_failure",
                message="Mock checkout failed synthetically; no order was sent.",
            )
        return MockCheckoutResult()
