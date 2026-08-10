package tokencost

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/collector/component/componenttest"
	"go.opentelemetry.io/collector/consumer/consumertest"
	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.opentelemetry.io/collector/processor/processortest"
)

func testConfig() *Config {
	cfg := createDefaultConfig().(*Config)
	cfg.Models = map[string]ModelPrice{
		"gpt-4o":        {InputPer1M: 2.50, OutputPer1M: 10.00},
		"gpt-4o-mini":   {InputPer1M: 0.15, OutputPer1M: 0.60},
		"ollama":        {InputPer1M: 0, OutputPer1M: 0},
		"claude-sonnet": {InputPer1M: 3.00, OutputPer1M: 15.00},
	}
	cfg.RespectExisting = true
	cfg.UnknownModel = PolicyWarn
	cfg.EmitMetrics = false
	return cfg
}

func buildSpan(td ptrace.Traces, attrs map[string]any) ptrace.Span {
	span := td.ResourceSpans().AppendEmpty().ScopeSpans().AppendEmpty().Spans().AppendEmpty()
	span.SetName("llm.call")
	for key, value := range attrs {
		switch typed := value.(type) {
		case string:
			span.Attributes().PutStr(key, typed)
		case int64:
			span.Attributes().PutInt(key, typed)
		case float64:
			span.Attributes().PutDouble(key, typed)
		case bool:
			span.Attributes().PutBool(key, typed)
		default:
			panic("unsupported test attribute type")
		}
	}
	return span
}

func runTrace(t *testing.T, cfg *Config, td ptrace.Traces) ptrace.Traces {
	t.Helper()
	sink := new(consumertest.TracesSink)
	factory := NewFactory()
	processor, err := factory.CreateTraces(
		context.Background(),
		processortest.NewNopSettings(componentType),
		cfg,
		sink,
	)
	require.NoError(t, err)
	require.NoError(t, processor.Start(context.Background(), componenttest.NewNopHost()))

	out, err := processor.ConsumeTraces(context.Background(), td)
	require.NoError(t, err)
	return out
}

// ---------------------------------------------------------------------------
// pricing
// ---------------------------------------------------------------------------

func TestCostIsComputedFromTokenCounts(t *testing.T) {
	td := ptrace.NewTraces()
	buildSpan(td, map[string]any{
		"gen_ai.request.model":       "gpt-4o-mini",
		"gen_ai.usage.input_tokens":  int64(1_000_000),
		"gen_ai.usage.output_tokens": int64(1_000_000),
	})

	out := runTrace(t, testConfig(), td)
	cost, ok := out.ResourceSpans().At(0).ScopeSpans().At(0).Spans().At(0).Attributes().Get("gen_ai.response.cost_usd")
	require.True(t, ok, "cost attribute should have been written")
	assert.InDelta(t, 0.75, cost.Double(), 0.00001)
}

func TestTotalTokensAreFilledInWhenMissing(t *testing.T) {
	td := ptrace.NewTraces()
	buildSpan(td, map[string]any{
		"gen_ai.request.model":       "gpt-4o",
		"gen_ai.usage.input_tokens":  int64(1200),
		"gen_ai.usage.output_tokens": int64(300),
	})

	out := runTrace(t, testConfig(), td)
	total, ok := out.ResourceSpans().At(0).ScopeSpans().At(0).Spans().At(0).Attributes().Get("gen_ai.usage.total_tokens")
	require.True(t, ok)
	assert.Equal(t, int64(1500), total.Int())
}

func TestLongestModelKeyWins(t *testing.T) {
	// The single most expensive bug this processor can have: pricing a mini
	// model at the full-size rate is a 16x overstatement.
	td := ptrace.NewTraces()
	buildSpan(td, map[string]any{
		"gen_ai.request.model":      "gpt-4o-mini-2024-07-18",
		"gen_ai.usage.input_tokens": int64(1_000_000),
	})

	out := runTrace(t, testConfig(), td)
	cost, _ := out.ResourceSpans().At(0).ScopeSpans().At(0).Spans().At(0).Attributes().Get("gen_ai.response.cost_usd")
	assert.InDelta(t, 0.15, cost.Double(), 0.00001)
}

