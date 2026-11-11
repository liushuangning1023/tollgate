"""Shared fixtures: config builders + local fake OpenAI upstream."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tollgate.config import Config, Hop, Provider
from tollgate.pricing import Pricing
from tollgate.router import Forwarder
from tollgate.store import CallStore


UPSTREAM_USAGE = {"prompt_tokens": 100, "completion_tokens": 50}
UPSTREAM_JSON = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "model": "fake-model",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "hello there"}}],
    "usage": UPSTREAM_USAGE,
}


class FakeUpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    behavior = "ok"  # class attr, mutated by tests: ok | rate_limit | server_error | stream
    hits = []

    def _log(self, body):
        FakeUpstreamHandler.hits.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization", ""),
                "body": body,
                "tag": self.headers.get("X-Tollgate-Tag", ""),
            }
        )

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            body = {}
        self._log(body)
        mode = FakeUpstreamHandler.behavior
        if mode == "rate_limit" and len(FakeUpstreamHandler.hits) == 1:
            payload = json.dumps({"error": {"message": "rate limited"}}).encode()
            self._send(429, payload, retry_after="0")
            return
        if mode == "server_error" and len(FakeUpstreamHandler.hits) == 1:
            self._send(500, b"boom", retry_after="0")
            return
        if body.get("stream") is True or mode == "stream":
            self._send_stream(body)
            return
        payload = json.dumps(UPSTREAM_JSON).encode()
        self._send(200, payload)

    def do_GET(self):
        if self.path.endswith("/models"):
            self._send(200, json.dumps({"object": "list", "data": []}).encode())
        else:
            self._send(404, b"{}")

    def _send(self, status, payload, retry_after=None):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if retry_after:
            self.send_header("Retry-After", retry_after)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_stream(self, body):
        chunks = [
            {"id": "chatcmpl-test", "model": body.get("model", "fake-model"),
             "choices": [{"index": 0, "delta": {"content": "hello "}}]},
            {"id": "chatcmpl-test", "model": body.get("model", "fake-model"),
             "choices": [{"index": 0, "delta": {"content": "there"}}]},
            {"id": "chatcmpl-test", "model": body.get("model", "fake-model"),
             "choices": [{"index": 0, "delta": {}}], "usage": UPSTREAM_USAGE},
        ]
        # End-of-stream is signalled by closing the connection: with
        # HTTP/1.1 keep-alive and no framing, EOF is the only terminator
        # a plain http.server can offer for SSE.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        for chunk in chunks:
            data = json.dumps(chunk, ensure_ascii=False)
            self.wfile.write(f"data: {data}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, fmt, *args):
        pass


@pytest.fixture()
def fake_upstream():
    FakeUpstreamHandler.behavior = "ok"
    FakeUpstreamHandler.hits = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstreamHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {
        "port": server.server_address[1],
        "hits": FakeUpstreamHandler.hits,
        "set_behavior": lambda mode: setattr(FakeUpstreamHandler, "behavior", mode),
    }
    server.shutdown()
    server.server_close()


def make_config(upstream_port: int, **overrides) -> Config:
    values = dict(
        listen_host="127.0.0.1",
        listen_port=0,
        db_path=":memory:",
        providers={"fake": Provider("fake", f"http://127.0.0.1:{upstream_port}/v1", "sk-fake")},
        provider_order=["fake"],
        routes={},
        budgets={},
        max_attempts=3,
        backoff_base=0.01,
        request_timeout=10,
        pricing_overrides={"fake-model": {"input": 2.0, "output": 4.0}},
        local_token="",
    )
    values.update(overrides)
    return Config(**values)


def make_upstream_config(upstream_port: int, **overrides) -> Config:
    values = dict(
        listen_host="127.0.0.1",
        listen_port=0,
        db_path=":memory:",
        providers={
            "fake": Provider("fake", f"http://127.0.0.1:{upstream_port}/v1", "sk-fake"),
        },
        provider_order=["fake"],
        routes={},
        budgets={},
        max_attempts=3,
        backoff_base=0.01,
        request_timeout=10,
        pricing_overrides={"fake-model": {"input": 2.0, "output": 4.0}},
        local_token="",
    )
    values.update(overrides)
    return Config(**values)


@pytest.fixture()
def store(tmp_path):
    return CallStore(str(tmp_path / "calls.db"))


@pytest.fixture()
def pricing():
    return Pricing.bundled({"fake-model": {"input": 2.0, "output": 4.0}})


@pytest.fixture()
def forwarder(fake_upstream):
    return Forwarder(make_upstream_config(fake_upstream["port"]))
