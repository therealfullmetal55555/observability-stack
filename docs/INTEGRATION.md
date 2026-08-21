# Wiring an agent into the stack

Five minutes per service. The pattern is always the same: initialise once at
startup, decorate the three things worth measuring (the agent entry point, the
tools, the model calls), and record answer quality where you already know the
retrieved sources.

If you only have time for one thing, do the model calls. Cost and latency per
model is where nearly every useful conversation starts.

---

## Python — FastAPI services

Covers `rag-support-bot`, `invoice-document-agent`, and anything else in a
`main.py`.

### 1. Install

```bash
pip install -e /path/to/observability-stack
# or, if you'd rather not vendor it:
pip install git+https://github.com/therealfullmetal55555/observability-stack.git
```

### 2. Initialise before the app object

```python
# app/telemetry.py
import os
from agent_observability import init_telemetry

init_telemetry(
    service_name=os.getenv("SERVICE_NAME", "rag-support-bot"),
    endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"),
    environment=os.getenv("ENVIRONMENT", "development"),
    # Start at 1.0, then drop to 0.1 in production once volume hurts. Errors and
    # slow traces are kept by tail_sampling in the collector regardless.
    sample_rate=float(os.getenv("OTEL_TRACES_SAMPLER_ARG", "1.0")),
)
```

```python
# app/main.py
from app.telemetry import init_telemetry  # must come first

from fastapi import FastAPI
from agent_observability import heartbeat

init_telemetry(service_name="rag-support-bot")
app = FastAPI()

@app.on_event("startup")
async def _beat():
    heartbeat("rag-support-bot")
```

### 3. Decorate

```python
from agent_observability import (
    record_response_quality, trace_agent, trace_llm, trace_tool,
)

@trace_tool(name="search_kb", service_name="rag-support-bot")
def search_kb(query: str, k: int = 5) -> list[dict]:
    return qdrant.search(collection="support_kb", query=query, limit=k)

@trace_agent(name="answer", service_name="rag-support-bot")
def answer(question: str) -> str:
    docs = search_kb(question)

    with trace_llm("gpt-4o-mini", service_name="rag-support-bot") as span:
        reply = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=build_messages(question, docs),
        )
        span.set_attribute("gen_ai.usage.input_tokens", reply.usage.prompt_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", reply.usage.completion_tokens)
        span.set_attribute("gen_ai.response.finish_reason", reply.choices[0].finish_reason)

    text = reply.choices[0].message.content
    # This is the call that makes the hallucination dashboard work. Pass the
    # number of documents you actually retrieved, not the number you cited.
    record_response_quality(span, "rag-support-bot", text, sources_retrieved=len(docs))
    return text
```

### 4. Nested spans come free

`trace_tool` inside `trace_agent` produces a child span automatically, so the
Tempo waterfall shows `agent.answer → tool.search_kb` and `agent.answer →
llm.gpt-4o-mini` with correct timings. No manual parent wiring.

### 5. If you use LangChain or LlamaIndex

They emit OpenTelemetry themselves, and their spans will nest under yours
because context is thread-local:

```bash
pip install opentelemetry-instrumentation-langchain
```

```python
from opentelemetry.instrumentation.langchain import LangchainInstrumentor

LangchainInstrumentor().instrument()   # after init_telemetry()
```

The result: LangChain's own spans land inside `agent.answer`, so you get chain
and retriever detail without re-instrumenting your graph. Their attribute names
differ from ours — that's fine, the dashboards only read the ones we set.

---

## Node / TypeScript services

Covers `synapse`, `browser-use-agent`, and the Agent Firewall piece.

### 1. Install

```bash
npm install @opentelemetry/sdk-node @opentelemetry/auto-instrumentations-node \
            @opentelemetry/exporter-trace-otlp-grpc @opentelemetry/exporter-metric-otlp-grpc
cp -r /path/to/observability-stack/instrumentation/javascript ./src/telemetry
```

### 2. Initialise first — before anything else is imported

```ts
// src/index.ts
import { initTelemetry } from './telemetry/otel-instrumentation';

initTelemetry({ serviceName: 'synapse' });

// Everything else after. Auto-instrumentation patches modules at require time,
// so an import above this line stays untraced.
const { start } = await import('./server');
start();
```

### 3. Wrap the three things worth measuring

