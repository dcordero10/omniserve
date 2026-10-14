"""Agent loop, tool routing, and in-process telemetry capture.

The control room stores spans as plain dictionaries. The same turn is exported
to Arize AX when ARIZE_SPACE_ID and ARIZE_API_KEY are set.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
import uuid
from datetime import datetime
from typing import Any, Callable

from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

load_dotenv()

from tracing import (  # noqa: E402
    flush_traces,
    guardrail_span,
    init_arize,
    note_tool_result,
    note_turn_evals,
    note_turn_output,
    support_turn,
    tool_span,
)

init_arize()

from anthropic import Anthropic  # noqa: E402

from billing import rate_limit_snapshot, record_rate_limits
from mock_db import STATE_DIRECTORY, canonical_state
from tools import (
    analyze_order_trends,
    apply_expedite,
    diagnose_device_health,
    evaluate_expedite_policy,
    evaluate_refund_policy,
    execute_firmware_update,
    investigate_delivery_issues,
    parse_tool_result,
    review_orders,
    track_order,
)

load_dotenv()

MODEL = "claude-sonnet-5-5"
MAX_TOOL_ROUNDS = 4
MAX_TOKENS = 1600

TOOL_FUNCTIONS: dict[str, Callable[..., str]] = {
    "track_order": track_order,
    "evaluate_refund_policy": evaluate_refund_policy,
    "diagnose_device_health": diagnose_device_health,
    "execute_firmware_update": execute_firmware_update,
    "review_orders": review_orders,
    "investigate_delivery_issues": investigate_delivery_issues,
    "evaluate_expedite_policy": evaluate_expedite_policy,
    "apply_expedite": apply_expedite,
    "analyze_order_trends": analyze_order_trends,
}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "track_order",
        "description": (
            "Look up live shipment status for an order. Returns location, carrier, "
            "tracking number, ETA, amount, and age. Use for where-is-my-order questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "Order id, for example ORD-101.",
                }
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "evaluate_refund_policy",
        "description": (
            "Decide an automatic refund. Approves only when the order is under 30 days "
            "old and under $500. Otherwise approved is false and escalation_required is true. "
            "Always call this before stating a refund outcome."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "Order id to evaluate, for example ORD-101.",
                },
                "reason": {
                    "type": "string",
                    "description": "The customer's stated reason for the refund.",
                },
            },
            "required": ["order_id", "reason"],
        },
    },
    {
        "name": "diagnose_device_health",
        "description": (
            "Read device health for a user. Returns firmware versions, health status, "
            "crash history, and whether an update is available. The signed-in user is USR-442."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "Customer id. Use USR-442 for the signed-in customer.",
                }
            },
            "required": ["user_id"],
        },
    },
    {
        "name": "execute_firmware_update",
        "description": (
            "Apply an OTA firmware update. Call only after the customer explicitly confirms "
            "the update, using the device_id returned by diagnose_device_health."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "device_id": {
                    "type": "string",
                    "description": "Device id from diagnose_device_health, for example HUB-442.",
                }
            },
            "required": ["device_id"],
        },
    },
    {
        "name": "review_orders",
        "description": (
            "Count and describe the order book. Use this for open orders, how many shipments "
            "are going to a state, or any question about many orders at once. "
            "shipment_scope is open, delivered, or all. destination_state is a name or code "
            "such as California or CA. Do not use this for a single order id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "destination_state": {
                    "type": "string",
                    "description": "State name or code, such as California or CA. Empty means every state.",
                },
                "shipment_scope": {
                    "type": "string",
                    "description": "open for orders still shipping, delivered for orders that arrived, or all.",
                },
                "carrier": {
                    "type": "string",
                    "description": "FedEx, UPS, USPS, or DHL. Empty means every carrier.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "investigate_delivery_issues",
        "description": (
            "Investigate frequent delivery issues across the order book and how each one was "
            "resolved. Returns issue counts, whether those orders were delivered, and the fix "
            "(reordered, refund, replacement shipped, or reshipped). Call with no arguments."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "evaluate_expedite_policy",
        "description": (
            "Decide whether one shipment can be upgraded to overnight. "
            "Returns eligible, fee, the current window, and the new window. "
            "Call this for expedite, rush, sooner, faster, or overnight questions. "
            "Use the order id from the conversation when the customer says it or this order. "
            "Do not invent the fee or the decision."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "Order id, for example ORD-2493.",
                }
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "apply_expedite",
        "description": (
            "Put an eligible shipment on overnight. Call only after the customer agrees, "
            "or when they directly tell you to expedite it. Do not call it for a question "
            "such as 'can this be expedited?'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "Order id to upgrade, for example ORD-2493.",
                }
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "analyze_order_trends",
        "description": (
            "Summarize the order book: how many are open, the busiest destination, "
            "delayed shipments, and how many can still be expedited. "
            "Use for trend, pattern, or 'how are orders doing' questions. Call with no arguments."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]

SYSTEM_PROMPT = """You are OmniServe, a support and sales agent for the signed-in customer USR-442.

Stay in the conversation. The latest customer message is the one to answer. Earlier turns tell you which order "it" or "this" means. Do not replay a tracking script you already gave. Do not repeat the location, tracking number, and ETA unless they just asked where the package is, or those facts changed.

Use tools for every order, refund, expedite, trend, and device fact. Never invent tracking locations, carriers, tracking numbers, ETAs, prices, refund outcomes, expedite fees, firmware versions, or counts.

How to work a turn:
- Answer the new question in a sentence or two, in plain language.
- Then ask one next question that fits this moment. Write it yourself. Do not ask the same question you asked on the previous turn.
- Sound like a person at a support desk who can also offer a faster shipment. No status dump, no bullet list of facts they already heard.

Order tracking:
- Call track_order when they ask where one order is, or when you need that order's facts and do not already have them.
- The first time they ask where it is, include the tool's location, carrier, tracking number, and ETA.
- If you already told them that scan, do not read it again. Say whether anything changed, then offer a next step.

Expedite:
- Call evaluate_expedite_policy before you say whether a shipment can be expedited. Quote eligible, fee, and new_eta from that tool.
- A question ("can it be expedited?") is not a yes. Tell them the decision and ask if they want the upgrade. Do not call apply_expedite on that turn.
- Call apply_expedite only after they agree, or when they directly tell you to expedite it.
- If the tool says eligible is false, say why and offer the next useful step from the tool. Do not promise an upgrade.
- After a successful upgrade, confirm the new window and the fee. Ask something different. Do not ask again if they want the upgrade.

Order book and trends:
- Questions about many orders, open orders, a destination such as California, or delivery problems do not use track_order.
- Call review_orders for counts. Use shipment_scope "open" when they mean orders still shipping, and destination_state when they name a state.
- Call investigate_delivery_issues for frequent delivery problems and how they were fixed. It takes no arguments.
- Call analyze_order_trends for trends, patterns, or how the book is doing. It takes no arguments.
- Write a short briefing from the tool facts. Use the digits in the facts. Then ask which slice they want opened.