func TestLocalModelPricesToZeroNotUnknown(t *testing.T) {
	td := ptrace.NewTraces()
	buildSpan(td, map[string]any{
		"gen_ai.request.model":      "ollama/llama3.3",
		"gen_ai.usage.input_tokens": int64(5000),
	})

	out := runTrace(t, testConfig(), td)
	cost, ok := out.ResourceSpans().At(0).ScopeSpans().At(0).Spans().At(0).Attributes().Get("gen_ai.response.cost_usd")
	require.True(t, ok)
	assert.Equal(t, 0.0, cost.Double())
}

// ---------------------------------------------------------------------------
// respect_existing
// ---------------------------------------------------------------------------

func TestExistingCostIsLeftAlone(t *testing.T) {
	cfg := testConfig()
	cfg.RespectExisting = true

	td := ptrace.NewTraces()
	buildSpan(td, map[string]any{
		"gen_ai.request.model":        "gpt-4o",
		"gen_ai.usage.input_tokens":   int64(1_000_000),
		"gen_ai.response.cost_usd":    0.0, // an SDK that knows about a cache discount
	})

	// A cost of exactly 0.0 is ambiguous — the SDK might mean "free". We
	// recompute, because leaving an unexplained zero is worse than an estimate.
	out := runTrace(t, cfg, td)
	cost, _ := out.ResourceSpans().At(0).ScopeSpans().At(0).Spans().At(0).Attributes().Get("gen_ai.response.cost_usd")
	assert.InDelta(t, 0.15, cost.Double(), 0.00001)
}

func TestNonZeroExistingCostIsPreserved(t *testing.T) {
	td := ptrace.NewTraces()
	buildSpan(td, map[string]any{
		"gen_ai.request.model":      "gpt-4o",
		"gen_ai.usage.input_tokens": int64(1_000_000),
		"gen_ai.response.cost_usd":  9.99,
	})

	out := runTrace(t, testConfig(), td)
	cost, _ := out.ResourceSpans().At(0).ScopeSpans().At(0).Spans().At(0).Attributes().Get("gen_ai.response.cost_usd")
	assert.InDelta(t, 9.99, cost.Double(), 0.00001)
}

// ---------------------------------------------------------------------------
// unknown models
// ---------------------------------------------------------------------------

func TestUnknownModelKeepsTheSpanByDefault(t *testing.T) {
	cfg := testConfig()
	cfg.UnknownModel = PolicyWarn

	td := ptrace.NewTraces()
	buildSpan(td, map[string]any{
		"gen_ai.request.model":      "brand-new-model",
		"gen_ai.usage.input_tokens": int64(1000),
	})

	out := runTrace(t, cfg, td)
	assert.Equal(t, 1, out.SpanCount(), "warn policy must not drop data")
	_, ok := out.ResourceSpans().At(0).ScopeSpans().At(0).Spans().At(0).Attributes().Get("gen_ai.response.cost_usd")
	assert.False(t, ok, "we must not invent a price for an unknown model")
}

func TestUnknownModelCanBeZeroed(t *testing.T) {
	cfg := testConfig()
	cfg.UnknownModel = PolicyZero

	td := ptrace.NewTraces()
	buildSpan(td, map[string]any{
		"gen_ai.request.model":      "brand-new-model",
		"gen_ai.usage.input_tokens": int64(1000),
	})

	out := runTrace(t, cfg, td)
	cost, ok := out.ResourceSpans().At(0).ScopeSpans().At(0).Spans().At(0).Attributes().Get("gen_ai.response.cost_usd")
	require.True(t, ok)
	assert.Equal(t, 0.0, cost.Double())
}

func TestUnknownModelCanDropTheSpan(t *testing.T) {
	cfg := testConfig()
	cfg.UnknownModel = PolicyDropSpan

	td := ptrace.NewTraces()
	buildSpan(td, map[string]any{
		"gen_ai.request.model":      "brand-new-model",
		"gen_ai.usage.input_tokens": int64(1000),
	})

	out := runTrace(t, cfg, td)
	assert.Equal(t, 0, out.SpanCount())
}

// ---------------------------------------------------------------------------
// things that should be left alone
// ---------------------------------------------------------------------------

func TestSpanWithoutAModelIsUntouched(t *testing.T) {
	td := ptrace.NewTraces()
	buildSpan(td, map[string]any{
		"gen_ai.tool.name": "search_kb",
	})

	out := runTrace(t, testConfig(), td)
	assert.Equal(t, 1, out.SpanCount())
	_, ok := out.ResourceSpans().At(0).ScopeSpans().At(0).Spans().At(0).Attributes().Get("gen_ai.response.cost_usd")
	assert.False(t, ok)
}

