# observability-stack

Tracing, metrics and alerting for a fleet of AI agents — built around the three
questions you actually ask at 9am on a Monday:

1. Which agent got slow, and is it the model or the tool?
2. What did we spend yesterday, and on what?
3. Which answers were not grounded in a retrieved source?

OpenTelemetry Collector → Tempo (traces) · Prometheus (metrics) · Loki (logs) ·
Grafana (five dashboards) · Alertmanager (fifteen rules). Docker Compose for
local, Helm for the cluster.

```
make up && make seed && open http://localhost:3000
```

---

## Why this exists

Eleven services across Python and TypeScript all call models, all call tools, and
some of them retrieve from a vector store before answering. Standard APM gives you
`POST /chat 2.3s` and stops there. It cannot tell you that the 2.3s was 400ms of
retrieval, 1.8s of generation, and that the answer cited nothing — which is the
part that costs money and trust.

This stack puts those three numbers on the same span, in the same trace, and turns
them into dashboards and alerts that a human can act on.

### What it measures that generic APM can't

| Signal | Attribute | Where it shows up |
|---|---|---|
| Cost per answer | `gen_ai.response.cost_usd` | Token Economics, budget burn |
| Tokens in vs out | `gen_ai.usage.input_tokens` / `output_tokens` | Cost per request, token rate |
| Tool retries | `gen_ai.tool.retry_count` | Retry rate panel, cost spikes |
| Answer grounded in sources | `gen_ai.response.citation_coverage` | Hallucination Guard |
| Guardrail refusals | `gen_ai.response.is_refusal` | Refusal rate, response breakdown |
| Which model answered | `gen_ai.request.model` vs `gen_ai.response.model` | Model routing, fallback checks |

---

## Quickstart

```bash
git clone https://github.com/therealfullmetal55555/observability-stack.git
cd observability-stack
cp .env.example .env          # edit the Slack webhook if you want alerts
make up                       # otel-collector, tempo, prometheus, loki, grafana, alertmanager
make seed                     # two hours of believable traffic, including one incident
```

Open <http://localhost:3000> (`admin` / `admin`). Five dashboards are already there
in the **AI Agent Observability** folder — no import, no clicking.

### Point an agent at it

```bash
pip install -e .          # python agents
```

```python
from agent_observability import init_telemetry, trace_agent, trace_tool, trace_llm, record_response_quality

init_telemetry(service_name="rag-support-bot")

@trace_tool(name="search_kb")
def search_kb(query: str) -> list[dict]:
    return qdrant.search("support_kb", query, limit=5)

@trace_agent(name="answer")
def answer(question: str) -> str:
    docs = search_kb(question)
    with trace_llm("gpt-4o-mini") as span:
        reply = client.chat.completions.create(model="gpt-4o-mini", messages=build(question, docs))
        span.set_attribute("gen_ai.usage.input_tokens", reply.usage.prompt_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", reply.usage.completion_tokens)
    text = reply.choices[0].message.content
    record_response_quality(span, "rag-support-bot", text, sources_retrieved=len(docs))
    return text
```

Node, n8n, and the services that can't be touched at all: [docs/INTEGRATION.md](docs/INTEGRATION.md).

---

## Specification matrix

