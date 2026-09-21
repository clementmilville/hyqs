import pytest

from hyqs.pipeline.pricing import PRICING, compute_cost


def test_claude_opus_5_resolves_and_prices_nonzero():
    assert "claude-opus-5" in PRICING
    assert compute_cost("claude-opus-5", 1_000_000, 1_000_000) > 0.0


def test_claude_sonnet_5_resolves_and_prices_nonzero():
    assert "claude-sonnet-5" in PRICING
    assert compute_cost("claude-sonnet-5", 1_000_000, 1_000_000) > 0.0


def test_claude_fable_5_resolves_and_prices_nonzero():
    assert "claude-fable-5" in PRICING
    assert compute_cost("claude-fable-5", 1_000_000, 1_000_000) > 0.0


def test_gpt_5_6_sol_resolves_and_prices_nonzero():
    assert "gpt-5.6-sol" in PRICING
    assert compute_cost("gpt-5.6-sol", 1_000_000, 1_000_000) > 0.0


def test_dated_snapshot_resolves_by_longest_prefix_match():
    dated = compute_cost("claude-haiku-4-5-20251001", 1_000_000, 1_000_000)
    base = compute_cost("claude-haiku-4-5", 1_000_000, 1_000_000)
    assert dated == base
    assert dated > 0.0


def test_cache_read_tokens_cost_point_one_times_input_rate():
    p = PRICING["claude-sonnet-4-6"]
    expected = (1_000_000 * p["read"]) / 1_000_000
    actual = compute_cost("claude-sonnet-4-6", 0, 0, cache_read_tokens=1_000_000)
    assert actual == expected == pytest.approx(p["in"] * 0.1)


def test_cache_read_tokens_cost_point_one_times_input_rate_new_model():
    p = PRICING["claude-opus-5"]
    actual = compute_cost("claude-opus-5", 0, 0, cache_read_tokens=1_000_000)
    assert actual == p["in"] * 0.1


def test_cache_creation_tokens_cost_1_25x_input_rate():
    p = PRICING["claude-sonnet-4-6"]
    actual = compute_cost("claude-sonnet-4-6", 0, 0, cache_creation_tokens=1_000_000)
    assert actual == p["in"] * 1.25


def test_cache_creation_tokens_cost_1_25x_input_rate_new_model():
    p = PRICING["claude-sonnet-5"]
    actual = compute_cost("claude-sonnet-5", 0, 0, cache_creation_tokens=1_000_000)
    assert actual == p["in"] * 1.25


def test_unknown_model_returns_zero():
    assert compute_cost("totally-unknown-model", 1_000_000, 1_000_000) == 0.0


def test_unknown_model_returns_zero_with_cache_args():
    assert (
        compute_cost(
            "totally-unknown-model",
            1_000_000,
            1_000_000,
            cache_creation_tokens=1_000_000,
            cache_read_tokens=1_000_000,
        )
        == 0.0
    )


def test_three_positional_args_matches_pre_change_formula():
    p = PRICING["claude-sonnet-4-6"]
    expected = (500_000 * p["in"] + 250_000 * p["out"]) / 1_000_000
    assert compute_cost("claude-sonnet-4-6", 500_000, 250_000) == expected


def test_openai_model_without_cache_tiers_falls_back_creation_to_in_rate():
    p = PRICING["gpt-4o"]
    actual = compute_cost("gpt-4o", 0, 0, cache_creation_tokens=1_000_000)
    assert actual == p["in"]


def test_gpt_5_6_sol_falls_back_creation_to_in_rate_and_uses_read_rate():
    p = PRICING["gpt-5.6-sol"]
    creation_cost = compute_cost("gpt-5.6-sol", 0, 0, cache_creation_tokens=1_000_000)
    read_cost = compute_cost("gpt-5.6-sol", 0, 0, cache_read_tokens=1_000_000)
    assert creation_cost == p["in"]
    assert read_cost == p["read"]


def test_o3_without_read_rate_falls_back_cache_read_to_in_rate():
    p = PRICING["o3"]
    assert "read" not in p
    actual = compute_cost("o3", 0, 0, cache_read_tokens=1_000_000)
    assert actual == p["in"]
