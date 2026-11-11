"""Budgets: cutoff vs alert, tag vs global, day window."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from tollgate.budget import budget_exceeded_after, check_budget, summarize_budgets
from tollgate.store import CallStore


def make_config(budgets):
    from tollgate.config import Config, Provider

    return Config(
        listen_host="127.0.0.1", listen_port=0, db_path=":memory:",
        providers={"p": Provider("p", "http://u/v1", "k")},
        provider_order=["p"], routes={}, budgets=budgets,
        max_attempts=3, backoff_base=0.0, request_timeout=10,
        pricing_overrides={}, local_token="",
    )


class TestBudget:
    def test_no_budget_always_allows(self, store):
        state = check_budget(make_config({}), store, "any")
        assert state.allowed

    def test_cutoff_blocks_when_over(self, store):
        now = time.time()
        store.record(ts=now, tag="a", cost_usd=1.5)
        cfg = make_config({"tags": {"a": {"daily_usd": 1.0, "mode": "cutoff"}}})
        state = check_budget(cfg, store, "a", now=now)
        assert not state.allowed
        assert "budget" in state.reason.lower()
        assert state.spent == 1.5 and state.cap == 1.0

    def test_alert_mode_allows_but_flags(self, store):
        now = time.time()
        store.record(ts=now, tag="a", cost_usd=1.5)
        cfg = make_config({"tags": {"a": {"daily_usd": 1.0, "mode": "alert"}}})
        state = check_budget(cfg, store, "a", now=now)
        assert state.allowed
        warning = budget_exceeded_after(cfg, store, "a", now=now)
        assert warning is not None and "1.0000" in warning

    def test_under_budget_allows(self, store):
        now = time.time()
        store.record(ts=now, tag="a", cost_usd=0.5)
        cfg = make_config({"tags": {"a": {"daily_usd": 1.0, "mode": "cutoff"}}})
        assert check_budget(cfg, store, "a", now=now).allowed

    def test_tag_cutoff_wins_over_global_alert(self, store):
        now = time.time()
        store.record(ts=now, tag="a", cost_usd=9.0)
        cfg = make_config({
            "global": {"daily_usd": 10.0, "mode": "alert"},
            "tags": {"a": {"daily_usd": 5.0, "mode": "cutoff"}},
        })
        state = check_budget(cfg, store, "a", now=now)
        assert not state.allowed
        assert "tag 'a'" in state.reason

    def test_global_cutoff_applies_to_any_tag(self, store):
        now = time.time()
        store.record(ts=now, tag="whatever", cost_usd=11.0)
        cfg = make_config({"global": {"daily_usd": 10.0, "mode": "cutoff"}})
        assert not check_budget(cfg, store, "other", now=now).allowed

    def test_day_window_boundary(self, store):
        now = time.time()
        yesterday = datetime.fromtimestamp(now) - timedelta(days=1)
        store.record(ts=yesterday.replace(hour=12).timestamp(), tag="a", cost_usd=99.0)
        cfg = make_config({"tags": {"a": {"daily_usd": 1.0, "mode": "cutoff"}}})
        assert check_budget(cfg, store, "a", now=now).allowed

    def test_summarize_rows(self, store):
        now = time.time()
        store.record(ts=now, tag="a", cost_usd=0.4)
        cfg = make_config({
            "global": {"daily_usd": 2.0, "mode": "alert"},
            "tags": {"a": {"daily_usd": 0.5, "mode": "cutoff"}},
        })
        rows = summarize_budgets(cfg, store, now=now)
        by_scope = {r[0]: r for r in rows}
        assert "global" in by_scope and "tag:a" in by_scope
        assert by_scope["tag:a"][3] == 0.4
