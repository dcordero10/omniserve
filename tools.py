"""Deterministic support tools and the Pydantic records they return."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from collections import Counter

from mock_db import DEVICES_DB, EXPEDITE_DB, ORDERS_DB, canonical_state, live_order

AUTO_REFUND_MAX_AGE_DAYS = 30
AUTO_REFUND_MAX_AMOUNT = 500.0


class OrderTrackingResult(BaseModel):
    found: bool
    order_id: str
    status: str | None = None
    location: str | None = None
    carrier: str | None = None
    tracking_number: str | None = None
    eta: str | None = None
    amount: float | None = None
    age_days: int | None = None
    currency: str | None = None
    delivered: bool | None = None
    destination_city: str | None = None
    destination_state: str | None = None
    issue: str | None = None
    resolution: str | None = None
    expedited: bool = False
    expedite_fee: float | None = None
    message: str


class RefundPolicyResult(BaseModel):
    found: bool
    order_id: str
    approved: bool
    escalation_required: bool
    refund_amount: float | None = None
    age_days: int | None = None
    order_amount: float | None = None
    status: str | None = None
    customer_reason: str
    reasoning: str


class DeviceHealthResult(BaseModel):
    found: bool
    user_id: str
    device_id: str | None = None
    device_name: str | None = None
    health_status: str | None = None
    firmware_version: str | None = None
    latest_firmware: str | None = None
    firmware_outdated: bool = False
    crash_history: list[dict[str, str]] = Field(default_factory=list)
    diagnosis: str


class FirmwareUpdateResult(BaseModel):
    success: bool
    executed: bool
    device_id: str
    device_name: str | None = None
    previous_firmware: str | None = None
    firmware_version: str | None = None
    health_status: str | None = None
    method: str | None = None
    message: str


def _dump(model: BaseModel) -> str:
    return model.model_dump_json()


def _order_id(order_id: str) -> str:
    return order_id.strip().upper()


def track_order(order_id: str) -> str:
    """Return shipment facts for an order id as a JSON string."""
    key = _order_id(order_id)
    order = live_order(key)
    if order is None:
        return _dump(
            OrderTrackingResult(
                found=False,
                order_id=key,
                message=f"No order found for {key}.",
            )
        )
    return _dump(
        OrderTrackingResult(
            found=True,
            order_id=order["order_id"],
            status=order["status"],
            location=order["location"],
            carrier=order["carrier"],
            tracking_number=order["tracking_number"],
            eta=order["eta"],
            amount=order["amount"],
            age_days=order["age_days"],
            currency=order["currency"],
            delivered=bool(order.get("delivered")),
            destination_city=order.get("destination_city") or None,
            destination_state=order.get("destination_state") or None,
            issue=order.get("issue"),
            resolution=order.get("resolution"),
            expedited=bool(order.get("expedited")),
            expedite_fee=order.get("expedite_fee"),
            message=(
                f"{order['order_id']} is {order['status']} via {order['carrier']} "
                f"at {order['location']}. ETA: {order['eta']}."
                + (
                    " Overnight upgrade is already on this shipment."
                    if order.get("expedited")
                    else ""
                )
            ),
        )
    )


def evaluate_refund_policy(order_id: str, reason: str) -> str:
    """Approve a refund only when the order is under 30 days old and under $500."""
    key = _order_id(order_id)
    customer_reason = reason.strip() or "Customer requested a refund"
    order = live_order(key)
    if order is None:
        return _dump(
            RefundPolicyResult(
                found=False,
                order_id=key,
                approved=False,
                escalation_required=True,
                customer_reason=customer_reason,
                reasoning=(
                    f"No order found for {key}. Automatic refund was not approved. "
                    "Escalate to a human. Do not promise a refund."
                ),
            )
        )

    age_days = int(order["age_days"])
    amount = float(order["amount"])
    # WALKTHROUGH: refund-rule
    age_ok = age_days < AUTO_REFUND_MAX_AGE_DAYS
    amount_ok = amount < AUTO_REFUND_MAX_AMOUNT

    if age_ok and amount_ok:
    # /WALKTHROUGH: refund-rule
        return _dump(
            RefundPolicyResult(
                found=True,
                order_id=key,
                approved=True,
                escalation_required=False,
                refund_amount=amount,
                age_days=age_days,
                order_amount=amount,
                status=order["status"],
                customer_reason=customer_reason,
                reasoning=(
                    f"Approved: order is {age_days} days old (< {AUTO_REFUND_MAX_AGE_DAYS}) "
                    f"and amount ${amount:.2f} (< ${AUTO_REFUND_MAX_AMOUNT:.0f}). "
                    f"Refund of ${amount:.2f} is approved. Customer reason: {customer_reason}."
                ),
            )
        )

    failures: list[str] = []
    if not age_ok:
        if age_days > AUTO_REFUND_MAX_AGE_DAYS:
            failures.append(f"order is {age_days} days old (> {AUTO_REFUND_MAX_AGE_DAYS} days)")
        else:
            failures.append(
                f"order is {age_days} days old (automatic approval requires age < {AUTO_REFUND_MAX_AGE_DAYS} days)"
            )
    if not amount_ok:
        if amount > AUTO_REFUND_MAX_AMOUNT:
            failures.append(f"amount ${amount:.2f} (> ${AUTO_REFUND_MAX_AMOUNT:.0f})")
        else:
            failures.append(
                f"amount ${amount:.2f} (automatic approval requires amount < ${AUTO_REFUND_MAX_AMOUNT:.0f})"
            )
    return _dump(
        RefundPolicyResult(
            found=True,
            order_id=key,
            approved=False,
            escalation_required=True,
            refund_amount=None,
            age_days=age_days,
            order_amount=amount,
            status=order["status"],
            customer_reason=customer_reason,
            reasoning=(
                "Not approved for an automatic refund: "
                + "; ".join(failures)
                + ". Escalate to a human. Do not promise a refund."
            ),
        )
    )


def diagnose_device_health(user_id: str) -> str:
    """Return firmware, health, and crash history for a user as a JSON string."""
    key = user_id.strip().upper()
    device = DEVICES_DB.get(key)
    if device is None:
        return _dump(
            DeviceHealthResult(
                found=False,
                user_id=key,
                diagnosis=f"No registered device found for {key}.",
            )
        )

    outdated = device["firmware_version"] != device["latest_firmware"]
    if outdated:
        diagnosis = (
            f"{device['device_name']} ({device['device_id']}) is {device['health_status']}. "
            f"Firmware {device['firmware_version']} is behind latest {device['latest_firmware']}. "
            "Crash history shows repeated memory-leak restarts. "
            "An OTA update is available. Ask the customer before applying it."
        )
    else:
        diagnosis = (
            f"{device['device_name']} ({device['device_id']}) is on current firmware "
            f"{device['firmware_version']}. Health status: {device['health_status']}."
        )
    return _dump(
        DeviceHealthResult(
            found=True,
            user_id=device["user_id"],
            device_id=device["device_id"],
            device_name=device["device_name"],
            health_status=device["health_status"],
            firmware_version=device["firmware_version"],
            latest_firmware=device["latest_firmware"],
            firmware_outdated=outdated,
            crash_history=list(device["crash_history"]),
            diagnosis=diagnosis,
        )
    )


def execute_firmware_update(device_id: str) -> str:
    """Apply the latest firmware to a device and return the OTA result as JSON."""
    target = device_id.strip().upper()
    device: dict[str, Any] | None = None
    for record in DEVICES_DB.values():
        if str(record["device_id"]).upper() == target:
            device = record
            break

    if device is None:
        return _dump(
            FirmwareUpdateResult(
                success=False,
                executed=False,
                device_id=target,
                message=f"No device found for {target}. OTA update was not started.",
            )
        )

    previous = device["firmware_version"]
    latest = device["latest_firmware"]
    if previous == latest:
        return _dump(
            FirmwareUpdateResult(
                success=True,
                executed=False,
                device_id=device["device_id"],
                device_name=device["device_name"],
                previous_firmware=previous,
                firmware_version=previous,
                health_status=device["health_status"],
                method="OTA",
                message=(
                    f"{device['device_name']} is already on {previous}. "
                    "No OTA update was required."
                ),
            )
        )

    device["firmware_version"] = latest
    device["health_status"] = "HEALTHY"
    return _dump(
        FirmwareUpdateResult(
            success=True,
            executed=True,
            device_id=device["device_id"],
            device_name=device["device_name"],
            previous_firmware=previous,
            firmware_version=latest,
            health_status=device["health_status"],
            method="OTA",
            message=(
                f"OTA firmware update succeeded. {device['device_name']} "
                f"moved from {previous} to {latest}."
            ),
        )
    )


class StateSlice(BaseModel):
    state: str
    state_name: str
    count: int
    open_count: int
    delivered_count: int


class StatusSlice(BaseModel):
    status: str
    count: int


class OrderExample(BaseModel):
    order_id: str
    delivered: bool
    status: str
    destination: str
    carrier: str
    issue: str | None = None
    resolution: str | None = None


class OrderReviewResult(BaseModel):
    success: bool
    message: str
    total_orders: int
    matched: int
    open_count: int
    delivered_count: int
    destination_state: str = ""
    destination_state_name: str = ""
    shipment_scope: str = "all"
    carrier: str = ""
    by_state: list[StateSlice] = Field(default_factory=list)
    by_status: list[StatusSlice] = Field(default_factory=list)
    facts: list[str] = Field(default_factory=list)
    briefing: str = ""
    headline_numbers: list[int] = Field(default_factory=list)
    examples: list[OrderExample] = Field(default_factory=list)


class ResolutionSlice(BaseModel):
    resolution: str
    count: int


class IssueSlice(BaseModel):
    issue: str
    count: int
    delivered: int
    not_delivered: int
    resolutions: list[ResolutionSlice]


class DeliveryIssueReport(BaseModel):
    success: bool
    message: str
    total_orders: int
    orders_with_issues: int
    issue_rate_percent: int
    delivered_with_issues: int
    not_delivered_with_issues: int
    top_issue: str = ""
    top_issue_count: int = 0
    top_resolution: str = ""
    by_issue: list[IssueSlice] = Field(default_factory=list)
    by_resolution: list[ResolutionSlice] = Field(default_factory=list)
    facts: list[str] = Field(default_factory=list)
    briefing: str = ""
    headline_numbers: list[int] = Field(default_factory=list)
    examples: list[OrderExample] = Field(default_factory=list)


def _scope_name(shipment_scope: str) -> str:
    text = shipment_scope.strip().lower()
    if text in {"open", "shipping", "in transit", "not delivered", "undelivered"}:
        return "open"
    if text in {"delivered", "closed", "complete", "arrived"}:
        return "delivered"
    return "all"


def _order_matches(order: dict[str, Any], state_code: str, scope: str, carrier_name: str) -> bool:
    if state_code and order.get("destination_state") != state_code:
        return False
    delivered = bool(order.get("delivered"))
    if scope == "open" and delivered:
        return False
    if scope == "delivered" and not delivered:
        return False
    if carrier_name and str(order.get("carrier", "")).lower() != carrier_name.lower():
        return False
    return True


def _example(order: dict[str, Any]) -> OrderExample:
    city = order.get("destination_city") or ""
    state = order.get("destination_state") or ""
    destination = f"{city}, {state}".strip(", ")
    return OrderExample(
        order_id=order["order_id"],
        delivered=bool(order.get("delivered")),
        status=str(order.get("status") or ""),
        destination=destination,
        carrier=str(order.get("carrier") or ""),
        issue=order.get("issue"),
        resolution=order.get("resolution"),
    )


def _count_pairs(rows: list[dict[str, Any]], key: str) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for row in rows:
        label = str(row.get(key) or "")
        if not label:
            continue
        counts[label] = counts.get(label, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def review_orders(destination_state: str = "", shipment_scope: str = "", carrier: str = "") -> str:
    """Count orders in the book, optionally filtered by state, delivery, or carrier."""
    total = len(ORDERS_DB)
    book_open = sum(1 for order in ORDERS_DB.values() if not order.get("delivered"))
    book_delivered = total - book_open
    scope = _scope_name(shipment_scope)
    carrier_name = carrier.strip()
    state_text = destination_state.strip()
    state_code = ""
    state_name = ""
    if state_text:
        found = canonical_state(state_text)
        if found is None:
            message = f"No destination named {state_text} is in the order book."
            return _dump(
                OrderReviewResult(
                    success=False,
                    message=message,
                    total_orders=total,
                    matched=0,
                    open_count=book_open,
                    delivered_count=book_delivered,
                    shipment_scope=scope,
                    carrier=carrier_name,
                    facts=[message],
                    briefing=message,
                )
            )
        state_code, state_name = found

    # WALKTHROUGH: order-review
    matched_orders = [
        order
        for order in ORDERS_DB.values()
        if _order_matches(order, state_code, scope, carrier_name)
    ]
    open_count = sum(1 for order in matched_orders if not order["delivered"])
    delivered_count = len(matched_orders) - open_count
    # /WALKTHROUGH: order-review

    facts = [
        f"The book has {total} orders. {book_open} are still open and {book_delivered} were delivered.",
    ]
    if state_name and scope == "open":
        facts.append(f"{len(matched_orders)} open orders are shipping to {state_name}.")
    elif state_name and scope == "delivered":
        facts.append(f"{len(matched_orders)} delivered orders were shipped to {state_name}.")
    elif state_name:
        facts.append(
            f"{len(matched_orders)} orders list {state_name} as the destination: "
            f"{open_count} still open and {delivered_count} delivered."
        )
    elif scope == "open":
        facts.append(f"{open_count} orders are still open and have not been delivered.")
    elif scope == "delivered":
        facts.append(f"{delivered_count} orders were delivered.")
    else:
        facts.append(
            f"{len(matched_orders)} orders match this review: "
            f"{open_count} open and {delivered_count} delivered."
        )
    if carrier_name:
        facts.append(f"The carrier filter is {carrier_name}.")

    state_names = {
        str(order.get("destination_state") or ""): str(order.get("destination_state_name") or "")
        for order in matched_orders
    }
    state_counts: dict[str, list[int]] = {}
    status_counts: dict[str, int] = {}
    for order in matched_orders:
        code = str(order.get("destination_state") or "")
        if code:
            bucket = state_counts.setdefault(code, [0, 0, 0])
            bucket[0] += 1
            if order.get("delivered"):
                bucket[1] += 1
            else:
                bucket[2] += 1
        status = str(order.get("status") or "Unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    by_state = [
        StateSlice(
            state=code,
            state_name=state_names.get(code) or code,
            count=counts[0],
            delivered_count=counts[1],
            open_count=counts[2],
        )
        for code, counts in sorted(state_counts.items(), key=lambda item: (-item[1][0], item[0]))
        if code
    ]
    if not state_name and by_state:
        top = by_state[0]
        facts.append(
            f"The busiest destination in this slice is {top.state_name} with {top.count} orders "
            f"({top.open_count} open, {top.delivered_count} delivered)."
        )
    facts.append("Every order is flagged delivered or not. These figures are the rollup.")

    examples = [
        _example(order)
        for order in sorted(matched_orders, key=lambda item: (item.get("age_days", 0), item["order_id"]))[:6]
    ]
    if state_name and scope == "open":
        headline = [len(matched_orders)]
    elif scope == "open":
        headline = [open_count]
    else:
        headline = [len(matched_orders)]
    briefing = " ".join(facts)
    scope_label = {"open": "open", "delivered": "delivered"}.get(scope, "all")
    where = f" to {state_name}" if state_name else ""
    via = f" on {carrier_name}" if carrier_name else ""
    message = f"Reviewed {len(matched_orders)} {scope_label} orders{where}{via}."
    return _dump(
        OrderReviewResult(
            success=True,
            message=message,
            total_orders=total,
            matched=len(matched_orders),
            open_count=open_count,
            delivered_count=delivered_count,
            destination_state=state_code,
            destination_state_name=state_name,
            shipment_scope=scope,
            carrier=carrier_name,
            by_state=by_state[:8],
            by_status=[
                StatusSlice(status=status, count=count)
                for status, count in sorted(status_counts.items(), key=lambda item: (-item[1], item[0]))
            ],
            facts=facts,
            briefing=briefing,
            headline_numbers=headline,
            examples=examples,
        )
    )


def investigate_delivery_issues() -> str:
    """Summarize delivery issues and the fix recorded on each one."""
    total = len(ORDERS_DB)
    issue_orders = [order for order in ORDERS_DB.values() if order.get("issue")]
    issue_count = len(issue_orders)
    delivered = sum(1 for order in issue_orders if order.get("delivered"))
    not_delivered = issue_count - delivered
    rate = round(issue_count / total * 100) if total else 0

    grouped: dict[str, list[dict[str, Any]]] = {}
    for order in issue_orders:
        grouped.setdefault(str(order["issue"]), []).append(order)

    by_issue: list[IssueSlice] = []
    for issue, rows in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0])):
        resolutions = [
            ResolutionSlice(resolution=label, count=count)
            for label, count in _count_pairs(rows, "resolution")
        ]
        by_issue.append(
            IssueSlice(
                issue=issue,
                count=len(rows),
                delivered=sum(1 for row in rows if row.get("delivered")),
                not_delivered=sum(1 for row in rows if not row.get("delivered")),
                resolutions=resolutions,
            )
        )

    by_resolution = [
        ResolutionSlice(resolution=label, count=count)
        for label, count in _count_pairs(issue_orders, "resolution")
    ]
    if not by_issue:
        message = "No delivery issues are recorded in the order book."
        return _dump(
            DeliveryIssueReport(
                success=True,
                message=message,
                total_orders=total,
                orders_with_issues=0,
                issue_rate_percent=0,
                delivered_with_issues=0,
                not_delivered_with_issues=0,
                facts=[message],
                briefing=message,
                headline_numbers=[0],
            )
        )

    top = by_issue[0]
    top_fix = top.resolutions[0].resolution if top.resolutions else ""
    fix_phrase = ", ".join(f"{item.resolution} ({item.count})" for item in top.resolutions)
    facts = [
        f"{issue_count} of {total} orders ({rate}%) have a delivery issue on the record.",
        (
            f"{not_delivered} of those were not delivered. {delivered} were delivered, "
            "and the problem was found on arrival or inside the box."
        ),
        (
            f"The most common issue is {top.issue}: {top.count} orders. "
            f"{top.delivered} of them were delivered and {top.not_delivered} were not. "
            f"Fixes on those orders: {fix_phrase}."
        ),
    ]
    for item in by_issue[1:]:
        phrase = ", ".join(f"{part.resolution} ({part.count})" for part in item.resolutions)
        facts.append(
            f"{item.issue}: {item.count} orders, {item.delivered} delivered and "
            f"{item.not_delivered} not delivered. Fixes: {phrase}."
        )
    if by_resolution:
        overall = ", ".join(f"{item.resolution} ({item.count})" for item in by_resolution)
        facts.append(f"Across every issue, the fixes were {overall}.")

    seen: set[tuple[str, str]] = set()
    examples: list[OrderExample] = []
    for order in sorted(issue_orders, key=lambda item: item["order_id"]):
        key = (str(order.get("issue")), str(order.get("resolution")))
        if key in seen:
            continue
        seen.add(key)
        examples.append(_example(order))
        if len(examples) == 6:
            break

    briefing = " ".join(facts)
    return _dump(
        DeliveryIssueReport(
            success=True,
            message=f"Reviewed {issue_count} delivery issues in a book of {total} orders.",
            total_orders=total,
            orders_with_issues=issue_count,
            issue_rate_percent=rate,
            delivered_with_issues=delivered,
            not_delivered_with_issues=not_delivered,
            top_issue=top.issue,
            top_issue_count=top.count,
            top_resolution=top_fix,
            by_issue=by_issue,
            by_resolution=by_resolution,
            facts=facts,
            briefing=briefing,
            headline_numbers=[issue_count, top.count],
            examples=examples,
        )
    )


EXPEDITE_FEE_USD = 15.0
EXPEDITE_ETA = "Tomorrow by 10:00 AM"
_OPENABLE = {"In Transit", "Processing", "Label Created"}


class ExpeditePolicyResult(BaseModel):
    found: bool
    order_id: str
    eligible: bool
    complimentary: bool = False
    requires_confirmation: bool = False
    fee: float = 0
    currency: str = "USD"
    current_status: str | None = None
    current_eta: str | None = None
    new_eta: str | None = None
    carrier: str | None = None
    reason_code: str
    reasoning: str
    next_question: str


class ExpediteApplyResult(BaseModel):
    success: bool
    executed: bool
    order_id: str
    fee: float = 0
    eta: str | None = None
    carrier: str | None = None
    message: str


class OpenLane(BaseModel):
    state_name: str
    open_count: int


class TrendReport(BaseModel):
    success: bool
    message: str
    total_orders: int
    open_count: int
    delivered_count: int
    delayed_count: int
    expedite_ready: int
    top_open_state: str
    top_open_state_count: int
    top_open_status: str
    top_open_status_count: int
    top_delayed_carrier: str
    top_delayed_carrier_count: int
    issue_rate_percent: float
    by_open_state: list[OpenLane]
    facts: list[str]
    briefing: str
    next_question: str
    headline_numbers: list[int]


def _expedite_decision(order: dict) -> dict[str, Any]:
    """Decide whether one shipment can move to overnight."""
    status = str(order.get("status") or "")
    delivered = bool(order.get("delivered")) or status == "Delivered"
    issue = order.get("issue")
    # WALKTHROUGH: expedite-rule
    # Overnight is $15 unless the carrier already delayed the box, in which case it is free.
    # A delivered box, a box already on the truck, and a box held for an issue cannot be expedited.
    if delivered:
        code = "delivered"
    elif status == "Out for Delivery":
        code = "on_truck"
    elif issue:
        code = "held"
    elif order.get("expedited"):
        code = "already"
    elif status == "Delayed":
        code = "complimentary"
    elif status in _OPENABLE:
        code = "paid"
    else:
        code = "unavailable"
    # /WALKTHROUGH: expedite-rule
    eligible = code in {"paid", "complimentary"}
    fee = 0.0 if code == "complimentary" else (EXPEDITE_FEE_USD if code == "paid" else 0.0)
    order_id = str(order.get("order_id") or "")
    eta = str(order.get("eta") or "the current window")
    carrier = str(order.get("carrier") or "the carrier")
    resolution = order.get("resolution") or "no fix recorded"
    if code == "delivered":
        reasoning = f"{order_id} is already delivered, so there is no shipment left to expedite."
        question = "If something was wrong with what arrived, I can check a refund. Should I?"
    elif code == "on_truck":
        reasoning = (
            f"{order_id} is out for delivery ({eta}). "
            "The carrier will not pull it back onto a faster service."
        )
        question = "Want me to check a refund if it arrives damaged, or look at another order?"
    elif code == "held":
        reasoning = (
            f"{order_id} is held for {issue}. Expedite stays blocked until that clears. "
            f"The recorded fix is {resolution}."
        )
        question = "Want me to check a refund on it, or walk through that fix?"
    elif code == "already":
        reasoning = f"{order_id} is already on overnight. The window is {eta}."
        question = "Want me to watch a different order, or leave this one as it is?"
    elif code == "complimentary":
        reasoning = (
            f"{order_id} is Delayed, so the overnight upgrade is free. "
            f"The window would move from {eta} to {EXPEDITE_ETA}."
        )
        question = "Should I send that overnight upgrade at no charge?"
    elif code == "paid":
        reasoning = (
            f"{order_id} is {status} with {carrier}, and the window is {eta}. "
            f"Overnight is ${fee:.2f} and would move that window to {EXPEDITE_ETA}."
        )
        question = "Want me to put that overnight upgrade on it?"
    else:
        reasoning = f"{order_id} is {status}, and this desk cannot upgrade that status."
        question = "Want me to look at a refund, or check a different order?"
    return {
        "code": code,
        "eligible": eligible,
        "complimentary": code == "complimentary",
        "fee": fee,
        "reasoning": reasoning,
        "next_question": question,
        "new_eta": EXPEDITE_ETA if eligible else None,
    }


def evaluate_expedite_policy(order_id: str) -> str:
    """Decide if a shipment can move to overnight, and what it would cost."""
    key = _order_id(order_id)
    order = live_order(key)
    if order is None:
        return _dump(
            ExpeditePolicyResult(
                found=False,
                order_id=key,
                eligible=False,
                reason_code="missing",
                reasoning=f"No order found for {key}, so it cannot be expedited.",
                next_question="Which order id should I check?",
            )
        )
    decision = _expedite_decision(order)
    return _dump(
        ExpeditePolicyResult(
            found=True,
            order_id=key,
            eligible=decision["eligible"],
            complimentary=decision["complimentary"],
            requires_confirmation=decision["eligible"],
            fee=decision["fee"],
            current_status=order.get("status"),
            current_eta=order.get("eta"),
            new_eta=decision["new_eta"],
            carrier=order.get("carrier"),
            reason_code=decision["code"],
            reasoning=decision["reasoning"],
            next_question=decision["next_question"],
        )
    )


def apply_expedite(order_id: str) -> str:
    """Put an eligible shipment on overnight. Refuses when the policy says no."""
    key = _order_id(order_id)
    order = live_order(key)
    if order is None:
        return _dump(
            ExpediteApplyResult(
                success=False,
                executed=False,
                order_id=key,
                message=f"No order found for {key}. Nothing was changed.",
            )
        )
    if order.get("expedited"):
        return _dump(
            ExpediteApplyResult(
                success=True,
                executed=True,
                order_id=key,
                fee=float(order.get("expedite_fee") or 0),
                eta=order.get("eta"),
                carrier=order.get("carrier"),
                message=f"{key} is already on overnight. The window is {order.get('eta')}.",
            )
        )
    decision = _expedite_decision(order)
    if not decision["eligible"]:
        return _dump(
            ExpediteApplyResult(
                success=False,
                executed=False,
                order_id=key,
                carrier=order.get("carrier"),
                eta=order.get("eta"),
                message=decision["reasoning"] + " Nothing was changed.",
            )
        )
    city = order.get("destination_city") or ""
    state = order.get("destination_state") or ""
    place = ", ".join(part for part in (city, state) if part)
    EXPEDITE_DB[key] = {
        "expedited": True,
        "expedite_fee": decision["fee"],
        "eta": EXPEDITE_ETA,
        "status": "In Transit",
        "location": f"Overnight desk in {place}" if place else "Overnight desk",
    }
    fee_line = "No charge." if decision["fee"] == 0 else f"Fee ${decision['fee']:.2f}."
    return _dump(
        ExpediteApplyResult(
            success=True,
            executed=True,
            order_id=key,
            fee=decision["fee"],
            eta=EXPEDITE_ETA,
            carrier=order.get("carrier"),
            message=(
                f"Overnight upgrade applied to {key}. New window {EXPEDITE_ETA}. {fee_line}"
            ),
        )
    )


def analyze_order_trends() -> str:
    """Summarize the open book: lanes, delays, and how many can still be expedited."""
    orders = [live for order_id in ORDERS_DB if (live := live_order(order_id))]
    total = len(orders)
    open_orders = [order for order in orders if not order.get("delivered")]
    delivered = total - len(open_orders)
    delayed = [order for order in open_orders if order.get("status") == "Delayed"]
    ready = [
        order
        for order in open_orders
        if _expedite_decision(order)["code"] in {"paid", "complimentary"}
    ]
    state_counts: Counter[str] = Counter(
        str(order.get("destination_state_name") or order.get("destination_state") or "Unknown")
        for order in open_orders
    )
    status_counts: Counter[str] = Counter(str(order.get("status") or "Unknown") for order in open_orders)
    carrier_counts: Counter[str] = Counter(
        str(order.get("carrier") or "Unknown") for order in delayed
    )
    top_states = state_counts.most_common(5)
    top_name, top_open = top_states[0] if top_states else ("", 0)
    top_status, top_status_count = status_counts.most_common(1)[0] if status_counts else ("", 0)
    top_carrier, top_carrier_count = carrier_counts.most_common(1)[0] if carrier_counts else ("", 0)
    issue_count = sum(1 for order in orders if order.get("issue"))
    rate = round(100 * issue_count / total, 1) if total else 0.0
    delay_clause = (
        f"{len(delayed)} open orders are Delayed, mostly with {top_carrier} ({top_carrier_count})."
        if top_carrier
        else f"{len(delayed)} open orders are Delayed."
    )
    briefing = (
        f"Of {total} orders, {len(open_orders)} are still open and {delivered} are delivered. "
        f"Most open shipments are {top_status} ({top_status_count}). "
        f"The busiest open lane is {top_name}, with {top_open} orders. "
        f"{delay_clause} "
        f"{len(ready)} open orders can still take an overnight upgrade. "
        f"The delivery-issue rate on the whole book is {rate}%."
    )
    next_question = (
        f"Want me to open the {top_name} lane, or look at the {len(delayed)} delayed shipments"
        + (f" on {top_carrier}?" if top_carrier else "?")
    )
    facts = [briefing, next_question]
    return _dump(
        TrendReport(
            success=True,
            message=f"Reviewed trends across {total} orders.",
            total_orders=total,
            open_count=len(open_orders),
            delivered_count=delivered,
            delayed_count=len(delayed),
            expedite_ready=len(ready),
            top_open_state=top_name,
            top_open_state_count=top_open,
            top_open_status=top_status,
            top_open_status_count=top_status_count,
            top_delayed_carrier=top_carrier,
            top_delayed_carrier_count=top_carrier_count,
            issue_rate_percent=rate,
            by_open_state=[
                OpenLane(state_name=name, open_count=count) for name, count in top_states
            ],
            facts=facts,
            briefing=briefing,
            next_question=next_question,
            headline_numbers=[len(open_orders), top_open, len(ready)],
        )
    )


def parse_tool_result(raw: str) -> Any:
    """Parse a tool JSON string for telemetry panels."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
