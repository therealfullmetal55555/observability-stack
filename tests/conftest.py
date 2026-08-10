"""
Shared fixtures.

The instrumentation module keeps one set of instrument handles in module-level
state, so every test needs a fresh in-memory provider. We don't call
`init_telemetry()` here — that would install real OTLP exporters and try to
reach a collector. Instead we build a MeterProvider/TracerProvider over
in-memory readers and inject them into the module's state, which is exactly the
seam the production code reads from.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "instrumentation" / "python"
for entry in (str(ROOT), str(PKG)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

pytest.importorskip("opentelemetry.sdk", reason="pip install -e '.[dev]' first")

from opentelemetry.sdk.metrics import MeterProvider  # noqa: E402
from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: E402
from opentelemetry.sdk.resources import Resource  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

SERVICE = "test-agent"


class Harness:
    """In-memory tracer + meter, plus small readers for assertions."""

    def __init__(self) -> None:
        resource = Resource.create({"service.name": SERVICE})

        self.span_exporter = InMemorySpanExporter()
        self.tracer_provider = TracerProvider(resource=resource)
        self.tracer_provider.add_span_processor(SimpleSpanProcessor(self.span_exporter))

        self.metric_reader = InMemoryMetricReader()
        self.meter_provider = MeterProvider(resource=resource, metric_readers=[self.metric_reader])

        self.tracer = self.tracer_provider.get_tracer("ai-agent-instrumentation", "1.0.0")
        self.meter = self.meter_provider.get_meter("ai-agent-instrumentation", "1.0.0")

    # -- spans ---------------------------------------------------------------

    @property
    def spans(self):
        return self.span_exporter.get_finished_spans()

    def span(self, name: str):
        matches = [s for s in self.spans if s.name == name]
        assert matches, f"no span named {name!r}; finished: {[s.name for s in self.spans]}"
        return matches[-1]

    def span_names(self) -> list[str]:
        return [s.name for s in self.spans]

    # -- metrics -------------------------------------------------------------

    def _metric(self, name: str):
        data = self.metric_reader.get_metrics_data()
        if data is None:
            return None
        for rm in data.resource_metrics:
            for sm in rm.scope_metrics:
                for metric in sm.metrics:
                    if metric.name == name:
                        return metric
        return None

    def counter(self, name: str, **labels) -> float:
        metric = self._metric(name)
        assert metric is not None, f"metric {name!r} was never recorded"
        return sum(
            point.value
            for point in metric.data.data_points
            if all(point.attributes.get(k) == v for k, v in labels.items())
        )

    def histogram_points(self, name: str, **labels):
        metric = self._metric(name)
        assert metric is not None, f"metric {name!r} was never recorded"
        return [
            point
            for point in metric.data.data_points
            if all(point.attributes.get(k) == v for k, v in labels.items())
        ]

    def histogram_count(self, name: str, **labels) -> int:
        return sum(p.count for p in self.histogram_points(name, **labels))

    def histogram_sum(self, name: str, **labels) -> float:
        return sum(p.sum for p in self.histogram_points(name, **labels))

    def shutdown(self) -> None:
        self.tracer_provider.shutdown()
        self.meter_provider.shutdown()


def _bind(inst, harness: Harness) -> None:
    """Point the module's handles at the in-memory harness."""
    inst._S.tracer = harness.tracer
    inst._S.meter = harness.meter
    inst._S.ready = True

    m = harness.meter
    inst._S.agent_calls = m.create_counter("gen_ai_agent_calls_total")
    inst._S.agent_latency = m.create_histogram("gen_ai_agent_latency_seconds")
    inst._S.tool_calls = m.create_counter("gen_ai_tool_calls_total")
    inst._S.tool_latency = m.create_histogram("gen_ai_tool_call_duration_seconds")
    inst._S.tool_retries = m.create_counter("gen_ai_tool_retries_total")
    inst._S.llm_calls = m.create_counter("gen_ai_llm_calls_total")
    inst._S.llm_latency = m.create_histogram("gen_ai_llm_latency_seconds")
    inst._S.llm_tokens = m.create_counter("gen_ai_llm_tokens_total")
    inst._S.llm_cost = m.create_counter("gen_ai_llm_cost_usd_total")
    inst._S.vector_ops = m.create_counter("vector_db_operations_total")
    inst._S.vector_latency = m.create_histogram("vector_db_query_duration_seconds")
    inst._S.agent_up = m.create_up_down_counter("gen_ai_agent_up")
    inst._S.citation_coverage = m.create_histogram("gen_ai_response_citation_coverage")
    inst._S.refusals = m.create_counter("gen_ai_responses_refusal_total")
    inst._S.responses = m.create_counter("gen_ai_responses_total")


@pytest.fixture
def harness():
    """A clean in-memory harness, wired into the instrumentation module."""
    core = pytest.importorskip("agent_observability.core")
    h = Harness()
    _bind(core, h)
    yield h
    h.shutdown()


@pytest.fixture(scope="module")
def repo_root() -> Path:
    return ROOT
