"""
db.py
-----
SQLite persistence (standard library only, no server to run).

Two tables:
  analyses   every completed analysis, so administrators can revisit and
             compare candidate sites for a village
  api_cache  responses from rainfall APIs, so repeated analyses of the same
             village do not re-download 20 years of daily data

The database file lives at $DATABASE_PATH (default: data/pond_planner.db).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path(os.environ.get(
    "DATABASE_PATH",
    Path(__file__).resolve().parent.parent / "data" / "pond_planner.db",
))
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS analyses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT    NOT NULL,
    village       TEXT,
    mode          TEXT    NOT NULL,
    lat           REAL    NOT NULL,
    lon           REAL    NOT NULL,
    summary_json  TEXT    NOT NULL,
    result_json   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_analyses_created ON analyses(created_at DESC);
CREATE TABLE IF NOT EXISTS api_cache (
    key         TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    payload     TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _lock, _connect() as conn:
        conn.executescript(SCHEMA)


def save_analysis(*, village: str | None, mode: str, lat: float, lon: float,
                  summary: dict, result: dict) -> int:
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO analyses (created_at, village, mode, lat, lon, summary_json, result_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), village, mode, lat, lon,
             json.dumps(summary), json.dumps(result)),
        )
        return int(cur.lastrowid)


def list_analyses(limit: int = 50) -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT id, created_at, village, mode, lat, lon, summary_json FROM analyses "
            "ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
    return [
        {k: r[k] for k in ("id", "created_at", "village", "mode", "lat", "lon")}
        | {"summary": json.loads(r["summary_json"])}
        for r in rows
    ]


def get_analysis(analysis_id: int) -> dict | None:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT result_json FROM analyses WHERE id = ?", (analysis_id,)).fetchone()
    return json.loads(row["result_json"]) if row else None


def delete_analysis(analysis_id: int) -> bool:
    with _lock, _connect() as conn:
        cur = conn.execute("DELETE FROM analyses WHERE id = ?", (analysis_id,))
        return cur.rowcount > 0


def cache_get(key: str, max_age_s: float) -> dict | None:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT created_at, payload FROM api_cache WHERE key = ?", (key,)).fetchone()
    if row and time.time() - row["created_at"] <= max_age_s:
        return json.loads(row["payload"])
    return None


def cache_put(key: str, payload: dict) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO api_cache (key, created_at, payload) VALUES (?, ?, ?)",
            (key, time.time(), json.dumps(payload)),
        )
