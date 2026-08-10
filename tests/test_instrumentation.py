"""
Tests for the Python instrumentation.

These assert on observable behaviour — a span exists with the right attributes,
a counter moved — not on implementation details. If someone swaps the exporter
or renames an internal, these should still pass. If someone breaks the semantic
conventions, they should fail loudly, because every dashboard and alert in this
repo keys off those names.
"""

from __future__ import annotations

import pytest

from agent_observability import core as inst
from agent_observability import pricing

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# pricing
# ---------------------------------------------------------------------------


class TestPricing:
    def test_prices_a_known_model(self):
        # gpt-4o-mini: $0.15/M in, $0.60/M out
        cost = pricing.price_call("gpt-4o-mini", 1_000_000, 1_000_000)
        assert cost == pytest.approx(0.75)

    def test_matches_on_substring_so_versioned_names_work(self):
        dated = pricing.price_call("gpt-4o-mini-2024-07-18", 1_000_000, 0)
        assert dated == pytest.approx(0.15)

    def test_prefers_the_most_specific_prefix(self):
        # "gpt-4o-mini" must not be priced as "gpt-4o"
        mini = pricing.price_call("gpt-4o-mini", 1_000_000, 0)
        full = pricing.price_call("gpt-4o", 1_000_000, 0)
        assert mini < full

    def test_unknown_model_costs_zero_rather_than_guessing(self):
        assert pricing.price_call("some-model-we-never-heard-of", 10_000, 10_000) == 0.0

    def test_output_tokens_are_priced_higher_than_input(self):
        model = "claude-sonnet-4"
        as_input = pricing.price_call(model, 1_000_000, 0)
        as_output = pricing.price_call(model, 0, 1_000_000)
        assert as_output > as_input


# ---------------------------------------------------------------------------
# citation heuristic
# ---------------------------------------------------------------------------


class TestCitationCoverage:
    def test_fully_cited_answer_scores_one(self):
        answer = "Refunds take five days [1]. Shipping is free over 50 EUR [2]."
        result = inst.estimate_citation_coverage(answer, sources_retrieved=2)
        assert result["citation_coverage"] == pytest.approx(1.0)
        assert result["has_citations"] is True
        assert result["citations"] == ["[1]", "[2]"]

    def test_uncited_answer_scores_zero(self):
        answer = "Refunds take five days. Shipping is free over 50 EUR."
        result = inst.estimate_citation_coverage(answer, sources_retrieved=3)
        assert result["citation_coverage"] == 0.0
        assert result["has_citations"] is False

    def test_partial_coverage_is_a_fraction(self):
        answer = "Refunds take five days [1]. Shipping is free. Returns are easy."
        result = inst.estimate_citation_coverage(answer, sources_retrieved=3)
        assert result["citation_coverage"] == pytest.approx(1 / 3)

    def test_no_retrieved_sources_means_no_coverage(self):
        # A confident answer with an empty retrieval set is the exact failure
        # this metric exists to catch.
        answer = "Refunds take five days [1]."
        assert inst.estimate_citation_coverage(answer, sources_retrieved=0)["citation_coverage"] == 0.0

    def test_urls_count_as_citations(self):
        answer = "See the policy at https://example.com/refunds for details."
        assert inst.estimate_citation_coverage(answer, 1)["citation_coverage"] == pytest.approx(1.0)

    def test_empty_answer_does_not_crash(self):
        result = inst.estimate_citation_coverage("", sources_retrieved=2)
        assert result["citation_coverage"] == 0.0
        assert result["citations"] == []


# ---------------------------------------------------------------------------
# decorators
# ---------------------------------------------------------------------------


class TestTraceAgent:
    def test_creates_a_span_named_after_the_operation(self, harness):
        @inst.trace_agent(name="answer", service_name="rag-bot")
        def handle(question: str) -> str:
            return f"answer to {question}"

        assert handle("refunds?") == "answer to refunds?"

        span = harness.span("agent.answer")
        assert span.attributes[SERVICE_ATTR] == "rag-bot"
        assert span.attributes[inst.AGENT_OP] == "answer"

    def test_records_a_successful_call(self, harness):
        @inst.trace_agent(name="answer", service_name="rag-bot")
        def handle() -> str:
            return "ok"

        handle()

        assert harness.counter(
            "gen_ai_agent_calls_total", **{"service.name": "rag-bot", "status": "success"}
        ) == 1
        assert harness.histogram_count("gen_ai_agent_latency_seconds") == 1

    def test_records_an_error_and_re_raises(self, harness):
        @inst.trace_agent(name="answer", service_name="rag-bot")
        def handle() -> str:
            raise ValueError("upstream model returned 502")

        with pytest.raises(ValueError, match="502"):
            handle()

        assert harness.counter(
            "gen_ai_agent_calls_total", **{"service.name": "rag-bot", "status": "error"}
        ) == 1

        span = harness.span("agent.answer")
        assert span.status.status_code.name == "ERROR"
        assert span.events, "the exception should be recorded as a span event"

    def test_preserves_the_wrapped_signature(self):
        @inst.trace_agent(name="x")
        def documented(a: int, b: int = 2) -> int:
            """Docstring survives the decorator."""
            return a + b

        assert documented(1) == 3
        assert documented.__name__ == "documented"
        assert "survives" in documented.__doc__


