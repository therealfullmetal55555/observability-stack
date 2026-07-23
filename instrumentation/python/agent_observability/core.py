"""
OpenTelemetry instrumentation for Python AI agents (FastAPI / LangChain / LlamaIndex).

Design notes that matter in practice:

* Metrics are module-level singletons created once. Creating a counter per call
  is the classic way to leak memory and get duplicate series.
* `trace_llm` takes usage numbers from the callback, not from a global counter —
  the vendor SDKs disagree too much for anything else to be reliable.
* Nothing here raises. If telemetry breaks, the agent still answers users.
"""

from __future__ import annotations

import functools
import logging
import os
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional, TypeVar

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import Counter, Histogram, Meter, UpDownCounter
from opentelemetry.propagate import extract, inject, set_global_textmap
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Span, Status, StatusCode
from opentelemetry.util.types import AttributeValue

from agent_observability.pricing import MODEL_PRICING, price_call  # noqa: F401

log = logging.getLogger(__name__)
T = TypeVar("T")

SERVICE = "service.name"
AGENT_NAME = "gen_ai.agent.name"
AGENT_OP = "gen_ai.operation.name"
AGENT_WORKFLOW = "gen_ai.workflow.id"
TOOL_NAME = "gen_ai.tool.name"
TOOL_STATUS = "gen_ai.tool.status"
TOOL_RETRIES = "gen_ai.tool.retry_count"
MODEL = "gen_ai.request.model"
INPUT_TOKENS = "gen_ai.usage.input_tokens"
OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
COST = "gen_ai.response.cost_usd"
VECTOR_OP = "db.operation.name"
VECTOR_COLLECTION = "db.collection.name"
HAS_CITATIONS = "gen_ai.response.has_citations"
CITATION_COVERAGE = "gen_ai.response.citation_coverage"
IS_REFUSAL = "gen_ai.response.is_refusal"

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class _State:
    tracer: Optional[trace.Tracer] = None
    meter: Optional[Meter] = None
    ready: bool = False

    # metrics
    agent_calls: Optional[Counter] = None
    agent_latency: Optional[Histogram] = None
    tool_calls: Optional[Counter] = None
    tool_latency: Optional[Histogram] = None
    tool_retries: Optional[Counter] = None
    llm_calls: Optional[Counter] = None
    llm_latency: Optional[Histogram] = None
    llm_tokens: Optional[Counter] = None
    llm_cost: Optional[Counter] = None
    vector_ops: Optional[Counter] = None
    vector_latency: Optional[Histogram] = None
    agent_up: Optional[UpDownCounter] = None
    citation_coverage: Optional[Histogram] = None
    refusals: Optional[Counter] = None
    responses: Optional[Counter] = None


_S = _State()


def init_telemetry(
    service_name: Optional[str] = None,
    endpoint: Optional[str] = None,
    sample_rate: float = 1.0,
    environment: Optional[str] = None,
) -> trace.Tracer:
    """
    Wire up trace + metric providers. Safe to call twice — the second call is a
    no-op, which matters because process forks and test runners both hit it.
    """
    if _S.ready:
        return _S.tracer  # type: ignore[return-value]

    service_name = service_name or os.getenv("SERVICE_NAME", "ai-agent")
    endpoint = endpoint or os.getenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317"
    )
    environment = environment or os.getenv("ENVIRONMENT", "development")

    resource = Resource.create(
        {
            SERVICE: service_name,
            "service.version": os.getenv("SERVICE_VERSION", "1.0.0"),
            "deployment.environment": environment,
        }
    )

    tracer_provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(TraceIdRatioBased(sample_rate)),
    )
    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=endpoint, insecure=True),
            max_queue_size=2048,
            schedule_delay_millis=5000,
        )
    )
    trace.set_tracer_provider(tracer_provider)
    _S.tracer = trace.get_tracer("ai-agent-instrumentation", "1.0.0")

    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=endpoint, insecure=True),
        export_interval_millis=15_000,
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(meter_provider)
    _S.meter = metrics.get_meter("ai-agent-instrumentation", "1.0.0")

    m = _S.meter
    _S.agent_calls = m.create_counter(
        "gen_ai_agent_calls_total", description="Agent invocations by operation and status"
    )
    _S.agent_latency = m.create_histogram(
        "gen_ai_agent_latency_seconds",
        description="End-to-end agent latency",
        unit="s",
        explicit_bucket_boundaries_advisory=LATENCY_BUCKETS_S,
    )
    _S.tool_calls = m.create_counter(
        "gen_ai_tool_calls_total", description="Tool invocations by tool and status"
    )
    _S.tool_latency = m.create_histogram(
        "gen_ai_tool_call_duration_seconds",
        description="Tool call latency",
        unit="s",
        explicit_bucket_boundaries_advisory=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30],
    )
    _S.tool_retries = m.create_counter(
        "gen_ai_tool_retries_total", description="Retries observed inside a tool call"
    )
    _S.llm_calls = m.create_counter(
        "gen_ai_llm_calls_total", description="Model calls by model and status"
    )
    _S.llm_latency = m.create_histogram(
        "gen_ai_llm_latency_seconds",
        description="Model call latency",
        unit="s",
        explicit_bucket_boundaries_advisory=[0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120, 300],
    )
    _S.llm_tokens = m.create_counter(
        "gen_ai_llm_tokens_total", description="Tokens consumed, type=input|output"
    )
    _S.llm_cost = m.create_counter(
        "gen_ai_llm_cost_usd_total", description="Estimated spend in USD"
    )
    _S.vector_ops = m.create_counter(
        "vector_db_operations_total", description="Vector store operations"
    )
    _S.vector_latency = m.create_histogram(
        "vector_db_query_duration_seconds",
        description="Vector store query latency",
        unit="s",
        explicit_bucket_boundaries_advisory=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5],
    )
    _S.agent_up = m.create_up_down_counter(
        "gen_ai_agent_up", description="1 while the agent process is alive"
    )
    _S.citation_coverage = m.create_histogram(
        "gen_ai_response_citation_coverage",
        description="Fraction of claims backed by a retrieved source (0-1)",
        unit="1",
        explicit_bucket_boundaries_advisory=[0, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 1],
    )
    _S.refusals = m.create_counter(
        "gen_ai_responses_refusal_total", description="Answers withheld by a guardrail"
    )
    _S.responses = m.create_counter(
        "gen_ai_responses_total",
        description="Answers produced, labelled has_citations=true|false",
    )

    set_global_textmap(TraceContextTextMapPropagator())
    _S.ready = True
    log.info("telemetry ready: service=%s endpoint=%s env=%s", service_name, endpoint, environment)
    return _S.tracer  # type: ignore[return-value]