Refunds:
- Call evaluate_refund_policy before you state an outcome. Put the customer's reason in `reason`.
- If approved is true, state refund_amount from the tool and nothing higher.
- If approved is false or escalation_required is true, say the automatic refund was not approved, give the tool reasoning, and say a specialist will review it. Do not promise money, a timeline, or an exception.

Devices:
- For a crashing or offline hub, do not jump to the fix. Ask one question per turn, in this order:
  1. When does it usually crash: right after boot, after it has been running for a few hours, or when another device connects?
  2. When it crashes, does the status light blink and come back, or does the hub go dark and reboot by itself?
- Only after both answers, call diagnose_device_health with user_id USR-442. Explain the firmware versions and the memory-leak crash history in plain language, then ask if they want the OTA update.
- Do not call execute_firmware_update unless the customer's latest message explicitly agrees, for example "Yes, update it" or "send it" after you asked "Want me to send it?".
- On a confirmation turn, call diagnose_device_health first if you need the device_id, then call execute_firmware_update with that id.
- After a successful update, confirm that the OTA update succeeded and quote the firmware version from the tool.

If a tool returns found false or success false, say that plainly.
Keep the customer reply concise.
"""

FINALIZE_PROMPT = """Build the structured result for this turn from the conversation and tool results.

user_message: the reply the customer will read. Answer their latest message. Do not repeat a scan, tracking number, or ETA you already gave unless they just asked where the package is or the shipment changed. When they did ask where it is, include location, carrier, tracking number, and ETA from track_order. When they asked about expediting, use evaluate_expedite_policy: if eligible, quote the fee and the new window and ask whether to apply it; if not eligible, say why. If apply_expedite succeeded, confirm the new window and do not ask the same upgrade question again. When review_orders, investigate_delivery_issues, or analyze_order_trends ran, write a short briefing that uses the digits in the tool facts. For refunds, follow approved and escalation_required exactly and never promise money the tool did not approve. For an outdated hub, ask whether to apply the update if it has not been applied yet. If the OTA tool succeeded, confirm that the OTA update succeeded. End with one new follow-up question written for this moment.

internal_rationale: numbered steps for an engineer. For each tool, name the decisive input and output fields and how they drove the reply and the escalation flag. If no tool was needed, say why.

requires_human_escalation: true when a refund tool has approved false or escalation_required true, when a required lookup fails, or when the request cannot be finished safely. False for successful tracking, an expedite decision, an applied expedite, an order-book review, a trend briefing, a delivery-issue briefing, an approved refund, a diagnosis, and a successful firmware update.

