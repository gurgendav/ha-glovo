#!/usr/bin/env python3
r"""Glovo customer API client with token JSON storage and auto-refresh.

AUTHENTICATION
--------------
The website stores an access token (JWT, ~20 min TTL) in the cookie
`glovo_auth_info` and a long-lived refresh token in localStorage
(`glovo_refresh_token`). This client keeps both in a JSON string:

    {"access_token": "...", "refresh_token": "...", "expires_at": 1780851244}

Library functions accept this JSON as a string and return an updated JSON
string (even when the token was not refreshed). File I/O is CLI-only.

When the access token is about to expire it is renewed via
POST /oauth/refresh, which returns a fresh access + refresh token pair.

ENDPOINTS
---------
GET /v3/customer/orders-list?offset=&limit=
    Paginated list of orders (active + past).
GET /v3/customer/orders/{id}
    Static order details: products, prices, addresses, payment.
    `courier` is often null for active orders; it is NOT a live-tracking feed.
GET /v3/customer/orders/{id}/flow
    Legacy live status: progress steps, animation, ETA text, map state.
    Poll every `secondsToNextRequest` seconds while the order is active.
GET /v1/customer/orders/{id}/tracking/summary?origin=&initial=
    New live tracking: courier map marker, timeline texts, ETA, progress.
    Poll every `data.pollingIntervalMillis` ms while `step` == IN_PROGRESS.

FIELD VALUES
------------
Values below come from the website's Zod schemas (k5n([...]) enums in the
JS bundle) where available, otherwise from observed API responses. Fields
marked "observed" are plain backend strings the frontend does NOT constrain,
so the list may be incomplete. See the ENUMS mapping for the source of truth
this client uses for human-readable output.

OBSERVED LIFECYCLE (tracking/summary, single delivered order)
-------------------------------------------------------------
    step            : IN_PROGRESS .......................... -> DELIVERED
    partnerStatus   : PREPARING -> READY
    courierStatus   : ASSIGNED -> WAITING -> ON_THE_WAY -> ARRIVING
    statusTag.id    : ON_TIME ........................ -> LATE (if delayed)

ETA SEMANTICS (tracking/summary)
--------------------------------
`eta.text` and `event.data.estimatedTimeOfArrivalFormat` change shape during
the lifecycle:

  * Before the courier leaves the store the ETA is an approximate clock-time
    range (format "timestamp"):
        eta.text                             = "16:05 - 16:25"
        event.data.estimatedTimeOfArrivalRangeLower = "16:05"  (min)
        event.data.estimatedTimeOfArrivalRangeUpper = "16:25"  (max)

  * Once the courier is on the way it becomes a single minutes countdown
    (format "countdown"); min and max collapse to the same value:
        eta.text                             = "16 minutes left"
        event.data.estimatedTimeOfArrival    = "16"            (min == max)

  * If the order is re-estimated as LATE, `eta.formerEta` keeps the original
    range (e.g. "16:05 - 16:25") while eta.text shows the updated countdown.

This client normalizes all of the above in summarize_active_order().

LAST ACTIVE ORDER SUMMARY (Home Assistant)
------------------------------------------
get_last_active_order_summary() always returns a flat dict ready to feed
into a Home Assistant REST/command-line sensor. When there are no active
orders, order_count is 0 and all other fields are None. Example fields:

    order_count,           # active orders in orders-list (0 if none)
    order_id, store_name, store_lat, store_lon,
    overall_status,        # our own enum, see ENUMS["overall.status"]
    overall_status_text,
    step, step_text, active, delivered, canceled,
    courier_name, courier_phone, courier_status, courier_status_text,
    courier_message, chat_available, courier_count,
    partner_status, partner_status_text, partner_message,
    progress_percent,
    is_late, lateness, lateness_text,
    eta_format,            # "timestamp" (range) | "countdown" (minutes)
    eta_min, eta_max,      # minutes left to the lower/upper bound (ints);
                           # equal when the API reports a single countdown;
                           # for scheduled orders, derived from scheduledTime /
                           # scheduledTimeEnd in v3 order details
    eta_text, eta_former,
    courier_lat, courier_lon, courier_heading,
    poll_interval_sec

When an order is split across several couriers (partial delivery), timeline.couriers
and markers may list more than one COURIER. In that case:

    courier_count          total couriers on the order
    courier_* (name, lat…) the one nearest to the delivery point (DROP_OFF marker)

partner_status, eta_*, overall_status and event.courierStatus are order-level fields
from the API analytics block — they may describe the whole order (e.g. partnerStatus
can stay READY while the first courier has already picked up part of it).

When several orders are active at once, order_count reflects the total; the rest
of the fields describe one of them (the first ACTIVE_ORDER row in orders-list).

Python (library — token as JSON string, no files):

    from glovo import get_last_active_order_summary, dump_token_json

    token_json = '{"access_token":"...","refresh_token":"...","expires_at":1780851244}'
    summary, token_json = get_last_active_order_summary(token_json)
    # persist token_json back to HA helper storage if it changed

CLI (reads/writes token file):

    python3 glovo.py --token-file token.json active
    python3 glovo.py --token-file token.json --humanize active
    python3 glovo.py --token-file token.json active --no-details   # skip v3 call

Offline (parse a saved dump):

    from glovo import summarize_active_order
    import json
    summary = summarize_active_order(
        json.load(open("..-tracking.json")),
        info=json.load(open("..-info.json")),   # optional: clean courier name
    )
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable
from typing import Any, Literal

API_URL = "https://api.glovoapp.com"
DEFAULT_REFRESH_MARGIN_SEC = 60
TrackingOrigin = Literal["ORDER_TRACKING", "ORDER_DETAILS"]
TERMINAL_TRACKING_STEPS = frozenset({"DELIVERED", "CANCELED", "CANCELLED"})
TRACKABLE_STEPS = frozenset({"SCHEDULED", "IN_PROGRESS"})

# Documented field values. Each inner mapping is code -> human-readable meaning.
# "source" in the comment marks where the values were confirmed:
#   [zod]      -> validated by the website (k5n enum), authoritative for the client
#   [observed] -> seen in live responses, frontend accepts any string (may be partial)
ENUMS: dict[str, dict[str, str]] = {
    # tracking/summary: top-level lifecycle step  [zod]
    "tracking.step": {
        "SCHEDULED": "scheduled for later, not started yet",
        "IN_PROGRESS": "order is active (being prepared / picked up / delivered)",
        "DELIVERED": "delivered (terminal)",
        "CANCELED": "canceled (terminal)",
    },
    # tracking/summary: data.statusTag.id, also event.data.orderLatenessStatus  [zod]
    # When LATE, statusTag.text becomes "Updated arrival" and eta.formerEta holds
    # the original time range. When ON_TIME, statusTag.text is "On time".
    "tracking.statusTag": {
        "MISSING": "no ETA available",
        "ESTIMATED": "ETA is a rough estimate",
        "ON_TIME": "delivery is on time",
        "LATE": "delivery is running late",
    },
    # tracking/summary: data.markers[].type  [zod]
    "tracking.marker": {
        "PICK_UP": "store / pickup point",
        "DROP_OFF": "delivery destination",
        "COURIER": "courier's live position",
    },
    # tracking/summary: event.data.partnerStatus  [observed]
    "tracking.partnerStatus": {
        "PREPARING": "store is preparing the order",
        "READY": "order is ready for pickup",
    },
    # tracking/summary: event.data.courierStatus  [observed]
    # Lifecycle order: ASSIGNED -> WAITING -> ON_THE_WAY -> ARRIVING.
    "tracking.courierStatus": {
        "ASSIGNED": "courier assigned, heading to the store",
        "WAITING": "courier is waiting for the order at the store",
        "ON_THE_WAY": "courier is on the way to you",
        "ARRIVING": "courier is arriving at your location",
    },
    # tracking/summary: event.data.orderStatusPage  [zod]
    "tracking.statusPage": {
        "map": "map view (courier is trackable)",
        "non_map": "status view without a map",
        "scheduled": "scheduled-order view",
        "delivered": "delivered view",
        "cancelled": "cancelled view",
    },
    # flow: page  [zod]
    "flow.page": {
        "STATUS": "status screen (no live map yet)",
        "MAP": "live map screen with courier",
        "DELIVERED": "delivered (terminal)",
        "CANCELLED": "cancelled (terminal)",
        "MARKETPLACE": "marketplace order screen",
    },
    # flow: statusData.progressData.steps[].status  [observed]
    "flow.stepStatus": {
        "EMPTY": "not started",
        "ANIMATED": "in progress (animated)",
        "FILLED": "completed",
    },
    # tracking/summary: event.data.estimatedTimeOfArrivalFormat  [observed]
    "tracking.etaFormat": {
        "timestamp": "approximate clock-time range (before courier departs)",
        "countdown": "single minutes countdown (courier on the way)",
    },
    # Custom client-side status combining step + partnerStatus + courierStatus.
    # Two parallel tracks meet at pickup:
    #   store:   PREPARING -> READY
    #   courier: (none) -> ASSIGNED -> WAITING(at store) -> ON_THE_WAY -> ARRIVING
    # "obs" marks states confirmed in real dumps; others are logically reachable
    # but were not captured (transitions happened between polls).
    "overall.status": {
        "SCHEDULED": "scheduled for later, not started yet",
        "PREPARING": "store is preparing the order, no courier assigned yet (obs)",
        "COURIER_ASSIGNED": "store preparing (PREPARING), courier assigned (ASSIGNED)",
        "COURIER_WAITING": "courier is at the store waiting for the order to be prepared (obs)",
        "AWAITING_PICKUP": "order is ready, waiting for courier pickup (at store or en route)",
        "ON_THE_WAY": "courier is on the way to you (obs)",
        "ARRIVING": "courier is arriving at your location (obs)",
        "DELIVERED": "delivered (obs)",
        "CANCELED": "canceled",
        "UNKNOWN": "could not determine the overall status",
    },
    # order details: currentStatus.type  [zod]
    "order.status": {
        "DraftStatus": "draft, not confirmed",
        "OrphanStatus": "orphaned (no courier/store assigned)",
        "NewStatus": "new, just created",
        "ScheduledStatus": "scheduled for later",
        "ProgressStatus": "in progress",
        "DeliveredStatus": "delivered",
        "CanceledStatus": "canceled",
    },
    # order details: handlingStrategy.type  [zod]
    "order.handling": {
        "DELIVERY": "courier delivery",
        "IN_STORE": "handled in store",
        "PICKUP": "customer pickup",
    },
    # order details: points[].type  [zod]
    "order.point": {
        "PICKUP": "pickup location",
        "DELIVERY": "delivery location",
        "STARTING": "courier start location",
        "RETURN": "return location",
    },
    # order details: subtype  [zod]
    "order.subtype": {
        "STANDARD": "standard delivery",
        "EXPRESS": "express delivery",
        "SCHEDULED": "scheduled delivery",
        "PURCHASE": "purchase (q-commerce / store)",
    },
    # order details: origin  [zod]
    "order.origin": {
        "STORES": "ordered from a store catalog",
        "CUSTOM": "custom order",
        "RETURN": "return order",
    },
    # order details: pricingBreakdown.lines[].type  [zod]
    "order.priceLine": {
        "PRODUCTS": "products subtotal",
        "MIN_BASKET_SURCHARGE": "small-basket surcharge",
        "DELIVERY": "delivery fee",
        "SERVICE": "service fee",
        "WEATHER_SURCHARGE": "bad-weather surcharge",
        "CANCELLATION_FEE": "cancellation fee",
        "DISCOUNT": "discount",
        "COMPENSATION": "compensation",
        "FULL_REFUND": "full refund",
        "SECURITY_DEPOSIT": "security deposit",
        "TOTAL": "total",
        "COURIER_TIP": "courier tip",
        "PROMOTIONS": "promotions",
    },
}


def describe(enum_key: str, value: Any) -> str:
    """Return "VALUE (meaning)" when known, otherwise "VALUE (unknown)"."""
    if value is None:
        return "—"
    meaning = ENUMS.get(enum_key, {}).get(value)
    return f"{value} ({meaning})" if meaning else f"{value} (unknown)"


def _ha_enum(value: str | None) -> str | None:
    """Normalize an API enum string to a homeassistant state value.

    Home Assistant stores enum states in lowercase and resolves UI translations
    from entity.sensor.<key>.state.<value>. Automations should match these
    machine values (e.g. ``delivered``), not the translated labels.
    """
    if value is None:
        return None
    normalized = value.lower()
    if normalized == "unknown":
        return None
    if normalized == "cancelled":
        normalized = "canceled"
    return normalized


def ha_enum_options(enum_key: str) -> list[str]:
    """Return sorted lowercase options for a homeassistant enum sensor."""
    return sorted({_ha_enum(key) for key in ENUMS[enum_key] if _ha_enum(key)})


def _strip_html(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"<[^>]+>", "", text).strip()


class GlovoApiError(RuntimeError):
    def __init__(self, status: int, body: Any):
        self.status = status
        # Keep response material private and out of repr/logging. Callers classify
        # solely by the allowlisted HTTP status and never need the provider body.
        del body
        super().__init__(f"Glovo API error {status}")


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    payload_b64 = token.split(".")[1]
    padding = "=" * (-len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(payload_b64 + padding))


def _access_token_expires_at(access_token: str) -> int:
    return int(_decode_jwt_payload(access_token)["exp"])


def parse_token_json(token_json: str) -> dict[str, Any]:
    """Parse token state from a JSON string. Empty/whitespace -> {}."""
    if not token_json or not token_json.strip():
        return {}
    data = json.loads(token_json)
    if not isinstance(data, dict):
        raise RuntimeError("Invalid token JSON: root must be an object")
    return data


def dump_token_json(state: dict[str, Any]) -> str:
    """Serialize token state to a JSON string."""
    return json.dumps(state, ensure_ascii=False, indent=2) + "\n"


def _load_token_file(token_file: Path) -> str:
    if not token_file.exists():
        return dump_token_json({})
    return token_file.read_text(encoding="utf-8")


def _save_token_file(token_file: Path, token_json: str) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(token_json, encoding="utf-8")


def _request_json(
    method: str,
    url: str,
    *,
    access_token: str | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if access_token:
        headers["Authorization"] = access_token

    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError:
            payload = raw
        raise GlovoApiError(exc.code, payload) from exc


def single_attempt_authed_get(
    method: str,
    access_token: str,
    path: str,
    query: dict[str, str],
) -> Any:
    """Perform exactly one authenticated GET for the serialized session.

    Path/query allowlisting belongs to ``SerializedApiSession``. This narrow
    transport seam adds no refresh, retry, fallback, or mutation behavior.
    """
    if method != "GET":
        raise RuntimeError("Only GET is available through this request seam")
    if not isinstance(path, str) or not path.startswith("/"):
        raise RuntimeError("Invalid API path")
    url = f"{API_URL}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    return _request_json("GET", url, access_token=access_token)


def refresh_access_token(refresh_token: str) -> tuple[str, str, int]:
    data = _request_json(
        "POST",
        f"{API_URL}/oauth/refresh",
        body={"refreshToken": refresh_token},
    )
    access_token = data.get("accessToken")
    new_refresh_token = data.get("refreshToken")
    if not access_token or not new_refresh_token:
        raise RuntimeError("Unexpected refresh response")
    return access_token, new_refresh_token, _access_token_expires_at(access_token)


def build_token_json(refresh_token: str) -> str:
    """Build a fresh token JSON string from a bare refresh token.

    Performs a refresh against the API, so it validates the refresh token and
    returns a complete state ({"access_token", "refresh_token", "expires_at"}).
    Raises GlovoApiError / RuntimeError when the refresh token is invalid.
    """
    access_token, new_refresh_token, expires_at = refresh_access_token(refresh_token)
    return dump_token_json(
        {
            "access_token": access_token,
            "refresh_token": new_refresh_token,
            "expires_at": expires_at,
        }
    )


def ensure_access_token(
    token_json: str,
    *,
    refresh_margin_sec: int = DEFAULT_REFRESH_MARGIN_SEC,
    force: bool = False,
) -> tuple[str, str]:
    """
    Return a valid access token and the (possibly updated) token JSON string.

    Token JSON shape:
      {"access_token": "...", "refresh_token": "...", "expires_at": 1780851244}

    If the stored access token is missing or will expire within refresh_margin_sec,
    request a new one via POST /oauth/refresh and embed it in the returned JSON.
    """
    state = parse_token_json(token_json)
    now = int(time.time())

    if not force:
        access_token = state.get("access_token")
        expires_at = state.get("expires_at")
        if isinstance(access_token, str) and isinstance(expires_at, int):
            if expires_at - now > refresh_margin_sec:
                return access_token, dump_token_json(state)

    refresh_token = state.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise RuntimeError(
            "Refresh token is required in token JSON under key 'refresh_token'"
        )

    access_token, new_refresh_token, new_expires_at = refresh_access_token(refresh_token)
    state = {
        "access_token": access_token,
        "refresh_token": new_refresh_token,
        "expires_at": new_expires_at,
    }
    return access_token, dump_token_json(state)


def _authed_get(
    token_json: str,
    path: str,
    *,
    query: dict[str, str] | None = None,
    refresh_margin_sec: int = DEFAULT_REFRESH_MARGIN_SEC,
) -> tuple[Any, str]:
    access_token, token_json = ensure_access_token(
        token_json,
        refresh_margin_sec=refresh_margin_sec,
    )
    url = f"{API_URL}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    return _request_json("GET", url, access_token=access_token), token_json


def get_orders_list(
    token_json: str,
    *,
    offset: int = 0,
    limit: int = 20,
    refresh_margin_sec: int = DEFAULT_REFRESH_MARGIN_SEC,
) -> tuple[dict[str, Any], str]:
    return _authed_get(
        token_json,
        "/v3/customer/orders-list",
        query={"offset": str(offset), "limit": str(limit)},
        refresh_margin_sec=refresh_margin_sec,
    )


def get_order(
    token_json: str,
    order_id: int | str,
    *,
    refresh_margin_sec: int = DEFAULT_REFRESH_MARGIN_SEC,
) -> tuple[dict[str, Any], str]:
    return _authed_get(
        token_json,
        f"/v3/customer/orders/{order_id}",
        refresh_margin_sec=refresh_margin_sec,
    )


def get_order_flow(
    token_json: str,
    order_id: int | str,
    *,
    refresh_margin_sec: int = DEFAULT_REFRESH_MARGIN_SEC,
) -> tuple[dict[str, Any], str]:
    """
    Legacy live status UI: progress steps, animation, ETA text.
    Poll every response['secondsToNextRequest'] seconds while order is active.
    """
    return _authed_get(
        token_json,
        f"/v3/customer/orders/{order_id}/flow",
        refresh_margin_sec=refresh_margin_sec,
    )


def get_order_tracking(
    token_json: str,
    order_id: int | str,
    *,
    origin: TrackingOrigin = "ORDER_TRACKING",
    initial: bool = True,
    refresh_margin_sec: int = DEFAULT_REFRESH_MARGIN_SEC,
) -> tuple[dict[str, Any], str]:
    """
    Live map tracking: courier marker, timeline, ETA, progress.
    Poll every response['data']['pollingIntervalMillis'] ms while step is IN_PROGRESS.
    """
    return _authed_get(
        token_json,
        f"/v1/customer/orders/{order_id}/tracking/summary",
        query={"origin": origin, "initial": str(initial).lower()},
        refresh_margin_sec=refresh_margin_sec,
    )


def poll_order_tracking(
    token_json: str,
    order_id: int | str,
    *,
    origin: TrackingOrigin = "ORDER_TRACKING",
    on_update: Callable[[dict[str, Any], str], None] | None = None,
    refresh_margin_sec: int = DEFAULT_REFRESH_MARGIN_SEC,
) -> tuple[dict[str, Any], str]:
    """
    Poll tracking/summary until the order is delivered or canceled.
    Calls on_update(data, token_json) after each successful response when provided.
    """
    data, token_json = get_order_tracking(
        token_json,
        order_id,
        origin=origin,
        initial=True,
        refresh_margin_sec=refresh_margin_sec,
    )
    if on_update:
        on_update(data, token_json)

    while data.get("step") not in TERMINAL_TRACKING_STEPS:
        interval_ms = (data.get("data") or {}).get("pollingIntervalMillis", 10_000)
        time.sleep(max(interval_ms, 1000) / 1000)
        data, token_json = get_order_tracking(
            token_json,
            order_id,
            origin=origin,
            initial=False,
            refresh_margin_sec=refresh_margin_sec,
        )
        if on_update:
            on_update(data, token_json)

    return data, token_json


def poll_order_flow(
    token_json: str,
    order_id: int | str,
    *,
    on_update: Callable[[dict[str, Any], str], None] | None = None,
    refresh_margin_sec: int = DEFAULT_REFRESH_MARGIN_SEC,
) -> tuple[dict[str, Any], str]:
    """
    Poll /flow until page becomes DELIVERED or CANCELLED.
    Calls on_update(data, token_json) after each successful response when provided.
    """
    data, token_json = get_order_flow(
        token_json,
        order_id,
        refresh_margin_sec=refresh_margin_sec,
    )
    if on_update:
        on_update(data, token_json)

    terminal_pages = {"DELIVERED", "CANCELLED", "CANCELED"}
    while data.get("page") not in terminal_pages:
        interval_sec = data.get("secondsToNextRequest", 30)
        time.sleep(max(interval_sec, 1))
        data, token_json = get_order_flow(
            token_json,
            order_id,
            refresh_margin_sec=refresh_margin_sec,
        )
        if on_update:
            on_update(data, token_json)

    return data, token_json


def humanize_orders_list(data: dict[str, Any]) -> str:
    rows = data.get("rows") or []
    out: list[str] = [f"Orders list: {len(rows)} item(s)"]
    for row in rows:
        d = row.get("data") or {}
        title = ((d.get("content") or {}).get("title")) or d.get("orderId")
        footer = ((d.get("footer") or {}).get("left") or {}).get("data")
        layout = d.get("layoutType")
        line = f"  - #{d.get('orderId')}: {title}"
        if footer:
            line += f" | {footer}"
        if layout:
            line += f" | {layout}"
        if d.get("courierName"):
            line += f" | courier: {d['courierName']}"
        out.append(line)
    next_offset = ((data.get("pagination") or {}).get("next") or {}).get("offset")
    if next_offset:
        out.append(f"  next offset: {next_offset}")
    return "\n".join(out)


def humanize_order(data: dict[str, Any]) -> str:
    out: list[str] = [
        f"Order #{data.get('id')} ({data.get('code')})",
        f"  store: {data.get('storeName')}",
        f"  summary: {_strip_html(data.get('shortSummary'))}",
        f"  status: {describe('order.status', (data.get('currentStatus') or {}).get('type'))}",
    ]
    if data.get("subtype"):
        out.append(f"  subtype: {describe('order.subtype', data.get('subtype'))}")
    if data.get("origin"):
        out.append(f"  origin: {describe('order.origin', data.get('origin'))}")
    handling = (data.get("handlingStrategy") or {}).get("type")
    if handling:
        out.append(f"  handling: {describe('order.handling', handling)}")

    courier = data.get("courier")
    if courier:
        name = courier.get("name") or "—"
        raw_phone = courier.get("phoneNumber")
        if isinstance(raw_phone, dict):
            phone = raw_phone.get("number") or "—"
        else:
            phone = raw_phone or "—"
        out.append(f"  courier: {name}, phone: {phone}")
    else:
        out.append("  courier: not assigned / not exposed yet")

    for point in data.get("points") or []:
        addr = (point.get("address") or {}).get("label")
        out.append(f"  point {describe('order.point', point.get('type'))}: {addr}")

    for line in (data.get("pricingBreakdown") or {}).get("lines") or []:
        out.append(
            f"  price [{describe('order.priceLine', line.get('type'))}]: "
            f"{line.get('description')} = {line.get('amount')}"
        )
    return "\n".join(out)


def humanize_order_flow(data: dict[str, Any]) -> str:
    out: list[str] = [f"Flow page: {describe('flow.page', data.get('page'))}"]
    status = data.get("statusData") or {}
    if status.get("body"):
        out.append(f"  status: {_strip_html(status.get('body'))}")
    if status.get("subtitle"):
        out.append(f"  eta: {status.get('subtitle')}")
    if status.get("etaNotice"):
        out.append(f"  eta notice: {status.get('etaNotice')}")

    steps = (status.get("progressData") or {}).get("steps") or []
    for step in steps:
        out.append(
            f"  step '{step.get('label')}': "
            f"{describe('flow.stepStatus', step.get('status'))}"
        )

    courier_loc = ((data.get("mapData") or {}).get("courierData") or {}).get("location")
    if courier_loc:
        out.append(
            f"  courier position: {courier_loc.get('latitude')}, {courier_loc.get('longitude')}"
        )

    nxt = data.get("secondsToNextRequest")
    if nxt is not None:
        out.append(f"  poll again in: {nxt}s")
    return "\n".join(out)


def humanize_order_tracking(data: dict[str, Any]) -> str:
    out: list[str] = [f"Tracking step: {describe('tracking.step', data.get('step'))}"]
    payload = data.get("data") or {}

    eta = payload.get("eta") or {}
    if eta.get("text"):
        former = f" (was {eta['formerEta']})" if eta.get("formerEta") else ""
        out.append(f"  eta: {eta['text']}{former}")

    if payload.get("progress") is not None:
        out.append(f"  progress: {round(float(payload['progress']) * 100)}%")

    tag = payload.get("statusTag") or {}
    if tag.get("id"):
        out.append(f"  status tag: {describe('tracking.statusTag', tag.get('id'))}")

    timeline = payload.get("timeline") or {}
    partner = timeline.get("partner") or {}
    if partner.get("text"):
        out.append(f"  store: {partner['text']}")
    for courier in timeline.get("couriers") or []:
        chat = "chat available" if courier.get("chatAvailable") else "no chat"
        out.append(f"  courier: {courier.get('text')} [{chat}]")

    for marker in payload.get("markers") or []:
        if marker.get("type") == "COURIER":
            heading = marker.get("heading")
            heading_str = f", heading {round(heading)}°" if heading is not None else ""
            out.append(
                f"  courier position: {marker.get('latitude')}, "
                f"{marker.get('longitude')}{heading_str}"
            )

    # Analytics block carries the raw backend statuses.
    event = (payload.get("event") or {}).get("data") or {}
    if event.get("partnerStatus"):
        out.append(f"  partnerStatus: {describe('tracking.partnerStatus', event['partnerStatus'])}")
    if event.get("courierStatus"):
        out.append(f"  courierStatus: {describe('tracking.courierStatus', event['courierStatus'])}")
    if event.get("orderLatenessStatus"):
        out.append(
            f"  lateness: {describe('tracking.statusTag', event['orderLatenessStatus'])}"
        )
    if event.get("orderStatusPage"):
        out.append(f"  page: {describe('tracking.statusPage', event['orderStatusPage'])}")

    interval = payload.get("pollingIntervalMillis")
    if interval is not None:
        out.append(f"  poll again in: {interval / 1000:.0f}s")
    return "\n".join(out)


def humanize(command: str, data: Any) -> str:
    """Render a human-readable summary for a given command's response."""
    # "active" always returns a dict; treat missing/empty as no active order.
    if command == "active":
        return humanize_active_order(data)
    if not isinstance(data, dict):
        return str(data)
    humanizers: dict[str, Callable[[dict[str, Any]], str]] = {
        "orders-list": humanize_orders_list,
        "order": humanize_order,
        "order-flow": humanize_order_flow,
        "order-tracking": humanize_order_tracking,
        "active": humanize_active_order,
    }
    fn = humanizers.get(command)
    return fn(data) if fn else json.dumps(data, ensure_ascii=False, indent=2)