| Dimension | This project |
|---|---|
| **Purpose** | Observability for LLM agents — cost, latency, tool reliability, answer groundedness |
| **Stack** | OTel Collector 0.106 · Tempo 2.5 · Prometheus 2.54 · Loki 2.9 · Grafana 11 · Alertmanager 0.27 |
| **Languages** | Python 3.10–3.12 (instrumentation) · TypeScript (agents, n8n nodes) · Go 1.22 (custom processor) · YAML (everything else) |
| **Entry points** | `make up` · `make validate` · `make test` · `make package` · `helm install` |
| **Interfaces** | OTLP gRPC :4317 / HTTP :4318 · Prometheus `/metrics` · Grafana HTTP API · n8n node palette |
| **Data stores** | Tempo (traces, 7d) · Prometheus (metrics, 30d) · Loki (logs, 7d) · filesystem by default, PVCs in Helm |
| **Custom components** | `tokencost` OTel processor (Go) — central price table so 21 services don't each carry a copy |
| **Dashboards** | 5 — Agent Overview, Token Economics, Tool Calls & Latency, Hallucination Guard, Executive Summary |
| **Alerts** | 15 rules, one copy in `prometheus/alerts.yml`, routed by severity; every rule has a runbook entry |
| **Sampling** | Tail-based: always keep errors, >10s traces, >$0.25 calls, uncited answers; 10% baseline otherwise |
| **Instrumentation** | Python package + TS module + n8n community nodes + auto-instrumentation fallback |
| **Tests** | 80+ across unit, config, Helm and integration suites; CI runs all of them plus a live stack |
| **Config validation** | `scripts/validate.py` — checks dashboards, alerts, pipelines and compose mounts before deploy |
| **Deployment** | Docker Compose (local, with a `custom` profile) · Helm chart (dev/staging/prod values, distroless image) |
| **Security** | Non-root containers, read-only rootfs, no prompt/response bodies captured by default, secrets via env or existing Secrets |
| **Licence** | MIT |

---

## What's in the box

```
docker-compose.yml                the whole stack, six services
otel-collector/
  config.yaml                     pipeline: sampling, cost, citations
  builder-config.yaml             ocb manifest for the custom build
  processor/tokencost/            Go processor + tests
grafana/
  dashboards/*.json               five dashboards, provisioned on boot
  provisioning/                   datasources, dashboards, contact points, routing
                                  (alert rules live in Prometheus — one copy only)
prometheus/alerts.yml             fifteen rules, the single source of truth
instrumentation/
  python/agent_observability/     pip-installable package
  javascript/                     TS module for Node agents
  n8n/                            n8n community nodes
helm/observability-stack/         chart with production values
scripts/
  validate.py                     config doctor
  seed_demo_data.py               two hours of backdated traffic, one incident included
  package.sh                      the deployable tarball
tests/                            unit, config, helm, integration, load generator
docs/                             architecture, integration, runbook, cost control
```

---

## The five dashboards

**Agent Overview** — request rate, error rate, latency p50/p95/p99, tool p99, and
a table of recent failing traces you can click straight into Tempo.

**Token Economics** — cost per request, cost accumulation over 1h and 24h, token
rates split by direction, and a budget gauge. The cost-spike panel compares the
last 5 minutes against the same window an hour ago, which is how you catch a
routing change before the invoice does.

**Tool Calls & Latency** — success rate, latency percentiles and retry rate per
tool. Retries are the signal people forget: a tool that retries three times with
no backoff turns one upstream hiccup into four failures and four charges.

**Hallucination Guard** — share of answers carrying citations, average citation
coverage per service, refusal rate, and a TraceQL table of the exact responses
with no sources behind them. This is the dashboard that makes the project worth
building.

**Executive Summary** — the four numbers a lead asks for: p50 and p99 latency,
success rate, call volume, LLM call rate and token throughput. No drill-down; if
they want one they'll open Agent Overview.

---

## Alerts

Fifteen rules across five layers. `prometheus/alerts.yml` is the only copy — two
copies of fifteen rules drift within a month, and the copy that drifts is always
the one nobody is reading.

| Layer | Rules |
|---|---|
| Agent | down, error rate >10%, p99 >30s |
| Tool | error rate >10%, p99 >10s, cost >$10/s |
| LLM | p99 >30s, error rate >10%, cost >$5/s |
| Vector DB | p99 >500ms, error rate >5% |
| Stack | collector down, collector memory, Prometheus down, Grafana down |

Every one has an entry in [docs/RUNBOOK.md](docs/RUNBOOK.md): what it means, how
to confirm it, what to do, and when to silence it and go back to bed.

---

## Cost control

