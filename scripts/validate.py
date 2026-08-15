#!/usr/bin/env python3
"""
Config doctor for the observability stack.

Catches the mistakes that actually happen: a dashboard referencing a datasource
uid that doesn't exist, an alert using a metric nobody emits, a pipeline entry
pointing at a processor that was never declared. Each of those takes down
silently — Grafana just shows "No data" and you blame the agent.

    python scripts/validate.py            # everything
    python scripts/validate.py --alerts   # one family
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

try:
    import yaml
except ImportError:  # pragma: no cover
    print("pyyaml is required: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

ROOT = Path(__file__).resolve().parent.parent

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

# Metrics the instrumentation package actually produces. If an alert or a
# dashboard query mentions a gen_ai_* metric that isn't here, it will never fire.
KNOWN_METRICS = {
    "gen_ai_agent_calls_total",
    "gen_ai_agent_latency_seconds_bucket",
    "gen_ai_agent_latency_seconds",
    "gen_ai_agent_up",
    "gen_ai_agent_heartbeat_total",
    "gen_ai_agent_last_heartbeat_timestamp_seconds",
    "gen_ai_tool_calls_total",
    "gen_ai_tool_call_duration_seconds_bucket",
    "gen_ai_tool_call_duration_seconds",
    "gen_ai_tool_cost_usd_total",
    "gen_ai_tool_retries_total",
    "gen_ai_tool_call_duration_seconds_count",
    "gen_ai_tool_call_duration_seconds_sum",
    "gen_ai_llm_calls_total",
    "gen_ai_llm_latency_seconds_bucket",
    "gen_ai_llm_latency_seconds",
    "gen_ai_llm_tokens_total",
    "gen_ai_llm_cost_usd_total",
    "gen_ai_response_citation_coverage",
    "gen_ai_response_citation_coverage_bucket",
    "gen_ai_response_citation_coverage_sum",
    "gen_ai_response_citation_coverage_count",
    "gen_ai_responses_total",
    "gen_ai_responses_refusal_total",
    "vector_db_operations_total",
    "vector_db_query_duration_seconds_bucket",
    "vector_db_query_duration_seconds",
    "gen_ai_requests_total",
    "gen_ai_requests_total_count",
    "gen_ai_request_duration_seconds_bucket",
    "gen_ai_request_duration_seconds_count",
    "gen_ai_llm_cost_usd_total_count",
    "gen_ai_llm_tokens_total_count",
}

# Metrics that come from the stack itself rather than from instrumentation.
INFRA_METRICS = {
    "up",
    "process_resident_memory_bytes",
    "prometheus_",
    "grafana_",
    "otelcol_",
    "loki_",
    "tempo_",
    "container_",
    "node_",
    "go_",
}

REQUIRED_ALERT_LABELS = {"severity"}
VALID_SEVERITIES = {"critical", "warning", "info"}


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, where: str, msg: str) -> None:
        self.errors.append(f"{where}: {msg}")

    def warn(self, where: str, msg: str) -> None:
        self.warnings.append(f"{where}: {msg}")

    def finish(self, title: str) -> bool:
        for w in self.warnings:
            print(f"  {YELLOW}warn{RESET}  {w}")
        for e in self.errors:
            print(f"  {RED}fail{RESET}  {e}")
        if self.errors:
            print(f"{RED}{title}: {len(self.errors)} error(s){RESET}")
            return False
        note = f" ({len(self.warnings)} warning(s))" if self.warnings else ""
        print(f"{GREEN}{title}: ok{RESET}{note}")
        return True


def load_yaml(path: Path) -> Any:
    with path.open() as fh:
        return yaml.safe_load(fh)


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


def check_configs(report: Report) -> None:
    print(f"{DIM}configs{RESET}")
    # Helm templates are Go templates, not YAML — they only parse after
    # rendering. tests/test_helm.py covers them with a template-aware pass;
    # here we only check the chart's plain-data files.
    template_dir = ROOT / "helm" / "observability-stack" / "templates"
    yaml_files = sorted(
        p for p in ROOT.rglob("*.y*ml")
        if ".git" not in p.parts and template_dir not in p.parents
    )

    for path in yaml_files:
        try:
            with path.open() as fh:
                list(yaml.safe_load_all(fh))
        except yaml.YAMLError as exc:
            report.error(rel(path), f"invalid YAML — {str(exc).splitlines()[0]}")

    compose = ROOT / "docker-compose.yml"
    if not compose.exists():
        report.error("docker-compose.yml", "missing")
        return

    data = load_yaml(compose)
    services = data.get("services", {})
    declared_volumes = set(data.get("volumes", {}) or {})
    declared_networks = set(data.get("networks", {}) or {})

    for name, svc in services.items():
        if "image" not in svc and "build" not in svc:
            report.error(f"compose.{name}", "neither image nor build specified")

        for mount in svc.get("volumes", []) or []:
            if not isinstance(mount, str) or not mount.startswith("./"):
                continue
            host = mount.split(":")[0]
            if not (ROOT / host).exists():
                report.warn(f"compose.{name}", f"bind mount source not found: {host}")

        if isinstance(svc.get("volumes"), list):
            for mount in svc["volumes"]:
                if not isinstance(mount, str) or mount.startswith((".", "/")):
                    continue
                vol = mount.split(":")[0]
                if vol and vol not in declared_volumes:
                    report.error(f"compose.{name}", f"uses undeclared volume '{vol}'")

        for net in svc.get("networks", []) or []:
            net_name = net if isinstance(net, str) else list(net)[0]
            if net_name not in declared_networks and net_name != "default":
                report.error(f"compose.{name}", f"uses undeclared network '{net_name}'")

    # The collector, prometheus and grafana containers must be reachable by the
    # names the configs use. A typo here is the #1 cause of "no data".
    expected = {"otel-collector", "tempo", "prometheus", "loki", "grafana"}
    missing = expected - set(services)
    if missing:
        report.error("docker-compose.yml", f"missing services: {', '.join(sorted(missing))}")


def check_collector(report: Report) -> None:
    print(f"{DIM}otel collector{RESET}")
    cfg_path = ROOT / "otel-collector" / "config.yaml"
    if not cfg_path.exists():
        report.error("otel-collector/config.yaml", "missing")
        return

    cfg = load_yaml(cfg_path)
    declared = {
        "receivers": set((cfg.get("receivers") or {}).keys()),
        "processors": set((cfg.get("processors") or {}).keys()),
        "exporters": set((cfg.get("exporters") or {}).keys()),
        "extensions": set((cfg.get("extensions") or {}).keys()),
    }

    pipelines = (cfg.get("service") or {}).get("pipelines") or {}
    if not pipelines:
        report.error("collector.service", "no pipelines defined")
        return

    for name, pipe in pipelines.items():
        for section in ("receivers", "processors", "exporters"):
            for ref in pipe.get(section, []) or []:
                # `transform/citations` is declared under its full name; a bare
                # `batch` may be declared either way.
                if ref in declared[section] or ref.split("/")[0] in declared[section]:
                    continue
                report.error(
                    f"collector.pipeline.{name}",
                    f"{section[:-1]} '{ref}' is used but never declared",
                )

    for section in ("processors", "exporters", "receivers"):
        for key, body in (cfg.get(section) or {}).items():
            if body is None:
                report.warn(f"collector.{section}.{key}", "empty block")

    referenced_extensions = set()
    for pipe in pipelines.values():
        referenced_extensions.update(pipe.get("extensions", []) or [])
    for ext in referenced_extensions:
        if ext.split("/")[0] not in declared["extensions"]:
            report.error("collector.service.extensions", f"'{ext}' referenced but not declared")


def check_dashboards(report: Report) -> None:
    print(f"{DIM}dashboards{RESET}")
    dash_dir = ROOT / "grafana" / "dashboards"
    if not dash_dir.exists():
        report.error("grafana/dashboards", "directory missing")
        return

    files = sorted(dash_dir.glob("*.json"))
    if not files:
        report.error("grafana/dashboards", "no dashboards found")
        return

    uids: set[str] = set()
    for path in files:
        try:
            dash = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            report.error(rel(path), f"invalid JSON — {exc}")
            continue

        for field in ("title", "uid", "panels"):
            if field not in dash:
                report.error(rel(path), f"missing top-level '{field}'")

        uid = dash.get("uid")
        if uid in uids:
            report.error(rel(path), f"duplicate dashboard uid '{uid}'")
        uids.add(uid)

        panels = dash.get("panels", [])
        if not panels:
            report.warn(rel(path), "no panels")

        for panel in panels:
            pid = panel.get("id")
            title = panel.get("title", f"panel {pid}")
            if "title" not in panel:
                report.warn(rel(path), f"panel {pid} has no title")
            if not panel.get("targets"):
                report.warn(rel(path), f"'{title}' has no queries")
            for target in panel.get("targets", []):
                expr = target.get("expr") or target.get("query") or ""
                if not expr:
                    continue
                check_promql_metrics(report, f"{rel(path)} :: {title}", expr)

    print(f"  {DIM}{len(files)} dashboards, {len(uids)} unique uids{RESET}")


def check_alerts(report: Report) -> None:
    print(f"{DIM}alerts{RESET}")
    seen_alerts: set[str] = set()
    # Prometheus owns the rules; Grafana only handles delivery. Keeping one
    # copy is the whole point — two copies drift within a month.
    files = [ROOT / "prometheus" / "alerts.yml"]

    for path in files:
        if not path.exists():
            report.warn(rel(path), "not present")
            continue

        doc = load_yaml(path)
        for group in doc.get("groups", []):
            gname = group.get("name", "?")
            if not group.get("rules"):
                report.warn(f"{rel(path)} :: {gname}", "group has no rules")

            for rule in group.get("rules", []):
                name = rule.get("alert") or rule.get("record")
                if not name:
                    report.error(f"{rel(path)} :: {gname}", "rule without alert/record name")
                    continue

                if name in seen_alerts:
                    report.warn(f"{rel(path)}", f"'{name}' defined more than once")
                seen_alerts.add(name)

                expr = rule.get("expr", "")
                if not expr:
                    report.error(f"{rel(path)} :: {name}", "no expr")
                else:
                    if expr.count("(") != expr.count(")"):
                        report.error(f"{rel(path)} :: {name}", "unbalanced parentheses in expr")
                    if not re.search(r"[<>]=?|==|absent|unless", expr):
                        report.warn(
                            f"{rel(path)} :: {name}",
                            "expr has no comparison — Prometheus treats it as a presence check",
                        )
                    check_promql_metrics(report, f"{rel(path)} :: {name}", expr)

                labels = rule.get("labels", {}) or {}
                if not REQUIRED_ALERT_LABELS & set(labels):
                    report.error(f"{rel(path)} :: {name}", "missing severity label")
                severity = labels.get("severity")
                if severity and severity not in VALID_SEVERITIES:
                    report.warn(
                        f"{rel(path)} :: {name}",
                        f"unusual severity '{severity}' (expected {', '.join(sorted(VALID_SEVERITIES))})",
                    )

                annotations = rule.get("annotations", {}) or {}
                for field in ("summary", "description"):
                    if not annotations.get(field):
                        report.warn(f"{rel(path)} :: {name}", f"no {field} annotation")
                for value in annotations.values():
                    for var in re.findall(r"\{\{\s*\$labels\.(\w+)\s*\}\}", str(value)):
                        if var not in labels and var not in {"service_name", "tool_name", "model", "operation", "instance", "job"}:
                            report.warn(
                                f"{rel(path)} :: {name}",
                                f"annotation references $labels.{var} which the rule never sets",
                            )

                if not rule.get("for") and severity == "critical":
                    report.warn(f"{rel(path)} :: {name}", "critical alert with no 'for' — fires on first scrape")

    print(f"  {DIM}{len(seen_alerts)} rules in {rel(files[0]) if files else 'no file'}{RESET}")


def check_promql_metrics(report: Report, where: str, expr: str) -> None:
    """Every gen_ai_* metric in a query must exist in the instrumentation."""
    tokens = re.findall(r"\b([a-zA-Z_:][a-zA-Z0-9_:]*)\b", expr)
    for token in tokens:
        if token.startswith("gen_ai_") or token.startswith("vector_db_"):
            if token in KNOWN_METRICS:
                continue
            # histogram_quantile queries reference _bucket series
            base = token.removesuffix("_bucket")
            if base in KNOWN_METRICS or token.removesuffix("_count") in KNOWN_METRICS:
                continue
            report.warn(where, f"references unknown metric '{token}'")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate observability stack configs")
    parser.add_argument("--configs", action="store_true", help="YAML + compose checks")
    parser.add_argument("--dashboards", action="store_true", help="dashboard JSON checks")
    parser.add_argument("--alerts", action="store_true", help="alert rule checks")
    parser.add_argument("--collector", action="store_true", help="collector pipeline checks")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    args = parser.parse_args(list(argv) if argv is not None else None)

    everything = not any([args.configs, args.dashboards, args.alerts, args.collector])

    report = Report()
    if everything or args.configs:
        check_configs(report)
    if everything or args.collector:
        check_collector(report)
    if everything or args.dashboards:
        check_dashboards(report)
    if everything or args.alerts:
        check_alerts(report)

    ok = report.finish("validate")
    if args.strict and report.warnings:
        print(f"{RED}strict mode: warnings treated as errors{RESET}")
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