# --- Last active order summary (Home Assistant friendly) -------------------

def find_active_order_ids(orders_list: dict[str, Any]) -> list[int]:
    """Return orderIds of all ACTIVE_ORDER rows in list order."""
    ids: list[int] = []
    for row in orders_list.get("rows") or []:
        d = row.get("data") or {}
        if d.get("layoutType") == "ACTIVE_ORDER":
            order_id = d.get("orderId")
            if order_id is not None:
                ids.append(order_id)
    return ids


def find_trackable_order_ids(
    orders_list: dict[str, Any],
    token_json: str,
    *,
    refresh_margin_sec: int = DEFAULT_REFRESH_MARGIN_SEC,
    max_probes: int = 5,
) -> tuple[list[int], str]:
    """Return orderIds that should be surfaced in Home Assistant.

    Includes all ACTIVE_ORDER rows. When none exist, probes recent orders-list
    rows via tracking/summary — scheduled orders appear as INACTIVE_ORDER in the
    list but report step=SCHEDULED. Stops probing once a terminal step is seen
    (delivered/canceled history below).
    """
    ids = find_active_order_ids(orders_list)
    if ids:
        return ids, token_json

    probes = 0
    for row in orders_list.get("rows") or []:
        if probes >= max_probes:
            break
        order_id = (row.get("data") or {}).get("orderId")
        if order_id is None:
            continue
        probes += 1
        try:
            tracking, token_json = get_order_tracking(
                token_json, order_id, refresh_margin_sec=refresh_margin_sec
            )
        except GlovoApiError:
            continue
        step = tracking.get("step")
        if step in TRACKABLE_STEPS:
            ids.append(order_id)
        elif step in TERMINAL_TRACKING_STEPS:
            break
    return ids, token_json


