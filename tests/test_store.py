"""Call store: record, aggregate, budget queries."""

from __future__ import annotations

import time

import pytest

from tollgate.store import CallStore


class TestStore:
    def test_record_and_recent(self, store):
        store.record(tag="proj-a", provider="fake", model_used="m1",
                     status=200, ok=True, latency_ms=12,
                     prompt_tokens=10, completion_tokens=5, cost_usd=0.001,
                     cost_known=True)
        store.record(tag="proj-b", model_used="m2", status=500, ok=False,
                     error="boom")
        rows = store.recent(10)
        assert len(rows) == 2
        assert rows[0]["tag"] == "proj-a"
        assert rows[1]["ok"] == 0

    def test_spend_since_filters_tag(self, store):
        now = time.time()
        store.record(ts=now, tag="a", cost_usd=0.5)
        store.record(ts=now, tag="b", cost_usd=0.25)
        assert store.spend_since(now - 60) == 0.75
        assert store.spend_since(now - 60, tag="a") == 0.5
        assert store.spend_since(now - 60, tag="missing") == 0.0

    def test_spend_ignores_older_rows(self, store):
        now = time.time()
        store.record(ts=now - 10_000, tag="a", cost_usd=5.0)
        store.record(ts=now, tag="a", cost_usd=1.0)
        assert store.spend_since(now - 60) == 1.0

    def test_summarize_groups(self, store):
        now = time.time()
        for _ in range(3):
            store.record(ts=now, tag="a", model_used="m1", provider="p1",
                         ok=True, latency_ms=10, cost_usd=0.1,
                         prompt_tokens=10, completion_tokens=10)
        store.record(ts=now, tag="b", model_used="m2", provider="p1",
                     ok=False, latency_ms=20, cost_usd=0.9)
        by_tag = {r["bucket"]: r for r in store.summarize(now - 60, "tag")}
        assert by_tag["a"]["calls"] == 3
        assert by_tag["a"]["cost_usd"] == pytest.approx(0.3)
        assert by_tag["b"]["ok_calls"] == 0
        by_model = {r["bucket"]: r for r in store.summarize(now - 60, "model_used")}
        assert set(by_model) == {"m1", "m2"}

    def test_after_id_and_max_id(self, store):
        first = store.record(tag="a")
        second = store.record(tag="b")
        assert store.max_id() == second
        rows = store.after_id(first)
        assert [r["id"] for r in rows] == [second]

    def test_invalid_group_by_rejected(self, store):
        try:
            store.summarize(0, "error; drop table calls")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for bad group_by")
