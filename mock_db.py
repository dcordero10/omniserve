"""Local mock data for the OmniServe support agent.

Ages and amounts on ORD-101 and ORD-202 are fixed so the refund scenarios stay
deterministic: ORD-101 qualifies, ORD-202 does not. The rest of the book is a
generated catalog of 4,998 shipments, for 5,000 orders in total.
"""

from __future__ import annotations

import copy
import json
import random
import threading
import uuid
from datetime import datetime
from pathlib import Path

# WALKTHROUGH: orders
ORDERS_DB: dict[str, dict] = {
    "ORD-101": {
        "order_id": "ORD-101",
        "status": "In Transit",
        "amount": 149.99,
        "age_days": 10,
        "carrier": "FedEx",
        "location": "Oakland, CA Sorting Facility",
        "tracking_number": "748920331904",
        "eta": "Tomorrow by 2:00 PM",
        "currency": "USD",
        "customer_id": "USR-442",
        "delivered": False,
        "destination_city": "Oakland",
        "destination_state": "CA",
        "destination_state_name": "California",
        "issue": None,
        "resolution": None,
        "issue_note": None,
    },
    "ORD-202": {
        "order_id": "ORD-202",
        "status": "Delivered",
        "amount": 1250.00,
        "age_days": 42,
        "carrier": "UPS",
        "location": "Delivered to recipient",
        "tracking_number": "1Z999AA10123456784",
        "eta": "Delivered",
        "currency": "USD",
        "customer_id": "USR-442",
        "delivered": True,
        "destination_city": "Seattle",
        "destination_state": "WA",
        "destination_state_name": "Washington",
        "issue": None,
        "resolution": None,
        "issue_note": None,
    },
}
# /WALKTHROUGH: orders

# WALKTHROUGH: order-book
# 5000 orders total. The two records above stay fixed for the refund demo.
# 750 of the 5000 (15%) get a delivery issue and a resolution. Those quotas are
# constants so the live briefing does not drift between presentations.
ISSUE_ORDER_COUNT = 750
OPEN_TO_CALIFORNIA = 370
# /WALKTHROUGH: order-book

STATE_DIRECTORY: list[tuple[str, str, list[str]]] = [
    ("CA", "California", ["Oakland", "Los Angeles", "San Jose", "San Diego", "Sacramento", "Fresno"]),
    ("TX", "Texas", ["Austin", "Dallas", "Houston", "San Antonio"]),
    ("NY", "New York", ["New York", "Buffalo", "Albany"]),
    ("WA", "Washington", ["Seattle", "Tacoma", "Spokane"]),
    ("FL", "Florida", ["Miami", "Orlando", "Tampa"]),
    ("IL", "Illinois", ["Chicago", "Naperville"]),
    ("CO", "Colorado", ["Denver", "Boulder"]),
    ("GA", "Georgia", ["Atlanta", "Savannah"]),
    ("MA", "Massachusetts", ["Boston", "Cambridge"]),
    ("AZ", "Arizona", ["Phoenix", "Tucson"]),
    ("OR", "Oregon", ["Portland", "Eugene"]),
    ("NV", "Nevada", ["Las Vegas", "Reno"]),
    ("PA", "Pennsylvania", ["Philadelphia", "Pittsburgh"]),
    ("NC", "North Carolina", ["Charlotte", "Raleigh"]),
    ("MI", "Michigan", ["Detroit", "Ann Arbor"]),
    ("OH", "Ohio", ["Columbus", "Cleveland"]),
]

_CARRIERS = ["FedEx", "UPS", "USPS", "DHL"]
_OPEN_STATUSES = ["In Transit", "Out for Delivery", "Processing", "Delayed", "Label Created"]

# issue, resolution, delivered, note, count. Counts sum to ISSUE_ORDER_COUNT (750).
_ISSUE_SPECS: list[tuple[str, str, bool, str, int]] = [
    (
        "Stolen package",
        "Reordered",
        False,
        "The package was reported stolen after the carrier scan. A replacement order was placed.",
        180,
    ),
    (
        "Stolen package",
        "Refund",
        False,
        "The package was reported stolen. The customer took a refund instead of a replacement.",
        100,
    ),
    (
        "Opened box",
        "Replacement shipped",
        True,
        "The box arrived with the seal broken. A replacement was shipped.",
        100,
    ),
    (
        "Opened box",
        "Refund",
        True,
        "The box arrived opened. The customer asked for a refund.",
        50,
    ),
    (
        "Damaged item",
        "Replacement shipped",
        True,
        "The item was crushed in transit. A replacement was shipped.",
        70,
    ),
    (
        "Damaged item",
        "Refund",
        False,
        "The item was damaged before it could be delivered. A refund was issued.",
        50,
    ),
    (
        "Missing item",
        "Reordered",
        True,
        "The carton arrived short an item. The missing piece was reordered.",
        70,
    ),
    (
        "Missing item",
        "Refund",
        True,
        "The carton arrived incomplete. A refund was issued for the missing piece.",
        30,
    ),
    (
        "Wrong address",
        "Reshipped to corrected address",
        False,
        "The label had the wrong street. It was reshipped after the address was corrected.",
        100,
    ),
]


