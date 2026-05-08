"""DuckDB-backed local cache for time-series market data.

Why DuckDB and not just JSON files
----------------------------------
The existing JSON cache (in `_common.cache_get/cache_set`) is fine for
*single-record* payloads — quotes, fundamentals snapshots, a stock-info
dict, a list of HS300 members. But for OHLCV bars, JSON breaks down
fast:

  - Each call writes a fresh file even if 99% of the rows are unchanged
  - Re-reading a year of bars costs us a JSON.parse of ~250 records
  - There's no way to ask "give me 2024-01 through 2025-06 only" without
    reading and filtering the full file
  - Backtests over 100 symbols hit disk 100 times instead of one query

DuckDB fixes all of this with a single embedded file (no server, no
daemon) plus SQL. We use it strictly for time series here:

  - OHLCV per (symbol, date, period, adjust)
  - Index OHLCV per (index_code, date)
  - A small _cache_meta table tracking last_updated per logical key

Single-record dicts (quotes, fundamentals, stock_info) keep using the
existing JSON cache — DuckDB is overkill for those and JSON is simpler
to debug.

Optional dependency
-------------------
DuckDB is recommended but not required. If the `duckdb` package isn't
installed, every public function in this module no-ops gracefully:
`get_*` returns None, `put_*` does nothing. Callers get the same
behavior as if DuckDB wasn't wired in at all — they fall through to
the live backend and the JSON cache, just slower.
"""
from __future__ import annotations
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from ._common import log, normalize_symbol

# ---------------------------------------------------------------------------
# Optional duckdb import
# ---------------------------------------------------------------------------
try:
    import duckdb  # type: ignore
    _DUCKDB_AVAILABLE = True
except ImportError:
    duckdb = None  # type: ignore
    _DUCKDB_AVAILABLE = False
    log.debug("duckdb not installed; OHLCV time-series cache disabled. "
              "pip install duckdb to enable.")


# Project layout: this file lives in scripts/, the data dir is a sibling.
_DEFAULT_DB_PATH = (Path(__file__).resolve().parent.parent
                    / "data" / "cache" / "market.duckdb")


def _db_path() -> Path:
    """Resolve the DuckDB file path. Override via GP_SKILL_DUCKDB env var."""
    override = os.environ.get("GP_SKILL_DUCKDB")
    if override:
        return Path(override)
    return _DEFAULT_DB_PATH


# ---------------------------------------------------------------------------
# Connection + schema management
# ---------------------------------------------------------------------------
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ohlcv (
    symbol     VARCHAR  NOT NULL,
    date       DATE     NOT NULL,
    period     VARCHAR  NOT NULL DEFAULT 'daily',
    adjust     VARCHAR  NOT NULL DEFAULT 'qfq',
    open       DOUBLE,
    high       DOUBLE,
    low        DOUBLE,
    close      DOUBLE,
    volume     DOUBLE,
    amount     DOUBLE,
    PRIMARY KEY (symbol, date, period, adjust)
);

CREATE TABLE IF NOT EXISTS index_ohlcv (
    index_code VARCHAR NOT NULL,
    date       DATE    NOT NULL,
    open       DOUBLE,
    high       DOUBLE,
    low        DOUBLE,
    close      DOUBLE,
    volume     DOUBLE,
    amount     DOUBLE,
    PRIMARY KEY (index_code, date)
);

