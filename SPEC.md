# Specification

Reference document for anyone maintaining or extending this stack. The README is
for people deciding whether to use it; this is for people who already have.

---

## 1. Scope

### In scope

- Instrumentation for Python and TypeScript AI agents, plus n8n workflows.
- A collector pipeline that turns raw spans into cost and quality signals.
- Storage for traces, metrics and logs, with retention chosen for incident work.
- Five dashboards and fifteen alerts, all provisioned from files in this repo.
- Local and cluster deployment paths.

### Out of scope

- Prompt or response storage. Deliberately not captured by default.
- Correctness evaluation. Citation coverage is a heuristic, not a grade.
- Auto-remediation, self-healing, or agent-driven alert response.
- Multi-cluster federation. One collector fleet per cluster, aggregated upstream
  if you need that.

---

## 2. Functional requirements

| ID | Requirement | Verified by |
|---|---|---|
| FR-1 | Python agents can be instrumented without importing OTel directly | `tests/test_instrumentation.py` |
| FR-2 | Tool spans nest under the agent span automatically | `test_tool_span_is_a_child_of_the_agent_span` |
| FR-3 | Cost is computed from token counts when the SDK doesn't provide it | `tests/test_pipeline.py`, `tokencost` processor tests |
| FR-4 | An existing non-zero cost from the SDK is preserved | `TestNonZeroExistingCostIsPreserved` (Go) |
| FR-5 | Longest model key wins, so `gpt-4o-mini` never prices as `gpt-4o` | `TestLongestModelKeyWins` (Go), `TestPricing` (Python) |
| FR-6 | Unknown models are visible, never silently priced at zero | `TestUnknownModelKeepsTheSpanByDefault` (Go) |
| FR-7 | Citation coverage is recorded per answer and aggregated per service | `tests/test_instrumentation.py`, hallucination dashboard |
| FR-8 | Errors, slow traces, expensive calls and uncited answers survive sampling | `test_tail_sampling_keeps_error_traces`, `test_uncited_answers_are_sampled_in` |
| FR-9 | Every alert has a severity, a summary, a description and a runbook entry | `test_every_alert_has_severity_and_annotations` |
| FR-10 | Dashboards reference only metrics the instrumentation emits | `test_dashboards_query_only_metrics_the_collector_can_scrape` |
| FR-11 | Traces link to logs and back | `grafana/provisioning/datasources/datasources.yaml` derived fields, `test_pipeline.py` |
| FR-12 | The stack starts with one command and no manual import step | `make up`, CI `stack` job |
| FR-13 | A fresh stack can be filled with believable data | `scripts/seed_demo_data.py` |
| FR-14 | UI dashboard edits can be pulled back into the repo | `scripts/export_dashboards.py` |

---

## 3. Non-functional requirements

| ID | Requirement | Target | Verified by |
|---|---|---|---|
| NFR-1 | Collector overhead | < 5% CPU on a 2-core box at 100 spans/s | manual, `docker stats` |
| NFR-2 | Trace→dashboard latency | < 30s p95 from span end to visible | `decision_wait` 30s + 5s batch |
| NFR-3 | Alert latency | < 90s from condition to Slack | rule `for` + group_wait |
| NFR-4 | Storage growth | < 5 GB/day at 100 spans/s with 10% sampling | Tempo/Prometheus retention settings |
| NFR-5 | Instrumentation must never break the agent | all instrumentation calls are non-raising | `test_heartbeat_does_not_raise_without_a_collector` |
| NFR-6 | Cold start | stack up and healthy in < 60s | `docker compose up -d --wait` in CI |
| NFR-7 | Config errors caught before deploy | 100% of dashboard/alert/pipeline wiring | `make validate` in CI |
| NFR-8 | Backwards-compatible metric names | no renames within a major version | semantic conventions doc |

---

## 4. Semantic conventions

Attribute names are the API. Renaming one silently breaks a dashboard, so they're
listed here and tested.

### Agent

| Attribute | Type | Required | Notes |
|---|---|---|---|
| `gen_ai.agent.name` | string | yes | Stable identifier, not the class name |
| `gen_ai.operation.name` | string | yes | Often the same as the agent name for a simple service |
| `gen_ai.workflow.id` | string | no | Groups steps of one run |

### Tool

| Attribute | Type | Required | Notes |
|---|---|---|---|
| `gen_ai.tool.name` | string | yes | Bounded set. Never interpolate an id into it |
| `gen_ai.tool.status` | string | yes | `success` \| `error` \| `retry` |
| `gen_ai.tool.retry_count` | int | no | Recorded when non-zero |
| `gen_ai.tool.duration_ms` | double | yes | Also a metric; kept on the span for trace filtering |
| `gen_ai.tool.cost_usd` | double | no | When the tool itself costs money |