def canonical_state(value: str) -> tuple[str, str] | None:
    """Map a state name or postal code to (code, name)."""
    text = value.strip()
    if not text:
        return None
    for code, name, _cities in STATE_DIRECTORY:
        if text.lower() == name.lower() or text.upper() == code:
            return code, name
    return None


def _blank_order(*, delivered: bool) -> dict:
    return {
        "delivered": delivered,
        "issue": None,
        "resolution": None,
        "issue_note": None,
    }


def _expand_issues() -> list[dict]:
    records: list[dict] = []
    for issue, resolution, delivered, note, count in _ISSUE_SPECS:
        for _ in range(count):
            record = _blank_order(delivered=delivered)
            record["issue"] = issue
            record["resolution"] = resolution
            record["issue_note"] = note
            records.append(record)
    return records


def _assign_states(records: list[dict], ca_count: int) -> None:
    california = next(item for item in STATE_DIRECTORY if item[0] == "CA")
    others = [item for item in STATE_DIRECTORY if item[0] != "CA"]
    for index, record in enumerate(records):
        code, name, cities = california if index < ca_count else others[(index - ca_count) % len(others)]
        record["destination_state"] = code
        record["destination_state_name"] = name
        record["destination_city"] = cities[index % len(cities)]


def _decorate(record: dict, index: int) -> None:
    city = record["destination_city"]
    state = record["destination_state"]
    place = f"{city}, {state}"
    if record["delivered"]:
        record["status"] = "Delivered"
        record["location"] = f"Delivered to recipient in {place}"
        record["eta"] = "Delivered"
    elif record.get("issue"):
        record["status"] = "Exception"
        record["location"] = f"Held in {place}"
        record["eta"] = "Delayed pending resolution"
    else:
        status = _OPEN_STATUSES[index % len(_OPEN_STATUSES)]
        record["status"] = status
        if status == "Out for Delivery":
            record["location"] = f"Out for delivery in {place}"
            record["eta"] = "Today by 8:00 PM"
        elif status == "Processing":
            record["location"] = f"Processing at {place}"
            record["eta"] = "In two days"
        elif status == "Label Created":
            record["location"] = f"Label created in {place}"
            record["eta"] = "Friday by 5:00 PM"
        elif status == "Delayed":
            record["location"] = f"Delayed in {place}"
            record["eta"] = "Tomorrow by 8:00 PM"
        else:
            record["location"] = f"{place} Sorting Facility"
            record["eta"] = "Tomorrow by 2:00 PM"
    record["order_id"] = f"ORD-{301 + index}"
    record["carrier"] = _CARRIERS[index % len(_CARRIERS)]
    record["amount"] = round(19 + ((index * 53) % 780) + (0, 0.5, 0.99, 0.25)[index % 4], 2)
    record["age_days"] = 1 + (index * 3) % 40
    record["tracking_number"] = f"{740000000000 + index:012d}"
    record["currency"] = "USD"
    record["customer_id"] = f"USR-{1000 + (index % 500)}"


def _generated_orders() -> dict[str, dict]:
    issues = _expand_issues()
    open_issues = [record for record in issues if not record["delivered"]]
    delivered_issues = [record for record in issues if record["delivered"]]
    open_clean = [_blank_order(delivered=False) for _ in range(1360)]
    delivered_clean = [_blank_order(delivered=True) for _ in range(2888)]
    _assign_states(open_issues, 80)
    _assign_states(open_clean, 289)
    _assign_states(delivered_issues, 60)
    _assign_states(delivered_clean, 350)
    records = open_issues + open_clean + delivered_issues + delivered_clean
    random.Random(442).shuffle(records)
    for index, record in enumerate(records):
        _decorate(record, index)
    return {record["order_id"]: record for record in records}


ORDERS_DB.update(_generated_orders())

_open_california = sum(
    1
    for order in ORDERS_DB.values()
    if not order["delivered"] and order.get("destination_state") == "CA"
)
_issue_count = sum(1 for order in ORDERS_DB.values() if order.get("issue"))
if len(ORDERS_DB) != 5000 or _issue_count != ISSUE_ORDER_COUNT or _open_california != OPEN_TO_CALIFORNIA:
    raise RuntimeError(
        "Order book quotas drifted: "
        f"{len(ORDERS_DB)} orders, {_issue_count} issues, {_open_california} open to California."
    )