CREATE TABLE IF NOT EXISTS _cache_meta (
    table_name   VARCHAR NOT NULL,
    cache_key    VARCHAR NOT NULL,
    last_updated TIMESTAMP NOT NULL,
    row_count    INTEGER,
    PRIMARY KEY (table_name, cache_key)
);
"""


_conn: Any = None


def _get_conn():
    """Return a process-wide DuckDB connection, lazily creating the file
    and schema on first use.
    """
    global _conn
    if not _DUCKDB_AVAILABLE:
        return None
    if _conn is not None:
        return _conn
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _conn = duckdb.connect(str(path))
        _conn.execute(_SCHEMA_SQL)
    except Exception as e:
        log.warning("DuckDB connect failed (%s); cache disabled this run", e)
        _conn = None
    return _conn


def close() -> None:
    """Close the DuckDB connection. Safe to call even if never opened.
    Useful for the sync script which may want to release the file lock
    before another process reads.
    """
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:   # pragma: no cover
            pass
    _conn = None


def is_available() -> bool:
    """True if duckdb is installed and a connection can be opened."""
    return _get_conn() is not None


# ---------------------------------------------------------------------------
# OHLCV
# ---------------------------------------------------------------------------
def get_ohlcv(symbol: str, *, period: str = "daily",
              start: str | None = None, end: str | None = None,
              adjust: str = "qfq") -> pd.DataFrame | None:
    """Read OHLCV bars from cache. Returns None on miss or unavailable.

    Returns an empty DataFrame *only if* there are zero rows for this
    (symbol, period, adjust) — which signals "we tried this query
    before and the source was empty"; callers should treat that as
    a soft miss and fall through to live backends. Strictly None
    means "no rows in cache at all".
    """
    conn = _get_conn()
    if conn is None:
        return None
    sym = normalize_symbol(symbol)

    where = ["symbol = ?", "period = ?", "adjust = ?"]
    params: list[Any] = [sym, period, adjust]
    if start:
        where.append("date >= ?")
        params.append(_to_date(start))
    if end:
        where.append("date <= ?")
        params.append(_to_date(end))

    sql = (f"SELECT date, open, high, low, close, volume, amount "
           f"FROM ohlcv WHERE {' AND '.join(where)} ORDER BY date")
    try:
        df = conn.execute(sql, params).fetch_df()
    except Exception as e:
        log.debug("DuckDB read failed for %s: %s", sym, e)
        return None
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"])
    return df


def put_ohlcv(symbol: str, df: pd.DataFrame, *,
              period: str = "daily", adjust: str = "qfq") -> int:
    """Upsert OHLCV bars. Returns the number of rows written.

    Uses INSERT OR REPLACE so that re-fetching today's partially-formed
    bar (after market close, after adjust factor change, etc.) updates
    the cached row instead of leaving stale data.
    """
    if df is None or df.empty:
        return 0
    conn = _get_conn()
    if conn is None:
        return 0
    sym = normalize_symbol(symbol)

    needed = ["date", "open", "high", "low", "close", "volume", "amount"]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        # Tolerate missing 'amount' specifically — sina/mootdx don't always
        # have it. Fill with NaN and proceed; a missing 'date' is fatal.
        if "date" in missing:
            log.warning("put_ohlcv skipped for %s: missing 'date' column", sym)
            return 0
        for c in missing:
            df = df.copy()
            df[c] = None

    rows = df[needed].copy()
    rows.insert(0, "adjust", adjust)
    rows.insert(0, "period", period)
    rows.insert(0, "symbol", sym)
    rows["date"] = pd.to_datetime(rows["date"]).dt.date

    try:
        conn.register("incoming_ohlcv", rows)
        conn.execute("""
            INSERT OR REPLACE INTO ohlcv
              (symbol, period, adjust, date, open, high, low, close, volume, amount)
            SELECT symbol, period, adjust, date, open, high, low, close, volume, amount
            FROM incoming_ohlcv
        """)
        conn.unregister("incoming_ohlcv")
        _touch_meta(conn, "ohlcv", f"{sym}|{period}|{adjust}", len(rows))
    except Exception as e:
        log.warning("DuckDB write failed for %s: %s", sym, e)
        return 0
    return len(rows)


def latest_cached_date(symbol: str, *, period: str = "daily",
                       adjust: str = "qfq") -> str | None:
    """Return the most recent cached date for this symbol as YYYYMMDD,
    or None if nothing cached. Used by the sync script to do incremental
    pulls — only re-fetch what's new.
    """
    conn = _get_conn()
    if conn is None:
        return None
    sym = normalize_symbol(symbol)
    try:
        row = conn.execute(
            "SELECT MAX(date) FROM ohlcv "
            "WHERE symbol = ? AND period = ? AND adjust = ?",
            [sym, period, adjust],
        ).fetchone()
    except Exception:
        return None
    if not row or row[0] is None:
        return None
    return row[0].strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# Index OHLCV
# ---------------------------------------------------------------------------
def get_index_ohlcv(index_code: str, *, start: str | None = None,
                    end: str | None = None) -> pd.DataFrame | None:
    conn = _get_conn()
    if conn is None:
        return None
    where = ["index_code = ?"]
    params: list[Any] = [index_code]
    if start:
        where.append("date >= ?")
        params.append(_to_date(start))
    if end:
        where.append("date <= ?")
        params.append(_to_date(end))
    try:
        df = conn.execute(
            f"SELECT date, open, high, low, close, volume, amount "
            f"FROM index_ohlcv WHERE {' AND '.join(where)} ORDER BY date",
            params,
        ).fetch_df()
    except Exception:
        return None
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"])
    return df


def put_index_ohlcv(index_code: str, df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    conn = _get_conn()
    if conn is None:
        return 0
    needed = ["date", "open", "high", "low", "close", "volume", "amount"]
    rows = df.copy()
    for c in needed:
        if c not in rows.columns:
            rows[c] = None
    rows = rows[needed]
    rows.insert(0, "index_code", index_code)
    rows["date"] = pd.to_datetime(rows["date"]).dt.date
    try:
        conn.register("incoming_idx", rows)
        conn.execute("""
            INSERT OR REPLACE INTO index_ohlcv
              (index_code, date, open, high, low, close, volume, amount)
            SELECT index_code, date, open, high, low, close, volume, amount
            FROM incoming_idx
        """)
        conn.unregister("incoming_idx")
        _touch_meta(conn, "index_ohlcv", index_code, len(rows))
    except Exception as e:
        log.warning("DuckDB index write failed for %s: %s", index_code, e)
        return 0
    return len(rows)


# ---------------------------------------------------------------------------
# Maintenance / inspection
# ---------------------------------------------------------------------------
def stats() -> dict[str, Any]:
    """Return a small summary for sanity checks: row counts, latest dates,
    file size. Used by sync_data.py and run_validation.ps1.
    """
    conn = _get_conn()
    if conn is None:
        return {"available": False}
    try:
        n_ohlcv = conn.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0]
        n_idx = conn.execute("SELECT COUNT(*) FROM index_ohlcv").fetchone()[0]
        n_sym = conn.execute(
            "SELECT COUNT(DISTINCT symbol) FROM ohlcv"
        ).fetchone()[0]
        latest = conn.execute("SELECT MAX(date) FROM ohlcv").fetchone()[0]
    except Exception:
        return {"available": True, "error": "stats query failed"}
    size_bytes = 0
    try:
        size_bytes = _db_path().stat().st_size
    except Exception:
        pass
    return {
        "available": True,
        "path": str(_db_path()),
        "size_mb": round(size_bytes / (1024 * 1024), 2),
        "ohlcv_rows": n_ohlcv,
        "index_ohlcv_rows": n_idx,
        "distinct_symbols": n_sym,
        "latest_ohlcv_date": (latest.strftime("%Y-%m-%d")
                              if latest is not None else None),
    }


def vacuum() -> None:
    """Reclaim space after large deletes/updates. Cheap; run after sync."""
    conn = _get_conn()
    if conn is None:
        return
    try:
        conn.execute("CHECKPOINT")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _to_date(s: str):
    """Accept YYYY-MM-DD or YYYYMMDD; return a python date."""
    s = str(s).replace("-", "")
    return datetime.strptime(s, "%Y%m%d").date()


def _touch_meta(conn, table: str, key: str, row_count: int) -> None:
    try:
        conn.execute("""
            INSERT OR REPLACE INTO _cache_meta
              (table_name, cache_key, last_updated, row_count)
            VALUES (?, ?, ?, ?)
        """, [table, key, datetime.utcnow(), row_count])
    except Exception:    # pragma: no cover — meta failure is non-fatal
        pass


if __name__ == "__main__":
    # Smoke test: python -m scripts.cache_db
    import json
    print(json.dumps(stats(), default=str, ensure_ascii=False, indent=2))
