// Package tokencost recomputes per-request LLM spend from token counts.
//
// Why this lives in the collector instead of in each agent:
//
// The same price table is needed by 21 services spread across Python and
// Node. Shipping it as a library means 21 copies to bump every time a provider
// changes a price, and 21 chances to get it wrong. Here it is one file, one
// restart, and every agent picks it up. Agents still report their own cost when
// they know better (batch discounts, cache reads) — `respect_existing` means we
// only fill in what's missing.
package tokencost

import (
	"context"
	"strconv"
	"sync"
	"sync/atomic"

	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/pmetric"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.uber.org/zap"
)

type processor struct {
	logger *zap.Logger
	cfg    *Config

	// One log line per unknown model, not one per span. A new model rolling out
	// across a fleet should not produce a million identical warnings.
	unknownSeen sync.Map

	priced    atomic.Int64
	skipped   atomic.Int64
	unknown   atomic.Int64
	malformed atomic.Int64

	spansPriced  metric.Int64Counter
	spansUnknown metric.Int64Counter
}

func newProcessor(settings component.TelemetrySettings, cfg *Config) (*processor, error) {
	p := &processor{
		logger: settings.Logger,
		cfg:    cfg,
	}

	if cfg.EmitMetrics {
		meter := settings.MeterProvider.Meter("github.com/therealfullmetal55555/observability-stack/tokencost")

		var err error
		p.spansPriced, err = meter.Int64Counter(
			"otelcol_processor_tokencost_spans_priced_total",
			metric.WithDescription("Spans whose cost was computed from token counts"),
		)
		if err != nil {
			return nil, err
		}

		p.spansUnknown, err = meter.Int64Counter(
			"otelcol_processor_tokencost_spans_unknown_model_total",
			metric.WithDescription("Spans whose model was not in the price table"),
		)
		if err != nil {
			return nil, err
		}
	}

	return p, nil
}

func (p *processor) start(_ context.Context, _ component.Host) error {
	p.logger.Info("tokencost started",
		zap.Int("models", len(p.cfg.Models)),
		zap.String("unknown_model_policy", string(p.cfg.UnknownModel)),
		zap.Bool("respect_existing", p.cfg.RespectExisting),
	)
	return nil
}

func (p *processor) shutdown(_ context.Context) error {
	p.logger.Info("tokencost stopped",
		zap.Int64("spans_priced", p.priced.Load()),
		zap.Int64("spans_skipped_existing_cost", p.skipped.Load()),
		zap.Int64("spans_unknown_model", p.unknown.Load()),
		zap.Int64("spans_malformed", p.malformed.Load()),
	)
	return nil
}

func (p *processor) processTraces(_ context.Context, td ptrace.Traces) (ptrace.Traces, error) {
	pricedBefore := p.priced.Load()

	td.ResourceSpans().RemoveIf(func(rs ptrace.ResourceSpans) bool {
		rs.ScopeSpans().RemoveIf(func(ss ptrace.ScopeSpans) bool {
			ss.Spans().RemoveIf(func(span ptrace.Span) bool {
				return p.drop(span)
			})
			return ss.Spans().Len() == 0
		})
		return rs.ScopeSpans().Len() == 0
	})

	if p.cfg.EmitMetrics {
		if delta := p.priced.Load() - pricedBefore; delta > 0 {
			p.spansPriced.Add(context.Background(), delta)
		}
	}
	return td, nil
}

// processMetrics handles the same attributes when an agent reports them as
// metric datapoints instead of span attributes. Some exporters only ship
// counters, and losing cost for those agents would make the numbers lie.
func (p *processor) processMetrics(_ context.Context, md pmetric.Metrics) (pmetric.Metrics, error) {
	md.ResourceMetrics().RemoveIf(func(rm pmetric.ResourceMetrics) bool {
		rm.ScopeMetrics().RemoveIf(func(sm pmetric.ScopeMetrics) bool {
			for i := 0; i < sm.Metrics().Len(); i++ {
				p.priceMetric(sm.Metrics().At(i))
			}
			return sm.Metrics().Len() == 0
		})
		return rm.ScopeMetrics().Len() == 0
	})
	return md, nil
}

func (p *processor) priceMetric(metric pmetric.Metric) {
	switch metric.Type() {
	case pmetric.MetricTypeSum:
		dps := metric.Sum().DataPoints()
		for i := 0; i < dps.Len(); i++ {
			p.priceAttributes(dps.At(i).Attributes())
		}
	case pmetric.MetricTypeGauge:
		dps := metric.Gauge().DataPoints()
		for i := 0; i < dps.Len(); i++ {
			p.priceAttributes(dps.At(i).Attributes())
		}
	default:
		// Histograms carry the token counts too in some SDKs, but writing a
		// scalar cost attribute onto a histogram is meaningless — skip.
	}
}