def find_active_order_id(orders_list: dict[str, Any]) -> int | None:
    """Return the orderId of the first ACTIVE_ORDER row, or None."""
    ids = find_active_order_ids(orders_list)
    return ids[0] if ids else None


def empty_active_order_summary() -> dict[str, Any]:
    """Summary shape when there are no active orders (order_count=0, rest None)."""
    return {
        "order_count": 0,
        "order_id": None,
        "store_name": None,
        "store_lat": None,
        "store_lon": None,
        "overall_status": None,
        "overall_status_text": None,
        "step": None,
        "step_text": None,
        "active": None,
        "delivered": None,
        "canceled": None,
        "courier_name": None,
        "courier_phone": None,
        "courier_status": None,
        "courier_status_text": None,
        "courier_message": None,
        "chat_available": None,
        "courier_count": None,
        "partner_status": None,
        "partner_status_text": None,
        "partner_message": None,
        "progress_percent": None,
        "is_late": None,
        "lateness": None,
        "lateness_text": None,
        "eta_format": None,
        "eta_min": None,
        "eta_max": None,
        "eta_text": None,
        "eta_former": None,
        "original_eta": None,
        "courier_lat": None,
        "courier_lon": None,
        "courier_heading": None,
        "poll_interval_sec": None,
    }


