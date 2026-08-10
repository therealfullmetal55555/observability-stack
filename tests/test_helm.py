"""
Helm chart checks that don't need Helm installed.

`helm lint` and `helm template` run in CI where the binary is available. These
catch the rest locally: a template referencing a file that isn't in the chart, a
values key that nothing reads, a chart copy of the dashboards that has drifted
from the repo copy.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

CHART = "helm/observability-stack"


@pytest.fixture(scope="module")
def chart_path(repo_root) -> Path:
    path = repo_root / CHART
    assert path.is_dir(), "helm chart directory is missing"
    return path


@pytest.fixture(scope="module")
def values(chart_path) -> dict:
    return yaml.safe_load((chart_path / "values.yaml").read_text())


@pytest.fixture(scope="module")
def templates(chart_path) -> dict[str, str]:
    return {
        p.name: p.read_text()
        for p in sorted((chart_path / "templates").iterdir())
        if p.suffix in {".yaml", ".tpl", ".txt"}
    }


# ---------------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------------


def test_chart_metadata_is_complete(chart_path):
    chart = yaml.safe_load((chart_path / "Chart.yaml").read_text())
    for field in ("apiVersion", "name", "description", "version", "appVersion"):
        assert chart.get(field), f"Chart.yaml is missing {field}"
    assert chart["apiVersion"] == "v2"
    # Helm enforces semver on version; a leading "v" is a classic silent failure.
    assert re.match(r"^\d+\.\d+\.\d+", chart["version"]), "chart version must be semver"


def test_chart_version_matches_the_python_package(repo_root, chart_path):
    chart = yaml.safe_load((chart_path / "Chart.yaml").read_text())
    pyproject = (repo_root / "pyproject.toml").read_text()
    version = re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE).group(1)
    assert chart["appVersion"] == version, "Chart.appVersion and the package version have drifted"


# ---------------------------------------------------------------------------
# templates
# ---------------------------------------------------------------------------


def test_every_files_get_reference_resolves(chart_path, templates):
    """`.Files.Get "rules/alerts.yml"` fails the install if the file isn't shipped."""
    for name, body in templates.items():
        for referenced in re.findall(r'\.Files\.Get\s+"([^"]+)"', body):
            assert (chart_path / referenced).exists(), f"{name} reads missing file {referenced}"


def _strip_go_template(body: str) -> str:
    """Blank out Go template actions so the YAML underneath can be parsed."""
    # {{/* ... */}} comments, single-line and multi-line.
    body = re.sub(r"\{\{-?\s*\/\*.*?\*\/\s*-?\}\}", "", body, flags=re.DOTALL)
    lines = []
    for line in body.splitlines():
        stripped = line.strip()
        # Control-flow-only lines contribute nothing parseable.
        if re.match(r"^\{\{-?\s*(if|else|else if|end|range|with|define)\b", stripped):
            continue
        if re.match(r"^-?\}\}\s*$", stripped):
            continue
        # Inline actions become a placeholder scalar so `key: value` stays valid.
        rewritten = re.sub(r"\{\{.*?\}\}", "PLACEHOLDER", line)
        # A line that was nothing but an action contributes no YAML. `{{- $x := ... -}}`
        # assignments are the common case and would otherwise parse as a stray scalar.
        if rewritten.strip() == "PLACEHOLDER":
            continue
        lines.append(rewritten)
    return "\n".join(lines)


def test_templates_parse_as_yaml_after_stripping_actions(chart_path, templates):
    """
    Crude, but it catches the failure mode that hurts: a block that ends up
    misindented and only shows up as an install error on someone else's cluster.
    """
    skip = {"_helpers.tpl", "NOTES.txt"}
    for name, body in templates.items():
        if name in skip:
            continue
        stripped = _strip_go_template(body)
        try:
            list(yaml.safe_load_all(stripped))
        except yaml.YAMLError as exc:
            pytest.fail(f"{name} does not parse as YAML once actions are stripped: {exc}")


def test_chart_templates_all_have_a_kind_and_name(chart_path):
    """
    `kind:` may be templated (`{{ if ... }}StatefulSet{{ else }}Deployment{{ end }}`),
    so the check is that at least one literal Kubernetes kind survives on the line.
    """
    resource_kinds = {
        "ConfigMap", "Secret", "Service", "ServiceAccount", "Deployment", "StatefulSet",
        "DaemonSet", "Ingress", "PersistentVolumeClaim", "PodDisruptionBudget",
        "ClusterRole", "ClusterRoleBinding", "Role", "RoleBinding", "ServiceMonitor",
        "HorizontalPodAutoscaler", "NetworkPolicy", "Job", "CronJob",
    }
    for path in (chart_path / "templates").glob("*.yaml"):
        for line in path.read_text().splitlines():
            if not line.startswith("kind:"):
                continue
            found = {k for k in resource_kinds if re.search(rf"\b{k}\b", line)}
            assert found, f"{path.name} has a kind that resolves to nothing: {line.strip()}"


def test_no_template_hardcodes_a_namespace(chart_path):
    """A chart that hardcodes `observability` breaks the second install."""
    for path in (chart_path / "templates").glob("*.yaml"):
        body = path.read_text()
        for line in body.splitlines():
            if "namespace:" not in line or "{{" not in line:
                continue
            assert "Release.Namespace" in line, f"{path.name} pins a namespace: {line.strip()}"


