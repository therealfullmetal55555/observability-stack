# tokencost processor

Recomputes per-call LLM spend in the collector, from token counts that the
agents already emit.

## Why this isn't a library

The same price table is needed by 21 services across Python and Node. Ship it
as a library and you get 21 copies to bump every time a provider changes a
price, plus 21 chances for one of them to be a version behind. As a collector
processor it is one file and one restart.

Agents that *do* know better — batch discounts, cached reads, negotiated
rates — still win. With `respect_existing: true` (the default) a non-zero cost
on the span is preserved and only missing costs are filled in.

## Config

```yaml
processors:
  tokencost:
    models:
      gpt-4o-mini: { input_usd_per_1m: 0.15, output_usd_per_1m: 0.60 }
      ollama: { input_usd_per_1m: 0, output_usd_per_1m: 0 }
    model_attr: gen_ai.request.model
    input_tokens_attr: gen_ai.usage.input_tokens
    output_tokens_attr: gen_ai.usage.output_tokens
    cost_attr: gen_ai.response.cost_usd
    total_tokens_attr: gen_ai.usage.total_tokens
    respect_existing: true
    unknown_model: warn      # warn | zero | drop_span
    emit_metrics: true
```

| Option | Default | Notes |
|---|---|---|
| `models` | — | Key is matched as a case-insensitive **substring**, longest key first. `gpt-4o-mini-2024-07-18` prices as `gpt-4o-mini`, not `gpt-4o`. |
| `respect_existing` | `true` | A non-zero cost from the SDK is left untouched. An explicit `0.0` is treated as "unknown" and recomputed, because an unexplained zero on a paid model is worse than an estimate. |
| `unknown_model` | `warn` | `warn` keeps the span unpriced and logs once per model; `zero` prices it at 0 (right for self-hosted models you forgot to list); `drop_span` discards it (billing-critical fleets only). |
| `emit_metrics` | `true` | Adds `otelcol_processor_tokencost_spans_priced_total` and `..._spans_unknown_model_total`. |

## Behaviour worth knowing

- **Tool and HTTP spans pass through untouched.** No `gen_ai.request.model`
  attribute means it isn't a model call.
- **A model call with no token counts is counted as malformed**, not priced at
  zero. It shows up in the shutdown log: a client is not reporting usage.
- **Negative or absent token counts are treated as zero.** A negative count is a
  broken client, not a refund.
- **Unknown models are logged once per model name**, not once per span. A new
  model rolling out across the fleet should not produce a million warnings.

## Build

This processor is not in the contrib distribution. It is compiled into a custom
collector via [ocb](https://github.com/open-telemetry/opentelemetry-collector/tree/main/cmd/builder):

```bash
make collector-build     # runs ocb against otel-collector/builder-config.yaml
docker build -f docker/Dockerfile.collector -t observability-collector:local .
```

## Tests

```bash
cd otel-collector/processor && go test ./tokencost/ -v
```

The suite covers the failure modes that actually cost money: a mini model
priced at the full-size rate, an unknown model silently reading as free, a
cookie-cutter discount overridden by the static table.