def _tracer() -> trace.Tracer:
    if _S.tracer is None:
        init_telemetry()
    return _S.tracer  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Recording primitives
# ---------------------------------------------------------------------------


def _attrs(**kw: Any) -> Dict[str, AttributeValue]:
    return {k: v for k, v in kw.items() if v is not None}


def record_agent_call(service: str, operation: str, status: str, duration_s: float) -> None:
    labels = _attrs(**{SERVICE: service, "operation": operation})
    if _S.agent_calls:
        _S.agent_calls.add(1, {**labels, "status": status})
    if _S.agent_latency:
        _S.agent_latency.record(duration_s, labels)


def record_tool_call(
    service: str,
    tool: str,
    status: str,
    duration_s: float,
    retries: int = 0,
    cost_usd: float = 0.0,
) -> None:
    labels = _attrs(**{SERVICE: service, TOOL_NAME: tool})
    if _S.tool_calls:
        _S.tool_calls.add(1, {**labels, "status": status})
    if _S.tool_latency:
        _S.tool_latency.record(duration_s, labels)
    if _S.tool_retries and retries:
        _S.tool_retries.add(retries, labels)
    if _S.llm_cost and cost_usd:
        _S.llm_cost.add(cost_usd, labels)


def record_llm_call(
    service: str,
    model: str,
    status: str,
    duration_s: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: Optional[float] = None,
) -> None:
    labels = _attrs(**{SERVICE: service, MODEL: model})
    if _S.llm_calls:
        _S.llm_calls.add(1, {**labels, "status": status})
    if _S.llm_latency:
        _S.llm_latency.record(duration_s, labels)
    if _S.llm_tokens:
        if input_tokens:
            _S.llm_tokens.add(input_tokens, {**labels, "type": "input"})
        if output_tokens:
            _S.llm_tokens.add(output_tokens, {**labels, "type": "output"})
    spend = cost_usd if cost_usd is not None else price_call(model, input_tokens, output_tokens)
    if _S.llm_cost and spend:
        _S.llm_cost.add(spend, labels)


def record_vector_op(service: str, operation: str, status: str, duration_s: float) -> None:
    labels = _attrs(**{SERVICE: service, "operation": operation})
    if _S.vector_ops:
        _S.vector_ops.add(1, {**labels, "status": status})
    if _S.vector_latency:
        _S.vector_latency.record(duration_s, labels)


def heartbeat(service: str) -> None:
    """Called on a timer. If this stops arriving, `AgentDown` fires."""
    if _S.agent_up:
        _S.agent_up.add(0, _attrs(**{SERVICE: service}))


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------


def _service_name(explicit: Optional[str]) -> str:
    return explicit or os.getenv("SERVICE_NAME", "ai-agent")