class TestTraceTool:
    def test_tool_span_is_a_child_of_the_agent_span(self, harness):
        @inst.trace_tool(name="search_kb", service_name="rag-bot")
        def search(query: str) -> list[str]:
            return ["doc-1"]

        @inst.trace_agent(name="answer", service_name="rag-bot")
        def handle() -> list[str]:
            return search("refunds")

        handle()

        tool = harness.span("tool.search_kb")
        agent = harness.span("agent.answer")
        assert tool.parent is not None
        assert tool.parent.span_id == agent.context.span_id

    def test_tool_metrics_carry_the_tool_name(self, harness):
        @inst.trace_tool(name="search_kb", service_name="rag-bot")
        def search() -> str:
            return "hit"

        search()

        assert harness.counter(
            "gen_ai_tool_calls_total",
            **{"service.name": "rag-bot", "gen_ai.tool.name": "search_kb", "status": "success"},
        ) == 1

    def test_failure_sets_the_status_attribute(self, harness):
        @inst.trace_tool(name="charge_card", service_name="invoice-agent")
        def charge() -> None:
            raise TimeoutError("stripe timed out")

        with pytest.raises(TimeoutError):
            charge()

        span = harness.span("tool.charge_card")
        assert span.attributes[inst.TOOL_STATUS] == "error"
        assert harness.counter(
            "gen_ai_tool_calls_total",
            **{"gen_ai.tool.name": "charge_card", "status": "error"},
        ) == 1

    def test_declared_cost_lands_in_the_cost_counter(self, harness):
        @inst.trace_tool(name="vision_ocr", service_name="invoice-agent", cost_usd=0.0042)
        def ocr() -> str:
            return "text"

        ocr()

        assert harness.counter(
            "gen_ai_llm_cost_usd_total", **{"gen_ai.tool.name": "vision_ocr"}
        ) == pytest.approx(0.0042)


class TestTraceLlm:
    def test_usage_is_read_back_off_the_span(self, harness):
        with inst.trace_llm("gpt-4o-mini", service_name="rag-bot") as span:
            span.set_attribute(inst.INPUT_TOKENS, 1200)
            span.set_attribute(inst.OUTPUT_TOKENS, 300)

        span = harness.span("llm.gpt-4o-mini")
        assert span.attributes[inst.INPUT_TOKENS] == 1200

        assert harness.counter(
            "gen_ai_llm_tokens_total", **{"type": "input", "gen_ai.request.model": "gpt-4o-mini"}
        ) == 1200
        assert harness.counter("gen_ai_llm_tokens_total", type="output") == 300

    def test_cost_is_derived_when_not_supplied(self, harness):
        with inst.trace_llm("gpt-4o-mini", service_name="rag-bot") as span:
            span.set_attribute(inst.INPUT_TOKENS, 1_000_000)
            span.set_attribute(inst.OUTPUT_TOKENS, 0)

        assert harness.counter("gen_ai_llm_cost_usd_total") == pytest.approx(0.15)

    def test_explicit_cost_wins_over_the_table(self, harness):
        with inst.trace_llm("gpt-4o-mini", service_name="rag-bot") as span:
            span.set_attribute(inst.INPUT_TOKENS, 1_000_000)
            span.set_attribute(inst.COST, 9.99)

        assert harness.counter("gen_ai_llm_cost_usd_total") == pytest.approx(9.99)

    def test_error_is_recorded_and_propagated(self, harness):
        with pytest.raises(RuntimeError, match="rate limited"):
            with inst.trace_llm("gpt-4o-mini", service_name="rag-bot"):
                raise RuntimeError("rate limited")

        assert harness.counter(
            "gen_ai_llm_calls_total", **{"gen_ai.request.model": "gpt-4o-mini", "status": "error"}
        ) == 1
        assert harness.span("llm.gpt-4o-mini").status.status_code.name == "ERROR"


# ---------------------------------------------------------------------------
# quality signals
# ---------------------------------------------------------------------------