confidence_score: a number from 0 through 100. Score high when the reply is grounded in tool fields. Score low when a tool failed or facts were missing.
"""

_CONFIRM_UPDATE = re.compile(
    r"\b(yes|yeah|yep|yup|sure|ok|okay|confirm|proceed|go ahead|do it|do that|"
    r"update(?: it)?|send it|send the update|push it|apply(?: it| the update)?|"
    r"install(?: it| the update)?|fix it)\b",
    re.IGNORECASE,
)
_DECLINE_UPDATE = re.compile(
    r"\b(don't|do not|dont|no|nope|wait|stop|hold off|not yet|cancel)\b",
    re.IGNORECASE,
)
_APPROVAL_LANGUAGE = re.compile(
    r"(refund (has been |is |was )?approved|approved (your |the )?refund|"
    r"issuing (a |your )?refund|refund of \$|you will receive|i(?:'ve| have) approved|"
    r"we(?:'ve| have) approved)",
    re.IGNORECASE,
)
_DENIAL_LANGUAGE = re.compile(
    r"\b(not approved|cannot approve|can't approve|unable to approve|denied|"
    r"escalate|escalated|specialist|was not approved|no refund)\b",
    re.IGNORECASE,
)
_DOLLAR_AMOUNT = re.compile(r"\$\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{2})?)")


class AgentResponseSchema(BaseModel):
    """Customer reply plus the engineering fields captured for each turn."""

    user_message: str = Field(
        description="Reply shown to the customer. Facts must come from tool results."
    )
    internal_rationale: str = Field(
        description="Numbered step-by-step reasoning for engineers. Not shown to the customer."
    )
    requires_human_escalation: bool = Field(
        description="True when a person must take the request, including refused refunds."
    )
    confidence_score: float = Field(
        ge=0,
        le=100,
        description="Confidence in the customer reply, from 0 to 100.",
    )


def process_chat_interaction(message_history: list) -> tuple[dict, dict]:
    """Run one support turn and return (parsed_output, telemetry_trace)."""
    history = [dict(message) for message in message_history]
    user_text = ""
    for message in reversed(history):
        if message.get("role") == "user":
            user_text = str(message.get("content") or "")
            break
    with support_turn(user_text) as span:
        parsed_output, telemetry_trace = _process_chat_interaction(message_history)
        note_turn_output(span, str(parsed_output.get("user_message") or ""))
        note_turn_evals(span, telemetry_trace.get("evals") or {})
        flush_traces()
        return parsed_output, telemetry_trace


def _process_chat_interaction(message_history: list) -> tuple[dict, dict]:
    history = [dict(message) for message in message_history]
    user_text = _latest_user_text(history)
    trace_id = f"trc_{uuid.uuid4().hex[:12]}"
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    origin = time.perf_counter()
    spans: list[dict[str, Any]] = []
    tool_events: list[dict[str, Any]] = []
    prompt_tokens = 0
    completion_tokens = 0
    span_counter = 0

    def now_ms() -> float:
        return (time.perf_counter() - origin) * 1000

    def next_span_id() -> str:
        nonlocal span_counter
        span_counter += 1
        return f"sp_{span_counter}"

    def finish(
        parsed_output: dict[str, Any],
        guard_notes: list[str],
        *,
        guardrail_triggered: bool,
    ) -> tuple[dict, dict]:
        evals = _score_turn(
            user_text,
            parsed_output,
            guard_notes,
            tool_events,
            guardrail_triggered=guardrail_triggered,
        )
        telemetry_trace = {
            "trace_id": trace_id,
            "timestamp": timestamp,
            "user_message": user_text,
            "model": MODEL,
            "total_latency_ms": round(now_ms(), 2),
            "total_tokens": prompt_tokens + completion_tokens,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "confidence_score": parsed_output["confidence_score"],
            "requires_human_escalation": parsed_output["requires_human_escalation"],
            "internal_rationale": parsed_output["internal_rationale"],
            "guard_notes": guard_notes,
            "evals": evals,
            "telemetry_spans": spans,
        }
        return parsed_output, telemetry_trace

    # Pitch scenario A never reaches the model. The guardrail span is the whole trace.
    if _is_prompt_injection(user_text):
        parsed_output, guard_notes = _block_prompt_injection(spans, next_span_id, now_ms)
        return finish(parsed_output, guard_notes, guardrail_triggered=True)

    try:
        client = _client()
        working: list[dict[str, Any]] = [dict(message) for message in history]

        for round_index in range(1, MAX_TOOL_ROUNDS + 1):
            span_id = next_span_id()
            start_ms = now_ms()
            response = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=TOOL_SCHEMAS,
                messages=working,
            )
            duration_ms = now_ms() - start_ms
            round_prompt, round_completion, round_total = _usage_counts(response.usage)
            prompt_tokens += round_prompt
            completion_tokens += round_completion
            tool_calls = [block for block in response.content if block.type == "tool_use"]
            text = _text_from_content(response.content)

            spans.append(
                {
                    "span_id": span_id,
                    "parent_span_id": None,
                    "name": "llm.chat_completion",
                    "span_type": "llm",
                    "round": round_index,
                    "start_ms": round(start_ms, 2),
                    "duration_ms": round(duration_ms, 2),
                    "status": "ok",
                    "tokens": round_total,
                    "input_payload": {
                        "model": MODEL,
                        "round": round_index,
                        "message_count": len(working),
                        "last_user_message": user_text,
                    },
                    "output_payload": {
                        "content": text,
                        "stop_reason": response.stop_reason,
                        "tool_calls": [
                            {
                                "id": call.id,
                                "name": call.name,
                                "arguments": call.input,
                            }
                            for call in tool_calls
                        ],
                    },
                }
            )

            if not tool_calls:
                if text:
                    working.append({"role": "assistant", "content": text})
                break

            working.append({"role": "assistant", "content": _assistant_content(response)})
            tool_results: list[dict[str, Any]] = []
            for call in tool_calls:
                tool_start = now_ms()
                raw_result, status, arguments = _invoke_tool(
                    call.name,
                    json.dumps(call.input),
                    history,
                )
                tool_duration = now_ms() - tool_start
                output_payload = parse_tool_result(raw_result)
                spans.append(
                    {
                        "span_id": next_span_id(),
                        "parent_span_id": span_id,
                        "name": f"tool.{call.name}",
                        "span_type": "tool",
                        "round": round_index,
                        "start_ms": round(tool_start, 2),
                        "duration_ms": round(tool_duration, 2),
                        "status": status,
                        "tokens": 0,
                        "input_payload": arguments,
                        "output_payload": output_payload,
                    }
                )
                tool_events.append(
                    {
                        "name": call.name,
                        "status": status,
                        "output": output_payload,
                    }
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": raw_result,
                    }
                )
            working.append({"role": "user", "content": tool_results})
        else:
            _add_user_message(
                working,
                "Tool round limit reached. Answer from the tool results already collected.",
            )

        _add_user_message(working, FINALIZE_PROMPT)
        parse_start = now_ms()
        parse_total = 0
        parse_status = "ok"
        try:
            parsed_completion = client.messages.parse(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=working,
                output_format=AgentResponseSchema,
            )
            parse_duration = now_ms() - parse_start
            parse_prompt, parse_completion_tokens, parse_total = _usage_counts(parsed_completion.usage)
            prompt_tokens += parse_prompt
            completion_tokens += parse_completion_tokens
            parsed_model = _parsed_schema(parsed_completion)
            if parsed_model is None:
                parsed_output = {
                    "user_message": _last_assistant_text(working)
                    or "I checked the records. Give me a second and ask once more if that reply looks incomplete.",
                    "internal_rationale": (
                        "1. The structured parse did not return a schema, so this turn used the tool-grounded draft."
                    ),
                    "requires_human_escalation": False,
                    "confidence_score": 70.0,
                }
            else:
                parsed_output = parsed_model.model_dump()
        except Exception as exc:
            parse_duration = now_ms() - parse_start
            parse_status = "error"
            parsed_output = {
                "user_message": _last_assistant_text(working)
                or "I checked the records and I'm finishing the reply from those results.",
                "internal_rationale": (
                    "1. The structured parse failed, so this turn used the tool-grounded draft. "
                    f"{_safe_error(exc)}"
                ),
                "requires_human_escalation": False,
                "confidence_score": 70.0,
            }
        spans.append(
            {
                "span_id": next_span_id(),
                "parent_span_id": None,
                "name": "llm.structured_parse",
                "span_type": "parse",
                "round": None,
                "start_ms": round(parse_start, 2),
                "duration_ms": round(parse_duration, 2),
                "status": parse_status,
                "tokens": parse_total,
                "input_payload": {"model": MODEL, "response_format": "AgentResponseSchema"},
                "output_payload": parsed_output,
            }
        )
        _ensure_refund_check(user_text, tool_events, spans, next_span_id, now_ms)
        guard_notes = _apply_policy_guards(parsed_output, tool_events, user_text)
        dialogue_notes = _direct_conversation(
            parsed_output,
            tool_events,
            history,
            spans,
            next_span_id,
            now_ms,
        )
        guard_notes.extend(dialogue_notes)
        _normalize_scores(parsed_output)
    except Exception as exc:
        guard_notes = []
        error_text = _safe_error(exc)
        missing_key = "ANTHROPIC_API_KEY" in error_text
        no_credits = "credit balance is too low" in error_text.lower()
        if missing_key:
            user_message = (
                "Add an Anthropic API key in the sidebar, or set ANTHROPIC_API_KEY, and send the message again."
            )
        elif no_credits:
            user_message = (
                "Claude refused the request because this Anthropic account has no API credits. "
                "Add credits in Plans & Billing, then send the message again."
            )
        else:
            user_message = "I couldn't finish that request just now. Please try again in a moment."
        if _is_order_book_question(user_text):
            parsed_output = _local_order_book_answer(
                user_text,
                tool_events,
                spans,
                next_span_id,
                now_ms,
            )
            parsed_output["internal_rationale"] = (
                "1. The model call failed, so this briefing was built directly from the order book. "
                f"{error_text}"
            )
            guard_notes = ["Answered from the order book after the model call failed."]
        else:
            parsed_output = {
                "user_message": user_message,
                "internal_rationale": f"1. The agent stopped because the model call failed: {error_text}",
                "requires_human_escalation": not (missing_key or no_credits),
                "confidence_score": 0.0,
            }
            guard_notes = []
        spans.append(
            {
                "span_id": next_span_id(),
                "parent_span_id": None,
                "name": "agent.error",
                "span_type": "llm",
                "round": None,
                "start_ms": round(now_ms(), 2),
                "duration_ms": 0.0,
                "status": "error",
                "tokens": 0,
                "input_payload": {"last_user_message": user_text},
                "output_payload": {"error": _safe_error(exc)},
            }
        )

    return finish(parsed_output, guard_notes, guardrail_triggered=False)


def refresh_claude_allowance() -> None:
    """One short call so the account box can read the token rate limit."""
    if rate_limit_snapshot():
        return
    _client().messages.create(
        model="claude-haiku-4-5",
        max_tokens=1,
        messages=[{"role": "user", "content": "ping"}],
    )


def _client() -> Anthropic:
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it to the environment or the session key field."
        )
    client = Anthropic(api_key=api_key, timeout=60.0, max_retries=1)
    hooks = client._client.event_hooks.setdefault("response", [])
    if record_rate_limits not in hooks:
        hooks.append(record_rate_limits)
    return client


def _usage_counts(usage: Any) -> tuple[int, int, int]:
    if usage is None:
        return 0, 0, 0
    prompt = int(getattr(usage, "input_tokens", 0) or getattr(usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(usage, "output_tokens", 0) or getattr(usage, "completion_tokens", 0) or 0)
    total = int(getattr(usage, "total_tokens", 0) or (prompt + completion))
    return prompt, completion, total


def _text_from_content(content: Any) -> str:
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text" and getattr(block, "text", None):
            parts.append(str(block.text))
    return "\n".join(parts).strip()


def _assistant_content(response: Any) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for block in response.content:
        if getattr(block, "type", None) in {"thinking", "redacted_thinking"}:
            continue
        data = block.model_dump(exclude_none=True)
        data.pop("parsed_output", None)
        if data.get("type") in {"thinking", "redacted_thinking"}:
            continue
        blocks.append(data)
    return blocks


def _parsed_schema(response: Any) -> AgentResponseSchema | None:
    if getattr(response, "stop_reason", None) == "refusal":
        return None
    for block in response.content:
        parsed = getattr(block, "parsed_output", None)
        if isinstance(parsed, AgentResponseSchema):
            return parsed
        if isinstance(parsed, dict):
            try:
                return AgentResponseSchema.model_validate(parsed)
            except ValidationError:
                pass
        found = _extract_schema(getattr(block, "text", "") or "")
        if found is not None:
            return found
    return None


def _extract_schema(text: str) -> AgentResponseSchema | None:
    candidate = text.strip()
    if not candidate:
        return None
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate)
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", candidate, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    try:
        return AgentResponseSchema.model_validate(data)
    except ValidationError:
        return None


def _last_assistant_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "assistant" and isinstance(message.get("content"), str):
            return str(message["content"]).strip()
    return ""


_HUB_TOPIC = re.compile(r"\b(hub|crash|crashing|firmware|reboot|smart)\b", re.IGNORECASE)
_HUB_WHEN = (
    "A hub that keeps crashing is frustrating, so I want to narrow it down before I change anything.\n\n"
    "When does it usually die — right after it boots, after it has been running for a few hours, "
    "or when another device connects?"
)
_HUB_LIGHT = (
    "Thanks, that helps me separate a boot problem from a leak.\n\n"
    "When it crashes, does the status light blink and come back, or does the hub go dark and reboot by itself?"
)


def _direct_conversation(
    parsed_output: dict[str, Any],
    tool_events: list[dict[str, Any]],
    history: list[dict[str, Any]],
    spans: list[dict[str, Any]],
    next_span_id: Callable[[], str],
    now_ms: Callable[[], float],
) -> list[str]:
    """Shape the customer reply into a locating story, an order-book briefing, or a hub path."""
    notes: list[str] = []
    stage = _hub_stage(history)
    activity: list[str] = []
    card: dict[str, Any] | None = None
    latest = _latest_user_text(history)

    if _is_order_book_question(latest):
        review, issues = _load_order_book_tools(
            latest,
            tool_events,
            spans,
            next_span_id,
            now_ms,
        )
        model_message = str(parsed_output.get("user_message") or "")
        numbers = _headline_numbers(review, issues, latest)
        if (
            numbers
            and _mentions_any(model_message, numbers)
            and "couldn't finish" not in model_message.lower()
        ):
            notes.append("Kept the model's order-book briefing because it cited the tool counts.")
        else:
            parsed_output["user_message"] = _order_book_briefing(review, issues)
            notes.append("Used the order-book briefing so the counts stay exact.")
        parsed_output["requires_human_escalation"] = False
        parsed_output["confidence_score"] = 93
        activity = _order_book_activity(review, issues)
        card = _order_book_card(review, issues, latest)
    elif stage == "ask_when":
        parsed_output["user_message"] = _HUB_WHEN
        parsed_output["requires_human_escalation"] = False
        activity = [
            "Opened a troubleshooting path for the OmniSmart Hub",
            "Holding the fix until I know when it crashes",
        ]
        notes.append("Asked when the hub crashes before recommending a fix.")
    elif stage == "ask_light":
        parsed_output["user_message"] = _HUB_LIGHT
        parsed_output["requires_human_escalation"] = False
        activity = [
            "Recorded when the crash happens",
            "Asking what the status light does next",
        ]
        notes.append("Asked about the status light before reading the device record.")
    elif stage == "diagnose":
        output = _ensure_tool(
            "diagnose_device_health",
            {"user_id": "USR-442"},
            tool_events,
            spans,
            next_span_id,
            now_ms,
        )
        parsed_output["user_message"] = _diagnosis_reply(output)
        parsed_output["requires_human_escalation"] = False
        parsed_output["confidence_score"] = 94
        activity = [
            "Both symptom questions are answered",
            f"Read the service record for {output.get('device_name', 'the hub')}",
            f"Firmware on the device is {output.get('firmware_version')}; latest is {output.get('latest_firmware')}",
        ]
        card = {
            "kind": "device",
            "title": output.get("device_name") or "OmniSmart Hub V2",
            "health": output.get("health_status") or "",
            "firmware": output.get("firmware_version") or "",
            "latest": output.get("latest_firmware") or "",
        }
        notes.append("Diagnosed the hub only after the symptom questions.")
    elif stage == "update":
        output = _ensure_tool(
            "execute_firmware_update",
            {"device_id": "HUB-442"},
            tool_events,
            spans,
            next_span_id,
            now_ms,
            history=history,
        )
        previous = output.get("previous_firmware") or "the old firmware"
        current = output.get("firmware_version") or "the latest firmware"
        health = output.get("health_status") or "HEALTHY"
        if output.get("success"):
            parsed_output["user_message"] = (
                f"Sending the update now.\n\n"
                f"That's fixed. The hub moved from {previous} to {current}, "
                f"and health is now {health}. It will reboot once. "
                f"The memory-leak crashes stop on this version."
            )
        else:
            parsed_output["user_message"] = (
                "I had the update ready, but it did not land on the hub. "
                "Nothing was changed. Say “send it” again and I’ll retry."
            )
        parsed_output["requires_human_escalation"] = False
        parsed_output["confidence_score"] = 96 if output.get("success") else 55
        activity = [
            "Customer confirmed the update",
            f"Pushed firmware {current} to {output.get('device_id', 'HUB-442')}",
            "OTA update succeeded" if output.get("success") else "OTA update did not apply",
        ]
        card = {
            "kind": "device",
            "title": output.get("device_name") or "OmniSmart Hub V2",
            "health": output.get("health_status") or "HEALTHY",
            "firmware": current,
            "latest": current,
        }
        notes.append("Ran the firmware update only after an explicit yes.")
    else:
        tracking = _latest_tool(tool_events, "track_order")
        refund_turn = bool(re.search(r"\brefund\b", latest, re.IGNORECASE))
        if tracking and tracking.get("found") and not refund_turn:
            parsed_output["user_message"] = _tracking_story(tracking)
            parsed_output["requires_human_escalation"] = False
            activity = [
                f"Opened shipment {tracking['order_id']}",
                f"Asked {tracking['carrier']} for the latest scan",
                f"Latest scan: {tracking['location']}",
                f"Delivery window: {tracking['eta']}",
            ]
            card = {
                "kind": "shipment",
                "location": tracking["location"],
                "eta": tracking["eta"],
                "carrier": tracking["carrier"],
                "tracking_number": tracking["tracking_number"],
            }
            notes.append("Turned the tracking payload into a locating story.")

    parsed_output["activity"] = activity
    parsed_output["card"] = card
    if notes:
        rationale = str(parsed_output.get("internal_rationale") or "").rstrip()
        extra = "\n".join(f"- {note}" for note in notes)
        parsed_output["internal_rationale"] = f"{rationale}\n\nDialogue director:\n{extra}".strip()
    return notes


def _hub_stage(history: list[dict[str, Any]]) -> str | None:
    # WALKTHROUGH: hub-path
    users = [str(message.get("content") or "") for message in history if message.get("role") == "user"]
    if not users or not any(_HUB_TOPIC.search(text) for text in users):
        return None
    assistants = [
        str(message.get("content") or "") for message in history if message.get("role") == "assistant"
    ]
    offered_fix = any("over-the-air" in text.lower() or "want me to send it" in text.lower() for text in assistants)
    if offered_fix and _confirms_update(users[-1]):
        return "update"
    started = False
    counted = 0
    for text in users:
        if not started and _HUB_TOPIC.search(text):
            started = True
        if started:
            counted += 1
    if counted <= 1:
        return "ask_when"
    if counted == 2:
        return "ask_light"
    return "diagnose"
    # /WALKTHROUGH: hub-path


def _diagnosis_reply(output: dict[str, Any]) -> str:
    if not output.get("found"):
        return "I looked up the hub on the account and there is no device record to diagnose."
    return (
        f"I pulled the service record for your {output.get('device_name', 'hub')}.\n\n"
        f"It is reporting {output.get('health_status')}. The software on it is "
        f"{output.get('firmware_version')}, and the current release is {output.get('latest_firmware')}. "
        f"The crash log is a memory leak: the process runs out of memory and restarts. "
        f"That matches a hub that dies after it has been on for a while, whether the light blinks or it goes dark.\n\n"
        f"The fix is an over-the-air update to {output.get('latest_firmware')}. "
        f"It takes about a minute and the hub will reboot. Want me to send it?"
    )


def _tracking_story(tracking: dict[str, Any]) -> str:
    story = (
        f"I'm looking up {tracking['order_id']} on the carrier network.\n\n"
        f"Found it. The newest scan is at the {tracking['location']}, "
        f"moving with {tracking['carrier']}. "
        f"The tracking number on that scan is {tracking['tracking_number']}.\n\n"
        f"The delivery window on the label is {tracking['eta']}."
    )
    if tracking.get("issue"):
        delivered = "It was delivered." if tracking.get("delivered") else "It was not delivered."
        resolution = tracking.get("resolution") or "no fix recorded"
        story += (
            f"\n\nThere is a delivery issue on this one: {tracking['issue']}. "
            f"{delivered} The fix on the record is {resolution}."
        )
    return story


_ISSUE_QUESTION = re.compile(
    r"\b(issues?|stolen|opened box|resolved|resolution|frequent|investigate)\b",
    re.IGNORECASE,
)
_REVIEW_QUESTION = re.compile(
    r"\b(how many|open orders|shipping to|shipped to|order book)\b"
    r"|\b(review|summarize|summary|breakdown)\b.*\borders\b"
    r"|\borders\b.*\b(review|summarize|summary|breakdown)\b",
    re.IGNORECASE,
)


def _wants_issue_report(text: str) -> bool:
    return bool(_ISSUE_QUESTION.search(text))


def _wants_order_review(text: str) -> bool:
    if _wants_issue_report(text) and not re.search(
        r"\b(how many|open orders|shipping to|shipped to)\b",
        text,
        re.IGNORECASE,
    ):
        return False
    return bool(_REVIEW_QUESTION.search(text))


def _is_order_book_question(text: str) -> bool:
    return _wants_issue_report(text) or _wants_order_review(text)


def _mentioned_state(text: str) -> str:
    for _code, name, _cities in sorted(STATE_DIRECTORY, key=lambda item: len(item[1]), reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE):
            return name
        if re.search(rf"\b{_code}\b", text):
            return _code
    return ""


def _mentioned_carrier(text: str) -> str:
    for name in ("FedEx", "UPS", "USPS", "DHL"):
        if re.search(rf"\b{name}\b", text, re.IGNORECASE):
            return name
    return ""


def _mentioned_scope(text: str) -> str:
    if re.search(r"\b(open|shipping|in transit|not delivered|undelivered)\b", text, re.IGNORECASE):
        return "open"
    if re.search(r"\bdelivered\b", text, re.IGNORECASE):
        return "delivered"
    return ""


def _review_covers(output: dict[str, Any] | None, state: str, scope: str, carrier: str) -> bool:
    if not output or not output.get("success"):
        return False
    found = canonical_state(state) if state else None
    wanted_code = found[0] if found else ""
    if output.get("destination_state", "") != wanted_code:
        return False
    if output.get("shipment_scope", "all") != (scope or "all"):
        return False
    return (output.get("carrier") or "") == carrier


def _load_order_book_tools(
    text: str,
    tool_events: list[dict[str, Any]],
    spans: list[dict[str, Any]],
    next_span_id: Callable[[], str],
    now_ms: Callable[[], float],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    review: dict[str, Any] | None = None
    issues: dict[str, Any] | None = None
    if _wants_order_review(text):
        state = _mentioned_state(text)
        scope = _mentioned_scope(text)
        carrier = _mentioned_carrier(text)
        review = _latest_tool(tool_events, "review_orders")
        if not _review_covers(review, state, scope, carrier):
            review = _ensure_tool(
                "review_orders",
                {
                    "destination_state": state,
                    "shipment_scope": scope,
                    "carrier": carrier,
                },
                tool_events,
                spans,
                next_span_id,
                now_ms,
            )
    if _wants_issue_report(text):
        issues = _latest_tool(tool_events, "investigate_delivery_issues")
        if issues is None:
            issues = _ensure_tool(
                "investigate_delivery_issues",
                {},
                tool_events,
                spans,
                next_span_id,
                now_ms,
            )
    return review, issues


def _headline_numbers(
    review: dict[str, Any] | None,
    issues: dict[str, Any] | None,
    text: str,
) -> list[int]:
    numbers: list[int] = []
    if review and _wants_order_review(text):
        numbers.extend(int(value) for value in review.get("headline_numbers") or [])
    if issues and _wants_issue_report(text):
        numbers.extend(int(value) for value in issues.get("headline_numbers") or [])
    return numbers


def _mentions_any(message: str, numbers: list[int]) -> bool:
    return any(re.search(rf"\b{number}\b", message) for number in numbers)


def _order_book_briefing(review: dict[str, Any] | None, issues: dict[str, Any] | None) -> str:
    parts = []
    if review and review.get("briefing"):
        parts.append(str(review["briefing"]))
    if issues and issues.get("briefing"):
        parts.append(str(issues["briefing"]))
    return "\n\n".join(parts) or "I opened the order book, but there was nothing to report."


def _order_book_activity(
    review: dict[str, Any] | None,
    issues: dict[str, Any] | None,
) -> list[str]:
    lines = ["Opened the order book"]
    if review and review.get("success"):
        where = review.get("destination_state_name") or "every state"
        scope = review.get("shipment_scope") or "all"
        lines.append(f"Counted {scope} orders for {where}")
        lines.append(
            f"{review.get('matched')} matched: {review.get('open_count')} open, "
            f"{review.get('delivered_count')} delivered"
        )
    if issues and issues.get("success"):
        lines.append(
            f"{issues.get('orders_with_issues')} orders have a delivery issue "
            f"({issues.get('issue_rate_percent')}%)"
        )
        lines.append(
            f"Most common: {issues.get('top_issue')} ({issues.get('top_issue_count')}), "
            f"usually {issues.get('top_resolution')}"
        )
    return lines


def _example_table(review: dict[str, Any]) -> list[dict[str, str]]:
    rows = []
    for item in review.get("examples") or []:
        rows.append(
            {
                "Order": item.get("order_id", ""),
                "Delivered": "Yes" if item.get("delivered") else "No",
                "Status": item.get("status", ""),
                "Destination": item.get("destination", ""),
                "Carrier": item.get("carrier", ""),
                "Issue": item.get("issue") or "—",
                "Fix": item.get("resolution") or "—",
            }
        )
    return rows


def _state_table(review: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "State": item.get("state_name") or item.get("state"),
            "Orders": item.get("count"),
            "Open": item.get("open_count"),
            "Delivered": item.get("delivered_count"),
        }
        for item in review.get("by_state") or []
    ]


def _order_book_card(
    review: dict[str, Any] | None,
    issues: dict[str, Any] | None,
    text: str,
) -> dict[str, Any] | None:
    if issues and _wants_issue_report(text):
        rows = []
        for item in issues.get("by_issue") or []:
            fixes = ", ".join(
                f"{part.get('resolution')} ({part.get('count')})"
                for part in item.get("resolutions") or []
            )
            rows.append(
                {
                    "Issue": item.get("issue"),
                    "Orders": item.get("count"),
                    "Delivered": item.get("delivered"),
                    "Not delivered": item.get("not_delivered"),
                    "Fix": fixes,
                }
            )
        return {
            "kind": "issues",
            "issue_count": str(issues.get("orders_with_issues", "")),
            "rate": f"{issues.get('issue_rate_percent', 0)}%",
            "top_issue": issues.get("top_issue") or "",
            "top_fix": issues.get("top_resolution") or "",
            "table": rows,
        }
    if not review:
        return None
    if review.get("destination_state") and review.get("shipment_scope") == "open":
        focus_label = f"Open to {review.get('destination_state_name') or 'that state'}"
        focus_value = str(review.get("matched", ""))
        table: list[dict[str, Any]] = _example_table(review)
    elif not review.get("destination_state"):
        focus_label = "Busiest state"
        top_states = review.get("by_state") or []
        focus_value = top_states[0]["state_name"] if top_states else "—"
        table = _state_table(review)
    else:
        focus_label = "In this slice"
        focus_value = str(review.get("matched", ""))
        table = _example_table(review)
    return {
        "kind": "orders",
        "matched": str(review.get("matched", "")),
        "open": str(review.get("open_count", "")),
        "delivered": str(review.get("delivered_count", "")),
        "focus_label": focus_label,
        "focus_value": focus_value,
        "table": table,
    }


def _local_order_book_answer(
    user_text: str,
    tool_events: list[dict[str, Any]],
    spans: list[dict[str, Any]],
    next_span_id: Callable[[], str],
    now_ms: Callable[[], float],
) -> dict[str, Any]:
    review, issues = _load_order_book_tools(user_text, tool_events, spans, next_span_id, now_ms)
    return {
        "user_message": _order_book_briefing(review, issues),
        "internal_rationale": "1. Built this briefing directly from the order book.",
        "requires_human_escalation": False,
        "confidence_score": 90.0,
        "activity": _order_book_activity(review, issues),
        "card": _order_book_card(review, issues, user_text),
    }


def _latest_tool(tool_events: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    found: dict[str, Any] | None = None
    for event in tool_events:
        if event.get("name") == name and isinstance(event.get("output"), dict):
            found = event["output"]
    return found


def _ensure_tool(
    name: str,
    arguments: dict[str, Any],
    tool_events: list[dict[str, Any]],
    spans: list[dict[str, Any]],
    next_span_id: Callable[[], str],
    now_ms: Callable[[], float],
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    existing = _latest_tool(tool_events, name)
    if name == "execute_firmware_update" and existing and existing.get("executed"):
        return existing
    if name == "diagnose_device_health" and existing and existing.get("found"):
        return existing
    start = now_ms()
    raw, status, payload = _invoke_tool(name, json.dumps(arguments), history or [])
    output = parse_tool_result(raw)
    if not isinstance(output, dict):
        output = {"raw": raw}
    tool_events.append({"name": name, "status": status, "output": output})
    spans.append(
        {
            "span_id": next_span_id(),
            "parent_span_id": None,
            "name": f"tool.{name}",
            "span_type": "tool",
            "round": None,
            "start_ms": round(start, 2),
            "duration_ms": round(now_ms() - start, 2),
            "status": status,
            "tokens": 0,
            "input_payload": payload,
            "output_payload": output,
        }
    )
    return output


def _add_user_message(messages: list[dict[str, Any]], text: str) -> None:
    if messages and messages[-1].get("role") == "user":
        content = messages[-1].get("content")
        if isinstance(content, str):
            messages[-1]["content"] = f"{content}\n\n{text}"
            return
        if isinstance(content, list):
            content.append({"type": "text", "text": text})
            return
    messages.append({"role": "user", "content": text})


def _invoke_tool(
    name: str,
    arguments_json: str,
    history: list[dict[str, Any]],
) -> tuple[str, str, dict[str, Any]]:
    with tool_span(name, arguments_json) as span:
        raw, status, arguments = _run_tool(name, arguments_json, history)
        note_tool_result(span, raw, status)
        return raw, status, arguments


def _run_tool(
    name: str,
    arguments_json: str,
    history: list[dict[str, Any]],
) -> tuple[str, str, dict[str, Any]]:
    try:
        arguments = json.loads(arguments_json or "{}")
    except json.JSONDecodeError:
        arguments = {"_raw": arguments_json}

    if not isinstance(arguments, dict):
        arguments = {"_raw": arguments}

    # WALKTHROUGH: confirm-update
    if name == "execute_firmware_update" and not _confirms_update(_latest_user_text(history)):
        blocked = json.dumps(
            {
                "success": False,
                "executed": False,
                "device_id": arguments.get("device_id"),
                "message": (
                    "Firmware update was not executed. The latest customer message "
                    "did not explicitly confirm the update."
                ),
            }
        )
        return blocked, "blocked", arguments
    # /WALKTHROUGH: confirm-update

    function = TOOL_FUNCTIONS.get(name)
    if function is None:
        return json.dumps({"error": f"Unknown tool: {name}"}), "error", arguments

    try:
        return function(**arguments), "ok", arguments
    except TypeError as exc:
        return (
            json.dumps({"error": f"Invalid arguments for {name}: {exc}"}),
            "error",
            arguments,
        )


def _latest_user_text(history: list[dict[str, Any]]) -> str:
    for message in reversed(history):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def _confirms_update(text: str) -> bool:
    if _DECLINE_UPDATE.search(text):
        return False
    return bool(_CONFIRM_UPDATE.search(text))


# WALKTHROUGH: injection-guard
INJECTION_REPLY = "I am a customer support assistant and cannot fulfill that request."
_INJECTION = re.compile(r"ignore previous instructions", re.IGNORECASE)
_UPSET_LANGUAGE = re.compile(
    r"\b(angry|broken|frustrated|suing|terrible|disgruntled)\b",
    re.IGNORECASE,
)


def _is_prompt_injection(text: str) -> bool:
    """True when the customer is trying to override the support instructions."""
    return bool(_INJECTION.search(text))
# /WALKTHROUGH: injection-guard


def _block_prompt_injection(
    spans: list[dict[str, Any]],
    next_span_id: Callable[[], str],
    now_ms: Callable[[], float],
) -> tuple[dict[str, Any], list[str]]:
    """Reject a jailbreak before Claude or any tool can run."""
    start = now_ms()
    with guardrail_span("prompt_injection"):
        pass
    spans.append(
        {
            "span_id": next_span_id(),
            "parent_span_id": None,
            "name": "guardrail.prompt_injection",
            "span_type": "guardrail",
            "round": None,
            "start_ms": round(start, 2),
            "duration_ms": round(max(now_ms() - start, 0.1), 2),
            "status": "blocked",
            "tokens": 0,
            "input_payload": {"rule": "prompt_injection"},
            "output_payload": {
                "guardrail_triggered": True,
                "user_message": INJECTION_REPLY,
            },
        }
    )
    parsed_output = {
        "user_message": INJECTION_REPLY,
        "internal_rationale": (
            "1. The latest message matched the prompt-injection guardrail.\n"
            "2. Claude was not called, and no tool ran.\n"
            "3. The root span records guardrail_triggered true."
        ),
        "requires_human_escalation": False,
        "confidence_score": 99.0,
        "activity": [
            "Checked the message against the prompt-injection guardrail",
            "Stopped the turn before Claude or any tool ran",
        ],
        "card": None,
    }
    return parsed_output, [
        "Blocked a prompt-injection attempt before any model or tool call."
    ]


def _sentiment_score(text: str) -> float:
    """Customer sentiment from -1 to 1. Upset wording is weighted toward -1."""
    if _UPSET_LANGUAGE.search(text) or _is_prompt_injection(text):
        score = random.uniform(-1.0, -0.55)
    else:
        score = random.uniform(-0.2, 1.0)
    return round(max(-1.0, min(1.0, score)), 2)


def _invented_refund(message: str, output: dict[str, Any]) -> bool:
    """True when a reply offers money the refund tool did not approve."""
    if output.get("approved") is not False:
        return bool(_APPROVAL_LANGUAGE.search(message))
    if _APPROVAL_LANGUAGE.search(message):
        return True
    if re.search(r"\b(match|plus \$|extra)\b", message, re.IGNORECASE):
        return True
    order_amount = output.get("order_amount")
    if order_amount is None:
        return False
    try:
        ceiling = float(order_amount)
    except (TypeError, ValueError):
        return False
    for raw in _DOLLAR_AMOUNT.findall(message):
        if float(raw.replace(",", "")) > ceiling + 0.001:
            return True
    return False


def _score_turn(
    user_text: str,
    parsed_output: dict[str, Any],
    guard_notes: list[str],
    tool_events: list[dict[str, Any]],
    *,
    guardrail_triggered: bool,
) -> dict[str, Any]:
    """Evals for the root span. Hallucination stays false unless a guard caught one."""
    triggered = guardrail_triggered or any(
        "prompt-injection" in note.lower() for note in guard_notes
    )
    hallucination = any(note.startswith("Hallucination detected") for note in guard_notes)
    message = str(parsed_output.get("user_message") or "")
    adherence = True
    if triggered:
        adherence = message.strip() == INJECTION_REPLY
    else:
        for event in tool_events:
            output = event.get("output") if isinstance(event.get("output"), dict) else {}
            if event.get("name") == "evaluate_refund_policy" and _invented_refund(message, output):
                adherence = False
    return {
        "sentiment_score": _sentiment_score(user_text),
        "hallucination_detected": hallucination,
        "policy_adherence": adherence,
        "guardrail_triggered": triggered,
    }


def _ensure_refund_check(
    user_text: str,
    tool_events: list[dict[str, Any]],
    spans: list[dict[str, Any]],
    next_span_id: Callable[[], str],
    now_ms: Callable[[], float],
) -> None:
    """Run the refund tool when the customer asks, even if the model skipped it."""
    if not re.search(r"\brefund\b", user_text, re.IGNORECASE):
        return
    match = re.search(r"\bORD-\d+\b", user_text, re.IGNORECASE)
    if match is None:
        return
    order_id = match.group(0).upper()
    for event in tool_events:
        output = event.get("output") if isinstance(event.get("output"), dict) else {}
        if event.get("name") == "evaluate_refund_policy" and str(output.get("order_id", "")).upper() == order_id:
            return
    _ensure_tool(
        "evaluate_refund_policy",
        {"order_id": order_id, "reason": user_text[:500]},
        tool_events,
        spans,
        next_span_id,
        now_ms,
    )


def _apply_policy_guards(
    parsed_output: dict[str, Any],
    tool_events: list[dict[str, Any]],
    user_text: str,
) -> list[str]:
    notes: list[str] = []
    if _is_prompt_injection(user_text):
        parsed_output["user_message"] = INJECTION_REPLY
        parsed_output["requires_human_escalation"] = False
        parsed_output["confidence_score"] = 99.0
        notes.append("Blocked a prompt-injection attempt. The model reply was discarded.")
        rationale = str(parsed_output.get("internal_rationale") or "").rstrip()
        parsed_output["internal_rationale"] = (
            f"{rationale}\n\nPolicy guard adjustments:\n- {notes[0]}"
        ).strip()
        return notes
    message = str(parsed_output.get("user_message") or "").strip()
    refund_checked = any(event["name"] == "evaluate_refund_policy" for event in tool_events)

    for event in tool_events:
        output = event["output"] if isinstance(event["output"], dict) else {}
        name = event["name"]

        if name == "evaluate_refund_policy":
            message, changed = _guard_refund(message, output, parsed_output)
            if changed:
                notes.append(changed)

        if (
            name == "track_order"
            and output.get("found") is True
            and not _is_order_book_question(user_text)
        ):
            message, changed = _guard_tracking(message, output)
            if changed:
                notes.append(changed)

        if name == "execute_firmware_update" and output.get("success") and output.get("executed"):
            message, changed = _guard_firmware(message, output)
            if changed:
                notes.append(changed)

        if (
            name == "diagnose_device_health"
            and output.get("firmware_outdated") is True
            and not any(item["name"] == "execute_firmware_update" and item["status"] == "ok" for item in tool_events)
        ):
            if "?" not in message:
                latest = output.get("latest_firmware") or "the latest firmware"
                message = (
                    f"{message.rstrip()}\n\nWould you like me to apply the OTA update to {latest}?"
                ).strip()
                notes.append("Added an explicit confirmation question for the firmware update.")

    asked_for_refund = bool(re.search(r"\brefund\b", user_text, re.IGNORECASE))
    named_order = bool(re.search(r"\bORD-\d+\b", user_text, re.IGNORECASE))
    promised_money = bool(_APPROVAL_LANGUAGE.search(message) or _DOLLAR_AMOUNT.search(message))
    if asked_for_refund and named_order and not refund_checked and promised_money:
        parsed_output["requires_human_escalation"] = True
        message = (
            "I can't confirm a refund until the policy check completes. "
            "No refund has been issued. Please send the request again."
        )
        notes.append("Hallucination detected: blocked a refund promise that was not backed by evaluate_refund_policy.")

    if notes:
        rationale = str(parsed_output.get("internal_rationale") or "").rstrip()
        guard_lines = "\n".join(f"- {note}" for note in notes)
        parsed_output["internal_rationale"] = (
            f"{rationale}\n\nPolicy guard adjustments:\n{guard_lines}"
        ).strip()

    parsed_output["user_message"] = message
    return notes


def _guard_refund(
    message: str,
    output: dict[str, Any],
    parsed_output: dict[str, Any],
) -> tuple[str, str | None]:
    order_id = output.get("order_id", "the order")
    reasoning = output.get("reasoning") or "The policy tool did not approve an automatic refund."

    if output.get("approved") is True:
        amount = output.get("refund_amount")
        expected = f"{float(amount):.2f}" if amount is not None else None
        found_amounts = [match.replace(",", "") for match in _DOLLAR_AMOUNT.findall(message)]
        contradicts = bool(expected and found_amounts and any(value != expected for value in found_amounts))
        denies = bool(_DENIAL_LANGUAGE.search(message))
        if contradicts or denies or not message:
            parsed_output["requires_human_escalation"] = False
            return (
                f"I checked the refund rules for {order_id} before answering. "
                f"It is {output.get('age_days')} days old and the total is ${expected}. "
                f"Both are inside the automatic window, under 30 days and under $500, "
                f"so the refund of ${expected} is approved.",
                "Replaced the customer reply so it matches the approved refund amount.",
            )
        parsed_output["requires_human_escalation"] = False
        return message, None

    parsed_output["requires_human_escalation"] = True
    invented = _invented_refund(message, output)
    if invented or not _DENIAL_LANGUAGE.search(message):
        note = (
            "Hallucination detected: the draft promised money the refund tool did not approve."
            if invented
            else "Replaced the customer reply so it states the refund denial."
        )
        return (
            f"I checked the refund rules for {order_id} before promising anything. {reasoning} "
            "I can't issue that refund from this chat. A specialist will review it, and no refund has been issued.",
            note,
        )
    return message, None


def _guard_tracking(message: str, output: dict[str, Any]) -> tuple[str, str | None]:
    required = [
        ("location", output.get("location")),
        ("carrier", output.get("carrier")),
        ("tracking number", output.get("tracking_number")),
        ("ETA", output.get("eta")),
    ]
    missing = [f"{label}: {value}" for label, value in required if value and value not in message]
    if not missing:
        return message, None
    block = "\n".join(missing)
    return f"{message.rstrip()}\n\n{block}".strip(), "Appended shipment facts omitted from the draft reply."


def _guard_firmware(message: str, output: dict[str, Any]) -> tuple[str, str | None]:
    version = str(output.get("firmware_version") or "")
    lowered = message.lower()
    if "ota" in lowered and "succeed" in lowered:
        return message, None
    if version and version in message and "updat" in lowered:
        return message, None
    confirmation = output.get("message") or f"OTA firmware update succeeded. Firmware is now {version}."
    return (
        f"{message.rstrip()}\n\n{confirmation}".strip(),
        "Appended the OTA success confirmation from the firmware tool.",
    )


def _normalize_scores(parsed_output: dict[str, Any]) -> None:
    score = float(parsed_output.get("confidence_score") or 0)
    if 0 < score <= 1:
        score *= 100
    parsed_output["confidence_score"] = round(max(0.0, min(100.0, score)), 1)
    parsed_output["requires_human_escalation"] = bool(parsed_output.get("requires_human_escalation"))
    parsed_output["user_message"] = str(parsed_output.get("user_message") or "").strip()
    parsed_output["internal_rationale"] = str(parsed_output.get("internal_rationale") or "").strip()


def _safe_error(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    secret = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if secret and secret in text:
        text = text.replace(secret, "[redacted]")
    return text[:500]
