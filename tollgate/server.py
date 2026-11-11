"""The local proxy server: OpenAI-compatible transparent forwarding.

- Any /v1/* request is forwarded to the routed provider (chat,
  completions, embeddings, models ...). Metered paths also land in
  the call store with cost accounting.
- SSE streaming is passed through chunk-by-chunk; usage is taken
  from the stream when the upstream provides it, otherwise estimated
  (chars/4) and flagged.
- Budget cutoffs short-circuit the request before it is billed.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

import requests

from . import __version__
from .budget import budget_exceeded_after, check_budget
from .config import Config
from .pricing import Pricing
from .router import Forwarder
from .store import CallStore

METERED_PATHS = {"/chat/completions", "/completions", "/embeddings"}
SSE_CONTENT_TYPE = "text/event-stream"


def _is_metered(path: str) -> bool:
    """Match with or without the /v1 prefix (clients use both forms)."""
    return any(path == metered or path.endswith(metered) for metered in METERED_PATHS)


class ServerContext:
    def __init__(self, config: Config, store: CallStore, pricing: Pricing, forwarder: Forwarder) -> None:
        self.config = config
        self.store = store
        self.pricing = pricing
        self.forwarder = forwarder
        self._alert_lock = threading.Lock()
        self._alerted: set = set()

    def warn_once(self, message: str) -> None:
        with self._alert_lock:
            key = message.split(" >= ")[0]
            if key in self._alerted:
                return
            self._alerted.add(key)
        sys.stderr.write(message + "\n")
        sys.stderr.flush()


def make_server(config: Config, store: CallStore, pricing: Pricing, forwarder: Forwarder) -> ThreadingHTTPServer:
    ctx = ServerContext(config, store, pricing, forwarder)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # ------------------------------------------------------------ utils
        def _ctx(self) -> ServerContext:
            return self.server.ctx  # type: ignore[attr-defined]

        def _client_tag(self) -> str:
            tag = self.headers.get("X-Tollgate-Tag")
            return tag.strip() if tag and tag.strip() else "default"

        def _authorized(self) -> bool:
            token = self._ctx().config.local_token
            if not token:
                return True
            header = self.headers.get("Authorization", "")
            return header == f"Bearer {token}"

        def _read_body(self) -> Optional[bytes]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return None
            return self.rfile.read(length)

        def _reply(self, status: int, payload: Dict[str, Any], content_type: str = "application/json") -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _reply_error(self, status: int, message: str, kind: str = "tollgate_error") -> None:
            self._reply(status, {"error": {"message": message, "type": kind, "code": status}})

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet default access log
            pass

        # ---------------------------------------------------------- methods
        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._reply(200, {"status": "ok", "version": __version__, "service": "tollgate"})
                return
            self._proxy("GET")

        def do_POST(self) -> None:
            self._proxy("POST")

        def do_DELETE(self) -> None:
            self._proxy("DELETE")

        # ------------------------------------------------------------ proxy
        def _proxy(self, method: str) -> None:
            ctx = self._ctx()
            if not self._authorized():
                self._reply_error(401, "invalid or missing local token", "auth_error")
                return

            raw = self._read_body()
            path = self.path.split("?")[0]
            tag = self._client_tag()

            body_obj: Optional[Dict[str, Any]] = None
            if raw:
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                    body_obj = parsed if isinstance(parsed, dict) else None
                except (ValueError, UnicodeDecodeError):
                    body_obj = None

            if method == "POST" and _is_metered(path):
                state = check_budget(ctx.config, ctx.store, tag)
                if not state.allowed:
                    self._reply_error(429, state.reason, "budget_exceeded")
                    return

            wants_stream = bool(body_obj and body_obj.get("stream") is True)
            result = ctx.forwarder.forward(
                method, path, body_obj, stream=wants_stream, model=(body_obj or {}).get("model")
            )

            if result.response is None:
                self._meter(result, path, tag, None, 0, 0, raw, "")
                self._reply_error(result.status, result.error or "upstream unavailable")
                return

            response: requests.Response = result.response
            upstream_json = None
            if not wants_stream:
                # Buffered non-streaming path (also used for upstream errors).
                try:
                    content = response.content
                except requests.RequestException as exc:
                    self._meter(result, path, tag, None, 0, 0, raw, f"read error: {exc}")
                    self._reply_error(502, f"upstream read failed: {exc}")
                    return
                if response.headers.get("Content-Type", "").startswith("application/json"):
                    try:
                        upstream_json = json.loads(content.decode("utf-8", errors="replace"))
                    except ValueError:
                        upstream_json = None
                usage = self._extract_usage(upstream_json)
                prompt_tokens, completion_tokens, known = usage if usage else (0, 0, False)
                if not known:
                    prompt_tokens, completion_tokens = self._estimate_tokens(raw, "")
                if _is_metered(path):
                    self._meter(result, path, tag, upstream_json, prompt_tokens, completion_tokens, raw, "")
                self._send_buffered(response, content)
                return

            # Streaming passthrough.
            if response.headers.get("Content-Type", "").startswith(SSE_CONTENT_TYPE):
                self._stream_passthrough(response, result, path, tag, raw)
                return

            # Client asked for a stream but upstream answered JSON (error etc.).
            content = response.content
            try:
                upstream_json = json.loads(content.decode("utf-8", errors="replace"))
            except ValueError:
                upstream_json = None
            usage = self._extract_usage(upstream_json)
            prompt_tokens, completion_tokens, known = usage if usage else (0, 0, False)
            if not known:
                prompt_tokens, completion_tokens = self._estimate_tokens(raw, "")
            if _is_metered(path):
                self._meter(result, path, tag, upstream_json, prompt_tokens, completion_tokens, raw, "")
            self._send_buffered(response, content)

        # -------------------------------------------------------- streaming
        def _stream_passthrough(self, response, result, path, tag, raw) -> None:
            ctx = self._ctx()
            usage_tokens: Tuple[int, int] = (0, 0)
            usage_known = False
            text_chars = 0
            broken = False
            started = time.monotonic()
            try:
                self.send_response(int(response.status_code))
                self.send_header("Content-Type", SSE_CONTENT_TYPE)
                self.send_header("Cache-Control", "no-cache")
                # No framing info available: end-of-stream is signalled by
                # closing the connection so clients can detect EOF.
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                buffer = ""
                for chunk in response.iter_content(chunk_size=None):
                    if chunk is None:
                        continue
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    buffer += chunk.decode("utf-8", errors="replace")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            continue
                        try:
                            event = json.loads(payload)
                        except ValueError:
                            continue
                        if isinstance(event.get("usage"), dict):
                            usage_tokens = (
                                int(event["usage"].get("prompt_tokens", 0) or 0),
                                int(event["usage"].get("completion_tokens", 0) or 0),
                            )
                            usage_known = bool(usage_tokens[0] or usage_tokens[1])
                        for choice in event.get("choices") or []:
                            delta = choice.get("delta") or {}
                            text_chars += len(delta.get("content") or "")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                broken = True
            except requests.RequestException as exc:
                sys.stderr.write(f"[tollgate] stream failed: {exc}\n")
            finally:
                if not usage_known:
                    prompt_est, completion_est = self._estimate_tokens(raw, "x" * text_chars)
                    usage_tokens = (prompt_est, completion_est)

            prompt_tokens, completion_tokens = usage_tokens
            self._meter(
                result, path, tag, None, prompt_tokens, completion_tokens, raw, "",
                streamed=True, latency_ms=int((time.monotonic() - started) * 1000),
            )
            if broken:
                sys.stderr.write("[tollgate] client disconnected mid-stream (call still metered)\n")

        # ----------------------------------------------------------- meter
        def _meter(self, result, path, tag, upstream_json, prompt_tokens, completion_tokens,
                   raw_request, error_note, streamed=False, latency_ms=None) -> None:
            ctx = self._ctx()
            model_used = result.model_used or ""
            cost, known = ctx.pricing.cost_usd(model_used, prompt_tokens, completion_tokens)
            try:
                ctx.store.record(
                    tag=tag,
                    provider=result.provider,
                    model_requested=(self._model_from_raw(raw_request) or model_used),
                    model_used=model_used,
                    path=path,
                    status=result.status,
                    ok=result.ok,
                    latency_ms=latency_ms if latency_ms is not None else result.latency_ms,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cost_usd=cost,
                    cost_known=known,
                    retries=result.retries,
                    streamed=streamed,
                    error=(error_note or None) or (result.error or None) or (
                        (upstream_json or {}).get("error", {}).get("message") if isinstance(upstream_json, dict) else None
                    ),
                )
            except Exception as exc:  # metering must never break proxying
                sys.stderr.write(f"[tollgate] metering failed: {exc}\n")
                return
            warning = budget_exceeded_after(ctx.config, ctx.store, tag)
            if warning:
                ctx.warn_once(warning)

        # ---------------------------------------------------------- tokens
        @staticmethod
        def _extract_usage(payload: Optional[Dict[str, Any]]) -> Optional[Tuple[int, int, bool]]:
            if not isinstance(payload, dict):
                return None
            usage = payload.get("usage")
            if not isinstance(usage, dict):
                return None
            prompt = int(usage.get("prompt_tokens", 0) or 0)
            completion = int(usage.get("completion_tokens", 0) or 0)
            return prompt, completion, True

        @staticmethod
        def _model_from_raw(raw: Optional[bytes]) -> Optional[str]:
            if not raw:
                return None
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return None
            if isinstance(data, dict) and data.get("model") is not None:
                return str(data["model"])
            return None

        @staticmethod
        def _estimate_tokens(raw: Optional[bytes], completion_text: str) -> Tuple[int, int]:
            """chars/4 heuristic — flagged as estimated when usage is absent."""
            prompt_chars = len(raw or b"")
            return max(1, prompt_chars // 4), max(1, len(completion_text) // 4)

        # ------------------------------------------------------- forwarding
        def _send_buffered(self, response, content: bytes) -> None:
            try:
                self.send_response(int(response.status_code))
                content_type = response.headers.get("Content-Type", "application/json")
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer((config.listen_host, config.listen_port), Handler)
    server.daemon_threads = True
    server.ctx = ctx  # type: ignore[attr-defined]
    return server


def serve(config: Config, store: CallStore, pricing: Pricing, forwarder: Forwarder) -> None:
    server = make_server(config, store, pricing, forwarder)
    host, port = server.server_address[:2]
    sys.stderr.write(
        f"[tollgate] listening on http://{host}:{port}  (Ctrl+C to stop)\n"
        f"[tollgate] point your app at  base_url = http://{host}:{port}/v1\n"
        f"[tollgate] tag calls with the X-Tollgate-Tag header for cost attribution\n"
    )
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\n[tollgate] shutting down\n")
    finally:
        server.server_close()
        store.close()
