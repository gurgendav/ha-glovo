"""Constants for the Glovo integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "glovo"

# Runtime attributes intended for access-control checks. The trust identifier is
# the SHA-256 of this canonical build descriptor:
#
# ha-glovo|upstream=0142e44c091f3ff594fe627499070d515598ac5a|version=1.1.0+home.24|profile=coordinator-source-provenance-v1
#
# It is deliberately a literal, stable pin: runtime code never derives it from
# credentials, order identifiers, coordinates, or mutable fixture contents.
DATA_PROVENANCE_ATTRIBUTE = "data_provenance"
INTEGRATION_TRUST_ID_ATTRIBUTE = "integration_trust_id"
INTEGRATION_TRUST_ID = (
    "ha-glovo:1.1.0+home.24:sha256:"
    "462673138c8af1d355247d65deae2479537a0f6ed3220217a2b58164d5fa4ddc"
)

PROVENANCE_LIVE_API = "live_api"
PROVENANCE_FIXTURE = "fixture"
PROVENANCE_UNAVAILABLE = "unavailable"
PROVENANCE_UNKNOWN = "unknown"

# Sensor key used for the combined order status enum.
OVERALL_STATUS_SENSOR_KEY = "overall_status"

# Config entry data keys.
CONF_REFRESH_TOKEN = "refresh_token"
# Full token state as a JSON string produced by the glovo library
# ({"access_token", "refresh_token", "expires_at"}). Persisted in entry.data
# so a fresh access token survives Home Assistant restarts.
CONF_TOKEN = "token"

# Options keys.
CONF_SCAN_INTERVAL = "scan_interval"
CONF_ALLOW_ORDERING = "allow_ordering"
CONF_ORDERING_ACKNOWLEDGED = "ordering_acknowledged"
# Preparation consent never authorizes spending. Checkout requires these newer,
# independent literal gates and is reset by reauthentication/migration.
CONF_ALLOW_LIVE_CHECKOUT = "allow_live_checkout"
CONF_LIVE_CHECKOUT_ACKNOWLEDGED = "live_checkout_acknowledged"

DEFAULT_SCAN_INTERVAL = 15
MIN_SCAN_INTERVAL = 5
MAX_SCAN_INTERVAL = 3600

# Once an order leaves the active list, keep surfacing its final
# (delivered/canceled) status for this long before falling back to "unknown".
CACHE_TERMINAL_HOLD_SEC = 60

# Overall-status values considered terminal for the grace-period cache.
TERMINAL_OVERALL_STATUSES = ("delivered", "canceled")

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.DEVICE_TRACKER,
]

# Temporary dev mode: when all fixture JSON files exist under this path
# (relative to the HA config directory), the coordinator serves data from
# them instead of calling the Glovo API. Remove the files to go live again.
FIXTURES_REL_PATH = "projects/ha-glovo"