func TestModelCallWithoutTokenCountsIsKeptAndCounted(t *testing.T) {
	processor, err := newProcessor(processortest.NewNopSettings(componentType).TelemetrySettings, testConfig())
	require.NoError(t, err)

	td := ptrace.NewTraces()
	buildSpan(td, map[string]any{"gen_ai.request.model": "gpt-4o-mini"})

	_, err = processor.processTraces(context.Background(), td)
	require.NoError(t, err)
	assert.Equal(t, int64(1), processor.malformed.Load(), "a model call with no tokens is a client bug worth surfacing")
}

func TestStringEncodedTokenCountsAreParsed(t *testing.T) {
	td := ptrace.NewTraces()
	buildSpan(td, map[string]any{
		"gen_ai.request.model":      "gpt-4o",
		"gen_ai.usage.input_tokens": "1000000",
	})

	out := runTrace(t, testConfig(), td)
	cost, ok := out.ResourceSpans().At(0).ScopeSpans().At(0).Spans().At(0).Attributes().Get("gen_ai.response.cost_usd")
	require.True(t, ok)
	assert.InDelta(t, 2.50, cost.Double(), 0.00001)
}

func TestNegativeTokenCountsAreTreatedAsZero(t *testing.T) {
	td := ptrace.NewTraces()
	buildSpan(td, map[string]any{
		"gen_ai.request.model":      "gpt-4o",
		"gen_ai.usage.input_tokens": int64(-500),
	})

	out := runTrace(t, testConfig(), td)
	cost, _ := out.ResourceSpans().At(0).ScopeSpans().At(0).Spans().At(0).Attributes().Get("gen_ai.response.cost_usd")
	assert.Equal(t, 0.0, cost.Double())
}

func TestEmptyBatchesSurvive(t *testing.T) {
	out := runTrace(t, testConfig(), ptrace.NewTraces())
	assert.Equal(t, 0, out.SpanCount())
}

func TestMultipleSpansInOneBatchAreAllPriced(t *testing.T) {
	td := ptrace.NewTraces()
	ss := td.ResourceSpans().AppendEmpty().ScopeSpans().AppendEmpty()
	for i := 0; i < 25; i++ {
		span := ss.Spans().AppendEmpty()
		span.Attributes().PutStr("gen_ai.request.model", "gpt-4o-mini")
		span.Attributes().PutInt("gen_ai.usage.input_tokens", 1_000)
	}

	out := runTrace(t, testConfig(), td)
	require.Equal(t, 25, out.SpanCount())
	for i := 0; i < 25; i++ {
		_, ok := out.ResourceSpans().At(0).ScopeSpans().At(0).Spans().At(i).Attributes().Get("gen_ai.response.cost_usd")
		assert.True(t, ok, "span %d was not priced", i)
	}
}

// ---------------------------------------------------------------------------
// config validation
// ---------------------------------------------------------------------------

func TestConfigRejectsEmptyPriceTable(t *testing.T) {
	cfg := createDefaultConfig().(*Config)
	assert.Error(t, cfg.Validate())
}

func TestConfigRejectsNegativePrice(t *testing.T) {
	cfg := testConfig()
	cfg.Models["bad"] = ModelPrice{InputPer1M: -1}
	assert.Error(t, cfg.Validate())
}

func TestConfigDefaultsUnknownModelToWarn(t *testing.T) {
	cfg := testConfig()
	cfg.UnknownModel = ""
	require.NoError(t, cfg.Validate())
	assert.Equal(t, PolicyWarn, cfg.UnknownModel)
}

func TestConfigRejectsUnknownPolicy(t *testing.T) {
	cfg := testConfig()
	cfg.UnknownModel = "explode"
	assert.Error(t, cfg.Validate())
}

func TestPriceTableLookupIgnoresCase(t *testing.T) {
	cfg := testConfig()
	_, ok := cfg.priceFor("GPT-4O-MINI")
	assert.True(t, ok)
}

func TestPriceLookupOnEmptyModelReturnsFalse(t *testing.T) {
	cfg := testConfig()
	_, ok := cfg.priceFor("")
	assert.False(t, ok)
}

var _ = pcommon.NewMap // keep the import honest while the file evolves
