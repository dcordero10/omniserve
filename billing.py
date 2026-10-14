"""Claude account balance and session cost.

The balance comes from the organization usage endpoint: monthly limit minus
spend so far. A workspace-scoped API key cannot read that endpoint.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

# Published list price for claude-sonnet-5-5, USD per million tokens.
INPUT_USD_PER_MILLION = 2.0
OUTPUT_USD_PER_MILLION = 10.0

_LIMITS: dict[str, str] = {}
_ORG_ID = ""
_BALANCE: dict[str, float] | None = None
_BALANCE_CHECKED = False


def record_rate_limits(response: Any) -> None:
    """Keep the newest rate-limit headers from a Claude response."""
    try:
        headers = getattr(response, "headers", None)
        if headers is None:
            return
        found = {
            key.lower(): value
            for key, value in headers.items()
            if key.lower().startswith("anthropic-ratelimit-")
        }
        if found:
            _LIMITS.clear()
            _LIMITS.update(found)
        org_id = headers.get("anthropic-organization-id")
        if org_id:
            global _ORG_ID
            _ORG_ID = str(org_id)
    except Exception:
        return


def rate_limit_snapshot() -> dict[str, str]:
    return dict(_LIMITS)


def account_balance() -> dict[str, float] | None:
    """Return remaining, spend, and limit in USD, or None if billing is unreadable."""
    global _BALANCE, _BALANCE_CHECKED
    if _BALANCE_CHECKED:
        return _BALANCE
    api_key = (
        os.getenv("ANTHROPIC_ADMIN_API_KEY", "").strip()
        or os.getenv("ANTHROPIC_API_KEY", "").strip()
    )
    if not api_key or not _ORG_ID:
        return None
    _BALANCE_CHECKED = True
    request = urllib.request.Request(
        f"https://api.anthropic.com/v1/organizations/{_ORG_ID}/usage",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode())
        spend = float(payload["current_spend"])
        limit = float(payload["customer_limit"])
    except (urllib.error.URLError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _BALANCE = None
        return None
    _BALANCE = {"remaining": limit - spend, "spend": spend, "limit": limit}
    return _BALANCE


def session_cost(logs: list[dict[str, Any]]) -> dict[str, float | int]:
    prompt = sum(int(log.get("prompt_tokens") or 0) for log in logs)
    completion = sum(int(log.get("completion_tokens") or 0) for log in logs)
    dollars = (prompt / 1_000_000) * INPUT_USD_PER_MILLION + (completion / 1_000_000) * OUTPUT_USD_PER_MILLION
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "dollars": dollars,
    }


def format_tokens(value: str | None) -> str:
    if not value or not str(value).isdigit():
        return "—"
    return f"{int(value):,}"


def format_dollars(amount: float) -> str:
    if amount <= 0:
        return "$0.00"
    if amount < 1:
        return f"${amount:.4f}"
    return f"${amount:,.2f}"


def format_reset(value: str | None) -> str:
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return ""
    clock = moment.strftime("%I:%M:%S %p").lstrip("0")
    return clock
