#!/usr/bin/env python3
"""
Fill the dashboards with a believable two-hour history.

The first ten minutes after `make up` are always the worst part of a demo: empty
panels, and you end up explaining what the graph *would* look like. This backdates
a couple of hours of traffic — including one incident — by sending spans with
timestamps in the past, so the panels have a story to tell the moment you open
them.

    python scripts/seed_demo_data.py --hours 2
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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests" / "load"))

from generate_traffic import MODELS, SERVICES, TOOLS, weighted_choice, lognormal_seconds  # noqa: E402

OTLP_HTTP = "http://localhost:4318/v1/traces"


def phase_for(offset_minutes: float, total_minutes: float) -> dict:
    """
    A demo should contain at least one thing going wrong, or the dashboards
    look like stock photos. The incident sits about two thirds of the way in.
    """
    progress = offset_minutes / max(total_minutes, 1)

    if 0.60 < progress < 0.72:
        return {"error_rate": 0.28, "slow": 2.5, "cost_mult": 1.6, "label": "incident"}
    if 0.72 <= progress < 0.80:
        return {"error_rate": 0.06, "slow": 1.4, "cost_mult": 1.1, "label": "recovery"}
    return {"error_rate": 0.02, "slow": 1.0, "cost_mult": 1.0, "label": "normal"}


def build(timestamp_ns: int, rng: random.Random, phase: dict) -> dict:
    service = weighted_choice(SERVICES)
    model_name = weighted_choice([(m[0], m[1]) for m in MODELS])
    _, _, input_price, output_price = next(m for m in MODELS if m[0] == model_name)

    tool_name = weighted_choice(TOOLS)
    tool_median = next(t[2] for t in TOOLS if t[0] == tool_name) * phase["slow"]

    error_rate = min(phase["error_rate"], 0.9)
    agent_failed = rng.random() < error_rate
    tool_failed = rng.random() < error_rate * 1.3

    # Business hours are busier and answers are longer. Costs follow usage.
    hour = time.localtime(timestamp_ns / 1e9).tm_hour
    traffic_weight = 0.5 + 0.5 * math.sin((hour - 6) / 24 * 2 * math.pi)

    input_tokens = int(rng.lognormvariate(math.log(1400 * (0.7 + traffic_weight)), 0.8))
    output_tokens = int(rng.lognormvariate(math.log(380), 0.9))
    cost = (
        input_tokens / 1_000_000 * input_price + output_tokens / 1_000_000 * output_price
    ) * phase["cost_mult"]

    uncited = rng.random() < (0.35 if phase["label"] == "incident" else 0.08)
    is_refusal = rng.random() < 0.03

    trace_id = "".join(rng.choice("0123456789abcdef") for _ in range(32))
    agent_id = "aaaa" + trace_id[4:16]

    def attrs(pairs):
        return [{"key": k, "value": {"stringValue": str(v)}} for k, v in pairs.items()]

    def span(span_id, name, start_ns, duration_ms, pairs, code=0, parent=None):
        entry = {
            "traceId": trace_id,
            "spanId": span_id,
            "name": name,
            "kind": 3,
            "startTimeUnixNano": str(int(start_ns)),
            "endTimeUnixNano": str(int(start_ns + duration_ms * 1e6)),
            "attributes": attrs(pairs),
            "status": {"code": code},
        }
        if parent:
            entry["parentSpanId"] = parent
        return entry

    spans = [
        span(
            agent_id,
            "agent.answer",
            timestamp_ns,
            lognormal_seconds(1.2) * 1000 * phase["slow"],
            {
                "gen_ai.agent.name": service,
                "gen_ai.operation.name": "answer",
                "gen_ai.response.has_citations": not uncited,
                "gen_ai.response.citation_coverage": 0.0 if uncited else round(rng.uniform(0.8, 1.0), 3),
                "gen_ai.response.is_refusal": is_refusal,
            },
            code=2 if agent_failed else 0,
        ),
        span(
            "bbbb" + trace_id[4:16],
            f"tool.{tool_name}",
            timestamp_ns + 4_000_000,
            lognormal_seconds(tool_median) * 1000,
            {
                "gen_ai.tool.name": tool_name,
                "gen_ai.tool.status": "error" if tool_failed else "success",
                "gen_ai.tool.retry_count": int(tool_failed) * rng.randint(1, 3),
            },
            code=2 if tool_failed else 0,
            parent=agent_id,
        ),
        span(
            "cccc" + trace_id[4:16],
            f"llm.{model_name}",
            timestamp_ns + 9_000_000,
            lognormal_seconds(1.8) * 1000 * phase["slow"],
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

    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": service}},
                        {"key": "deployment.environment", "value": {"stringValue": "demo"}},
                    ]
                },
                "scopeSpans": [{"scope": {"name": "seed-script"}, "spans": spans}],
            }
        ]
    }


def post(payload: dict, endpoint: str) -> bool:
    request = urllib.request.Request(
        endpoint, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status == 200
    except urllib.error.URLError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hours", type=float, default=2.0, help="how much history to fabricate")
    parser.add_argument("--per-minute", type=int, default=40, help="traces per simulated minute")
    parser.add_argument("--endpoint", default=OTLP_HTTP)
    parser.add_argument("--seed", type=int, default=20260929)
    parser.add_argument("--batch", type=int, default=200, help="traces per HTTP request")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    now_ns = time.time_ns()
    window_s = args.hours * 3600
    total_traces = int(args.hours * 60 * args.per_minute)

    print(f"seeding {total_traces} traces across {args.hours:g}h ending now → {args.endpoint}")

    sent = failed = 0
    pending = 0
    started = time.time()

    for i in range(total_traces):
        offset_s = window_s * (i / total_traces)
        timestamp_ns = now_ns - int((window_s - offset_s) * 1e9)
        phase = phase_for(offset_s / 60, args.hours * 60)

        if not post(build(timestamp_ns, rng, phase), args.endpoint):
            failed += 1
            if failed == 1:
                print("  send failed — is the stack up? (`make up`)", file=sys.stderr)
            if failed > 20:
                return 1
        else:
            sent += 1

        pending += 1
        if pending >= args.batch:
            pending = 0
            elapsed = time.time() - started
            done = i + 1
            print(f"  {done}/{total_traces}  ({done / elapsed:.0f}/s)", end="\r", flush=True)

    print()
    print(f"seeded {sent} traces ({failed} failed) in {time.time() - started:.1f}s")
    print("give it ~30s for the collector to flush, then open http://localhost:3000")
    return 0


if __name__ == "__main__":
    sys.exit(main())