def test_helpers_define_the_names_templates_use(chart_path, templates):
    helpers = (chart_path / "templates" / "_helpers.tpl").read_text()
    defined = set(re.findall(r'define\s+"([^"]+)"', helpers))
    used = set()
    for body in templates.values():
        used.update(re.findall(r'include\s+"([^"]+)"', body))
    missing = used - defined
    assert not missing, f"templates call undefined helpers: {sorted(missing)}"


def test_every_top_level_helper_has_an_include(chart_path, templates):
    helpers = (chart_path / "templates" / "_helpers.tpl").read_text()
    defined = set(re.findall(r'define\s+"([^"]+)"', helpers))
    used = set()
    for body in templates.values():
        used.update(re.findall(r'include\s+"([^"]+)"', body))
    unused = {h for h in defined - used if not h.endswith("selectorLabels")}
    # A helper nobody calls is dead weight, but it's a warning not a failure —
    # some exist for chart consumers to use from their own templates.
    assert len(unused) <= 2, f"unused helpers piling up: {sorted(unused)}"


# ---------------------------------------------------------------------------
# values
# ---------------------------------------------------------------------------


def _mib(quantity: str) -> int:
    """Parse a Kubernetes memory quantity into MiB. `1Gi` is 1024, not 1."""
    match = re.match(r"^(\d+)([KMGT]i?)?$", quantity.strip())
    assert match, f"unparseable memory quantity {quantity!r}"
    amount, unit = int(match.group(1)), (match.group(2) or "")
    factor = {"": 1, "K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024,
              "Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1024 * 1024}[unit]
    return int(amount * factor)


def test_required_value_groups_exist(values):
    for group in ("collector", "tempo", "prometheus", "loki", "grafana", "alertmanager"):
        assert group in values, f"values.yaml is missing the {group} section"


def test_every_component_can_be_disabled(values):
    """Every component gets an `enabled` switch so the chart composes."""
    for group in ("tempo", "prometheus", "loki", "grafana", "alertmanager", "nodeExporter"):
        assert "enabled" in values[group], f"{group} has no enabled flag"


def test_resource_limits_exist_and_requests_fit(values):
    for group in ("collector", "tempo", "prometheus", "loki", "grafana", "alertmanager"):
        resources = values[group].get("resources", {})
        requests = resources.get("requests", {})
        limits = resources.get("limits", {})
        assert requests and limits, f"{group} has no resource requests/limits"

        assert _mib(requests["memory"]) <= _mib(limits["memory"]), (
            f"{group} requests more memory than it's allowed "
            f"({requests['memory']} > {limits['memory']})"
        )


def test_memory_limiter_sits_below_the_container_limit(values):
    """A limiter above the container limit never trips — the OOM killer wins."""
    limiter = values["collector"]["config"]["memoryLimiter"]["limitMib"]
    container_limit = _mib(values["collector"]["resources"]["limits"]["memory"])
    assert limiter < container_limit, (
        f"memory_limiter ({limiter} MiB) is not below the container limit ({container_limit} MiB)"
    )


def test_tail_sampling_buffer_covers_the_decision_window(values):
    """
    num_traces must hold everything that arrives during decision_wait, or the
    sampler evicts traces before it decides and the errors you cared about are
    the first to go.
    """
    sampling = values["collector"]["config"]["tailSampling"]
    wait_s = int(re.sub(r"[^0-9]", "", sampling["decisionWait"]))
    needed = wait_s * sampling["expectedNewTracesPerSec"]
    assert sampling["numTraces"] >= needed, (
        f"num_traces={sampling['numTraces']} holds less than one decision window ({needed} traces)"
    )


def test_production_values_raise_retention_and_resources(values, chart_path):
    prod = yaml.safe_load((chart_path / "values-production.yaml").read_text())
    assert prod["collector"]["replicaCount"] >= 2, "a single collector is a single point of failure"
    assert prod["collector"]["config"]["debugExporter"] is False, "the debug exporter must be off in prod"
    assert prod["grafana"]["ingress"]["enabled"] is True, "no ingress means nobody opens Grafana"
    assert prod["serviceMonitor"]["enabled"] is True
    assert prod["tempo"]["persistence"]["size"] != values["tempo"]["persistence"]["size"]


def test_production_does_not_inline_secrets(chart_path):
    prod = yaml.safe_load((chart_path / "values-production.yaml").read_text())
    assert prod["grafana"]["admin"].get("existingSecret"), "prod must source the admin password from a secret"
    assert prod["grafana"]["alerting"].get("existingSecret"), "prod must source contact-point creds from a secret"


# ---------------------------------------------------------------------------
# synced copies
# ---------------------------------------------------------------------------


def test_chart_dashboards_match_the_repo_dashboards(repo_root, chart_path):
    repo = {p.name: p.read_text() for p in (repo_root / "grafana/dashboards").glob("*.json")}
    shipped = {p.name: p.read_text() for p in (chart_path / "dashboards").glob("*.json")}

    assert set(repo) == set(shipped), (
        f"dashboard sets differ — repo has {sorted(repo)}, chart has {sorted(shipped)}. "
        "Run `make helm-sync`."
    )
    for name in repo:
        assert json.loads(repo[name]) == json.loads(shipped[name]), (
            f"{name} has drifted between grafana/dashboards and the chart. Run `make helm-sync`."
        )


def test_chart_alert_rules_match_the_repo_rules(repo_root, chart_path):
    repo = (repo_root / "prometheus/alerts.yml").read_text()
    shipped = (chart_path / "rules/alerts.yml").read_text()
    assert yaml.safe_load(repo) == yaml.safe_load(shipped), (
        "prometheus/alerts.yml and the chart's copy have drifted. Run `make helm-sync`."
    )
