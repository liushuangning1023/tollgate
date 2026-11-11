"""Router: routing, failover, retry, model rewriting."""

from __future__ import annotations

from unittest import mock

import pytest
import requests

from tollgate.config import Config, Hop, Provider
from tollgate.router import Forwarder, parse_model_from_body


def fake_response(status, json_body=None, headers=None):
    response = mock.Mock(spec=requests.Response)
    response.status_code = status
    response.headers = headers or {}
    if json_body is not None:
        response.json.return_value = json_body
    return response


def make_config(providers, routes=None, order=None, **kw):
    return Config(
        listen_host="127.0.0.1", listen_port=0, db_path=":memory:",
        providers=providers, provider_order=order or list(providers),
        routes=routes or {}, budgets={}, max_attempts=kw.pop("max_attempts", 3),
        backoff_base=kw.pop("backoff_base", 0.0), request_timeout=10,
        pricing_overrides={}, local_token="",
    )


class TestRouting:
    def test_exact_route_used_with_model_rewrite(self):
        cfg = make_config(
            {"p1": Provider("p1", "http://u1/v1", "k1"),
             "p2": Provider("p2", "http://u2/v1", "k2")},
            routes={"cheap": [Hop("p1", "mini"), Hop("p2", "flash")]},
        )
        chain = cfg.route_for("cheap")
        assert [(h.provider, h.model) for h in chain] == [("p1", "mini"), ("p2", "flash")]

    def test_default_route_for_unknown_model(self):
        cfg = make_config(
            {"p1": Provider("p1", "http://u1/v1", "k1")},
            routes={"default": [Hop("p1", "fallback-model")]},
        )
        chain = cfg.route_for("whatever")
        assert chain[0].model == "fallback-model"

    def test_unknown_model_passes_through_every_provider(self):
        cfg = make_config(
            {"p1": Provider("p1", "http://u1/v1", "k1"),
             "p2": Provider("p2", "http://u2/v1", "k2")},
            order=["p2", "p1"],
        )
        chain = cfg.route_for("brand-new-model")
        assert [h.provider for h in chain] == ["p2", "p1"]
        assert all(h.model == "brand-new-model" for h in chain)


class TestForward:
    def test_success_first_hop(self):
        cfg = make_config(
            {"p1": Provider("p1", "http://u1/v1", "k1")},
            routes={"m": [Hop("p1", "m")]},
        )
        transport = mock.Mock(return_value=fake_response(200, {"ok": True}))
        forwarder = Forwarder(cfg, transport)
        result = forwarder.forward("POST", "/chat/completions", {"model": "m"})
        assert result.ok and result.status == 200 and result.retries == 0
        url = transport.call_args.args[1]
        assert url == "http://u1/v1/chat/completions"
        assert transport.call_args.kwargs["headers"]["Authorization"] == "Bearer k1"

    def test_model_rewritten_on_hop(self):
        cfg = make_config(
            {"p1": Provider("p1", "http://u1/v1", "k1")},
            routes={"alias": [Hop("p1", "real-name")]},
        )
        transport = mock.Mock(return_value=fake_response(200))
        Forwarder(cfg, transport).forward("POST", "/chat/completions", {"model": "alias"})
        assert transport.call_args.kwargs["json"]["model"] == "real-name"

    def test_failover_after_429(self):
        cfg = make_config(
            {"p1": Provider("p1", "http://u1/v1", "k1"),
             "p2": Provider("p2", "http://u2/v1", "k2")},
            routes={"m": [Hop("p1", "m1"), Hop("p2", "m2")]},
        )
        transport = mock.Mock(side_effect=[fake_response(429), fake_response(200)])
        with mock.patch("tollgate.router.time.sleep") as sleep:
            result = Forwarder(cfg, transport).forward("POST", "/v1/chat/completions", {"model": "m"})
        assert result.ok and result.retries == 1 and result.provider == "p2"
        assert result.model_used == "m2"
        assert sleep.called

    def test_retry_after_header_respected(self):
        cfg = make_config(
            {"p1": Provider("p1", "http://u1/v1", "k1"),
             "p2": Provider("p2", "http://u2/v1", "k2")},
            routes={"m": [Hop("p1", "m1"), Hop("p2", "m2")]},
            backoff_base=5.0,
        )
        transport = mock.Mock(side_effect=[fake_response(429, headers={"Retry-After": "0"}),
                                           fake_response(200)])
        with mock.patch("tollgate.router.time.sleep") as sleep:
            Forwarder(cfg, transport).forward("POST", "/c", {"model": "m"})
            delay = sleep.call_args.args[0]
        assert delay < 5.0  # Retry-After won over the exponential base

    def test_network_error_fails_over(self):
        cfg = make_config(
            {"p1": Provider("p1", "http://u1/v1", "k1"),
             "p2": Provider("p2", "http://u2/v1", "k2")},
            routes={"m": [Hop("p1", "m"), Hop("p2", "m")]},
        )
        transport = mock.Mock(side_effect=[requests.ConnectionError("down"), fake_response(200)])
        with mock.patch("tollgate.router.time.sleep"):
            result = Forwarder(cfg, transport).forward("POST", "/c", {"model": "m"})
        assert result.ok and result.provider == "p2"

    def test_client_error_not_retried(self):
        cfg = make_config(
            {"p1": Provider("p1", "http://u1/v1", "k1"),
             "p2": Provider("p2", "http://u2/v1", "k2")},
            routes={"m": [Hop("p1", "m"), Hop("p2", "m")]},
        )
        transport = mock.Mock(return_value=fake_response(401))
        result = Forwarder(cfg, transport).forward("POST", "/c", {"model": "m"})
        assert not result.ok and result.status == 401
        assert transport.call_count == 1

    def test_max_attempts_bounds_failover(self):
        cfg = make_config(
            {"p1": Provider("p1", "http://u1/v1", "k1"),
             "p2": Provider("p2", "http://u2/v1", "k2"),
             "p3": Provider("p3", "http://u3/v1", "k3")},
            routes={"m": [Hop("p1", "m"), Hop("p2", "m"), Hop("p3", "m")]},
            max_attempts=2,
        )
        transport = mock.Mock(side_effect=[
            fake_response(429), fake_response(429), fake_response(200),
        ])
        with mock.patch("tollgate.router.time.sleep"):
            result = Forwarder(cfg, transport).forward("POST", "/c", {"model": "m"})
        assert not result.ok and transport.call_count == 2

    def test_missing_body_model_rejected(self):
        cfg = make_config({"p1": Provider("p1", "http://u1/v1", "k1")})
        result = Forwarder(cfg, lambda *a, **k: None).forward("POST", "/c", {})
        assert result.status == 400

    def test_no_provider_configured(self):
        cfg = make_config({})
        result = Forwarder(cfg, lambda *a, **k: None).forward("POST", "/c", {"model": "m"})
        assert result.status == 503


class TestModelParsing:
    def test_parse_model(self):
        assert parse_model_from_body(b'{"model": "x"}') == "x"
        assert parse_model_from_body(b"not json") is None
        assert parse_model_from_body(b"[]") is None
