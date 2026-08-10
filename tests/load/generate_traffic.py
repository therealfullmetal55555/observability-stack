#!/usr/bin/env python3
"""
Synthetic agent traffic for the dashboard.

Realistic enough that the panels show the shapes they're meant to show:
a long tail of cheap calls, a few expensive reasoning runs, occasional tool
failures, and answers that sometimes forget their citations. Flat random noise
makes every dashboard look fine, which teaches you nothing.

    python tests/load/generate_traffic.py --duration 60 --rate 20
    python tests/load/generate_traffic.py --scenario cost-spike
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

OTLP_HTTP = "http://localhost:4318/v1/traces"

# A plausible fleet: mostly a cheap support bot, one expensive analyst.
SERVICES = [
    ("rag-support-bot", 0.45),
    ("n8n-ai-executive-assistant", 0.20),
    ("invoice-document-agent", 0.15),
    ("browser-use-agent", 0.12),
    ("synapse", 0.08),
]

MODELS = [
    ("gpt-4o-mini", 0.55, 0.15, 0.60),
    ("gpt-4o", 0.20, 2.50, 10.00),
    ("claude-haiku-3.5", 0.15, 0.80, 4.00),
    ("claude-sonnet-4", 0.08, 3.00, 15.00),
    ("ollama/llama3.3", 0.02, 0.0, 0.0),
]

TOOLS = [
    ("search_kb", 0.40, 2.0),        # (name, share, median latency seconds)
    ("sql_query", 0.20, 12.0),
    ("http_request", 0.18, 5.0),
    ("create_invoice_pdf", 0.12, 40.0),
    ("browser_navigate", 0.10, 90.0),
]


@dataclass
class Scenario:
    """Knobs the scenarios turn to reproduce a specific dashboard shape."""

    error_rate: float = 0.02
    uncited_rate: float = 0.08
    refusal_rate: float = 0.03
    cost_multiplier: float = 1.0
    slow_tool_share: float = 0.0
    notes: str = ""
    extra: dict = field(default_factory=dict)


SCENARIOS = {
    "healthy": Scenario(notes="everything nominal"),
    # A prompt change that stopped attaching sources. Shows up on the
    # hallucination dashboard within one refresh.
    "citation-regression": Scenario(uncited_rate=0.65, notes="prompt rewrite dropped citations"),
    # One agent switched to the expensive model without anyone noticing.
    "cost-spike": Scenario(cost_multiplier=1.0, notes="expensive model", extra={"force_model": "claude-sonnet-4"}),
    # A vendor API went wobbly: errors up, latency up.
    "vendor-outage": Scenario(error_rate=0.35, slow_tool_share=0.5, notes="upstream degraded"),
    # Guardrails tightened, refusals way up. Useful for checking the refusal
    # panel isn't just an unused gauge.
    "tight-guardrails": Scenario(refusal_rate=0.30, uncited_rate=0.02, notes="policy tightened"),
}


def weighted_choice(pairs):
    names = [p[0] for p in pairs]
    weights = [p[1] for p in pairs]
    return random.choices(names, weights=weights, k=1)[0]


def lognormal_seconds(median: float, sigma: float = 1.1) -> float:
    """Latency is log-normal in real systems. Uniform noise looks fake in Grafana."""
    return random.lognormvariate(math.log(max(median, 0.001)), sigma)


def build_payload(service: str, scenario: Scenario, rng: random.Random) -> dict:
    now = time.time_ns()
    forced = scenario.extra.get("force_model")
    model_name = forced if forced else weighted_choice([(m[0], m[1]) for m in MODELS])
    model_entry = next(m for m in MODELS if m[0] == model_name)
    _, _, input_price, output_price = model_entry

    # Agent span
    agent_failed = rng.random() < scenario.error_rate
    tool_name = weighted_choice(TOOLS)
    tool_median = next(t[2] for t in TOOLS if t[0] == tool_name)
    if rng.random() < scenario.slow_tool_share:
        tool_median *= 6

    tool_failed = rng.random() < scenario.error_rate * 1.4
    tool_status = "error" if tool_failed else "success"

    input_tokens = int(rng.lognormvariate(math.log(1400), 0.8))
    output_tokens = int(rng.lognormvariate(math.log(380), 0.9))
    cost = (
        input_tokens / 1_000_000 * input_price + output_tokens / 1_000_000 * output_price
    ) * scenario.cost_multiplier

    is_refusal = rng.random() < scenario.refusal_rate
    has_citations = (not is_refusal) and rng.random() > scenario.uncited_rate
    coverage = rng.uniform(0.75, 1.0) if has_citations else 0.0

    agent_ms = 400 + lognormal_seconds(1.2) * 1000
    tool_ms = lognormal_seconds(tool_median) * 1000
    llm_ms = lognormal_seconds(1.8) * 1000

    def span(span_id, name, start_ns, duration_ms, attrs, status_code=0, parent=None):
        item = {
            "traceId": trace_id,
            "spanId": span_id,
            "name": name,
            "kind": 3,
            "startTimeUnixNano": str(start_ns),
            "endTimeUnixNano": str(start_ns + int(duration_ms * 1e6)),
            "attributes": [
                {"key": key, "value": {"stringValue": str(value)}} for key, value in attrs.items()
            ],
            "status": {"code": status_code},
        }
        if parent:
            item["parentSpanId"] = parent
        return item

    trace_id = "".join(rng.choice("0123456789abcdef") for _ in range(32))
    agent_id, tool_id, llm_id = "aaaa" + trace_id[4:16], "bbbb" + trace_id[4:16], "cccc" + trace_id[4:16]

    spans = [
        span(
            agent_id,
            "agent.answer",
            now,
            agent_ms,
            {
                "gen_ai.agent.name": service,
                "gen_ai.operation.name": "answer",
                "gen_ai.response.has_citations": has_citations,
                "gen_ai.response.citation_coverage": round(coverage, 3),
                "gen_ai.response.is_refusal": is_refusal,
            },
            status_code=2 if agent_failed else 0,
        ),
        span(
            tool_id,
            f"tool.{tool_name}",
            now + 5_000_000,
            tool_ms,
            {"gen_ai.tool.name": tool_name, "gen_ai.tool.status": tool_status},
            status_code=2 if tool_failed else 0,
            parent=agent_id,
        ),
        span(
            llm_id,
            f"llm.{model_name}",
            now + 10_000_000,
            llm_ms,
            {
                "gen_ai.request.model": model_name,
                "gen_ai.usage.input_tokens": input_tokens,
                "gen_ai.usage.output_tokens": output_tokens,
                "gen_ai.response.cost_usd": round(cost, 6),
                "gen_ai.response.finish_reason": "refusal" if is_refusal else "stop",
            },
            parent=agent_id,
        ),
    ]

    # Token usage as a datapoint too — some of the shipped agents report that
    # way instead of using span attributes.
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": service}},
                        {"key": "deployment.environment", "value": {"stringValue": "demo"}},
                    ]
                },
                "scopeSpans": [{"scope": {"name": "load-generator"}, "spans": spans}],
            }
        ],
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": service}}]
                },
                "scopeMetrics": [
                    {
                        "scope": {"name": "load-generator"},
                        "metrics": [
                            {
                                "name": "gen_ai_llm_cost_usd_total",
                                "sum": {
                                    "aggregationTemporality": 2,
                                    "isMonotonic": True,
                                    "dataPoints": [
                                        {
                                            "asDouble": round(cost, 6),
                                            "timeUnixNano": str(now),
                                            "startTimeUnixNano": str(now - 60_000_000_000),
                                            "attributes": [
                                                {"key": "gen_ai.request.model", "value": {"stringValue": model_name}}
                                            ],
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                ],
            }
        ],
    }


def send(payload: dict, endpoint: str) -> bool:
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status == 200
    except urllib.error.URLError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--duration", type=int, default=60, help="seconds to run (0 = forever)")
    parser.add_argument("--rate", type=float, default=20, help="traces per second")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="healthy")
    parser.add_argument("--endpoint", default=OTLP_HTTP)
    parser.add_argument("--seed", type=int, default=None, help="fix the RNG for reproducible runs")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    scenario = SCENARIOS[args.scenario]

    print(f"scenario: {args.scenario} — {scenario.notes}")
    print(f"target:   {args.rate} traces/s for {args.duration or '∞'}s → {args.endpoint}")

    sent = failed = 0
    started = time.time()
    interval = 1.0 / args.rate

    try:
        while args.duration == 0 or time.time() - started < args.duration:
            service = weighted_choice(SERVICES)
            if send(build_payload(service, scenario, rng), args.endpoint):
                sent += 1
            else:
                failed += 1
                if failed == 1:
                    print("  first send failed — is the stack up? (`make up`)", file=sys.stderr)
                if failed > 20:
                    print("giving up after 20 consecutive failures", file=sys.stderr)
                    return 1
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\ninterrupted")

    elapsed = max(time.time() - started, 0.001)
    print(f"sent {sent} traces, {failed} failed, {sent / elapsed:.1f}/s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
