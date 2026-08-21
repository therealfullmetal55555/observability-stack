"""
agent_observability — OpenTelemetry instrumentation for AI agents.

    from agent_observability import init_telemetry, trace_agent, trace_tool

    init_telemetry(service_name="rag-support-bot")

    @trace_agent(name="answer")
    def answer(question: str) -> str:
        docs = search(question)          # @trace_tool
        with trace_llm("gpt-4o-mini") as span:
            reply = model.complete(question, docs)
            span.set_attribute("gen_ai.usage.input_tokens", reply.usage.prompt_tokens)
        record_response_quality(span, "rag-support-bot", reply.text, len(docs))
        return reply.text
"""

from agent_observability.core import (
    estimate_citation_coverage,
    extract_context,
    heartbeat,
    inject_context,
    init_telemetry,
    record_agent_call,
    record_llm_call,
    record_response_quality,
    record_tool_call,
    record_vector_op,
    trace_agent,
    trace_llm,
    trace_tool,
)
from agent_observability.pricing import MODEL_PRICING, price_call

__version__ = "1.0.0"

__all__ = [
    "MODEL_PRICING",
    "__version__",
    "estimate_citation_coverage",
    "extract_context",
    "heartbeat",
    "inject_context",
    "init_telemetry",
    "price_call",
    "record_agent_call",
    "record_llm_call",
    "record_response_quality",
    "record_tool_call",
    "record_vector_op",
    "trace_agent",
    "trace_llm",
    "trace_tool",
]
