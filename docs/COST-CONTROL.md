# Cost control

Observability is only worth having if it changes a decision. Here is the
decision this stack is built to change: *which change do I make first to cut
spend without cutting quality?*

Answer it with four queries, in this order.

---

## 1. Where is the money going?

```promql
topk(10, sum by (service_name, model) (increase(gen_ai_llm_cost_usd_total[24h])))
```

**Token Economics → Cost Accumulation** does this visually. Expect it to be
lopsided — usually two services are 80% of the bill. Anything else means your
cost is spread thin and you'll need a different approach.

## 2. Is it volume or price?

```promql
# calls per service
sum by (service_name) (increase(gen_ai_llm_calls_total[24h]))

# average cost per call
sum by (service_name) (increase(gen_ai_llm_cost_usd_total[24h]))
/ sum by (service_name) (increase(gen_ai_llm_calls_total[24h]))
```

High volume with normal per-call cost is a product decision: more users, more
spend, expected. High per-call cost is an engineering decision, and it's the one
you can fix this afternoon.

## 3. Is it input tokens?

```promql
sum by (service_name) (increase(gen_ai_llm_tokens_total{type="input"}[24h]))
/ sum by (service_name) (increase(gen_ai_llm_tokens_total{type="output"}[24h]))
```

A ratio above 20:1 means you're paying to read context. That's the retriever
returning too much, a conversation history that never gets truncated, or a
system prompt with a few thousand tokens of examples in it.

**This is the fix that works:** five documents instead of fifty. Compare the
citation coverage on **Hallucination Guard** before and after — if coverage
holds and cost drops, you found free money.

```python
# before
docs = search_kb(question, k=50)

# after — and check the dashboard an hour later
docs = search_kb(question, k=5)
```

## 4. Is it a loop?

```promql
# calls per completed agent run. A healthy agent is 2-6.
sum by (service_name) (increase(gen_ai_llm_calls_total[1h]))
/ sum by (service_name) (increase(gen_ai_agent_calls_total{status="success"}[1h]))
```

Above 10 and something is retrying. The usual cause is a tool that fails in a way
the model interprets as "try again" — an empty result, or a validation error that
reads like a transient failure. Look for the pair: high tool error rate *and*
high calls-per-run.

An agent looping on a broken tool can quietly spend more than the entire rest of
the fleet.

---

## Model routing

The cheapest win when the above four are all clean:

| Task | Model | Why |
|---|---|---|
| Intent classification, routing, extraction | `gpt-4o-mini`, `claude-haiku-3.5` | Deterministic, short output, no reasoning needed |
| RAG answers with retrieved context | mid-tier | The context does the work, not the model |
| Multi-step planning, ambiguous requests | `gpt-4o`, `claude-sonnet-4` | Cheap models fail these in ways that cost more in retries |

**Check it with data, not instinct.** The `gen_ai.request.model` label lets you
compare cost *and* error rate per model:

```promql
# error rate by model — the cheap model that fails 15% of the time isn't cheap
sum by (model) (rate(gen_ai_llm_calls_total{status="error"}[1h]))
/ sum by (model) (rate(gen_ai_llm_calls_total[1h]))
```

---

## Caching

Prompt caching is the highest-leverage single change for agents with a long
system prompt. Cache reads are 50–90% cheaper depending on provider, and the
panel that proves it is **Token Economics → Token Rate**.

Verify it's actually working by looking for the drop in cost per call at constant
token counts:

```promql
sum(rate(gen_ai_llm_cost_usd_total[30m]))
/ sum(rate(gen_ai_llm_tokens_total[30m]))
```

No change after enabling caching means your prompt has a timestamp or a session
id near the top, which invalidates the prefix on every call. That's a very common
and very expensive mistake.

---

## Budgets and alerts

`gen_ai_llm_cost_usd_total` is a counter, so all of this is a rate over a window:

```promql
# projected month-end spend
sum(increase(gen_ai_llm_cost_usd_total[24h])) * 30

# today vs the same day last week
sum(increase(gen_ai_llm_cost_usd_total[24h]))
/ sum(increase(gen_ai_llm_cost_usd_total[24h] offset 7d))
```

The second one is the better alert. Absolute thresholds either cry wolf during
growth or stay silent through a 3x regression in a service that used to be cheap.

Set the daily budget once, in the dashboard variable:

```bash
MONTHLY_BUDGET_USD=500   # .env
```

**Token Economics → Budget Burn** then reads as a percentage, and the `LLMCostHigh`
rule catches the fast spikes that a daily number hides.

---

## What not to do

- **Don't sample away your cost data.** `tail_sampling` drops traces, and the cost
  counter comes from spans. If you sample 10% you'll see 10% of the spend. Keep
  cost as a *metric* exported by the agent, or sample much less aggressively than
  you would for a plain web service.
- **Don't set `unknown_model: zero`** to quieten the warnings. That turns
  "untracked spend" into "free", which is the one outcome you can't recover from.
- **Don't optimise output tokens first.** Input is usually 80% of the bill.
- **Don't cut the observability itself.** Loki and Prometheus storage is rounding
  error next to what a runaway agent costs in a week.

---

## Monthly review, twenty minutes

Run these four, write the answers down, compare with last month:

```promql
sum(increase(gen_ai_llm_cost_usd_total[30d]))                     # total
topk(5, sum by (service_name, model) (increase(gen_ai_llm_cost_usd_total[30d])))  # top spenders
sum(increase(gen_ai_llm_tokens_total{type="input"}[30d]))         # context volume
sum(rate(gen_ai_responses_total{has_citations="false"}[30d]))     # ungrounded answers
```

The fourth number is the one people forget. A cheap answer that cites nothing
isn't cheap — it's a support ticket waiting to happen.
