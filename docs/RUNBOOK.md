# Runbook

One section per alert in `prometheus/alerts.yml`. Each one says what it means,
how to confirm it, what to do, and — importantly — when to silence it and go
back to bed.

The general shape of an investigation:

1. Alert fires in `#critical-alerts` or `#warnings`.
2. Open **Agent Overview**, set the time range to the last hour, find the spike.
3. Click through to a trace in Tempo from the error table.
4. Fix, or silence with a reason.

---

## AgentDown

**Means:** no heartbeat from a service for 60 seconds. `gen_ai_agent_up` is 0 or
the series is absent entirely.

**Confirm:**
```promql
gen_ai_agent_up
absent(gen_ai_agent_up{service_name="rag-support-bot"})
```

**Do:**
1. `docker compose ps` on the agent's host — is the container up?
2. Check whether it's one service or all of them. All of them means the collector
   or the network, not the agent.
3. If only one: look at its logs before you look at anything else. This alert
   almost always fires because the process is genuinely gone.

**Silence it when:** you're deploying. A rolling restart trips this every time;
either raise `for` to 5m for that service or accept the page.

---

## AgentHighErrorRate

**Means:** more than 10% of agent calls failed over 5 minutes.

**Confirm:** the spike is real and not one bad user retrying in a loop.
```promql
sum by (service_name) (rate(gen_ai_agent_calls_total{status="error"}[5m]))
```