class TestResponseQuality:
    def test_writes_coverage_onto_the_span_and_the_histogram(self, harness):
        with harness.tracer.start_as_current_span("agent.answer") as span:
            inst.record_response_quality(
                span,
                service="rag-bot",
                answer="Refunds take five days [1]. Shipping is free over 50 EUR [2].",
                sources_retrieved=2,
            )

        span = harness.span("agent.answer")
        assert span.attributes[inst.HAS_CITATIONS] is True
        assert span.attributes[inst.CITATION_COVERAGE] == pytest.approx(1.0)
        assert harness.histogram_count(
            "gen_ai_response_citation_coverage", **{"service.name": "rag-bot"}
        ) == 1

    def test_counts_the_answer_in_the_has_citations_breakdown(self, harness):
        with harness.tracer.start_as_current_span("agent.answer") as span:
            inst.record_response_quality(
                span, "rag-bot", "No sources behind this one.", sources_retrieved=2
            )

        assert harness.counter("gen_ai_responses_total", has_citations="false") == 1
        assert harness.counter("gen_ai_responses_total", has_citations="true") == 0

    def test_refusals_are_counted_separately(self, harness):
        with harness.tracer.start_as_current_span("agent.answer") as span:
            inst.record_response_quality(
                span,
                "rag-bot",
                "I can't help with that.",
                sources_retrieved=0,
                is_refusal=True,
                refusal_reason="policy:financial_advice",
            )

        assert harness.counter("gen_ai_responses_refusal_total") == 1
        assert (
            harness.span("agent.answer").attributes["gen_ai.response.refusal_reason"]
            == "policy:financial_advice"
        )


# ---------------------------------------------------------------------------
# propagation
# ---------------------------------------------------------------------------


class TestPropagation:
    def test_injected_context_is_recoverable_by_the_receiver(self, harness):
        with harness.tracer.start_as_current_span("agent.answer") as span:
            carrier = inst.inject_context({})

        assert "traceparent" in carrier
        assert span.context.trace_id != 0

        # The receiving side (an n8n webhook, a worker) extracts the same trace
        ctx = inst.extract_context(carrier)
        assert ctx is not None

    def test_injection_into_an_empty_carrier_does_not_mutate_it(self, harness):
        # Dicts are mutable; a caller passing a shared header dict shouldn't get
        # surprise keys when there's no active span.
        headers = inst.inject_context({"x-request-id": "abc"})
        assert headers["x-request-id"] == "abc"

    def test_heartbeat_does_not_raise_without_a_collector(self, harness):
        inst.heartbeat("rag-bot")  # must be a no-op, never an error


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


class TestMeterRegistration:
    def test_init_is_idempotent(self, monkeypatch):
        monkeypatch.setattr(inst._S, "ready", True)
        before = inst._S.tracer
        inst.init_telemetry(service_name="should-be-ignored")
        assert inst._S.tracer is before

    def test_every_metric_the_dashboards_query_actually_exists(self, harness):
        """Guard against a dashboard querying a metric nobody emits."""
        expected = {
            "gen_ai_agent_calls_total",
            "gen_ai_agent_latency_seconds",
            "gen_ai_tool_calls_total",
            "gen_ai_tool_call_duration_seconds",
            "gen_ai_llm_calls_total",
            "gen_ai_llm_latency_seconds",
            "gen_ai_llm_tokens_total",
            "gen_ai_llm_cost_usd_total",
            "vector_db_operations_total",
            "vector_db_query_duration_seconds",
            "gen_ai_agent_up",
            "gen_ai_response_citation_coverage",
            "gen_ai_responses_refusal_total",
            "gen_ai_responses_total",
        }
        bound = {
            "gen_ai_agent_calls_total": inst._S.agent_calls,
            "gen_ai_agent_latency_seconds": inst._S.agent_latency,
            "gen_ai_tool_calls_total": inst._S.tool_calls,
            "gen_ai_tool_call_duration_seconds": inst._S.tool_latency,
            "gen_ai_llm_calls_total": inst._S.llm_calls,
            "gen_ai_llm_latency_seconds": inst._S.llm_latency,
            "gen_ai_llm_tokens_total": inst._S.llm_tokens,
            "gen_ai_llm_cost_usd_total": inst._S.llm_cost,
            "vector_db_operations_total": inst._S.vector_ops,
            "vector_db_query_duration_seconds": inst._S.vector_latency,
            "gen_ai_agent_up": inst._S.agent_up,
            "gen_ai_response_citation_coverage": inst._S.citation_coverage,
            "gen_ai_responses_refusal_total": inst._S.refusals,
            "gen_ai_responses_total": inst._S.responses,
        }
        assert set(bound) == expected
        assert all(instrument is not None for instrument in bound.values())


SERVICE_ATTR = "service.name"
