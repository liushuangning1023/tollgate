"""Pricing: USD per 1M tokens, unified across providers.

The bundled ``pricing.json`` is a *reference* convenience table —
providers change prices frequently. Overrides in the config file win;
the config always wins over the bundle. Unknown models are metered
but costed at 0 with an ``unknown pricing`` flag so nothing is hidden.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

BUNDLED_PATH = Path(__file__).with_name("pricing.json")


class Pricing:
    def __init__(self, table: Optional[Dict[str, Dict[str, float]]] = None,
                 overrides: Optional[Dict[str, Any]] = None) -> None:
        self._table: Dict[str, Dict[str, float]] = dict(table or {})
        self._overrides: Dict[str, Dict[str, float]] = {}
        for model, prices in (overrides or {}).items():
            if isinstance(prices, dict):
                self._overrides[model] = {
                    "input": float(prices.get("input", 0.0)),
                    "output": float(prices.get("output", 0.0)),
                }

    # ------------------------------------------------------------------ api
    @classmethod
    def bundled(cls, overrides: Optional[Dict[str, Any]] = None) -> "Pricing":
        table: Dict[str, Dict[str, float]] = {}
        try:
            raw = json.loads(BUNDLED_PATH.read_text(encoding="utf-8"))
            for model, prices in raw.items():
                if model.startswith("_") or not isinstance(prices, dict):
                    continue
                table[model] = {
                    "input": float(prices.get("input", 0.0)),
                    "output": float(prices.get("output", 0.0)),
                }
        except (OSError, json.JSONDecodeError):
            table = {}
        return cls(table=table, overrides=overrides)

    def rates(self, model: str) -> Optional[Dict[str, float]]:
        """Exact rates for *model* from overrides or the bundled table."""
        if model in self._overrides:
            return self._overrides[model]
        return self._table.get(model)

    def cost_usd(self, model: str, prompt_tokens: int, completion_tokens: int) -> Tuple[float, bool]:
        """Return (cost_usd, known). known=False means no pricing entry."""
        rates = self.rates(model)
        if rates is None:
            return 0.0, False
        cost = (
            prompt_tokens * rates.get("input", 0.0) / 1_000_000
            + completion_tokens * rates.get("output", 0.0) / 1_000_000
        )
        return round(cost, 8), True

    def describe(self, model: str) -> str:
        rates = self.rates(model)
        if rates is None:
            return f"{model}: no pricing entry (costed $0, flagged)"
        source = "override" if model in self._overrides else "bundled"
        return (f"{model}: ${rates['input']}/1M in, ${rates['output']}/1M out ({source})")

    def all_models(self) -> Dict[str, Dict[str, float]]:
        merged = dict(self._table)
        merged.update(self._overrides)
        return merged