**Do:**
1. **Agent Overview → Error Rate by Service.** One service or all?
2. **Recent Errors table.** Click the top trace, read the exception on the span.
3. Classify:
   - `429` / `rate_limit` → see [Vendor rate limits](#vendor-rate-limits)
   - `5xx` from the provider → their status page; this is not yours to fix
   - tool throwing → **Tool Calls** dashboard, then the tool's own trace
   - your own bug → the trace has the stack

**Don't** raise the threshold to make it stop firing. 10% is already generous.

---

## AgentHighLatency

**Means:** p99 over 30 seconds. Users are noticing.

**Confirm:** whether it's the model or the tool.
```promql
histogram_quantile(0.99, sum by (le, service_name) (rate(gen_ai_agent_latency_seconds_bucket[5m])))
histogram_quantile(0.99, sum by (le, tool_name) (rate(gen_ai_tool_call_duration_seconds_bucket[5m])))
```

**Do:**
1. If tool p99 is also up, it's a tool — go to **Tool Calls**, find it, look at a
   slow trace. `sql_query` and `browser_navigate` are the usual suspects.
2. If only the agent is slow, it's the model. Check `gen_ai.usage.output_tokens`
   on recent traces: a prompt change that lets the model ramble triples your
   latency and your bill at the same time.
3. If neither moved, it's the box. `node-exporter` metrics and `docker stats`.

---

## ToolHighErrorRate / ToolHighLatency

**Means:** one tool is failing or slow. This is the most actionable alert in the
set — the name is right there in the label.

**Do:** read the `tool_name` label, go to **Tool Calls**, filter by it, open the
slowest or most recent failing trace. The tool's span has the parameters on it.

Common causes and their fingerprints:

| Symptom | Usually |
|---|---|
| Sudden 100% failure | Credentials rotated, or the vendor renamed a field |
| Elevated but not total | Rate limiting. Look for `429` in the span events |
| Latency up, errors flat | The upstream got slower, or a retry loop is hiding in your code |
| One instance only | `gen_ai.tool.retry_count` is probably non-zero — check your retry config |

**Fix the retry amplification first.** A tool that retries 3 times with no
backoff turns one vendor hiccup into four failures and four charges.

---

## LLMCostHigh / ToolHighCost

**Means:** the rate of spend passed $5/sec (model) or $10/sec (tool). That's
$432/day if it holds.

**Confirm:** where the money is actually going.
```promql
topk(10, sum by (model, service_name) (rate(gen_ai_llm_cost_usd_total[5m])))
sum(increase(gen_ai_llm_cost_usd_total[24h]))
```

**Do:** see [COST-CONTROL.md](COST-CONTROL.md). The short version:

1. **Token Economics → Cost by Model.** If one model dominates, something is
   routing to it that shouldn't be.
2. Check `gen_ai.usage.input_tokens` on recent traces. A retriever returning 50
   documents instead of 5 is the single most common cause, and it costs ~10x
   with no visible change in output quality.
3. If it's a loop: a tool returning an error the model keeps retrying will burn
   money steadily and silently.

**Don't** set `unknown_model: zero` to make the number go down. That hides
untracked spend, which is worse than seeing it.

---

## LLMHighErrorRate / LLMLatencyHigh

**Means:** the provider is having a bad time.

**Do:**
1. Check your provider's status page before you change anything.
2. If you have a fallback model configured, confirm it actually engaged — grep
   the traces for `gen_ai.response.model` differing from `gen_ai.request.model`.
3. If this is the third time this month, it's a signal to add a fallback, not to
   tune a threshold.

---

## VectorDBHighLatency / VectorDBHighErrorRate

**Means:** retrieval is slow or failing. The hallucination dashboard will follow
within a few minutes — agents that can't retrieve start answering from memory,
which is exactly when they cite nothing.

**Confirm:**
```promql
histogram_quantile(0.99, sum by (le, operation) (rate(vector_db_query_duration_seconds_bucket[5m])))
```

**Do:**
1. Check the collection's size and index settings — a collection that grew 10x
   overnight behaves differently.
2. Check for a missing index on a filtered field. Filtered HNSW is slow when the
   filter is wrong.
3. If it's error rate rather than latency, look at `db.collection.name` on the
   failing spans. A renamed collection fails 100% of the time, immediately.

---

## CollectorDown

**Means:** `up{job="otel-collector"} == 0`. **Everything is blind.** Traces,
metrics and logs from all 21 agents are being dropped right now.

**Do:**
1. `docker compose logs otel-collector` — this is almost always a config error
   after an edit. Look for "unknown type" or "invalid configuration".
2. If the config is fine, check memory. `GOMEMLIMIT` and the `memory_limiter`
   processor both exist to prevent an OOM, but a payload spike can still get
   through.
3. Restart it. Accept the data gap and write it down.

**Prevent it:** `make validate` before every deploy catches the config class of
failure, which is most of them.

---

## CollectorHighMemory

**Means:** the collector is over 1 GB resident.

**Do:**
1. `otelcol_processor_tokencost_spans_*` and `otelcol_exporter_queue_size` — a
   full export queue means the backend is refusing writes and memory is piling up.
2. Check Tempo and Prometheus are accepting writes. A collector with a healthy
   pipeline is rarely the problem; a collector whose downstream is down always is.
3. If genuinely load: raise `limit_mib`, then lower
   `tail_sampling.expected_new_traces_per_sec` — that number gates how much the
   sampler will hold in memory.

---

## PrometheusDown / GrafanaDown

**Means:** the monitoring is down, so you're flying blind. Treat it as seriously
as an agent outage, because it *is* one.

**Do:**
1. `docker compose ps` — is it a crash or a disk problem?
2. Prometheus: a full disk is the usual cause. Prometheus stops writing, then
   stops starting.
3. Grafana: check it can still reach Prometheus. A Grafana that's up but blind
   looks identical to a Grafana that's down.

**Silence the alerts you can't see.** If Prometheus is down, nothing else is
firing, and the incident you discover on restart may have been running for an
hour.

---

## Vendor rate limits

Not an alert, but the most common cause of two of them.

**Fingerprint:** 429s in the span events, `status="error"` on LLM spans, retry
counts climbing.

**Do:**
1. Check whether you're retrying on the same key without backoff. Look at
   `gen_ai.tool.retry_count` — anything consistently above 1 is suspect.
2. Check whether one agent is hogging the quota. **Agent Overview → LLM Call Rate
   by Model** broken down by `service.name` shows the split.
3. Reduce concurrency before you request a quota increase. Most 429s on agent
   fleets are self-inflicted.

---

## On-call hygiene

- **Silence with a reason.** `reason="deploy rag-support-bot v1.4"`. Future you
  will not remember.
- **Never silence `CollectorDown`.** Fix it or accept that you're blind.
- **Watch for alerts that fire weekly and nobody acts on.** Either fix the
  threshold, fix the underlying thing, or delete the rule. A noisy alert
  trains people to ignore the ones that matter.