def trace_agent(name: Optional[str] = None, service_name: Optional[str] = None) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Trace a top-level agent entry point (a handler, a graph node, a chain)."""

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        operation = name or fn.__name__
        service = _service_name(service_name)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            tracer = _tracer()
            with tracer.start_as_current_span(
                f"agent.{operation}",
                attributes=_attrs(**{SERVICE: service, AGENT_NAME: operation, AGENT_OP: operation}),
            ) as span:
                started = time.perf_counter()
                try:
                    result = fn(*args, **kwargs)
                    span.set_status(Status(StatusCode.OK))
                    return result
                except Exception as exc:  # noqa: BLE001 — must re-raise
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                    span.record_exception(exc)
                    raise
                finally:
                    record_agent_call(
                        service, operation,
                        "error" if span.status.status_code is StatusCode.ERROR else "success",
                        time.perf_counter() - started,
                    )

        return wrapper

    return decorator


def trace_tool(
    name: Optional[str] = None,
    service_name: Optional[str] = None,
    cost_usd: float = 0.0,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Trace a single tool. Nest it inside `trace_agent` and it lands as a child span."""

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        tool = name or fn.__name__
        service = _service_name(service_name)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            tracer = _tracer()
            with tracer.start_as_current_span(
                f"tool.{tool}",
                attributes=_attrs(**{SERVICE: service, TOOL_NAME: tool, TOOL_STATUS: "success"}),
            ) as span:
                started = time.perf_counter()
                ok = True
                try:
                    result = fn(*args, **kwargs)
                    span.set_status(Status(StatusCode.OK))
                    return result
                except Exception as exc:  # noqa: BLE001
                    ok = False
                    span.set_attribute(TOOL_STATUS, "error")
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                    span.record_exception(exc)
                    raise
                finally:
                    elapsed = time.perf_counter() - started
                    span.set_attribute("gen_ai.tool.duration_ms", elapsed * 1000)
                    record_tool_call(
                        service, tool, "success" if ok else "error", elapsed, cost_usd=cost_usd
                    )

        return wrapper

    return decorator


@contextmanager
def trace_llm(
    model: str,
    service_name: Optional[str] = None,
    workflow_id: Optional[str] = None,
) -> Iterator[Span]:
    """
    Context manager for a model call. Record usage inside the block:

        with trace_llm("gpt-4o-mini") as span:
            resp = client.chat(...)
            span.set_attribute("gen_ai.usage.input_tokens", resp.usage.prompt_tokens)
    """
    service = _service_name(service_name)
    tracer = _tracer()
    started = time.perf_counter()
    ok = True

    with tracer.start_as_current_span(
        f"llm.{model}",
        attributes=_attrs(**{SERVICE: service, MODEL: model, AGENT_WORKFLOW: workflow_id}),
    ) as span:
        try:
            yield span
            span.set_status(Status(StatusCode.OK))
        except Exception as exc:  # noqa: BLE001
            ok = False
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
            raise
        finally:
            attrs = span.attributes or {}
            record_llm_call(
                service,
                model,
                "success" if ok else "error",
                time.perf_counter() - started,
                input_tokens=int(attrs.get(INPUT_TOKENS, 0) or 0),
                output_tokens=int(attrs.get(OUTPUT_TOKENS, 0) or 0),
                cost_usd=attrs.get(COST),  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# Answer quality
# ---------------------------------------------------------------------------


def estimate_citation_coverage(answer: str, sources_retrieved: int) -> Dict[str, Any]:
    """
    Sentence-level heuristic: a sentence counts as sourced if it carries a
    bracketed marker or a URL. Deliberately simple — run it on every answer,
    eyeball the low-coverage traces, and only then reach for an LLM judge.
    """
    sentences = [s.strip() for s in __import__("re").split(r"(?<=[.!?])\s+", answer) if s.strip()]
    markers = __import__("re").findall(r"\[[^\]]+\]|https?://\S+", answer)
    cited = [s for s in sentences if __import__("re").search(r"\[[^\]]+\]|https?://\S+", s)]

    coverage = 0.0 if not sources_retrieved or not sentences else len(cited) / len(sentences)
    return {
        "citation_coverage": coverage,
        "has_citations": bool(markers),
        "citations": markers,
    }


def record_response_quality(
    span: Span,
    service: str,
    answer: str,
    sources_retrieved: int,
    is_refusal: bool = False,
    refusal_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Attach quality signals to the current span and record the metrics."""
    q = estimate_citation_coverage(answer, sources_retrieved)
    span.set_attribute(HAS_CITATIONS, q["has_citations"])
    span.set_attribute(CITATION_COVERAGE, q["citation_coverage"])
    span.set_attribute(IS_REFUSAL, is_refusal)
    if is_refusal and refusal_reason:
        span.set_attribute("gen_ai.response.refusal_reason", refusal_reason)

    labels = _attrs(**{SERVICE: service})
    if _S.citation_coverage:
        _S.citation_coverage.record(q["citation_coverage"], labels)
    if _S.responses:
        _S.responses.add(1, {**labels, "has_citations": str(bool(q["has_citations"])).lower()})
    if _S.refusals and is_refusal:
        _S.refusals.add(1, labels)
    return q


# ---------------------------------------------------------------------------
# Context propagation
# ---------------------------------------------------------------------------


def inject_context(carrier: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Put the current trace context into headers before an outbound call."""
    carrier = carrier if carrier is not None else {}
    inject(carrier)
    return carrier


def extract_context(carrier: Dict[str, str]):
    """Pull trace context out of inbound headers (n8n webhook, queue message)."""
    return extract(carrier)
