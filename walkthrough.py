"""Presenter notes for the in-app code walkthrough."""

from __future__ import annotations

import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent

CHAPTERS: list[dict[str, str]] = [
    {
        "id": "layout",
        "title": "1. Why the app is split into four files",
        "say": (
            "I kept the model, the tools, the data, and the screen apart. "
            "If a refund rule changes, I edit the tool, not the chat UI."
        ),
        "why": (
            "The assignment asked for a small agent with native telemetry, not a tracing library. "
            "Splitting the files makes that boundary obvious when I present it: mock_db is the world, "
            "tools are the only way to read it, agent.py decides and records spans, and app.py only renders."
        ),
        "file": "app.py",
        "region": "",
        "code": '''CUSTOMER_PORTAL = "💬 Customer Portal"
TELEMETRY_ROOM = "📊 Telemetry Control Room"
CODE_WALKTHROUGH = "🧭 Code Walkthrough"

view = st.selectbox(
    "⚙️ View Mode",
    [CUSTOMER_PORTAL, TELEMETRY_ROOM, CODE_WALKTHROUGH],
)''',
    },
    {
        "id": "orders",
        "title": "2. The two orders are a test, not filler",
        "say": (
            "ORD-101 is young and cheap, so the policy approves it. "
            "ORD-202 is old and expensive, so the same function must refuse it."
        ),
        "why": (
            "I fixed the age and the price in the mock data so the demo cannot drift. "
            "If those numbers were 'about a month ago', a live date check would eventually flip the answer."
        ),
        "file": "mock_db.py",
        "region": "orders",
        "code": "",
    },
    {
        "id": "refund",
        "title": "3. The refund rule is code, not a prompt",
        "say": (
            "The model is not allowed to decide the money. "
            "Both conditions have to be true: under 30 days and under $500. Either miss and it escalates."
        ),
        "why": (
            "A prompt that says 'be careful with refunds' still hallucinates. "
            "Putting the comparison in evaluate_refund_policy means the chat can only report the tool's decision."
        ),
        "file": "tools.py",
        "region": "refund-rule",
        "code": "",
    },
    {
        "id": "confirm",
        "title": "4. The hub cannot be updated by accident",
        "say": (
            "Saying the hub is crashing is not a yes. "
            "execute_firmware_update is blocked until the latest customer message is an explicit confirmation."
        ),
        "why": (
            "The troubleshooting path asks questions first. "
            "The gate is what stops the model from applying the fix on the first turn, even if it calls the tool early."
        ),
        "file": "agent.py",
        "region": "confirm-update",
        "code": "",
    },
    {
        "id": "hub",
        "title": "5. Hub help is a scripted path, then a fix",
        "say": (
            "Turn one asks when it crashes. Turn two asks what the light does. "
            "Only the third turn reads the device record and offers the firmware update."
        ),
        "why": (
            "If I let the model jump straight to 'install v2.1.0', the demo looks like a lookup, not support. "
            "The dialogue director counts customer turns in the hub thread and overwrites the reply so the path stays stable."
        ),
        "file": "agent.py",
        "region": "hub-path",
        "code": "",
    },
    {
        "id": "telemetry",
        "title": "6. Two traces: this page, and Arize",
        "say": (
            "Every model call and tool call is still a span dict on this page, with the input and output JSON. "
            "The same turn is exported to Arize AX as an agent span, the Claude calls, and each tool."
        ),
        "why": (
            "The control room has to show the exact JSON without logging into another product. "
            "Arize is where I open the LLM span, the token counts, and the tool tree when I demo observability."
        ),
        "file": "agent.py",
        "region": "",
        "code": '''spans.append({
    "span_id": next_span_id(),
    "name": f"tool.{call.name}",
    "span_type": "tool",
    "start_ms": round(tool_start, 2),
    "duration_ms": round(tool_duration, 2),
    "input_payload": arguments,
    "output_payload": output_payload,
})''',
    },
    {
        "id": "agent-graph",
        "title": "9. Arize draws the agent from these node ids",
        "say": (
            "The support span is the root. Claude and each tool point back to it with "
            "graph.node.parent_id. That is the metadata the agent graph in Arize reads."
        ),
        "why": (
            "Anthropic's instrumentor records the model call, but it does not know this app's graph. "
            "Arize builds the path from graph.node.id and graph.node.parent_id on the spans we care about."
        ),
        "file": "tracing.py",
        "region": "agent-graph",
        "code": "",
    },
    {
        "id": "voice",
        "title": "7. The locating story is rendered from the tool",
        "say": (
            "After track_order returns, I don't hope the model remembers the city. "
            "The reply is built from location, carrier, tracking number, and ETA."
        ),
        "why": (
            "The structured-output call sometimes comes back empty. "
            "Falling back to the tool payload means the customer still hears Oakland and the delivery window."
        ),
        "file": "agent.py",
        "region": "",
        "code": '''def _tracking_story(tracking: dict) -> str:
    return (
        f"I'm looking up {tracking['order_id']} on the carrier network.\\n\\n"
        f"Found it. The newest scan is at the {tracking['location']}, "
        f"moving with {tracking['carrier']}. "
        f"The tracking number on that scan is {tracking['tracking_number']}.\\n\\n"
        f"The delivery window on the label is {tracking['eta']}."
    )''',
    },
    {
        "id": "order-book",
        "title": "8. The model explains the book. It does not invent it.",
        "say": (
            "There are 5,000 orders. Every one is flagged delivered or not. "
            "750 of them, 15 percent, have an issue and a fix. "
            "The chat calls review_orders or investigate_delivery_issues, then explains those counts in its own words."
        ),
        "why": (
            "I froze the quotas in the generator so California and stolen-package stay the same every time I present. "
            "The model is good at the briefing and bad at arithmetic, so the counts come from the tool."
        ),
        "file": "mock_db.py",
        "region": "order-book",
        "code": "",
    },
    {
        "id": "evals",
        "title": "10. Evals and the injection guardrail",
        "say": (
            "Every turn writes sentiment, hallucination, and policy adherence onto the root span. "
            "The injection button never reaches Claude. The refund bait still has to pass the $500 and 30-day rule."
        ),
        "why": (
            "Arize is the place to show quality over time, not only that a call succeeded. "
            "The guardrail span is the failing trace. The refund tool is the trace that shows the policy holding."
        ),
        "file": "agent.py",
        "region": "injection-guard",
        "code": "",
    },
]


def region_source(file_name: str, region: str) -> str:
    """Return the source between a pair of WALKTHROUGH markers."""
    text = (ROOT / file_name).read_text()
    start = f"# WALKTHROUGH: {region}"
    end = f"# /WALKTHROUGH: {region}"
    begin = text.find(start)
    finish = text.find(end)
    if begin == -1 or finish == -1 or finish < begin:
        return f"# Could not find region {region} in {file_name}"
    body = text[begin + len(start) : finish]
    return textwrap.dedent(body).strip("\n")


def chapter_code(chapter: dict[str, str]) -> str:
    if chapter["region"]:
        return region_source(chapter["file"], chapter["region"])
    return chapter["code"]
