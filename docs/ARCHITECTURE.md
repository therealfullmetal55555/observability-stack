# Architecture

## The shape of the problem

Twenty-one agents, written in Python and TypeScript, running as FastAPI services,
n8n workflows, and a browser automation loop. They all call models, they all call
tools, some of them retrieve from a vector store before answering. Nobody can
answer three questions:

1. Which agent is slow right now, and is it the model or the tool?
2. How much did we spend today, and on what?
3. Which answers were not grounded in a retrieved source?

Everything below exists to answer those three questions and nothing else. That
constraint is why there is no service mesh, no eBPF agent, and no second
dashboarding tool.

## Data flow

```
   agents (21)
   ├─ Python:  agent_observability   ──┐
   ├─ Node:    otel-instrumentation.ts │  OTLP :4317 / :4318
   └─ n8n:     OTel Trace nodes       ─┘
                                       │
                                       ▼
                         ┌───────────────────────────┐
                         │   OTel Collector          │
                         │                           │
                         │  memory_limiter           │  backpressure
                         │  resourcedetection        │  host/cloud labels
                         │  transform/agent_defaults │  fill in gaps
                         │  tokencost    ◄ custom    │  tokens → USD
                         │  transform/citations      │  markers → boolean
                         │  tail_sampling            │  keep errors, slow, costly, uncited
                         │  batch                    │  fewer, larger exports
                         └────┬──────────┬───────────┘
                              │          │
              traces ─────────┘          └───────── metrics, logs
                    │                                   │
                    ▼                                   ▼
              ┌──────────┐                    ┌──────────────┐
              │  Tempo   │                    │  Prometheus  │◄── scrapes agents
              │  7d      │                    │  30d         │    + collector
              └────┬─────┘                    └──────┬───────┘
                   │                                 │
                   │                                 ├──► Alertmanager
                   │                                 │      ├─ Slack
                   │                                 │      ├─ email
                   │                                 │      └─ PagerDuty (critical only)
                   ▼                                 ▼
              ┌──────────────────────────────────────────┐
              │              Grafana 11                  │
              │  5 dashboards, 15 rules evaluated in      │
              │  Prometheus, delivery via Grafana         │
              └──────────────────────────────────────────┘
```

Loki sits alongside Prometheus for logs, with a derived field that turns a
`trace_id=` in a log line into a clickable link into Tempo. That correlation is
the difference between "the tool failed" and "the tool failed because the vendor
returned 429 for four minutes".

## Decisions worth explaining

### Metrics for alerting, traces for diagnosis

Prometheus holds the aggregates every alert is built on. Tempo holds the
individual traces. When an alert fires you look at the metric to confirm, then
jump to a trace to find out why. This is why the dashboards have both timeseries
panels and a TraceQL table on the same page — the left half tells you something
is wrong, the right half tells you which request.

### Where the computation happens

| Computed in | What | Why there |
|---|---|---|
| Agent | spans, token counts, citation markers | Only the agent sees the raw response |
| Collector (`tokencost`) | USD from tokens | One price table for 21 services, no redeploys |
| Collector (`transform`) | missing-attribute defaults | Agents shouldn't each reimplement hygiene |
| Prometheus | rates, quantiles, ratios | Cheap, retained for 30 days, alertable |
| Grafana | joins, thresholds, layout | Presentation only — no alert logic here |

The rule: a number that pages someone lives in Prometheus. A number that helps
someone understand lives in Grafana.

### Why `tokencost` is the only custom processor

Cost is the one value that has to be recomputed centrally. Everything else —
citation coverage, tool status, latency — an agent knows at the moment it
happens. Cost depends on a price table that changes without warning and belongs
to nobody in particular, which is exactly the kind of thing that rots when
copied into 21 repos. See [`otel-collector/processor/tokencost/`](../otel-collector/processor/tokencost/README.md).

### One copy of the alert rules

The rules live in `prometheus/alerts.yml` and nowhere else. Grafana provisions
contact points and routing, but not rules. Two copies of fifteen rules drift
within a month, and the copy that drifts is always the one nobody is looking at.

## Cardinality

Labels are chosen to stay bounded. `service.name` (21 values), `gen_ai.tool.name`
(~30), `gen_ai.request.model` (~12), `status` (3). Nothing is labelled with a
user id, a session id, a document id, or a prompt. Those belong on spans, where
they cost storage rather than a time series per value.

The one place this gets violated is `gen_ai.tool.name` when a tool is
dynamically named. If your tool names include an id, bucket them in the agent
before emitting.

## Retention

| Store | Default | Rationale |
|---|---|---|
| Tempo | 7 days | Long enough to compare this week's incident with last week's |
| Prometheus | 30 days | Enough history for a monthly cost report |
| Loki | 7 days | Logs are for the incident, not for the quarter |

Raise Tempo before Prometheus — traces are what you actually open during an
incident, and 7 days is the first thing that starts to hurt.

## What this deliberately doesn't do

- **No LLM-as-judge scoring.** `estimate_citation_coverage` is a heuristic:
  a sentence is sourced if it carries a bracketed marker. It's cheap, it runs on
  every answer, and it catches the failure mode that matters — a confident
  paragraph with nothing behind it. Add a judge later, when you have the
  heuristic's baseline to compare against.
- **No prompt or response bodies by default.** They're the fastest way to leak
  PII into a telemetry backend and the fastest way to 10x your storage bill.
  Capture them behind an explicit flag if you need them.
- **No auto-remediation.** Alerts tell a human. An agent paging itself is a
  different project with a different review process.
