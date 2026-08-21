"""
Per-million-token pricing.

This table is the only thing standing between you and a surprise invoice, so it
lives on its own — no OpenTelemetry imports, easy to unit-test, easy to extend
when a provider ships a new model.
"""

from __future__ import annotations

from typing import Dict

MODEL_PRICING: Dict[str, Dict[str, float]] = {
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4.1": {"input": 2.00, "output": 8.00},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "o3-mini": {"input": 1.10, "output": 4.40},
    "claude-sonnet-4": {"input": 3.00, "output": 15.00},
    "claude-haiku-3.5": {"input": 0.80, "output": 4.00},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00},
    "llama-3.3-70b": {"input": 0.60, "output": 0.70},
    "text-embedding-3-small": {"input": 0.02, "output": 0.0},
    "text-embedding-3-large": {"input": 0.13, "output": 0.0},
}

LATENCY_BUCKETS_S = [0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300]


def price_call(model: str, input_tokens: int, output_tokens: int) -> float:
    """
    Estimate USD for a single call. Returns 0.0 for unknown models.

    Longest key wins, so `gpt-4o-mini-2024-07-18` is priced as `gpt-4o-mini` and
    not as `gpt-4o` — a 16x difference that would otherwise show up as a
    mysterious cost spike on the dashboard.
    """
    lowered = model.lower()
    for key in sorted(MODEL_PRICING, key=len, reverse=True):
        if key in lowered:
            price = MODEL_PRICING[key]
            return (input_tokens / 1_000_000) * price["input"] + (
                output_tokens / 1_000_000
            ) * price["output"]
    return 0.0