def _courier_name_from_message(message: str | None) -> str | None:
    # Timeline texts look like "Jeen is on the way with your order".
    if not message:
        return None
    m = re.match(r"^(.*?)\s+is\s", message)
    return m.group(1).strip() if m else None


def _courier_markers(markers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in markers if m.get("type") == "COURIER"]


def _finite_coordinate(value: Any, *, lower: float, upper: float) -> float | None:
    """Return a finite, in-range JSON number, rejecting booleans and coercion."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        coordinate = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(coordinate) or not lower <= coordinate <= upper:
        return None
    return coordinate


def _pickup_coordinates(
    markers: list[dict[str, Any]],
) -> tuple[float | None, float | None]:
    """Return the sole valid PICK_UP position, or a null pair when ambiguous."""
    pickup_markers = [marker for marker in markers if marker.get("type") == "PICK_UP"]
    if len(pickup_markers) != 1:
        return None, None

    marker = pickup_markers[0]
    latitude = _finite_coordinate(marker.get("latitude"), lower=-90, upper=90)
    longitude = _finite_coordinate(marker.get("longitude"), lower=-180, upper=180)
    if latitude is None or longitude is None:
        return None, None
    return latitude, longitude


def _drop_off_marker(markers: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((m for m in markers if m.get("type") == "DROP_OFF"), None)


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_m = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * radius_m * math.asin(math.sqrt(a))


def _marker_distance_to_drop_off(
    marker: dict[str, Any],
    drop_off: dict[str, Any] | None,
) -> float:
    if drop_off is None:
        return float("inf")
    lat1, lon1 = marker.get("latitude"), marker.get("longitude")
    lat2, lon2 = drop_off.get("latitude"), drop_off.get("longitude")
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return float("inf")
    return _haversine_m(float(lat1), float(lon1), float(lat2), float(lon2))


def _pick_nearest_courier_marker(
    markers: list[dict[str, Any]],
) -> dict[str, Any] | None:
    courier_markers = _courier_markers(markers)
    if not courier_markers:
        return None
    drop_off = _drop_off_marker(markers)
    return min(
        courier_markers,
        key=lambda marker: _marker_distance_to_drop_off(marker, drop_off),
    )


def _courier_timeline_priority(text: str | None) -> int:
    """Lower value = further along delivery (arriving beats waiting at store)."""
    if not text:
        return 99
    lower = text.lower()
    if "arriving" in lower:
        return 0
    if "on the way" in lower:
        return 1
    if "waiting" in lower:
        return 2
    if "assigned" in lower or "heading" in lower:
        return 3
    return 4


def _select_primary_courier_timeline(
    couriers: list[dict[str, Any]],
) -> dict[str, Any]:
    if not couriers:
        return {}
    if len(couriers) == 1:
        return couriers[0]
    return min(
        couriers,
        key=lambda courier: _courier_timeline_priority(courier.get("text")),
    )


def _courier_count(
    timeline_couriers: list[dict[str, Any]],
    courier_markers: list[dict[str, Any]],
) -> int:
    return max(len(timeline_couriers), len(courier_markers))


def _resolve_now(now: datetime | None) -> datetime:
    """Normalize now to a timezone-aware datetime for ETA math."""
    if now is None:
        return datetime.now().astimezone()
    if now.tzinfo is None:
        return now.astimezone()
    return now


def _ms_to_local_datetime(
    value: int | float | str | None, *, now: datetime
) -> datetime | None:
    """Convert a Glovo epoch-millis field to a datetime in ``now``'s timezone."""
    if value is None:
        return None
    try:
        utc = datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
        return utc.astimezone(_resolve_now(now).tzinfo)
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _format_local_clock(dt: datetime) -> str:
    """Format a datetime as a local wall-clock HH:MM string."""
    return dt.strftime("%H:%M")