### LLM

Aligned with the OpenTelemetry GenAI conventions:

| Attribute | Type | Required |
|---|---|---|
| `gen_ai.request.model` | string | yes |
| `gen_ai.response.model` | string | no — set when it differs, i.e. a fallback engaged |
| `gen_ai.usage.input_tokens` | int | yes |
| `gen_ai.usage.output_tokens` | int | yes |
| `gen_ai.usage.total_tokens` | int | filled by `tokencost` if absent |
| `gen_ai.response.cost_usd` | double | filled by `tokencost` if absent |
| `gen_ai.response.finish_reason` | string | no |
| `gen_ai.response.id` | string | no |

### Answer quality

| Attribute | Type | Notes |
|---|---|---|
| `gen_ai.response.has_citations` | bool | Drives the sampling policy and the breakdown panel |
| `gen_ai.response.citation_coverage` | double 0–1 | Fraction of sentences carrying a marker |
| `gen_ai.response.citations` | string | JSON array, truncated to 50 entries |
| `gen_ai.response.sources` | string | JSON array of retrieved document ids |
| `gen_ai.response.is_refusal` | bool | Guardrail or model refusal |
| `gen_ai.response.refusal_reason` | string | Policy name |

---

## 5. Metrics

Every metric below is emitted by the instrumentation. Anything else in a dashboard
is a bug, and `make validate` will tell you.

| Metric | Type | Labels | Used by |
|---|---|---|---|
| `gen_ai_agent_calls_total` | counter | `service.name`, `operation`, `status` | Agent Overview, alerts |
| `gen_ai_agent_latency_seconds` | histogram | `service.name`, `operation` | Agent Overview, Executive Summary |
| `gen_ai_agent_up` | up/down counter | `service.name` | `AgentDown` |
| `gen_ai_tool_calls_total` | counter | `service.name`, `tool.name`, `status` | Tool Calls |
| `gen_ai_tool_call_duration_seconds` | histogram | `service.name`, `tool.name` | Tool Calls, `ToolHighLatency` |
| `gen_ai_tool_retries_total` | counter | `service.name`, `tool.name` | Retry rate panel |
| `gen_ai_llm_calls_total` | counter | `service.name`, `model`, `status` | Executive Summary, `LLMHighErrorRate` |
| `gen_ai_llm_latency_seconds` | histogram | `service.name`, `model` | `LLMLatencyHigh` |
| `gen_ai_llm_tokens_total` | counter | `service.name`, `model`, `type` | Token rates |
| `gen_ai_llm_cost_usd_total` | counter | `service.name`, `model` | Token Economics, cost alerts |
| `gen_ai_responses_total` | counter | `service.name`, `has_citations` | Hallucination Guard |
| `gen_ai_responses_refusal_total` | counter | `service.name` | Refusal rate |
| `gen_ai_response_citation_coverage` | histogram | `service.name` | Coverage panels |
| `vector_db_operations_total` | counter | `service.name`, `operation`, `status` | Vector DB alerts |
| `vector_db_query_duration_seconds` | histogram | `service.name`, `operation` | Vector DB alerts |

**Histogram buckets.** Chosen per signal, not shared:

| Metric | Boundaries (seconds) |
|---|---|
| agent latency | 0.01 … 300 |
| tool duration | 0.005 … 60 |
| LLM latency | 0.1 … 300 |
| vector query | 0.001 … 5 |

Agent and LLM buckets are deliberately wide. A bucket boundary at 1s tells you
nothing about an agent that either answers in 300ms or takes 12 seconds.

---

## 6. Dashboards

| File | UID | Panels | Audience | Refresh |
|---|---|---|---|---|
| `agent-overview.json` | `agent-overview` | 7 | On-call engineer, first thing | 10s |
| `token-economics.json` | `token-economics` | 7 | Whoever owns the budget | 1m |
| `tool-calls.json` | `tool-calls` | 5 | Whoever owns the broken tool | 10s |
| `hallucination-guard.json` | `hallucination-guard` | 7 | Whoever owns answer quality | 1m |
| `executive-summary.json` | `executive-summary` | 6 | A lead, on a Monday | 5m |

Constraints:

- UIDs are stable and fixed. Renaming one breaks every saved link.
- No panel queries a metric that isn't in section 5.
- Trace tables use TraceQL and link into Tempo, not a second tool.
- Template variables: `service`, `model`, `tool`, `daily_budget_usd`.

---

## 7. Alerting

Ownership: **Prometheus evaluates, Grafana delivers.** Two copies of a rule set
drift; one doesn't.

