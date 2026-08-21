# Changelog

Notable changes only. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versions follow [Semantic Versioning](https://semver.org/).

---

## [1.0.0] — 2026-09-29

First release. Everything below is new; the notes explain *why* each piece is
shaped the way it is, because that's what you'll want in six months.

### Added

**Pipeline**

- OTel Collector config with a deliberate split of responsibility: agents emit raw
  token counts and citation markers, the collector turns them into the numbers the
  dashboards read. A provider price change is one edit, not 21 redeploys.
- Tail sampling that always keeps errors, slow traces (>10s), expensive calls
  (>$0.25) and answers with no citations. A 10% probabilistic baseline covers the
  rest.
- `transform` processors for the two things agents always forget: `tool.status`
  and `has_citations`.

**Custom processor — `tokencost`** (Go, `otel-collector/processor/tokencost/`)

- Recomputes USD from token counts against a central price table.
- Longest-key matching, so `gpt-4o-mini-2024-07-18` prices as `gpt-4o-mini`
  (0.15/M) rather than `gpt-4o` (2.50/M). A 16x error, and one that surfaces as an
  unexplained cost spike weeks later.
- `respect_existing` leaves a cost the SDK computed — vendor SDKs know about
  cached reads and batch discounts a static table doesn't. An explicit `0.0` is
  recomputed, because an unexplained zero on a paid model is worse than an estimate.
- `unknown_model: warn | zero | drop_span`. Warn is the default and logs once per
  model name, not once per span. `zero` exists for self-hosted models you forgot
  to list; `drop_span` is for fleets where untracked spend is a billing incident.
- Counts model calls that arrive with no token counts as *malformed* rather than
  pricing them at zero — that's a client bug, and it should be visible.

**Python instrumentation** — `agent_observability` (pip-installable)

- `init_telemetry`, `trace_agent`, `trace_tool`, `trace_llm` (context manager),
  `record_response_quality`, `inject_context` / `extract_context`, `heartbeat`.
- Token counts are read back off the span, not guessed. Vendor SDKs disagree too
  much for anything else to be reliable.
- `estimate_citation_coverage`: sentence-level heuristic — a sentence counts as
  sourced if it carries a bracketed marker or a URL. Cheap enough to run on every
  answer, and it catches the failure mode that matters.
- Nothing in the module raises. If telemetry breaks, the agent still answers users.

**TypeScript instrumentation**

- Same surface as Python: `traceAgent`, `traceTool`, `traceLlm`, `traceVectorDb`,
  `recordResponseQuality`, `AgentLifecycle`, `initTelemetry`.
- `model_pricing` table with a `priceCall` helper so a Node service doesn't have
  to wait for the collector round-trip to know what it just spent.

**n8n community nodes**

- `OTel Trace` / `OTel Trace End`, TypeScript, as n8n requires. Two modes:
  wrap a whole workflow (with `Wait for Downstream`) or a single step.
- Adds `n8n.workflow.id`, `n8n.execution.id`, `n8n.node.name` to every span, and
  writes `_otel.traceId` onto each item so downstream nodes can correlate their
  own logs with the trace.

**Dashboards** — five, provisioned on boot

- **Agent Overview** — request rate, error rate, latency percentiles, tool p99, recent errors.
- **Token Economics** — cost per request, cost accumulation, token rates, budget burn, cost spike ratio.
- **Tool Calls & Latency** — success rate, latency percentiles, retry rate, volume by status, slowest traces.
- **Hallucination Guard** — citation coverage, uncited answers, refusal rate, traces missing citations.
- **Executive Summary** — the four numbers a lead opens on a Monday: p50/p99, success rate, call volume, token throughput.

**Alerting** — fifteen rules in `prometheus/alerts.yml`, one copy only

- Agent, tool, LLM, vector DB and stack-health layers.
- Errors and expensive calls are never sampled out; every rule has a runbook entry.
- Routing: critical → PagerDuty + Slack, warnings → Slack, cost → its own channel
  with a 12h repeat interval, because a $5k bill is not a 2am page.

**Operations**

- `scripts/validate.py` — catches the mistakes that fail silently: a dashboard
  referencing a metric nobody emits, an alert whose expression can never fire, a
  pipeline pointing at an undeclared processor.
- `scripts/seed_demo_data.py` — two hours of backdated traffic including one
  incident, so the dashboards have a story the moment you open them.
- `tests/load/generate_traffic.py` — five scenarios (healthy, citation regression,
  cost spike, vendor outage, tight guardrails) for reproducing a specific shape.
- `scripts/export_dashboards.py` — pull UI edits back into the repo, so
  provisioning isn't a one-way door.
- `scripts/package.sh` — the deployable tarball.

**Deployment**

- `docker-compose.yml` for local, with a `custom` profile that builds the collector.
- Helm chart with values for staging and production, distroless collector image,
  PDBs, anti-affinity, and an optional ServiceMonitor.
- `Dockerfile.collector` builds the custom distribution via `ocb`.

**Tests** — 80+ across four suites

- Unit tests for instrumentation: pricing edge cases, citation heuristic, span
  parenting, error propagation, propagation round-trips.
- Config tests: every dashboard reference resolves, every alert has a severity and
  a description, every pipeline component is declared, every bind mount exists.
- Helm tests: templates parse, helpers resolve, synced copies haven't drifted,
  `memory_limiter` sits below the container limit, and `num_traces` covers a full
  decision window.
- Integration tests against a live stack, skipped automatically when it isn't running.

### Notes

- Cost and latency only make sense with a *baseline*. Run the healthy scenario for
  a day before you tune any threshold, or you'll set them to whatever today looks
  like and then never look again.
- Trace retention (7 days) is the first thing that hurts at scale. Raise Tempo
  before Prometheus — traces are what you actually open during an incident.
- The `tokencost` processor is not in contrib. Build it with
  `make collector-build` or use the published image; without it the pipeline still
  works but agents must report their own cost.

[1.0.0]: https://github.com/therealfullmetal55555/observability-stack/releases/tag/v1.0.0