// drop returns true if the span should be removed. It is only ever true under
// the drop_span policy, which is off by default.
func (p *processor) drop(span ptrace.Span) bool {
	return p.priceAttributes(span.Attributes()) == dropSpan
}

type decision int

const (
	keepSpan decision = iota
	dropSpan
)

func (p *processor) priceAttributes(attrs pcommon.Map) decision {
	model := stringAttr(attrs, p.cfg.ModelAttr)
	if model == "" {
		// Not every span is a model call. Tool spans, HTTP spans and retriever
		// spans all flow through here; silence is correct for them.
		return keepSpan
	}

	if p.cfg.RespectExisting && hasNonZeroDouble(attrs, p.cfg.CostAttr) {
		p.skipped.Add(1)
		return keepSpan
	}

	inputTokens := intAttr(attrs, p.cfg.InputTokensAttr)
	outputTokens := intAttr(attrs, p.cfg.OutputTokensAttr)

	if inputTokens == 0 && outputTokens == 0 {
		// A model call with no token counts is a broken client, not a free
		// call. Count it so it shows up on the pipeline health panel.
		p.malformed.Add(1)
		return keepSpan
	}

	price, known := p.cfg.priceFor(model)
	if !known {
		p.unknown.Add(1)
		if p.spansUnknown != nil {
			p.spansUnknown.Add(context.Background(), 1, metric.WithAttributes(attribute.String("model", model)))
		}
		p.warnOnce(model)

		switch p.cfg.UnknownModel {
		case PolicyDropSpan:
			return dropSpan
		case PolicyZero:
			attrs.PutDouble(p.cfg.CostAttr, 0)
			return keepSpan
		default:
			return keepSpan
		}
	}

	cost := price.cost(inputTokens, outputTokens)
	attrs.PutDouble(p.cfg.CostAttr, cost)

	if p.cfg.TotalTokensAttr != "" && !attrs.Has(p.cfg.TotalTokensAttr) {
		attrs.PutInt(p.cfg.TotalTokensAttr, inputTokens+outputTokens)
	}

	p.priced.Add(1)
	if p.spansPriced != nil {
		p.spansPriced.Add(context.Background(), 1, metric.WithAttributes(
			attribute.String("model", model),
			attribute.String("service.name", stringAttr(attrs, "service.name")),
		))
	}

	return keepSpan
}

func (p *processor) warnOnce(model string) {
	if _, loaded := p.unknownSeen.LoadOrStore(model, struct{}{}); loaded {
		return
	}
	p.logger.Warn("tokencost: model not in price table — its spend is not being tracked",
		zap.String("model", model),
		zap.String("policy", string(p.cfg.UnknownModel)),
		zap.String("fix", "add it to processors.tokencost.models in otel-collector/config.yaml"),
	)
}

// ---- attribute helpers -----------------------------------------------------

func stringAttr(attrs pcommon.Map, key string) string {
	if key == "" {
		return ""
	}
	value, ok := attrs.Get(key)
	if !ok {
		return ""
	}
	if value.Type() != pcommon.ValueTypeStr {
		return ""
	}
	return value.Str()
}

func intAttr(attrs pcommon.Map, key string) int64 {
	if key == "" {
		return 0
	}
	value, ok := attrs.Get(key)
	if !ok {
		return 0
	}
	switch value.Type() {
	case pcommon.ValueTypeInt:
		return value.Int()
	case pcommon.ValueTypeDouble:
		return int64(value.Double())
	case pcommon.ValueTypeStr:
		// Some SDKs serialise token counts into strings. Parse leniently and
		// treat junk as zero rather than failing the whole batch.
		parsed, err := strconv.ParseInt(value.Str(), 10, 64)
		if err != nil {
			return 0
		}
		return parsed
	default:
		return 0
	}
}

func hasNonZeroDouble(attrs pcommon.Map, key string) bool {
	if key == "" {
		return false
	}
	value, ok := attrs.Get(key)
	if !ok {
		return false
	}
	switch value.Type() {
	case pcommon.ValueTypeDouble:
		return value.Double() > 0
	case pcommon.ValueTypeInt:
		return value.Int() > 0
	case pcommon.ValueTypeStr:
		parsed, err := strconv.ParseFloat(value.Str(), 64)
		if err != nil {
			return false
		}
		return parsed > 0
	default:
		return false
	}
}