```ts
import {
  AgentLifecycle,
  estimateCitationCoverage,
  recordResponseQuality,
  traceAgent,
  traceLlm,
  traceTool,
  traceVectorDb,
  startAgentSpan,
} from './telemetry/otel-instrumentation';

const lifecycle = new AgentLifecycle('synapse').start();
process.on('SIGTERM', () => lifecycle.stop());

const reply = await traceAgent(
  { serviceName: 'synapse', agentName: 'answer', operation: 'answer' },
  async () => {
    const docs = await traceVectorDb(
      { serviceName: 'synapse', operation: 'search', collection: 'kb', topK: 5 },
      async () => ({ results: await qdrant.search('kb', question, 5) }),
    );

    const text = await traceLlm(
      { serviceName: 'synapse', model: 'gpt-4o-mini' },
      async () => {
        const completion = await openai.chat.completions.create({ /* ... */ });
        return {
          response: completion.choices[0].message.content ?? '',
          usage: {
            inputTokens: completion.usage?.prompt_tokens ?? 0,
            outputTokens: completion.usage?.completion_tokens ?? 0,
            finishReason: completion.choices[0].finish_reason,
            responseId: completion.id,
          },
        };
      },
    );

    return { text, docs };
  },
);
```

Quality is recorded on a span you create explicitly, because the agent wrapper
closes its span before you have an answer to grade:

```ts
const span = startAgentSpan({ serviceName: 'synapse', agentName: 'answer', operation: 'answer' });
try {
  const { text, docs } = await doWork(question);
  const { citationCoverage, citations } = estimateCitationCoverage(text, docs.length);
  recordResponseQuality(span, 'synapse', {
    hasCitations: citations.length > 0,
    citationCoverage,
    citations,
    isRefusal: text.startsWith('I can\'t help'),
    sources: docs.map((d) => d.id),
  });
} finally {
  span.end();
}
```

### 4. `browser-use-agent` specifically

The interesting signal there is *tool* latency, not model latency — a page that
takes 40 seconds to settle dominates everything. Wrap each browser action:

```ts
await traceTool(
  { serviceName: 'browser-use-agent', toolName: 'browser_navigate' },
  async () => page.goto(url, { waitUntil: 'networkidle' }),
);
```

Then the "Tool Call p99 Latency" panel tells you whether the model is slow or
the internet is.

---

## n8n — `n8n-ai-executive-assistant`

n8n custom nodes have to be TypeScript, so the nodes ship as a proper community
node package in [`instrumentation/n8n/`](../instrumentation/n8n/README.md).

```bash
cd instrumentation/n8n && npm install && npm run build
mkdir -p ~/.n8n/custom && cp -r dist ~/.n8n/custom/n8n-nodes-otel-trace
# restart n8n — "OTel Trace" and "OTel Trace End" appear in the node palette
```

Set the endpoint once so you don't configure it per node:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

**Wrapping a whole workflow.** Put `OTel Trace` at the top of the branch with
*Wait for Downstream* on, and `OTel Trace End` at the bottom with *Start Node
Name* pointing at it. That's one span covering the whole run, with every LLM and
HTTP node nested inside it.

**Wrapping a single step** — usually the model call — leave *Wait for Downstream*
off and the span closes when the node finishes.

**What it adds that n8n doesn't have:** `n8n.workflow.id`, `n8n.execution.id`,
and `n8n.node.name` on every span, plus a `_otel.traceId` written onto each item
so a downstream node can correlate its own logs with the trace. Set *Span Kind*
to `Client` for outbound calls and `Producer` when the node drops work onto a
queue — the waterfall reads much better with the kinds right.

**If you can't install custom nodes**, call the collector directly from an HTTP
Request node:

```
POST http://localhost:4318/v1/traces
Content-Type: application/json
```

with a hand-built OTLP payload. It works, but do the node instead — the payload
shape is fiddly and every workflow will have a slightly different copy.

---

## Anything that can't be instrumented

For a service you can't touch, three fallbacks in order of preference:

1. **Auto-instrumentation without code changes.** HTTP, DB and gRPC spans come
   free:
   ```bash
   opentelemetry-instrument --traces_exporter otlp python -m uvicorn app:app
   ```
2. **`prometheus/alerts.yml`-style recording rules** won't help here, but a
   blackbox probe will tell you it's up and how slow it is. The
   `blackbox-exporter` in the compose file is there for this.
3. **Logs into Loki.** If it writes logs, Promtail picks them up and the derived
   `trace_id=` field links them to traces from the services that *are*
   instrumented.

---

## Checklist before you call it done

- [ ] `SERVICE_NAME` is set and matches a `service.name` you'll recognise in Grafana
- [ ] The service appears on **Agent Overview** within a minute
- [ ] `curl localhost:13133` returns `Server available`
- [ ] **Token Economics** shows a non-zero cost for this service
- [ ] **Hallucination Guard** shows this service on the citation panel
- [ ] `make validate` still passes
- [ ] You can go from an alert in Slack to a trace in Tempo in under a minute — try it once, for real
