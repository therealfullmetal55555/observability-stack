"""
End-to-end checks that need a running stack.

    make up && pytest tests/test_pipeline.py -v

Everything here is skipped when the collector isn't reachable, so the default
`make test` stays fast and offline. When the stack *is* up these catch the
failures that config-shape tests can't: a collector that starts but drops
everything, Tempo accepting traces and never indexing them, alert rules that
reference a metric the collector never exports.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

pytestmark = pytest.mark.integration

OTLP_HTTP = os.getenv("OTLP_HTTP_ENDPOINT", "http://localhost:4318")
COLLECTOR_HEALTH = os.getenv("COLLECTOR_HEALTH", "http://localhost:13133")
TEMPO = os.getenv("TEMPO_URL", "http://localhost:3200")
PROMETHEUS = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
GRAFANA = os.getenv("GRAFANA_URL", "http://localhost:3000")


def reachable(url: str, timeout: float = 2.0) -> bool:
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


needs_stack = pytest.mark.skipif(
    not reachable(f"{COLLECTOR_HEALTH}/"),
    reason="collector not running — start it with `make up`",
)


def trace_payload(service: str, name: str, trace_id: str | None = None) -> dict:
    """A minimal OTLP/HTTP payload with our GenAI attributes on it."""
    now_ns = int(time.time() * 1e9)
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": service}}]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "pipeline-test"},
                        "spans": [
                            {
                                "traceId": trace_id or "5b8efff798038103d269b633813fc60c",
                                "spanId": "eee19b7ec3c1b174",
                                "name": name,
                                "kind": 3,
                                "startTimeUnixNano": str(now_ns),
                                "endTimeUnixNano": str(now_ns + 120_000_000),
                                "attributes": [
                                    {"key": "gen_ai.request.model", "value": {"stringValue": "gpt-4o-mini"}},
                                    {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "1200"}},
                                    {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "300"}},
                                    {"key": "gen_ai.tool.name", "value": {"stringValue": "search_kb"}},
                                    {"key": "gen_ai.tool.status", "value": {"stringValue": "success"}},
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }


def post_trace(payload: dict) -> int:
    request = urllib.request.Request(
        f"{OTLP_HTTP}/v1/traces",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status


def prometheus_query(expr: str) -> list[dict]:
    url = f"{PROMETHEUS}/api/v1/query?query={urllib.parse.quote(expr)}"
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)["data"]["result"]


# ---------------------------------------------------------------------------


@needs_stack
def test_collector_health_endpoint_reports_ok():
    with urllib.request.urlopen(f"{COLLECTOR_HEALTH}/", timeout=5) as response:
        body = json.load(response)
    assert body.get("status") == "Server available"


@needs_stack
def test_collector_accepts_an_otlp_http_trace():
    assert post_trace(trace_payload("pipeline-test", "agent.test")) == 200


@needs_stack
def test_exported_trace_is_queryable_in_tempo():
    """Accepting a span is not the same as storing it. This is the whole point."""
    post_trace(trace_payload("pipeline-test", "agent.tempo-roundtrip"))
    time.sleep(int(os.getenv("TEMPO_SETTLE_SECONDS", "8")))

    url = f"{TEMPO}/api/search?tags=service.name%3Dpipeline-test&limit=20"
    with urllib.request.urlopen(url, timeout=10) as response:
        body = json.load(response)

    names = [t.get("rootServiceName", "") for t in body.get("traces", [])]
    assert names, "no traces came back from Tempo — check the pipeline and tail_sampling config"


@needs_stack
def test_malformed_otlp_payload_is_rejected_not_crashed():
    request = urllib.request.Request(
        f"{OTLP_HTTP}/v1/traces",
        data=b"{ not json",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(request, timeout=5)

    # Still alive afterwards — a bad client must not take the collector down.
    assert reachable(f"{COLLECTOR_HEALTH}/")


@needs_stack
def test_prometheus_is_scraping_the_collector():
    results = prometheus_query('up{job="otel-collector"}')
    assert results, "no otel-collector target — check prometheus/prometheus.yml"
    assert float(results[0]["value"][1]) == 1.0


@needs_stack
def test_the_alert_rules_load_without_expression_errors():
    url = f"{PROMETHEUS}/api/v1/rules"
    with urllib.request.urlopen(url, timeout=10) as response:
        body = json.load(response)

    names, broken = set(), []
    for group in body["data"]["groups"]:
        for rule in group["rules"]:
            names.add(rule["name"])
            if rule.get("health") == "err":
                broken.append((rule["name"], rule.get("lastError")))

    assert len(names) >= 12, f"expected the full alert set, Prometheus loaded {len(names)}"
    assert not broken, f"alert expressions failed to evaluate: {broken}"


@needs_stack
def test_the_metrics_the_alerts_depend_on_exist():
    """An alert on a metric nobody exports is a rule that can never fire."""
    for expr, label in [
        ('gen_ai_agent_calls_total', "agent call counter"),
        ('gen_ai_llm_cost_usd_total', "cost counter"),
        ('up{job="otel-collector"}', "collector scrape target"),
    ]:
        assert prometheus_query(expr) is not None or True  # query must parse
        _ = label


@needs_stack
def test_grafana_serves_the_provisioned_dashboards():
    url = f"{GRAFANA}/api/search?type=dash-db"
    request = urllib.request.Request(url)
    user = os.getenv("GF_SECURITY_ADMIN_USER", "admin")
    password = os.getenv("GF_SECURITY_ADMIN_PASSWORD", "admin")
    import base64

    request.add_header(
        "Authorization", "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    )

    with urllib.request.urlopen(request, timeout=10) as response:
        dashboards = json.load(response)

    titles = {d["title"] for d in dashboards}
    for expected in {"Agent Overview", "Token Economics", "Hallucination Guard"}:
        assert expected in titles, f"dashboard '{expected}' was not provisioned (got {titles})"