The four queries that tell you which change to make first, and what not to do —
[the full guide is here](docs/COST-CONTROL.md). The short version:

```promql
# 1. where the money goes
topk(10, sum by (service_name, model) (increase(gen_ai_llm_cost_usd_total[24h])))

# 2. is it volume or price
sum by (service_name) (increase(gen_ai_llm_cost_usd_total[24h]))
/ sum by (service_name) (increase(gen_ai_llm_call_total[24h]))

# 3. is it input tokens (ratio above 20:1 means you're paying to read context)
sum by (service_name) (increase(gen_ai_llm_tokens_total{type="input"}[24h]))
/ sum by (service_name) (increase(gen_ai_llm_tokens_total{type="output"}[24h]))

# 4. is it a loop (above 10 calls per run, something is retrying)
sum by (service_name) (increase(gen_ai_llm_calls_total[1h]))
/ sum by (service_name) (increase(gen_ai_agent_calls_total{status="success"}[1h]))
```

---

## Development

```bash
make help              # every target, one line each
make validate          # config sanity — dashboards, alerts, pipeline, compose
make test              # unit + config + helm tests, no network needed
make up && pytest tests -q    # integration tests against the live stack
make lint fmt          # ruff + black

make collector-build   # Go processor + ocb → custom collector binary
make test-processor    # Go tests with -race
make test-n8n          # n8n node package

make helm-sync         # copy dashboards + rules into the chart
make helm-lint

make load SCENARIO=vendor-outage    # reproduce a specific dashboard shape
make package           # dist/observability-stack-<version>.tar.gz
```

The load generator has five scenarios so you can reproduce a shape instead of
guessing: `healthy`, `citation-regression`, `cost-spike`, `vendor-outage`,
`tight-guardrails`. Every one of them produces a visibly different dashboard.

---

## Deployment

```bash
helm install observability helm/observability-stack \
  -n observability --create-namespace \
  -f helm/observability-stack/values-production.yaml
```

The chart builds the collector config from values, so there's no second copy of
the pipeline to keep in sync. `collector.customBinary=true` switches to the image
with `tokencost` compiled in; without it the pipeline runs on stock contrib and
agents have to report their own cost.

Production values raise replicas to two, turn the debug exporter off, size Tempo
for 14 days of traces, and require secrets from existing Secrets rather than
inline values.

---

## Design notes worth knowing

**Metrics alert, traces explain.** Prometheus holds the aggregates every rule is
built on and keeps them for 30 days. Tempo holds the individual traces for 7. When
an alert fires you confirm on the left half of a dashboard and diagnose on the
right half.

**Sampling never drops the interesting traces.** Errors, slow runs, expensive calls
and answers without citations are kept unconditionally. A 10% baseline covers the
rest. If you turn sampling down further, remember that your cost counter comes
from the same spans.

**Labels stay bounded.** `service.name` (21), `gen_ai.tool.name` (~30),
`gen_ai.request.model` (~12), `status` (3). No user ids, session ids or document
ids on labels — those go on spans, where they cost storage instead of a time series
per value.

**No prompt or response bodies by default.** They're the fastest route to PII in
your telemetry backend and the fastest route to a 10x storage bill. There's a flag
if you need them; think twice before setting it.

More detail in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Limitations, stated plainly

- **Citation coverage is a heuristic.** A sentence carrying `[1]` or a URL counts
  as sourced. It's cheap enough to run on every answer and it catches the failure
  that matters. It is not a correctness measure. Add an LLM judge later, once you
  have this baseline to compare against.
- **Cost is an estimate.** The price table is static; providers don't always bill
  what the table says. Treat it as a leading indicator, not an invoice.
- **Single-node stores.** Tempo, Prometheus and Loki run as single replicas. That's
  correct up to a few hundred spans per second and knowingly insufficient beyond it.
- **No auto-remediation.** Alerts tell a human. An agent paging itself is a
  different project with a different review process.

---

## Licence

MIT — see [LICENSE](LICENSE).
