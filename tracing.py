"""Arize AX export for OmniServe turns.

Local span dicts still drive the control room. This module sends a parallel
OpenTelemetry trace: one agent span, each Claude call, and each tool call.
Agent graph attributes are what Arize AX uses to draw the path.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

from dotenv import load_dotenv
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.trace import Status, StatusCode
from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes

load_dotenv()

PROJECT_NAME = os.getenv("ARIZE_PROJECT_NAME", "omniserve-support").strip() or "omniserve-support"
SUPPORT_NODE_ID = "support_agent"
CLAUDE_NODE_ID = "claude"

_STATE = ""
_DETAIL = ""


def init_arize() -> str:
    """Register Arize and instrument Anthropic. Call this before importing anthropic."""
    global _STATE, _DETAIL
    if _STATE:
        return _STATE

    space_id = os.getenv("ARIZE_SPACE_ID", "").strip()
    api_key = os.getenv("ARIZE_API_KEY", "").strip()
    if not space_id or not api_key:
        _STATE = "missing"
        _DETAIL = "Add ARIZE_SPACE_ID and ARIZE_API_KEY to .env, then restart the app."
        return _STATE

    try:
        from arize.otel import register
        from openinference.instrumentation.anthropic import AnthropicInstrumentor

        provider = register(
            space_id=space_id,
            api_key=api_key,
            project_name=PROJECT_NAME,
            batch=False,
            verbose=False,
        )
        provider.add_span_processor(_ClaudeGraphProcessor())
        AnthropicInstrumentor().instrument(tracer_provider=provider)
        _STATE = "ready"
        _DETAIL = ""
    except Exception as exc:
        _STATE = "error"
        text = str(exc)
        if api_key and api_key in text:
            text = text.replace(api_key, "****")
        _DETAIL = text[:300]
    return _STATE


def tracing_status() -> dict[str, str]:
    if not _STATE:
        init_arize()
    return {"state": _STATE, "project": PROJECT_NAME, "detail": _DETAIL}


def flush_traces() -> None:
    if _STATE != "ready":
        return
    provider = trace.get_tracer_provider()
    force_flush = getattr(provider, "force_flush", None)
    if callable(force_flush):
        force_flush(timeout_millis=5000)


# WALKTHROUGH: agent-graph
def annotate_agent_node(
    span: Any,
    node_id: str,
    display_name: str,
    *,
    parent_id: str = "",
    agent_name: str = "",
) -> None:
    """Mark a span so Arize can draw it on the agent graph."""
    if span is None:
        return
    span.set_attribute(SpanAttributes.GRAPH_NODE_ID, node_id)
    span.set_attribute(SpanAttributes.GRAPH_NODE_NAME, display_name)
    span.set_attribute("graph.node.display_name", display_name)
    if parent_id:
        span.set_attribute(SpanAttributes.GRAPH_NODE_PARENT_ID, parent_id)
    if agent_name:
        span.set_attribute(SpanAttributes.AGENT_NAME, agent_name)
# /WALKTHROUGH: agent-graph


class _ClaudeGraphProcessor(SpanProcessor):
    """Label auto-instrumented Claude spans that happen inside a support turn."""

    def on_start(self, span: Any, parent_context: Context | None = None) -> None:
        name = str(getattr(span, "name", "") or "")
        if not (name.startswith("messages.") or name.startswith("beta.messages.")):
            return
        parent = trace.get_current_span(parent_context)
        if str(getattr(parent, "name", "") or "") != "omniserve.support_turn":
            return
        annotate_agent_node(span, CLAUDE_NODE_ID, "Claude", parent_id=SUPPORT_NODE_ID)

    def on_end(self, span: Any) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


@contextmanager
def support_turn(user_text: str) -> Iterator[Any]:
    if _STATE != "ready":
        yield None
        return
    tracer = trace.get_tracer("omniserve")
    with tracer.start_as_current_span("omniserve.support_turn") as span:
        span.set_attribute(
            SpanAttributes.OPENINFERENCE_SPAN_KIND,
            OpenInferenceSpanKindValues.AGENT.value,
        )
        annotate_agent_node(
            span,
            SUPPORT_NODE_ID,
            "Support agent",
            agent_name="OmniServe",
        )
        span.set_attribute(SpanAttributes.INPUT_VALUE, user_text[:8000])
        span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "text/plain")
        yield span


def note_turn_output(span: Any, text: str) -> None:
    if span is None:
        return
    span.set_attribute(SpanAttributes.OUTPUT_VALUE, text[:8000])
    span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "text/plain")


def note_turn_evals(span: Any, evals: dict[str, Any]) -> None:
    """Attach quality evals to the root span before it is flushed to Arize.

    The flat names are the ones the pitch quotes. The nested eval.<name>.score
    and eval.<name>.label attributes are what Arize groups into eval dashboards.
    """
    if span is None or not evals:
        return
    sentiment = float(evals.get("sentiment_score") or 0.0)
    hallucination = bool(evals.get("hallucination_detected"))
    adherence = bool(evals.get("policy_adherence"))
    guardrail = bool(evals.get("guardrail_triggered"))
    sentiment_label = "negative" if sentiment < 0 else "positive"
    span.set_attribute("eval.sentiment_score", sentiment)
    span.set_attribute("eval.hallucination_detected", hallucination)
    span.set_attribute("eval.policy_adherence", adherence)
    span.set_attribute("guardrail_triggered", guardrail)
    span.set_attribute("eval.sentiment.score", sentiment)
    span.set_attribute("eval.sentiment.label", sentiment_label)
    span.set_attribute("eval.hallucination.score", 1.0 if hallucination else 0.0)
    span.set_attribute("eval.hallucination.label", "hallucinated" if hallucination else "factual")
    span.set_attribute("eval.policy_adherence.score", 1.0 if adherence else 0.0)
    span.set_attribute("eval.policy_adherence.label", "pass" if adherence else "fail")
    if guardrail:
        span.set_status(Status(StatusCode.ERROR, "Guardrail triggered"))


@contextmanager
def tool_span(name: str, arguments_json: str) -> Iterator[Any]:
    if _STATE != "ready":
        yield None
        return
    tracer = trace.get_tracer("omniserve.tools")
    with tracer.start_as_current_span(f"tool.{name}") as span:
        span.set_attribute(
            SpanAttributes.OPENINFERENCE_SPAN_KIND,
            OpenInferenceSpanKindValues.TOOL.value,
        )
        span.set_attribute(SpanAttributes.TOOL_NAME, name)
        annotate_agent_node(
            span,
            name,
            name.replace("_", " ").title(),
            parent_id=SUPPORT_NODE_ID,
        )
        span.set_attribute(SpanAttributes.INPUT_VALUE, arguments_json[:12000])
        span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")
        yield span


@contextmanager
def guardrail_span(rule: str) -> Iterator[Any]:
    """Child span for a blocked turn, parented to the open support span."""
    if _STATE != "ready":
        yield None
        return
    tracer = trace.get_tracer("omniserve.guardrails")
    with tracer.start_as_current_span("guardrail.prompt_injection") as span:
        span.set_attribute(
            SpanAttributes.OPENINFERENCE_SPAN_KIND,
            OpenInferenceSpanKindValues.GUARDRAIL.value,
        )
        annotate_agent_node(
            span,
            "prompt_injection_guard",
            "Prompt injection guard",
            parent_id=SUPPORT_NODE_ID,
        )
        span.set_attribute("guardrail_triggered", True)
        span.set_attribute("guardrail.rule", rule)
        span.set_status(Status(StatusCode.ERROR, "Prompt injection blocked"))
        yield span


def note_tool_result(span: Any, raw: str, status: str) -> None:
    if span is None:
        return
    span.set_attribute(SpanAttributes.OUTPUT_VALUE, raw[:12000])
    span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")
    if status != "ok":
        span.set_status(Status(StatusCode.ERROR))
