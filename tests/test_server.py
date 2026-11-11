"""End-to-end proxy tests against a local fake OpenAI upstream."""

from __future__ import annotations

import json
import threading

import requests

from tollgate.config import Hop
from tollgate.router import Forwarder
from tollgate.server import make_server
from tollgate.store import CallStore

from .conftest import make_upstream_config


class TollgateFixture:
    def __init__(self, upstream_port, tmp_path, **config_overrides):
        self.config = make_upstream_config(upstream_port, **config_overrides)
        self.store = CallStore(str(tmp_path / "calls.db"))
        from tollgate.pricing import Pricing
        self.pricing = Pricing.bundled(self.config.pricing_overrides)
        self.forwarder = Forwarder(self.config)
        self.server = make_server(self.config, self.store, self.pricing, self.forwarder)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.close()


def start_tollgate(fake_upstream, tmp_path, **overrides):
    return TollgateFixture(fake_upstream["port"], tmp_path, **overrides)


def chat(base, model="fake-model", tag=None, **body_extra):
    headers = {}
    if tag:
        headers["X-Tollgate-Tag"] = tag
    return requests.post(
        f"{base}/v1/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": "hi"}], **body_extra},
        headers=headers,
        timeout=10,
    )


class TestProxy:
    def test_healthz(self, fake_upstream, tmp_path):
        tg = start_tollgate(fake_upstream, tmp_path)
        try:
            response = requests.get(f"{tg.base}/healthz", timeout=5)
            assert response.status_code == 200
            assert response.json()["service"] == "tollgate"
        finally:
            tg.close()

    def test_nonstream_passthrough_and_metering(self, fake_upstream, tmp_path):
        tg = start_tollgate(fake_upstream, tmp_path)
        try:
            response = chat(tg.base, tag="proj-a")
            assert response.status_code == 200
            assert response.json()["usage"]["prompt_tokens"] == 100
            rows = tg.store.recent(5)
            assert len(rows) == 1
            row = rows[0]
            assert row["tag"] == "proj-a"
            assert row["provider"] == "fake"
            assert row["prompt_tokens"] == 100 and row["completion_tokens"] == 50
            # pricing override: 2.0/M in + 4.0/M out
            assert row["cost_usd"] == (100 * 2.0 + 50 * 4.0) / 1_000_000
            assert row["cost_known"] == 1
            assert row["ok"] == 1 and row["streamed"] == 0
        finally:
            tg.close()

    def test_stream_passthrough_and_usage(self, fake_upstream, tmp_path):
        tg = start_tollgate(fake_upstream, tmp_path)
        try:
            response = chat(tg.base, stream=True)
            assert response.status_code == 200
            assert response.headers["Content-Type"].startswith("text/event-stream")
            text = response.text
            assert "hello" in text and "[DONE]" in text
            rows = tg.store.recent(5)
            assert len(rows) == 1
            row = rows[0]
            assert row["streamed"] == 1
            assert row["prompt_tokens"] == 100 and row["completion_tokens"] == 50
        finally:
            tg.close()

    def test_upstream_auth_replaced(self, fake_upstream, tmp_path):
        tg = start_tollgate(fake_upstream, tmp_path)
        try:
            chat(tg.base)
            hit = fake_upstream["hits"][-1]
            assert hit["auth"] == "Bearer sk-fake"
        finally:
            tg.close()

    def test_models_passthrough_unmetered(self, fake_upstream, tmp_path):
        tg = start_tollgate(fake_upstream, tmp_path)
        try:
            response = requests.get(f"{tg.base}/v1/models", timeout=5)
            assert response.status_code == 200
            assert tg.store.max_id() == 0  # not metered
        finally:
            tg.close()

    def test_budget_cutoff_returns_429(self, fake_upstream, tmp_path):
        tg = start_tollgate(
            fake_upstream, tmp_path,
            budgets={"tags": {"proj-a": {"daily_usd": 0.0, "mode": "cutoff"}}},
        )
        try:
            response = chat(tg.base, tag="proj-a")
            assert response.status_code == 429
            body = response.json()
            assert body["error"]["type"] == "budget_exceeded"
            assert tg.store.max_id() == 0  # never reached the upstream
        finally:
            tg.close()

    def test_budget_alert_still_proxies(self, fake_upstream, tmp_path):
        tg = start_tollgate(
            fake_upstream, tmp_path,
            budgets={"tags": {"proj-a": {"daily_usd": 0.0, "mode": "alert"}}},
        )
        try:
            response = chat(tg.base, tag="proj-a")
            assert response.status_code == 200
            assert tg.store.max_id() == 1
        finally:
            tg.close()

    def test_local_token_required(self, fake_upstream, tmp_path):
        tg = start_tollgate(fake_upstream, tmp_path, local_token="secret")
        try:
            assert chat(tg.base).status_code == 401
            response = requests.post(
                f"{tg.base}/v1/chat/completions",
                json={"model": "fake-model", "messages": []},
                headers={"Authorization": "Bearer secret"},
                timeout=10,
            )
            assert response.status_code == 200
        finally:
            tg.close()

    def test_failover_with_second_provider(self, fake_upstream, tmp_path):
        # Route "fake-model" so that hop 1 hits a dead provider, hop 2 the fake upstream.
        from tollgate.config import Provider

        config = make_upstream_config(fake_upstream["port"])
        config.providers = {
            "dead": Provider("dead", "http://127.0.0.1:9/v1", "k"),
            "fake": config.providers["fake"],
        }
        config.provider_order = ["dead", "fake"]
        config.routes = {"fake-model": [Hop("dead", "fake-model"), Hop("fake", "fake-model")]}

        store = CallStore(":memory:")
        from tollgate.pricing import Pricing
        pricing = Pricing.bundled(config.pricing_overrides)
        forwarder = Forwarder(config)
        server = make_server(config, store, pricing, forwarder)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            response = requests.post(
                f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions",
                json={"model": "fake-model", "messages": []},
                timeout=20,
            )
            assert response.status_code == 200
            row = store.recent(1)[0]
            assert row["provider"] == "fake" and row["retries"] == 1
        finally:
            server.shutdown()
            server.server_close()
            store.close()
