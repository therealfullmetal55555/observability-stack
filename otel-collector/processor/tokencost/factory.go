package tokencost

import (
	"context"

	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/consumer"
	"go.opentelemetry.io/collector/processor"
	"go.opentelemetry.io/collector/processor/processorhelper"
)

const (
	// typeStr is the name used in config.yaml: `tokencost:`.
	typeStr = "tokencost"
	// stability reflects reality: the config surface may still change.
	stability = component.StabilityLevelBeta
)

var componentType = component.MustNewType(typeStr)

// NewFactory returns the processor factory. Register it in the distribution
// manifest (otel-collector/builder-config.yaml) or the collector will refuse to
// start with "unknown type: tokencost".
func NewFactory() processor.Factory {
	return processor.NewFactory(
		componentType,
		createDefaultConfig,
		processor.WithTraces(createTracesProcessor, stability),
		processor.WithMetrics(createMetricsProcessor, stability),
	)
}

func createDefaultConfig() component.Config {
	return &Config{
		Models: map[string]ModelPrice{},
		InputTokensAttr:  "gen_ai.usage.input_tokens",
		OutputTokensAttr: "gen_ai.usage.output_tokens",
		ModelAttr:        "gen_ai.request.model",
		CostAttr:         "gen_ai.response.cost_usd",
		TotalTokensAttr:  "gen_ai.usage.total_tokens",
		RespectExisting:  true,
		UnknownModel:     PolicyWarn,
		EmitMetrics:      true,
	}
}

func createTracesProcessor(
	ctx context.Context,
	set processor.Settings,
	cfg component.Config,
	next consumer.Traces,
) (processor.Traces, error) {
	proc, err := newProcessor(set.TelemetrySettings, cfg.(*Config))
	if err != nil {
		return nil, err
	}
	return processorhelper.NewTraces(
		ctx,
		set,
		cfg,
		next,
		proc.processTraces,
		processorhelper.WithCapabilities(consumer.Capabilities{MutatesData: true}),
		processorhelper.WithStart(proc.start),
		processorhelper.WithShutdown(proc.shutdown),
	)
}

func createMetricsProcessor(
	ctx context.Context,
	set processor.Settings,
	cfg component.Config,
	next consumer.Metrics,
) (processor.Metrics, error) {
	proc, err := newProcessor(set.TelemetrySettings, cfg.(*Config))
	if err != nil {
		return nil, err
	}
	return processorhelper.NewMetrics(
		ctx,
		set,
		cfg,
		next,
		proc.processMetrics,
		processorhelper.WithCapabilities(consumer.Capabilities{MutatesData: true}),
		processorhelper.WithStart(proc.start),
		processorhelper.WithShutdown(proc.shutdown),
	)
}
