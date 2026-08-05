"""Regression tests for Glovo tracking marker normalization."""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


@pytest.fixture(scope="module")
def glovo_module() -> ModuleType:
    """Load the standalone API helper without importing Home Assistant."""
    path = (
        Path(__file__).parents[1] / "custom_components" / "glovo" / "glovo.py"
    )
    spec = importlib.util.spec_from_file_location("glovo_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tracking(*markers: dict[str, Any]) -> dict[str, Any]:
    return {
        "step": "IN_PROGRESS",
        "data": {
            "markers": list(markers),
            "timeline": {},
            "event": {"data": {}},
        },
    }


def test_pickup_marker_populates_store_coordinates(glovo_module: ModuleType) -> None:
    summary = glovo_module.summarize_active_order(
        _tracking(
            {
                "type": "COURIER",
                "latitude": 40.0,
                "longitude": 29.0,
            },
            {
                "type": "PICK_UP",
                "latitude": 41.0082,
                "longitude": 28.9784,
            },
        )
    )

    assert summary["store_lat"] == pytest.approx(41.0082)
    assert summary["store_lon"] == pytest.approx(28.9784)
    assert summary["store_lat"] != summary["courier_lat"]
    assert summary["store_lon"] != summary["courier_lon"]


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [
        (True, 28.9),
        (41.0, False),
        ("41.0", 28.9),
        (41.0, "28.9"),
        (None, 28.9),
        (41.0, None),
        (math.nan, 28.9),
        (41.0, math.inf),
        (-math.inf, 28.9),
        (91.0, 28.9),
        (41.0, 181.0),
    ],
)
def test_malformed_pickup_coordinates_are_rejected_as_a_pair(
    glovo_module: ModuleType,
    latitude: Any,
    longitude: Any,
) -> None:
    summary = glovo_module.summarize_active_order(
        _tracking(
            {
                "type": "PICK_UP",
                "latitude": latitude,
                "longitude": longitude,
            },
            {
                "type": "COURIER",
                "latitude": 40.0,
                "longitude": 29.0,
            },
        )
    )

    assert summary["store_lat"] is None
    assert summary["store_lon"] is None
    assert summary["courier_lat"] == 40.0
    assert summary["courier_lon"] == 29.0


@pytest.mark.parametrize(
    "markers",
    [
        [],
        [{"type": "COURIER", "latitude": 40.0, "longitude": 29.0}],
        [
            {"type": "PICK_UP", "latitude": 41.0, "longitude": 28.9},
            {"type": "PICK_UP", "latitude": 41.1, "longitude": 29.0},
        ],
        [
            {"type": "PICK_UP", "latitude": 41.0, "longitude": 28.9},
            {"type": "PICK_UP", "latitude": None, "longitude": None},
        ],
    ],
)
def test_missing_or_ambiguous_pickup_marker_is_null(
    glovo_module: ModuleType,
    markers: list[dict[str, Any]],
) -> None:
    summary = glovo_module.summarize_active_order(_tracking(*markers))

    assert summary["store_lat"] is None
    assert summary["store_lon"] is None


def test_empty_summary_includes_null_store_coordinates(glovo_module: ModuleType) -> None:
    summary = glovo_module.empty_active_order_summary()

    assert summary["store_lat"] is None
    assert summary["store_lon"] is None
