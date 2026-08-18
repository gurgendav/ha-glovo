"""Regression guard for the independent live-spending consent migration contract."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_live_spending_gate_is_a_new_migration_and_reauth_reset_contract() -> None:
    """Keep the public configuration contract default-off without importing HA."""
    constants = (ROOT / "custom_components" / "glovo" / "const.py").read_text()
    integration = (ROOT / "custom_components" / "glovo" / "__init__.py").read_text()
    flow = (ROOT / "custom_components" / "glovo" / "config_flow.py").read_text()
    assert 'CONF_ALLOW_LIVE_CHECKOUT = "allow_live_checkout"' in constants
    assert 'CONF_LIVE_CHECKOUT_ACKNOWLEDGED = "live_checkout_acknowledged"' in constants
    assert "CONFIG_ENTRY_MINOR_VERSION = 18" in integration
    assert "MINOR_VERSION = 18" in flow
    assert "allow_live_checkout = False" in integration
    assert "live_checkout_acknowledged = False" in integration
    assert "CONF_ALLOW_LIVE_CHECKOUT: False" in flow
    assert "CONF_LIVE_CHECKOUT_ACKNOWLEDGED: False" in flow
    assert 'errors["base"] = "live_checkout_ack_required"' in flow
    assert flow.count("selector.BooleanSelector()") == 4
    assert "if allow_checkout and (" in flow
    assert "not allow_ordering or not ordering_ack or not checkout_ack" in flow
    assert "allow_checkout if allow_ordering and ordering_ack else False" in flow
    assert "checkout_ack if allow_checkout else False" in flow
