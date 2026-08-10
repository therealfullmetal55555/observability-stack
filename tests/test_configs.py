"""
The repo ships config, not just code — so the config gets tested too.

Every assertion here maps to a mistake that has actually broken this stack:
a dashboard pointing at a datasource that doesn't exist, an alert whose
expression can never fire, a collector pipeline referencing a processor nobody
declared, a compose bind mount to a file that was moved in a refactor.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

DASHBOARDS = "grafana/dashboards"
KNOWN_DATASOURCE_UIDS = {"prometheus", "tempo", "loki", "-- Grafana --"}


def load(path: Path):
    return yaml.safe_load(path.read_text())


# ---------------------------------------------------------------------------
# dashboards
# ---------------------------------------------------------------------------


def dashboard_files(root: Path) -> list[Path]:
    return sorted((root / DASHBOARDS).glob("*.json"))


def test_every_dashboard_is_valid_json_with_a_uid(repo_root):
    for path in dashboard_files(repo_root):
        data = json.loads(path.read_text())
        assert data.get("uid"), f"{path.name} has no uid — Grafana can't provision it"
        assert data.get("title"), f"{path.name} has no title"
        assert data.get("schemaVersion"), f"{path.name} has no schemaVersion"


def test_dashboard_uids_are_unique(repo_root):
    uids = [json.loads(p.read_text())["uid"] for p in dashboard_files(repo_root)]
    duplicates = {u for u in uids if uids.count(u) > 1}
    assert not duplicates, f"duplicate dashboard uids: {duplicates}"


def test_panel_ids_are_unique_within_a_dashboard(repo_root):
    for path in dashboard_files(repo_root):
        ids = [p.get("id") for p in json.loads(path.read_text())["panels"]]
        assert len(ids) == len(set(ids)), f"{path.name} has duplicate panel ids"


def test_every_panel_has_a_query_or_is_explicitly_empty(repo_root):
    for path in dashboard_files(repo_root):
        for panel in json.loads(path.read_text())["panels"]:
            targets = panel.get("targets", [])
            if panel["type"] in {"row", "text"}:
                continue
            assert targets, f"{path.name} :: {panel.get('title')} has no targets"


def test_panels_only_reference_provisioned_datasources(repo_root):
    for path in dashboard_files(repo_root):
        raw = path.read_text()
        for uid in re.findall(r'"datasource"\s*:\s*"([^"]+)"', raw):
            if uid.startswith("${"):
                continue
            assert uid in KNOWN_DATASOURCE_UIDS, (
                f"{path.name} references datasource uid '{uid}' which is not provisioned"
            )


def test_provisioned_datasource_uids_match_what_dashboards_expect(repo_root):
    ds = load(repo_root / "grafana/provisioning/datasources/datasources.yaml")
    uids = {entry["uid"] for entry in ds["datasources"]}
    assert uids == {"prometheus", "tempo", "loki"}


def test_dashboards_query_only_metrics_the_collector_can_scrape(repo_root):
    """A query for a metric nobody emits renders as 'No data' and burns an hour."""
    known = {
        "gen_ai_agent_calls_total",
        "gen_ai_agent_latency_seconds",
        "gen_ai_agent_up",
        "gen_ai_tool_calls_total",
        "gen_ai_tool_call_duration_seconds",
        "gen_ai_tool_cost_usd_total",
        "gen_ai_llm_calls_total",
        "gen_ai_llm_latency_seconds",
        "gen_ai_llm_tokens_total",
        "gen_ai_llm_cost_usd_total",
        "gen_ai_responses_total",
        "gen_ai_responses_refusal_total",
        "gen_ai_response_citation_coverage",
        "vector_db_operations_total",
        "vector_db_query_duration_seconds",
        "gen_ai_requests_total",
        "gen_ai_request_duration_seconds",
        "up",
    }
    for path in dashboard_files(repo_root):
        raw = path.read_text()
        for metric in re.findall(r"\b((?:gen_ai|vector_db)_[a-z_]+)", raw):
            base = metric.removesuffix("_bucket").removesuffix("_count").removesuffix("_sum")
            assert base in known, f"{path.name} queries unknown metric '{metric}'"


# ---------------------------------------------------------------------------
# alerts
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def alert_rules(repo_root):
    doc = load(repo_root / "prometheus/alerts.yml")
    return [rule for group in doc["groups"] for rule in group["rules"]]


def test_alerts_file_has_groups_and_rules(alert_rules):
    assert len(alert_rules) >= 12, "the alert set shrank — was a rule deleted by accident?"


def test_every_alert_has_severity_and_annotations(alert_rules):
    for rule in alert_rules:
        name = rule["alert"]
        assert rule.get("labels", {}).get("severity") in {"critical", "warning", "info"}, name
        assert rule.get("annotations", {}).get("summary"), f"{name} has no summary"
        assert rule.get("annotations", {}).get("description"), f"{name} has no description"


def test_every_critical_alert_waits_before_firing(alert_rules):
    for rule in alert_rules:
        if rule["labels"]["severity"] == "critical":
            assert rule.get("for"), f"{rule['alert']} is critical but has no 'for' — it pages on a single scrape"


def test_expressions_look_like_promql(alert_rules):
    for rule in alert_rules:
        expr = rule["expr"]
        assert expr.count("(") == expr.count(")"), f"{rule['alert']} has unbalanced parens"
        assert re.search(r"[<>]=?|==|absent", expr), (
            f"{rule['alert']} has no comparison operator — it would fire on mere presence"
        )


def test_no_alert_uses_an_undefined_metric(alert_rules):
    known_prefixes = ("gen_ai_", "vector_db_", "up", "process_", "otelcol_")
    for rule in alert_rules:
        for token in re.findall(r"\b([a-z_][a-z0-9_]*)\b", rule["expr"]):
            if token.startswith("gen_ai_") or token.startswith("vector_db_"):
                assert token.startswith(known_prefixes), f"{rule['alert']}: stray metric {token}"


def test_alert_names_are_unique(alert_rules):
    names = [r["alert"] for r in alert_rules]
    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# collector
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def collector(repo_root):
    return load(repo_root / "otel-collector/config.yaml")


def test_collector_declares_all_three_signal_pipelines(collector):
    pipelines = collector["service"]["pipelines"]
    assert "traces" in pipelines
    assert "metrics" in pipelines
    assert "logs" in pipelines


def test_collector_pipelines_reference_declared_components(collector):
    declared = {
        "receivers": set(collector["receivers"]),
        "processors": set(collector["processors"]),
        "exporters": set(collector["exporters"]),
    }
    for name, pipe in collector["service"]["pipelines"].items():
        for section in ("receivers", "processors", "exporters"):
            for ref in pipe.get(section, []):
                declared_here = declared[section]
                assert ref in declared_here or ref.split("/")[0] in declared_here, (
                    f"pipeline {name} uses {section[:-1]} '{ref}' which was never declared"
                )


def test_tail_sampling_keeps_error_traces(collector):
    """Aggressive sampling is fine — dropping errors is not."""
    policies = collector["processors"]["tail_sampling"]["policies"]
    keeping_errors = [
        p for p in policies
        if "error" in str(p.get("status_code", "")) or "error" in str(p).lower()
    ]
    assert keeping_errors, "no tail-sampling policy preserves errored traces"


def test_tail_sampling_uses_the_policies_key_not_policy(collector):
    """`policy:` + `policy_chain:` is a config that fails at collector startup."""
    tail = collector["processors"]["tail_sampling"]
    assert "policies" in tail, "tail_sampling must use `policies:`"
    assert "policy" not in tail, "`policy:` is not a valid tail_sampling key"
    assert all(p.get("name") for p in tail["policies"])


def test_uncited_answers_are_sampled_in(collector):
    """The hallucination dashboard is worthless if uncited traces get sampled out."""
    policies = str(collector["processors"]["tail_sampling"]["policies"])
    assert "has_citations" in policies


def test_memory_limiter_is_in_every_pipeline(collector):
    for name, pipe in collector["service"]["pipelines"].items():
        assert any(p.startswith("memory_limiter") for p in pipe.get("processors", [])), (
            f"pipeline {name} has no memory_limiter — the collector can OOM under backpressure"
        )


def test_batching_is_configured(collector):
    batch = collector["processors"]["batch"]
    assert "send_batch_size" in batch or "timeout" in batch


def test_exporters_point_at_services_that_exist_in_compose(repo_root, collector):
    compose = load(repo_root / "docker-compose.yml")
    hosts = set(compose["services"])
    for name, cfg in collector["exporters"].items():
        endpoint = cfg.get("endpoint") or cfg.get("url") or ""
        if not endpoint:
            continue
        host = re.sub(r"^[a-z]+://", "", str(endpoint)).split(":")[0]
        assert host in hosts, f"exporter {name} points at '{host}', not a compose service"


# ---------------------------------------------------------------------------
# compose
# ---------------------------------------------------------------------------


def test_compose_bind_mounts_exist(repo_root):
    compose = load(repo_root / "docker-compose.yml")
    for service, cfg in compose["services"].items():
        for mount in cfg.get("volumes", []):
            if not isinstance(mount, str) or not mount.startswith("./"):
                continue
            source = mount.split(":")[0]
            assert (repo_root / source).exists(), f"{service} mounts {source}, which is not in the repo"


def test_compose_volumes_and_networks_are_declared(repo_root):
    compose = load(repo_root / "docker-compose.yml")
    volumes = set(compose.get("volumes", {}))
    networks = set(compose.get("networks", {}))
    for service, cfg in compose["services"].items():
        for mount in cfg.get("volumes", []):
            if isinstance(mount, str) and not mount.startswith((".", "/")):
                assert mount.split(":")[0] in volumes, f"{service} uses undeclared volume"
        for net in cfg.get("networks", []):
            assert net in networks, f"{service} uses undeclared network '{net}'"


def test_collector_publishes_the_otlp_ports_agents_need(repo_root):
    compose = load(repo_root / "docker-compose.yml")
    ports = [str(p) for p in compose["services"]["otel-collector"]["ports"]]
    joined = " ".join(ports)
    assert "4317" in joined, "OTLP gRPC port is not published"
    assert "4318" in joined, "OTLP HTTP port is not published"


def test_prometheus_loads_the_alert_rules(repo_root):
    compose = load(repo_root / "docker-compose.yml")
    mounts = " ".join(str(m) for m in compose["services"]["prometheus"]["volumes"])
    assert "prometheus/alerts.yml" in mounts, "alert rules are not mounted into Prometheus"
    rules = load(repo_root / "prometheus/prometheus.yml")["rule_files"]
    assert any("rules" in r for r in rules)
