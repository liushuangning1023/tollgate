"""TollGate: local AI-call butler.

A zero-deployment OpenAI-compatible proxy that meters every call,
unifies cost accounting across providers, retries with backoff and
failover, and enforces per-tag budgets.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