def _minutes_until(target: datetime, now: datetime) -> int:
    """Return rounded minutes from now until target (may be negative)."""
    return round((target - now).total_seconds() / 60)


def _parse_scheduled_eta(
    info: dict[str, Any] | None, now: datetime | None = None
) -> dict[str, Any]:
    """Build ETA fields from v3 order details scheduledTime / scheduledTimeEnd.

    Used when tracking/summary has no live ETA yet (typical for step=SCHEDULED).
    """
    empty: dict[str, Any] = {
        "format": None,
        "min": None,
        "max": None,
        "text": None,
        "former": None,
    }
    if not info:
        return empty

    resolved_now = _resolve_now(now)
    start = _ms_to_local_datetime(info.get("scheduledTime"), now=resolved_now)
    if start is None:
        return empty

    end = _ms_to_local_datetime(info.get("scheduledTimeEnd"), now=resolved_now) or start
    if end < start:
        start, end = end, start

    low_clock = _format_local_clock(start)
    high_clock = _format_local_clock(end)
    text = (
        f"{low_clock} - {high_clock}"
        if low_clock != high_clock
        else low_clock
    )

    return {
        "format": "timestamp",
        "min": _minutes_until(start, resolved_now),
        "max": _minutes_until(end, resolved_now),
        "text": text,
        "former": None,
    }


def _clock_to_minutes_left(clock: str | None, now: datetime) -> int | None:
    """Convert a local "HH:MM" wall-clock time into minutes left from now.

    Clock times are interpreted in the local timezone. A bound that already
    passed yields a negative value (delivery running late); a value far in the
    past is treated as belonging to the next day (handles midnight wrap).
    """
    if not clock:
        return None
    match = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", clock)
    if not match:
        return None
    target = now.replace(
        hour=int(match.group(1)),
        minute=int(match.group(2)),
        second=0,
        microsecond=0,
    )
    minutes = round((target - now).total_seconds() / 60)
    if minutes < -360:
        minutes += 24 * 60
    return minutes