| Rule | Condition | For | Severity |
|---|---|---|---|
| `AgentDown` | no heartbeat >60s | 1m | critical |
| `AgentHighErrorRate` | errors/total >10% over 5m | 2m | warning |
| `AgentHighLatency` | p99 >30s | 5m | warning |
| `ToolHighErrorRate` | >10% per tool | 2m | warning |
| `ToolHighLatency` | p99 >10s per tool | 5m | warning |
| `ToolHighCost` | >$10/s per tool | 5m | warning |
| `LLMLatencyHigh` | p99 >30s per model | 5m | warning |
| `LLMHighErrorRate` | >10% per model | 2m | warning |
| `LLMCostHigh` | >$5/s per model | 5m | warning |
| `VectorDBHighLatency` | p99 >500ms | 5m | warning |
| `VectorDBHighErrorRate` | >5% | 2m | warning |
| `CollectorDown` | `up == 0` | 1m | critical |
| `CollectorHighMemory` | RSS >1GB | 5m | warning |
| `PrometheusDown` | `up == 0` | 1m | critical |
| `GrafanaDown` | `up == 0` | 1m | critical |

Routing:

| Severity | Channel | Repeat |
|---|---|---|
| critical | PagerDuty + `#critical-alerts` + email | 1h |
| warning | `#warnings` + email | 4h |
| cost (`*Cost*`, `*Budget*`) | `#llm-cost` | 12h |

Inhibition: a critical suppresses its warning sibling on the same alertname and
service.

---

## 8. Retention and sizing

| Store | Default | Production | Notes |
|---|---|---|---|
| Tempo | 7d | 14d | Raise this first — it's what you open during an incident |
| Prometheus | 30d | 90d | Needed for a monthly cost comparison |
| Loki | 7d | 30d | Logs serve the incident, not the quarter |

Sizing at 100 spans/s with 10% sampling and ~2 KB/span:

- Tempo: ~1.7 GB/day → 12 GB at 7d
- Prometheus: ~2 GB at 30d for ~500 active series
- Loki: depends entirely on what your agents log

---

## 9. Security

- All containers run non-root. The collector uses a distroless base with no shell.
- Grafana's root filesystem is read-only where the image allows it.
- No prompt or response bodies are captured. Nothing in the semantic conventions
  carries user content — `gen_ai.response.citations` holds document ids.
- `gen_ai.tool.parameters` is captured, and it is the one place user data can leak.
  Redact at the agent if your tool arguments carry PII.
- Secrets come from environment variables locally and from existing Kubernetes
  Secrets in production. No secret is inlined in `values-production.yaml`.
- The collector's pprof and zpages extensions bind to `127.0.0.1` only.

---

## 10. Failure modes

| Failure | Detection | Behaviour | Recovery |
|---|---|---|---|
| Collector OOM | `CollectorHighMemory`, `CollectorDown` | memory_limiter refuses data before the kernel kills it | raise `limit_mib`, reduce `expected_new_traces_per_sec` |
| Tempo full | exporter retries, `otelcol_exporter_queue_size` climbs | spans queue in memory, then drop | raise retention volume, or lower Tempo retention |
| Prometheus refuses writes | `up{job="prometheus"} == 0` | collector keeps queueing | free disk, restart |
| Agent stops emitting | `AgentDown` after 60s | nothing else notices | it's your problem — the heartbeat is 1 line |
| Unknown model | `tokencost` warning, once per model | span kept, unpriced | add to the price table |
| Config error on restart | collector won't start, `CollectorDown` | full telemetry outage | `make validate` before deploy; this is what it's for |
| Tail sampler evicts | nothing — it's silent | traces missing pre-decision | raise `num_traces`; validated against `decision_wait` |

The last row is the one that bites. A sampler that evicts before deciding looks
identical to a healthy sampler until you need a trace that isn't there, which is
why `num_traces` is checked against `decision_wait × expected_new_traces_per_sec`.

---

## 11. Extension points

**Add a metric.** Emit it from the instrumentation, add it to section 5 here,
add it to `KNOWN_METRICS` in `scripts/validate.py`, then build the panel. In that
order — the validator is what stops a dashboard from silently querying nothing.

**Add an alert.** Add it to `prometheus/alerts.yml` with a severity, a summary and
a description; write the runbook entry; `make helm-sync`. It gets a runbook entry
or it doesn't ship.

**Add a price.** Add it to `tokencost.models` in `otel-collector/config.yaml` and
to `MODEL_PRICING` in the instrumentation — the collector recomputes centrally,
but agents report cost when they can, and the two tables should agree.

**Add a dashboard.** Drop the JSON in `grafana/dashboards/`, give it a unique uid,
run `make validate && make test`, then `make helm-sync`.

**Swap a store.** Keep the interfaces: OTLP in, PromQL and TraceQL out. Anything
behind those — ClickHouse for traces, Mimir for metrics — is a config change in
`otel-collector/config.yaml` plus a datasource uid.
