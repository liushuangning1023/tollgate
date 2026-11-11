"""SQLite call store: every proxied request lands here.

One row per completed upstream attempt-sequence (the client-visible
call), including retries count and final status.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    tag TEXT NOT NULL DEFAULT 'default',
    provider TEXT,
    model_requested TEXT,
    model_used TEXT,
    path TEXT,
    status INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    cost_known INTEGER NOT NULL DEFAULT 0,
    retries INTEGER NOT NULL DEFAULT 0,
    streamed INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls (ts);
CREATE INDEX IF NOT EXISTS idx_calls_tag ON calls (tag);
CREATE INDEX IF NOT EXISTS idx_calls_model ON calls (model_used);
"""

COLUMNS = (
    "id, ts, tag, provider, model_requested, model_used, path, status, ok, "
    "latency_ms, prompt_tokens, completion_tokens, cost_usd, cost_known, "
    "retries, streamed, error"
)


class CallStore:
    """Thread-safe SQLite wrapper (a single lock guards one connection)."""

    def __init__(self, db_path: str) -> None:
        self.path = Path(db_path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    # ---------------------------------------------------------------- write
    def record(self, **fields: Any) -> int:
        row = {
            "ts": fields.get("ts", time.time()),
            "tag": fields.get("tag", "default"),
            "provider": fields.get("provider"),
            "model_requested": fields.get("model_requested"),
            "model_used": fields.get("model_used"),
            "path": fields.get("path", "/v1/chat/completions"),
            "status": int(fields.get("status", 0) or 0),
            "ok": 1 if fields.get("ok") else 0,
            "latency_ms": int(fields.get("latency_ms", 0) or 0),
            "prompt_tokens": int(fields.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(fields.get("completion_tokens", 0) or 0),
            "cost_usd": float(fields.get("cost_usd", 0.0) or 0.0),
            "cost_known": 1 if fields.get("cost_known") else 0,
            "retries": int(fields.get("retries", 0) or 0),
            "streamed": 1 if fields.get("streamed") else 0,
            "error": fields.get("error"),
        }
        columns = ", ".join(row.keys())
        placeholders = ", ".join("?" for _ in row)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                f"INSERT INTO calls ({columns}) VALUES ({placeholders})", tuple(row.values())
            )
            return int(cursor.lastrowid)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ----------------------------------------------------------------- read
    def _rows(self, where: str = "", params: Tuple = ()) -> List[Dict[str, Any]]:
        query = f"SELECT {COLUMNS} FROM calls {where} ORDER BY ts"
        with self._lock:
            return [dict(r) for r in self._conn.execute(query, params).fetchall()]

    def since(self, since_ts: float) -> List[Dict[str, Any]]:
        return self._rows("WHERE ts >= ?", (since_ts,))

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {COLUMNS} FROM calls ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def after_id(self, last_id: int, limit: int = 100) -> List[Dict[str, Any]]:
        query = f"SELECT {COLUMNS} FROM calls WHERE id > ? ORDER BY id LIMIT ?"
        with self._lock:
            rows = self._conn.execute(query, (int(last_id), int(limit))).fetchall()
        return [dict(r) for r in rows]

    def max_id(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COALESCE(MAX(id), 0) FROM calls").fetchone()
        return int(row[0])

    def summarize(self, since_ts: float, group_by: str) -> List[Dict[str, Any]]:
        """Aggregate cost/latency since *since_ts* grouped by a column."""
        allowed = {"tag", "model_used", "provider", "status"}
        if group_by not in allowed:
            raise ValueError(f"group_by must be one of {sorted(allowed)}")
        query = f"""
            SELECT {group_by} AS bucket,
                   COUNT(*) AS calls,
                   SUM(ok) AS ok_calls,
                   SUM(retries) AS retries,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(cost_usd) AS cost_usd,
                   AVG(latency_ms) AS avg_latency_ms
            FROM calls
            WHERE ts >= ?
            GROUP BY {group_by}
            ORDER BY cost_usd DESC
        """
        with self._lock:
            return [dict(r) for r in self._conn.execute(query, (since_ts,)).fetchall()]

    def spend_since(self, since_ts: float, tag: Optional[str] = None) -> float:
        query = "SELECT COALESCE(SUM(cost_usd), 0) FROM calls WHERE ts >= ?"
        params: Tuple = (since_ts,)
        if tag is not None:
            query += " AND tag = ?"
            params = (since_ts, tag)
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        return float(row[0])
