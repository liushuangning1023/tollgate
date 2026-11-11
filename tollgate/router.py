"""Router: hop chains, retry with backoff, provider failover.

Only transient failures are retried (429/5xx/timeout/network); other
4xx responses are forwarded to the client untouched. Retries skip to
the next provider in the chain, which bounds worst-case billing.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import requests

from .config import Config, Hop, Provider

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

Transport = Callable[..., "requests.Response"]


def normalize_path(path: str) -> str:
    """Drop a leading ``/v1`` segment: provider base URLs already carry it.

    A client calling ``http://localhost:8787/v1/chat/completions`` sends
    ``/v1/chat/completions``; joining that onto ``https://api.openai.com/v1``
    would double the prefix, so one leading version segment is removed.
    """
    if path.startswith("/v1/"):
        return path[3:]
    if path == "/v1":
        return "/"
    return path


def default_transport(method: str, url: str, **kwargs: Any) -> requests.Response:
    return requests.request(method, url, **kwargs)


@dataclass
class ForwardResult:
    ok: bool
    status: int
    provider: Optional[str] = None
    model_used: Optional[str] = None
    latency_ms: int = 0
    retries: int = 0
    error: Optional[str] = None
    response: Optional[Any] = None  # requests.Response (streaming or buffered)
    attempts: List[Dict[str, Any]] = field(default_factory=list)


class Forwarder:
    """Sends one client request through the provider chain."""

    def __init__(self, config: Config, transport: Optional[Transport] = None) -> None:
        self.config = config
        self.transport = transport or default_transport

    # ------------------------------------------------------------------ api
    def forward(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]],
        stream: bool = False,
        model: Optional[str] = None,
    ) -> ForwardResult:
        requested_model = model or (body or {}).get("model")
        if body is not None and requested_model is None:
            return ForwardResult(ok=False, status=400, error="request body has no 'model' field")
        chain = self.config.route_for(str(requested_model))
        if not chain:
            return ForwardResult(
                ok=False, status=503,
                model_used=str(requested_model),
                error="no provider configured (see `tollgate init`)",
            )

        attempts: List[Dict[str, Any]] = []
        max_attempts = max(1, self.config.max_attempts)
        last_error = "no provider attempted"
        last_status = 503

        for index, hop in enumerate(chain):
            if index >= max_attempts:
                break
            provider = self.config.provider(hop.provider)
            if provider is None or not provider.base_url:
                attempts.append({"provider": hop.provider, "status": 0, "error": "provider not configured"})
                last_error = f"provider '{hop.provider}' not configured"
                last_status = 503
                continue

            out_body = dict(body) if body is not None else None
            if out_body is not None and hop.model != requested_model:
                out_body["model"] = hop.model

            url = provider.base_url.rstrip("/") + normalize_path(path)
            headers = {"Authorization": f"Bearer {provider.api_key}"}
            if out_body is not None:
                headers["Content-Type"] = "application/json"

            started = time.monotonic()
            try:
                response = self.transport(
                    method,
                    url,
                    json=out_body,
                    headers=headers,
                    timeout=self.config.request_timeout,
                    stream=stream,
                )
            except requests.RequestException as exc:
                attempts.append({"provider": hop.provider, "status": 0, "error": f"{type(exc).__name__}: {exc}"[:200]})
                last_error = f"provider '{hop.provider}' unreachable: {type(exc).__name__}"
                last_status = 503
                self._sleep_backoff(index)
                continue

            latency_ms = int((time.monotonic() - started) * 1000)
            status = int(getattr(response, "status_code", 0) or 0)
            attempts.append({"provider": hop.provider, "status": status, "latency_ms": latency_ms})

            if status >= 200 and status < 300:
                return ForwardResult(
                    ok=True,
                    status=status,
                    provider=hop.provider,
                    model_used=hop.model,
                    latency_ms=latency_ms,
                    retries=index,
                    response=response,
                    attempts=attempts,
                )
            if status in RETRYABLE_STATUS:
                last_status = status
                last_error = f"provider '{hop.provider}' returned {status}"
                self._sleep_backoff(index, response)
                continue
            # Non-retryable client error: forward as-is.
            return ForwardResult(
                ok=False,
                status=status,
                provider=hop.provider,
                model_used=hop.model,
                latency_ms=latency_ms,
                retries=index,
                error=f"upstream status {status}",
                response=response,
                attempts=attempts,
            )

        return ForwardResult(
            ok=False,
            status=last_status,
            model_used=str(requested_model) if requested_model else None,
            retries=max(0, len(attempts) - 1),
            error=last_error,
            attempts=attempts,
        )

    # -------------------------------------------------------------- helpers
    def _sleep_backoff(self, attempt_index: int, response: Optional[Any] = None) -> None:
        retry_after = None
        if response is not None:
            raw = response.headers.get("Retry-After") if getattr(response, "headers", None) else None
            if raw is None and isinstance(getattr(response, "headers", None), dict):
                raw = response.headers.get("Retry-After")
            if raw:
                try:
                    retry_after = float(raw)
                except (TypeError, ValueError):
                    retry_after = None
        if retry_after is not None:
            delay = min(retry_after, 30.0)
        else:
            delay = self.config.backoff_base * (2 ** attempt_index)
        delay = min(delay + random.uniform(0, 0.1), 30.0)
        time.sleep(delay)


def parse_model_from_body(raw_body: bytes) -> Optional[str]:
    try:
        data = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(data, dict):
        model = data.get("model")
        return str(model) if model is not None else None
    return None
