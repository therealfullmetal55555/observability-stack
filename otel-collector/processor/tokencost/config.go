package tokencost

import (
	"errors"
	"fmt"
	"sort"
	"strings"

	"go.opentelemetry.io/collector/component"
)

// ModelPrice is USD per one million tokens.
type ModelPrice struct {
	InputPer1M  float64 `mapstructure:"input_usd_per_1m"`
	OutputPer1M float64 `mapstructure:"output_usd_per_1m"`
}

// UnknownModelPolicy decides what happens to a span whose model isn't in the
// price table. Silently writing $0 is the worst outcome: the dashboard shows a
// cheap day and nobody notices the model was never accounted for.
type UnknownModelPolicy string

const (
	// PolicyWarn keeps the span, leaves cost unset, and logs once per model.
	PolicyWarn UnknownModelPolicy = "warn"
	// PolicyZero keeps the span and writes cost 0. Use for local models that
	// are intentionally free but you still want counted.
	PolicyZero UnknownModelPolicy = "zero"
	// PolicyDropSpan is for teams where an unaccounted model is a billing
	// incident. Off by default because dropping data is irreversible.
	PolicyDropSpan UnknownModelPolicy = "drop_span"
)

// Config for the tokencost processor.
type Config struct {
	// Models maps a model name (or a distinctive substring of one) to prices.
	Models map[string]ModelPrice `mapstructure:"models"`

	InputTokensAttr  string `mapstructure:"input_tokens_attr"`
	OutputTokensAttr string `mapstructure:"output_tokens_attr"`
	ModelAttr        string `mapstructure:"model_attr"`
	CostAttr         string `mapstructure:"cost_attr"`
	TotalTokensAttr  string `mapstructure:"total_tokens_attr"`

	// RespectExisting leaves a cost that the SDK already computed. Vendor SDKs
	// know about cached reads and batch discounts that a static table doesn't.
	RespectExisting bool `mapstructure:"respect_existing"`

	UnknownModel UnknownModelPolicy `mapstructure:"unknown_model"`

	// EmitMetrics records how many spans were priced, skipped, and unknown.
	EmitMetrics bool `mapstructure:"emit_metrics"`
}

var _ component.Config = (*Config)(nil)

func (c *Config) Validate() error {
	if len(c.Models) == 0 {
		return errors.New("tokencost: at least one model price is required")
	}
	for name, price := range c.Models {
		if price.InputPer1M < 0 || price.OutputPer1M < 0 {
			return fmt.Errorf("tokencost: model %q has a negative price", name)
		}
	}
	if c.ModelAttr == "" {
		return errors.New("tokencost: model_attr must not be empty")
	}
	switch c.UnknownModel {
	case PolicyWarn, PolicyZero, PolicyDropSpan:
	case "":
		c.UnknownModel = PolicyWarn
	default:
		return fmt.Errorf(
			"tokencost: unknown_model must be one of warn, zero, drop_span; got %q",
			c.UnknownModel,
		)
	}
	return nil
}

// priceFor resolves a model name against the table.
//
// Longest key wins, so `gpt-4o-mini-2024-07-18` is priced as `gpt-4o-mini` and
// not as `gpt-4o`. The difference is 16x, and it shows up as a cost spike that
// nobody can explain three weeks later.
func (c *Config) priceFor(model string) (ModelPrice, bool) {
	if model == "" {
		return ModelPrice{}, false
	}
	lowered := strings.ToLower(model)

	keys := make([]string, 0, len(c.Models))
	for key := range c.Models {
		keys = append(keys, key)
	}
	sort.Slice(keys, func(i, j int) bool { return len(keys[i]) > len(keys[j]) })

	for _, key := range keys {
		if strings.Contains(lowered, strings.ToLower(key)) {
			return c.Models[key], true
		}
	}
	return ModelPrice{}, false
}

// cost computes USD for a single call. Token counts arrive as int64 from pdata;
// anything negative means a broken client and is treated as zero.
func (p ModelPrice) cost(inputTokens, outputTokens int64) float64 {
	if inputTokens < 0 {
		inputTokens = 0
	}
	if outputTokens < 0 {
		outputTokens = 0
	}
	return float64(inputTokens)/1_000_000*p.InputPer1M +
		float64(outputTokens)/1_000_000*p.OutputPer1M
}
