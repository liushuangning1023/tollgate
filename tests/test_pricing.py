"""Pricing table and cost calculation."""

from __future__ import annotations

import pytest

from tollgate.pricing import Pricing


class TestPricing:
    def test_bundled_loads(self):
        table = Pricing.bundled()
        assert "gpt-4o-mini" in table.all_models()

    def test_cost_calculation(self):
        pricing = Pricing(table={"m": {"input": 2.0, "output": 4.0}})
        cost, known = pricing.cost_usd("m", 1_000_000, 500_000)
        assert known and cost == pytest.approx(2.0 + 2.0)

    def test_unknown_model_flagged(self):
        pricing = Pricing.bundled()
        cost, known = pricing.cost_usd("never-heard-of-it", 100, 100)
        assert cost == 0.0 and known is False

    def test_override_beats_bundle(self):
        pricing = Pricing.bundled({"gpt-4o-mini": {"input": 99.0, "output": 99.0}})
        cost, known = pricing.cost_usd("gpt-4o-mini", 1_000_000, 0)
        assert known and cost == pytest.approx(99.0)
        assert "override" in pricing.describe("gpt-4o-mini")

    def test_missing_side_defaults_to_zero(self):
        pricing = Pricing(table={"m": {"input": 1.0}})
        cost, known = pricing.cost_usd("m", 1_000_000, 1_000_000)
        assert known and cost == pytest.approx(1.0)

    def test_zero_tokens_zero_cost(self):
        pricing = Pricing(table={"m": {"input": 2.0, "output": 4.0}})
        cost, known = pricing.cost_usd("m", 0, 0)
        assert known and cost == 0.0
