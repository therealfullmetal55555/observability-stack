# n8n OpenTelemetry nodes

Two nodes — `OTel Trace` and `OTel Trace End` — that put your n8n workflows on
the same dashboards as everything else.

## Install

```bash
npm install
npm run build

mkdir -p ~/.n8n/custom
cp -r dist package.json ~/.n8n/custom/n8n-nodes-otel-trace/

# restart n8n
```

In Docker, mount the built package:

```yaml
n8n:
  volumes:
    - ./n8n-nodes-otel-trace:/home/node/.n8n/custom/n8n-nodes-otel-trace
  environment:
    N8N_CUSTOM_EXTENSIONS: /home/node/.n8n/custom/n8n-nodes-otel-trace
    OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector:4318
```

Point the endpoint at `4318` (HTTP) from inside a container — `4317` is gRPC and
needs more client setup than an n8n node is worth.

## Two ways to use it

### Wrap a whole workflow

```
[Webhook] → [OTel Trace: wait for downstream ✓] → [AI Agent] → [HTTP] → [OTel Trace End]
```

- `OTel Trace`: *Wait for Downstream* on, *Operation Name* = `executive-brief`
- `OTel Trace End`: *Start Node Name* = the exact name of the start node

One span covers the run. Every LLM and HTTP node nests inside it, so the Tempo
waterfall reads like the workflow diagram.

### Wrap one step

Leave *Wait for Downstream* off and the span closes when the node finishes. Use
this when you only care about one expensive call and don't want to restructure
the workflow.

## Attributes it adds

| Attribute | Source |
|---|---|
| `n8n.workflow.id` | the workflow |
| `n8n.execution.id` | this run |
| `n8n.node.name` / `n8n.node.type` | the node |
| `gen_ai.agent.name` | *Service Name* parameter |
| `gen_ai.operation.name` | *Operation Name*, or the node name |
| `gen_ai.tool.cost_usd` | *Cost Field* on the incoming item, if *Record Cost* is on |

Every item also gets `_otel: { traceId, spanId }` written onto it. A Function
node downstream can put that trace id in its own logs, and the Loki derived
field will link the log line back to the trace.

## Cost from a node that already knows

The AI nodes return usage metadata. Map it into a cost field before the trace
node and turn on *Record Cost*:

```javascript
// Function node, right after the OpenAI node
const usage = $json.usage ?? {};
const PRICE = { input: 0.15 / 1e6, output: 0.60 / 1e6 };
return [{
  json: {
    ...$json,
    cost_usd: (usage.prompt_tokens ?? 0) * PRICE.input
            + (usage.completion_tokens ?? 0) * PRICE.output,
  },
}];
```

Otherwise the collector's `tokencost` processor fills it in from the price table
— but only if the node sets `gen_ai.usage.input_tokens`, and n8n's built-in nodes
won't do that for you.

## Gotchas

**"No open span for X"** — the start node didn't have *Wait for Downstream* on,
or the two nodes are on different branches. The keys are per execution *and* per
item index; a start node that fans out to 10 items needs an end node that
receives all 10.

**Nothing arrives at the collector** — check the endpoint is reachable *from
inside the n8n container*. `localhost:4318` from inside a container is the
container, not your host.

**Spans arrive without a parent** — expected with *Wait for Downstream* off.
They'll still show up on the dashboards; they just won't have a workflow span
above them.

## Development

```bash
npm install
npm run dev     # tsc --watch
npm test        # jest
npm run lint    # eslint
```
