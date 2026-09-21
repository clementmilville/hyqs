"""Shared pricing table for agent token cost estimation.

USD per 1M tokens. Cache tiers follow documented multipliers off input:
write-5m = 1.25x, write-1h = 2x, read = 0.1x. OpenAI models only define
``in``/``out`` since they don't expose cache tiers via Codex CLI output.
"""

from __future__ import annotations

PRICING: dict[str, dict[str, float]] = {
    # Claude models
    "claude-opus-5": {"in": 5.0, "out": 25.0, "w5": 6.25, "w1h": 10.0, "read": 0.50},
    "claude-sonnet-5": {"in": 3.0, "out": 15.0, "w5": 3.75, "w1h": 6.0, "read": 0.30},
    "claude-fable-5": {"in": 10.0, "out": 50.0, "w5": 12.5, "w1h": 20.0, "read": 1.00},
    "claude-opus-4-8": {"in": 5.0, "out": 25.0, "w5": 6.25, "w1h": 10.0, "read": 0.50},
    "claude-opus-4-7": {"in": 5.0, "out": 25.0, "w5": 6.25, "w1h": 10.0, "read": 0.50},
    "claude-sonnet-4-6": {"in": 3.0, "out": 15.0, "w5": 3.75, "w1h": 6.0, "read": 0.30},
    "claude-haiku-4-5": {"in": 1.0, "out": 5.0, "w5": 1.25, "w1h": 2.0, "read": 0.10},
    # OpenAI / Codex models
    "codex-mini-latest": {"in": 1.50, "out": 6.0},
    "o4-mini": {"in": 1.10, "out": 4.40},
    "o3": {"in": 10.0, "out": 40.0},
    "gpt-4o": {"in": 2.50, "out": 10.0},
    "gpt-5.6-sol": {"in": 5.0, "out": 30.0, "read": 0.50},
}


def _price(model: str) -> dict | None:
    """Longest-prefix match so dated snapshots (e.g. …-4-5-20251001) resolve."""
    best: tuple[str, dict] | None = None
    for name, p in PRICING.items():
        if model.startswith(name) and (best is None or len(name) > len(best[0])):
            best = (name, p)
    return best[1] if best else None


def compute_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Return cost in USD for the given token counts, or 0.0 for unknown models.

    Cache-creation tokens price at the ``w5`` rate and cache-read tokens at the
    ``read`` rate when the model defines them; models without cache tiers (e.g.
    OpenAI entries) fall back to the ``in`` rate for cache-creation and ``read``
    if present, else ``in``, for cache-read.
    """
    p = _price(model)
    if p is None:
        return 0.0
    creation_rate = p.get("w5", p["in"])
    read_rate = p.get("read", p["in"])
    total = (
        input_tokens * p["in"]
        + output_tokens * p["out"]
        + cache_creation_tokens * creation_rate
        + cache_read_tokens * read_rate
    )
    return total / 1_000_000