def _parse_eta(tracking: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Normalize ETA into {format, min, max, text, former}.

    `min`/`max` are always integer minutes left until arrival:
      * timestamp format -> minutes left to the lower / upper clock-time bound
        (e.g. "16:05 - 16:25" => min=minutes to 16:05, max=minutes to 16:25);
      * countdown format -> min == max == the reported minutes value.
    Clock-time bounds are computed against the timezone of ``now``. The Home
    Assistant integration passes ``homeassistant.util.dt.now(hass.config.time_zone)``.
    """
    now = _resolve_now(now)

    payload = tracking.get("data") or {}
    eta = payload.get("eta") or {}
    event = (payload.get("event") or {}).get("data") or {}

    eta_format = event.get("estimatedTimeOfArrivalFormat")
    text = eta.get("text")
    former = eta.get("formerEta")
    eta_min: int | None = None
    eta_max: int | None = None

    low_clock: str | None = None
    high_clock: str | None = None
    countdown_value: str | None = None

    if eta_format == "timestamp":
        low_clock = event.get("estimatedTimeOfArrivalRangeLower")
        high_clock = event.get("estimatedTimeOfArrivalRangeUpper")
    elif eta_format == "countdown":
        countdown_value = event.get("estimatedTimeOfArrival")
    else:
        # Fall back to parsing eta.text when the analytics block is absent.
        if text and " - " in text:
            eta_format = "timestamp"
            low, _, high = text.partition(" - ")
            low_clock, high_clock = low.strip(), high.strip()
        elif text:
            m = re.search(r"(\d+)\s+minute", text)
            if m:
                eta_format = "countdown"
                countdown_value = m.group(1)

    if eta_format == "timestamp":
        eta_min = _clock_to_minutes_left(low_clock, now)
        eta_max = _clock_to_minutes_left(high_clock, now)
    elif eta_format == "countdown":
        if countdown_value is not None and str(countdown_value).isdigit():
            eta_min = eta_max = int(countdown_value)

    return {
        "format": eta_format,
        "min": eta_min,
        "max": eta_max,
        "text": text,
        "former": former,
    }


_COURIER_STATUS_PRIORITY = {
    "ARRIVING": 0,
    "ON_THE_WAY": 1,
    "WAITING": 2,
    "ASSIGNED": 3,
}


def _pick_primary_courier_status(raw: str | None) -> str | None:
    """Extract the most advanced courier status from a possibly comma-separated value.

    When a split delivery is in progress, Glovo returns e.g. "ARRIVING,ON_THE_WAY".
    We pick the one closest to arrival (lowest priority number).
    """
    if not raw:
        return None
    parts = [s.strip() for s in raw.split(",") if s.strip()]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return min(parts, key=lambda s: _COURIER_STATUS_PRIORITY.get(s, 99))


def compute_overall_status(
    step: str | None,
    partner_status: str | None,
    courier_status: str | None,
) -> str:
    """Collapse step + partnerStatus + courierStatus into one custom status.

    See ENUMS["overall.status"] for the meaning of each returned value.
    ``courier_status`` may be comma-separated for split deliveries; the most
    advanced individual status is used.
    """
    if step == "DELIVERED":
        return "DELIVERED"
    if step in ("CANCELED", "CANCELLED"):
        return "CANCELED"
    if step == "SCHEDULED":
        return "SCHEDULED"
    if step != "IN_PROGRESS":
        return "UNKNOWN"

    primary = _pick_primary_courier_status(courier_status)

    # Once the courier has left the store its movement drives the status.
    if primary == "ARRIVING":
        return "ARRIVING"
    if primary == "ON_THE_WAY":
        return "ON_THE_WAY"

    # Before pickup: courier may be at the store or still on the way.
    if primary == "WAITING":
        return "AWAITING_PICKUP" if partner_status == "READY" else "COURIER_WAITING"

    if partner_status == "READY":
        return "AWAITING_PICKUP"
    if primary == "ASSIGNED":
        return "COURIER_ASSIGNED"
    return "PREPARING"


def summarize_active_order(
    tracking: dict[str, Any],
    *,
    order_id: int | str | None = None,
    info: dict[str, Any] | None = None,
    list_row: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a flat, Home-Assistant-friendly summary from a tracking response.

    `info` (v3 order details) and `list_row` (an orders-list row's `data`)
    are optional and only enrich store name / courier name / phone.
    """
    payload = tracking.get("data") or {}
    step = tracking.get("step")
    timeline = payload.get("timeline") or {}
    couriers = timeline.get("couriers") or []
    markers = payload.get("markers") or []
    courier_markers = _courier_markers(markers)
    courier_count = _courier_count(couriers, courier_markers)
    primary_courier = (
        _select_primary_courier_timeline(couriers) if courier_count > 1 else (couriers[0] if couriers else {})
    )
    partner = timeline.get("partner") or {}
    tag = payload.get("statusTag") or {}
    event = (payload.get("event") or {}).get("data") or {}

    courier_message = primary_courier.get("text")
    courier_name = None
    courier_phone = None
    if info and courier_count <= 1:
        courier_obj = info.get("courier") or {}
        courier_name = courier_obj.get("name")
        phone = courier_obj.get("phoneNumber")
        if isinstance(phone, dict):
            courier_phone = phone.get("number")
        elif isinstance(phone, str):
            courier_phone = phone
    if not courier_name:
        courier_name = _courier_name_from_message(courier_message)

    store_name = None
    if info:
        store_name = info.get("storeName")
    if not store_name and list_row:
        store_name = (list_row.get("content") or {}).get("title")

    if order_id is None:
        order_id = payload.get("orderId") or (info or {}).get("id")

    lateness = event.get("orderLatenessStatus") or tag.get("id")
    progress = payload.get("progress")
    resolved_now = _resolve_now(now)
    eta = _parse_eta(tracking, now=resolved_now)
    if eta["min"] is None and eta["max"] is None:
        scheduled_eta = _parse_scheduled_eta(info, now=resolved_now)
        if scheduled_eta["min"] is not None or scheduled_eta["max"] is not None:
            eta = scheduled_eta

    overall = compute_overall_status(
        step, event.get("partnerStatus"), event.get("courierStatus")
    )

    courier_marker = _pick_nearest_courier_marker(markers)
    store_lat, store_lon = _pickup_coordinates(markers)

    interval_ms = payload.get("pollingIntervalMillis")

    return {
        "order_id": order_id,
        "store_name": store_name,
        "store_lat": store_lat,
        "store_lon": store_lon,
        "overall_status": _ha_enum(overall),
        "overall_status_text": ENUMS["overall.status"].get(overall),
        "step": _ha_enum(step),
        "step_text": ENUMS["tracking.step"].get(step)
        or (ENUMS["tracking.step"].get("CANCELED") if step == "CANCELLED" else None),
        "active": step == "IN_PROGRESS",
        "delivered": step == "DELIVERED",
        "canceled": step in ("CANCELED", "CANCELLED"),
        "courier_name": courier_name,
        "courier_phone": courier_phone,
        "courier_status": _ha_enum(_pick_primary_courier_status(event.get("courierStatus"))),
        "courier_status_text": ENUMS["tracking.courierStatus"].get(
            _pick_primary_courier_status(event.get("courierStatus"))
        ),
        "courier_message": courier_message,
        "chat_available": primary_courier.get("chatAvailable"),
        "courier_count": courier_count,
        "partner_status": _ha_enum(event.get("partnerStatus")),
        "partner_status_text": ENUMS["tracking.partnerStatus"].get(
            event.get("partnerStatus")
        ),
        "partner_message": partner.get("text"),
        "progress_percent": round(float(progress) * 100) if progress is not None else None,
        "is_late": lateness == "LATE",
        "lateness": lateness,
        "lateness_text": ENUMS["tracking.statusTag"].get(lateness),
        "eta_format": eta["format"],
        "eta_min": max(eta["min"], 0) if eta["min"] is not None else None,
        "eta_max": max(eta["max"], 0) if eta["max"] is not None else None,
        "eta_text": eta["text"],
        "eta_former": eta["former"],
        "original_eta": eta["former"] or eta["text"],
        "courier_lat": (courier_marker or {}).get("latitude"),
        "courier_lon": (courier_marker or {}).get("longitude"),
        "courier_heading": (courier_marker or {}).get("heading"),
        "poll_interval_sec": interval_ms / 1000 if interval_ms is not None else None,
    }


def _list_row_for(orders: dict[str, Any] | None, order_id: int | str) -> dict[str, Any] | None:
    """Return the orders-list `data` row for order_id, if present."""
    for row in (orders or {}).get("rows") or []:
        if (row.get("data") or {}).get("orderId") == order_id:
            return row.get("data")
    return None


def get_order_summary(
    token_json: str,
    order_id: int | str,
    *,
    orders: dict[str, Any] | None = None,
    include_details: bool = True,
    refresh_margin_sec: int = DEFAULT_REFRESH_MARGIN_SEC,
    now: datetime | None = None,
) -> tuple[dict[str, Any], str]:
    """Build a summary for a specific order id, even if it is no longer active.

    The tracking endpoint keeps returning a DELIVERED/CANCELED step after the
    order leaves the active orders list, so this can be used to surface a final
    status for a short grace period. `orders` may be passed to reuse an
    orders-list response for store-name enrichment.

    Returns (summary, updated_token_json).
    """
    list_row = _list_row_for(orders, order_id)

    tracking, token_json = get_order_tracking(
        token_json, order_id, refresh_margin_sec=refresh_margin_sec
    )

    info = None
    if include_details:
        try:
            info, token_json = get_order(
                token_json, order_id, refresh_margin_sec=refresh_margin_sec
            )
        except GlovoApiError:
            info = None

    summary = summarize_active_order(
        tracking,
        order_id=order_id,
        info=info,
        list_row=list_row,
        now=now,
    )
    return summary, token_json


def _pick_nearest_order(
    order_ids: list[int],
    orders: dict[str, Any],
    token_json: str,
    *,
    include_details: bool = True,
    refresh_margin_sec: int = DEFAULT_REFRESH_MARGIN_SEC,
    now: datetime | None = None,
) -> tuple[int | str, str]:
    """Among multiple trackable orders, return the one with the smallest eta_min.

    Fetches tracking (and optionally info) for each order to compute ETA.
    Falls back to the first order if ETA cannot be determined for any.
    """
    best_id = order_ids[0]
    best_eta: int | None = None

    for oid in order_ids:
        try:
            tracking, token_json = get_order_tracking(
                token_json, oid, refresh_margin_sec=refresh_margin_sec
            )
        except GlovoApiError:
            continue

        info = None
        if include_details:
            try:
                info, token_json = get_order(
                    token_json, oid, refresh_margin_sec=refresh_margin_sec
                )
            except GlovoApiError:
                pass

        resolved_now = _resolve_now(now)
        eta = _parse_eta(tracking, now=resolved_now)
        if eta["min"] is None:
            scheduled_eta = _parse_scheduled_eta(info, now=resolved_now)
            if scheduled_eta["min"] is not None:
                eta = scheduled_eta

        if eta["min"] is not None:
            if best_eta is None or eta["min"] < best_eta:
                best_eta = eta["min"]
                best_id = oid

    return best_id, token_json


def get_last_active_order_summary(
    token_json: str,
    *,
    fallback_order_id: int | str | None = None,
    include_details: bool = True,
    refresh_margin_sec: int = DEFAULT_REFRESH_MARGIN_SEC,
    now: datetime | None = None,
) -> tuple[dict[str, Any], str]:
    """Return a flat summary of an active order, or an empty summary if none.

    Makes up to 3 API calls: orders-list (find active orders), tracking/summary
    (live state) and, when include_details is True, the v3 order details (for a
    clean courier name and phone). Designed to be polled by Home Assistant.

    When no orders are trackable, returns empty_active_order_summary() with
    order_count=0. Trackable orders are ACTIVE_ORDER rows and scheduled orders
    (INACTIVE_ORDER in the list, step=SCHEDULED in tracking). When several are
    trackable, order_count is the total and the remaining fields describe the
    first one.

    If no order is active but `fallback_order_id` is given, the summary is built
    from that order's tracking instead (order_count=0). This lets callers keep
    showing a just-delivered/canceled order for a grace period after it drops
    out of the active list. On any API error for the fallback order, an empty
    summary is returned.

    Returns (summary, updated_token_json).
    """
    orders, token_json = get_orders_list(
        token_json, refresh_margin_sec=refresh_margin_sec
    )
    active_order_ids, token_json = find_trackable_order_ids(
        orders, token_json, refresh_margin_sec=refresh_margin_sec
    )
    order_count = len(active_order_ids)

    if order_count == 0:
        if fallback_order_id is not None:
            try:
                summary, token_json = get_order_summary(
                    token_json,
                    fallback_order_id,
                    orders=orders,
                    include_details=include_details,
                    refresh_margin_sec=refresh_margin_sec,
                    now=now,
                )
            except GlovoApiError:
                return empty_active_order_summary(), token_json
            summary["order_count"] = 0
            return summary, token_json
        return empty_active_order_summary(), token_json

    # When multiple orders are trackable, pick the one closest to arrival.
    if len(active_order_ids) == 1:
        order_id = active_order_ids[0]
    else:
        order_id, token_json = _pick_nearest_order(
            active_order_ids, orders, token_json,
            include_details=include_details,
            refresh_margin_sec=refresh_margin_sec,
            now=now,
        )

    list_row = _list_row_for(orders, order_id)

    tracking, token_json = get_order_tracking(
        token_json, order_id, refresh_margin_sec=refresh_margin_sec
    )

    info = None
    if include_details:
        try:
            info, token_json = get_order(
                token_json, order_id, refresh_margin_sec=refresh_margin_sec
            )
        except GlovoApiError:
            info = None

    summary = summarize_active_order(
        tracking,
        order_id=order_id,
        info=info,
        list_row=list_row,
        now=now,
    )
    summary["order_count"] = order_count
    return summary, token_json


# --- Local fixture files (temporary dev / offline testing) ------------------

FIXTURE_FILES: tuple[str, ...] = (
    "orders-list.json",
    "order-flow.json",
    "order-info.json",
    "order-tracking.json",
)


def fixtures_available(fixtures_dir: str | Path) -> bool:
    """Return True when every expected fixture file is present."""
    root = Path(fixtures_dir)
    return all((root / name).is_file() for name in FIXTURE_FILES)


def _load_fixture(fixtures_dir: Path, name: str) -> dict[str, Any]:
    with (fixtures_dir / name).open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise RuntimeError(f"Fixture {name} must be a JSON object")
    return data


def get_last_active_order_summary_from_fixtures(
    fixtures_dir: str | Path,
    *,
    fallback_order_id: int | str | None = None,
    include_details: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build an order summary from local JSON dumps instead of the API.

    Expects FIXTURE_FILES under ``fixtures_dir``. ``order-flow.json`` must
    exist but is not consumed here (kept for parity with real API dumps).

    Mirrors get_last_active_order_summary(): when no order is active but
    ``fallback_order_id`` is given, the summary is built from the tracking
    fixture (order_count=0) so a just-delivered order can still be surfaced.
    """
    root = Path(fixtures_dir)
    orders = _load_fixture(root, "orders-list.json")
    tracking = _load_fixture(root, "order-tracking.json")

    active_order_ids = find_active_order_ids(orders)
    order_count = len(active_order_ids)

    if order_count == 0:
        step = tracking.get("step")
        if step in TRACKABLE_STEPS:
            order_id = (tracking.get("data") or {}).get("orderId")
            if order_id is None:
                for row in orders.get("rows") or []:
                    order_id = (row.get("data") or {}).get("orderId")
                    if order_id is not None:
                        break
            if order_id is not None:
                active_order_ids = [order_id]
                order_count = 1

    if order_count == 0:
        if fallback_order_id is not None:
            info = _load_fixture(root, "order-info.json") if include_details else None
            summary = summarize_active_order(
                tracking,
                order_id=fallback_order_id,
                info=info,
                now=now,
            )
            summary["order_count"] = 0
            return summary
        return empty_active_order_summary()

    order_id = active_order_ids[0]
    list_row = _list_row_for(orders, order_id)

    info = _load_fixture(root, "order-info.json") if include_details else None
    summary = summarize_active_order(
        tracking,
        order_id=order_id,
        info=info,
        list_row=list_row,
        now=now,
    )
    summary["order_count"] = order_count
    return summary


def humanize_active_order(summary: dict[str, Any] | None) -> str:
    if not summary or summary.get("order_count") == 0:
        return "No active order."
    out: list[str] = [
        f"Active order #{summary.get('order_id')} from {summary.get('store_name') or '—'}",
    ]
    if summary.get("order_count", 1) > 1:
        out.append(f"  active orders total: {summary['order_count']} (first shown)")
    out.extend([
        f"  overall: {summary.get('overall_status')} "
        f"({summary.get('overall_status_text') or 'unknown'})",
        f"  stage: {summary.get('step')} "
        f"({summary.get('step_text') or 'unknown'})",
    ])
    if summary.get("courier_count", 0) > 1:
        out.append(f"  couriers total: {summary['courier_count']} (nearest shown below)")
    if summary.get("courier_name"):
        phone = summary.get("courier_phone")
        out.append(f"  courier: {summary['courier_name']}" + (f" ({phone})" if phone else ""))
    if summary.get("courier_status"):
        out.append(
            f"  courier status: {summary['courier_status']} "
            f"({summary.get('courier_status_text') or 'unknown'})"
        )
    if summary.get("partner_status"):
        out.append(
            f"  partner status: {summary['partner_status']} "
            f"({summary.get('partner_status_text') or 'unknown'})"
        )
    if summary.get("progress_percent") is not None:
        out.append(f"  progress: {summary['progress_percent']}%")

    eta_min = summary.get("eta_min")
    eta_max = summary.get("eta_max")
    if eta_min is not None and eta_max is not None:
        if eta_min == eta_max:
            out.append(f"  eta: {eta_min} min left")
        else:
            out.append(f"  eta: {eta_min}–{eta_max} min left")
    elif summary.get("eta_text"):
        out.append(f"  eta: {summary['eta_text']}")

    late = "yes" if summary.get("is_late") else "no"
    out.append(f"  late: {late} ({summary.get('lateness') or '—'})")
    if summary.get("eta_former"):
        out.append(f"  original eta: {summary['eta_former']}")
    if summary.get("courier_lat") is not None:
        out.append(
            f"  courier position: {summary['courier_lat']}, {summary['courier_lon']}"
        )
    return "\n".join(out)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Glovo customer API helper")
    parser.add_argument(
        "--token-file",
        default="glovo_token.json",
        help="Path to JSON file with cached access token state",
    )
    parser.add_argument(
        "--refresh-token",
        help="Glovo refresh token; if omitted, read from token file",
    )
    parser.add_argument(
        "--humanize",
        action="store_true",
        help="Print a human-readable summary instead of raw JSON",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    orders_list_parser = subparsers.add_parser("orders-list", help="Fetch orders list")
    orders_list_parser.add_argument("--offset", type=int, default=0)
    orders_list_parser.add_argument("--limit", type=int, default=12)

    order_parser = subparsers.add_parser("order", help="Fetch order details (v3)")
    order_parser.add_argument("order_id")

    order_flow_parser = subparsers.add_parser("order-flow", help="Fetch legacy order status (v3 /flow)")
    order_flow_parser.add_argument("order_id")
    order_flow_parser.add_argument(
        "--watch",
        action="store_true",
        help="Poll until DELIVERED or CANCELLED",
    )

    order_tracking_parser = subparsers.add_parser(
        "order-tracking",
        help="Fetch live map tracking (v1 /tracking/summary)",
    )
    order_tracking_parser.add_argument("order_id")
    order_tracking_parser.add_argument(
        "--origin",
        choices=["ORDER_TRACKING", "ORDER_DETAILS"],
        default="ORDER_TRACKING",
    )
    order_tracking_parser.add_argument(
        "--initial",
        choices=["true", "false"],
        default="true",
        help="First request should use initial=true",
    )
    order_tracking_parser.add_argument(
        "--watch",
        action="store_true",
        help="Poll until DELIVERED or CANCELED",
    )

    active_parser = subparsers.add_parser(
        "active",
        help="Summary of the last active order (Home Assistant friendly)",
    )
    active_parser.add_argument(
        "--no-details",
        action="store_true",
        help="Skip the extra v3 order details call (no clean courier name/phone)",
    )

    subparsers.add_parser("refresh", help="Force token refresh and print token file path")

    return parser


def _dump_json(data: Any) -> None:
    json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _cli_prepare_token_json(token_file: Path, refresh_token: str | None) -> str:
    """Load token JSON from file; optionally inject refresh_token from CLI."""
    token_json = _load_token_file(token_file)
    if refresh_token:
        state = parse_token_json(token_json)
        state["refresh_token"] = refresh_token
        token_json = dump_token_json(state)
    return token_json


def _cli_save_token_if_changed(token_file: Path, before: str, after: str) -> None:
    if after != before:
        _save_token_file(token_file, after)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    token_file = Path(args.token_file)
    token_json = _cli_prepare_token_json(token_file, args.refresh_token)
    token_json_before = token_json

    def emit(data: Any) -> None:
        if args.humanize:
            print(humanize(args.command, data))
        else:
            _dump_json(data)

    try:
        if args.command == "orders-list":
            data, token_json = get_orders_list(
                token_json,
                offset=args.offset,
                limit=args.limit,
            )
        elif args.command == "order":
            data, token_json = get_order(token_json, args.order_id)
        elif args.command == "order-flow":
            if args.watch:
                def on_flow_update(data: dict[str, Any], updated_token: str) -> None:
                    nonlocal token_json
                    token_json = updated_token
                    emit(data)

                data, token_json = poll_order_flow(
                    token_json,
                    args.order_id,
                    on_update=on_flow_update,
                )
                _cli_save_token_if_changed(token_file, token_json_before, token_json)
                return 0
            data, token_json = get_order_flow(token_json, args.order_id)
        elif args.command == "order-tracking":
            initial = args.initial == "true"
            if args.watch:
                def on_tracking_update(data: dict[str, Any], updated_token: str) -> None:
                    nonlocal token_json
                    token_json = updated_token
                    emit(data)

                data, token_json = poll_order_tracking(
                    token_json,
                    args.order_id,
                    origin=args.origin,
                    on_update=on_tracking_update,
                )
                _cli_save_token_if_changed(token_file, token_json_before, token_json)
                return 0
            data, token_json = get_order_tracking(
                token_json,
                args.order_id,
                origin=args.origin,
                initial=initial,
            )
        elif args.command == "active":
            data, token_json = get_last_active_order_summary(
                token_json,
                include_details=not args.no_details,
            )
        elif args.command == "refresh":
            _, token_json = ensure_access_token(token_json, force=True)
            _save_token_file(token_file, token_json)
            print(token_file)
            return 0
        else:
            parser.error(f"Unknown command: {args.command}")
            return 2

        _cli_save_token_if_changed(token_file, token_json_before, token_json)
        emit(data)
        return 0
    except GlovoApiError as exc:
        print(f"Glovo API error {exc.status}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
