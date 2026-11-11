"""Budgets: per-tag and global daily caps in USD.

Modes:
- ``alert``  — log a warning when exceeded, let calls through.
- ``cutoff`` — refuse calls with HTTP 429 + a clear JSON error.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .config import Config
from .store import CallStore


@dataclass
class BudgetState:
    allowed: bool
    reason: str = ""
    spent: float = 0.0
    cap: Optional[float] = None
    scope: str = ""


def _day_start(ts: Optional[float] = None) -> float:
    now = datetime.fromtimestamp(ts if ts is not None else time.time())
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp()


def check_budget(config: Config, store: CallStore, tag: str, now: Optional[float] = None) -> BudgetState:
    """Evaluate tag-level then global budgets. The strictest verdict wins."""
    day_start = _day_start(now)
    now = now if now is not None else time.time()

    tag_budget = config.tag_budget(tag)
    if tag_budget and tag_budget.get("daily_usd") is not None and tag_budget.get("mode") == "cutoff":
        cap = float(tag_budget["daily_usd"])
        spent = store.spend_since(day_start, tag=tag)
        if spent >= cap:
            return BudgetState(
                allowed=False,
                reason=f"daily budget exceeded for tag '{tag}': ${spent:.4f} / ${cap:.4f}",
                spent=spent,
                cap=cap,
                scope=f"tag:{tag}",
            )

    global_budget = config.global_budget
    if global_budget and global_budget.get("daily_usd") is not None and global_budget.get("mode") == "cutoff":
        cap = float(global_budget["daily_usd"])
        spent = store.spend_since(day_start)
        if spent >= cap:
            return BudgetState(
                allowed=False,
                reason=f"global daily budget exceeded: ${spent:.4f} / ${cap:.4f}",
                spent=spent,
                cap=cap,
                scope="global",
            )

    return BudgetState(allowed=True)


def budget_exceeded_after(config: Config, store: CallStore, tag: str, now: Optional[float] = None) -> Optional[str]:
    """Return a warning message if an 'alert'-mode budget is now exceeded."""
    day_start = _day_start(now)
    messages = []

    tag_budget = config.tag_budget(tag)
    if tag_budget and tag_budget.get("daily_usd") is not None and tag_budget.get("mode") == "alert":
        cap = float(tag_budget["daily_usd"])
        spent = store.spend_since(day_start, tag=tag)
        if spent >= cap:
            messages.append(f"[tollgate] BUDGET ALERT tag '{tag}': ${spent:.4f} >= ${cap:.4f} today")

    global_budget = config.global_budget
    if global_budget and global_budget.get("daily_usd") is not None and global_budget.get("mode") == "alert":
        cap = float(global_budget["daily_usd"])
        spent = store.spend_since(day_start)
        if spent >= cap:
            messages.append(f"[tollgate] BUDGET ALERT global: ${spent:.4f} >= ${cap:.4f} today")

    return "\n".join(messages) or None


def summarize_budgets(config: Config, store: CallStore, now: Optional[float] = None) -> list:
    day_start = _day_start(now)
    rows = []

    global_budget = config.global_budget
    if global_budget and global_budget.get("daily_usd") is not None:
        cap = float(global_budget["daily_usd"])
        spent = store.spend_since(day_start)
        rows.append(("global", "-", cap, spent, str(global_budget.get("mode", "alert"))))

    for tag, budget in (config.budgets.get("tags") or {}).items():
        if budget.get("daily_usd") is None:
            continue
        cap = float(budget["daily_usd"])
        spent = store.spend_since(day_start, tag=tag)
        rows.append((f"tag:{tag}", tag, cap, spent, str(budget.get("mode", "alert"))))

    return rows


def window_start(days: int, now: Optional[float] = None) -> float:
    now = now if now is not None else time.time()
    return now - timedelta(days=days).total_seconds()