_DEVICES_DB_INITIAL: dict[str, dict] = {
    "USR-442": {
        "user_id": "USR-442",
        "device_id": "HUB-442",
        "device_name": "OmniSmart Hub V2",
        "health_status": "DEGRADED_PERFORMANCE",
        "firmware_version": "v1.2.0",
        "latest_firmware": "v2.1.0",
        "crash_history": [
            {
                "timestamp": "2026-10-05T02:14:00Z",
                "code": "MEM_LEAK",
                "detail": "Heap exhaustion after 18h uptime; hub process killed",
            },
            {
                "timestamp": "2026-10-06T03:02:00Z",
                "code": "MEM_LEAK",
                "detail": "Restart loop caused by retained telemetry buffers",
            },
        ],
    }
}

DEVICES_DB: dict[str, dict] = copy.deepcopy(_DEVICES_DB_INITIAL)
EXPEDITE_DB: dict[str, dict] = {}


def live_order(order_id: str) -> dict | None:
    """Return an order plus any overnight upgrade applied during this session."""
    order = ORDERS_DB.get(order_id)
    if order is None:
        return None
    merged = dict(order)
    merged.update(EXPEDITE_DB.get(order_id, {}))
    return merged


def reset_runtime_state() -> None:
    """Restore mutable device records and expedites so the demo can be repeated."""
    DEVICES_DB.clear()
    DEVICES_DB.update(copy.deepcopy(_DEVICES_DB_INITIAL))
    EXPEDITE_DB.clear()


# Chat transcripts and Arize-style traces. Separate from the order book so the
# 5,000-order quotas stay fixed. Rows survive Reset conversation and a process
# restart; they are not part of the refund or expedite demo state.
_HISTORY_PATH = Path(__file__).with_name(".omniserve_history.json")
_HISTORY_LOCK = threading.Lock()

CHAT_LOG: dict[str, dict] = {}
TRACE_LOG: dict[str, dict] = {}


def _load_history() -> None:
    if not _HISTORY_PATH.exists():
        return
    try:
        payload = json.loads(_HISTORY_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return
    chats = payload.get("chat_log")
    traces = payload.get("trace_log")
    if isinstance(chats, dict):
        CHAT_LOG.update(chats)
    if isinstance(traces, dict):
        TRACE_LOG.update(traces)


def _save_history() -> None:
    payload = {"chat_log": CHAT_LOG, "trace_log": TRACE_LOG}
    temporary = _HISTORY_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload))
    temporary.replace(_HISTORY_PATH)


def record_chat_message(
    *,
    session_id: str,
    role: str,
    content: str,
    trace_id: str = "",
    requires_human_escalation: bool = False,
    activity: list[str] | None = None,
) -> str:
    """Append one customer or assistant turn to the chat log table."""
    message_id = f"MSG-{uuid.uuid4().hex[:12]}"
    row = {
        "message_id": message_id,
        "session_id": session_id,
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "role": role,
        "content": content,
        "trace_id": trace_id,
        "requires_human_escalation": requires_human_escalation,
        "activity": list(activity or []),
    }
    with _HISTORY_LOCK:
        CHAT_LOG[message_id] = row
        _save_history()
    return message_id


def record_trace(trace: dict, session_id: str) -> str:
    """Append one agent turn to the trace table. A repeated trace id is kept once."""
    trace_id = str(trace.get("trace_id") or f"trc_{uuid.uuid4().hex[:12]}")
    row = copy.deepcopy(trace)
    row["trace_id"] = trace_id
    row["session_id"] = session_id
    with _HISTORY_LOCK:
        TRACE_LOG[trace_id] = row
        _save_history()
    return trace_id


def chat_log_rows(session_id: str = "") -> list[dict]:
    """Chat rows oldest first. Pass a session id to limit the list."""
    with _HISTORY_LOCK:
        rows = list(CHAT_LOG.values())
    if session_id:
        rows = [row for row in rows if row.get("session_id") == session_id]
    return sorted(rows, key=lambda row: (row.get("timestamp") or "", row.get("message_id") or ""))


def trace_log_rows(session_id: str = "") -> list[dict]:
    """Trace rows oldest first. Pass a session id to limit the list."""
    with _HISTORY_LOCK:
        rows = [copy.deepcopy(row) for row in TRACE_LOG.values()]
    if session_id:
        rows = [row for row in rows if row.get("session_id") == session_id]
    return sorted(rows, key=lambda row: (row.get("timestamp") or "", row.get("trace_id") or ""))


_load_history()
