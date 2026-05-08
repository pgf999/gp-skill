"""Data layer.

Backend chain (each backend is optional; missing ones are silently skipped):
  1. tushare  — preferred, when TUSHARE_TOKEN env var is set + pkg installed.
                Best uptime + the only source with rich fundamentals
                (ROE / margins / YoY growth / PE / PB) populated reliably.
  2. mootdx   — TDX (通达信) protocol. Daily OHLCV only, no fundamentals.
                Independent infra path — survives most akshare/tushare
                outages because TDX servers are decoupled from web sources.
  3. akshare  — public eastmoney + sina endpoints. Broad coverage but
                flaky under load. The original (and still mandatory if no
                other backend is installed) safety net.

The DuckDB cache layer (scripts/cache_db.py) sits in front of the chain
for OHLCV — read-through, write-through, and the source of truth for
the nightly batch sync (scripts/sync_data.py).

Public functions (keep these stable; callers depend on them):
  fetch_ohlcv(symbol, period, start, end, adjust) -> DataFrame
  fetch_realtime_quote(symbol) -> dict
  fetch_fundamentals(symbol) -> dict
  fetch_stock_info(symbol) -> dict
  fetch_hs300_members() -> list[str]
  is_trading_day(date_str) -> bool
  fetch_index_ohlcv(index_code) -> DataFrame
"""
from __future__ import annotations
import os
import time
from typing import Any

import pandas as pd

from ._common import (
    cache_get, cache_set, log, normalize_symbol, today_str, days_ago_str,
)

# Throttle: AkShare hits public endpoints and can be rate-limited.
_MIN_CALL_INTERVAL = 0.3
_last_call_ts = 0.0


def _throttle():
    global _last_call_ts
    wait = _MIN_CALL_INTERVAL - (time.time() - _last_call_ts)
    if wait > 0:
        time.sleep(wait)
    _last_call_ts = time.time()


# Memoized backend clients. _MISSING is a sentinel: distinguishes
# "haven't tried yet" (None default) from "tried and not available".
_MISSING = object()
_tushare_client: Any = _MISSING
_mootdx_client: Any = _MISSING


def _reset_backend_cache() -> None:
    """Reset memoized backend clients. Used by tests; callers don't
    normally need this. Useful when TUSHARE_TOKEN is set/changed at
    runtime.
    """
    global _tushare_client, _mootdx_client
    _tushare_client = _MISSING
    _mootdx_client = _MISSING


# AkShare fronts sina/eastmoney/xueqiu and those endpoints drop connections
# under load. A single failure shouldn't kill the whole analysis — retry
# transient errors with exponential backoff before giving up.
_TRANSIENT_MARKERS = (
    "remotedisconnected", "remote end closed connection",
    "connection aborted", "connection reset", "connection refused",
    "timeout", "timed out", "read timed out",
    "max retries exceeded", "temporary failure",
    "bad gateway", "service unavailable",
)


def _retry_akshare(fn, *args, retries: int = 3, base_delay: float = 1.0,
                   **kwargs):
    """Throttle + retry an AkShare call on transient network errors.

    We classify errors by message fragment rather than exception type so
    we don't need to pin urllib3/requests versions. Non-transient errors
    (bad symbol, AkShare schema change) re-raise immediately — no point
    in retrying those.
    """
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        _throttle()
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            msg = str(e).lower()
            transient = any(m in msg for m in _TRANSIENT_MARKERS)
            if not transient or attempt == retries:
                raise
            last_exc = e
            delay = base_delay * (2 ** (attempt - 1))
            log.warning("AkShare transient error (attempt %d/%d, retry in "
                        "%.1fs): %s", attempt, retries, delay, e)
            time.sleep(delay)
    # Unreachable — loop either returns or raises — but keeps type checkers happy
    raise last_exc  # pragma: no cover


def _try_import_akshare():
    try:
        import akshare as ak  # type: ignore
        return ak
    except ImportError:
        log.error("AkShare not installed. Run: pip install akshare --upgrade")
        return None


def _try_import_tushare():
    """Return a tushare pro_api client if TUSHARE_TOKEN is set and the
    package is installed; otherwise None.

    Tushare Pro is the recommended primary backend — it has better
    uptime than the public eastmoney/sina endpoints AkShare wraps, and
    its `fina_indicator` / `daily_basic` give us the rich fundamentals
    (ROE, margins, YoY growth, PE/PB) that AkShare's
    `stock_financial_abstract` schema returns sparsely.

    Third-party tushare-compatible relays
    -------------------------------------
    Setting `TUSHARE_HTTP_URL` (e.g. `http://jiaoch.site`) routes API
    calls to a community/third-party relay that mirrors tushare's
    schema. These relays require the token to be re-bound to the
    private `_DataApi__token` attribute *after* `pro_api(token)`
    constructs the client — `pro_api` only stores the token in its
    own state, but the underlying request path the relay intercepts
    reads from the private attribute. The same goes for the URL
    override. We do the patch only when the user opts in via env var,
    so default behavior (talking to official api.tushare.pro) is
    untouched.
    """
    global _tushare_client
    if _tushare_client is not _MISSING:
        return _tushare_client
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        _tushare_client = None
        return None
    try:
        import tushare as ts  # type: ignore
        ts.set_token(token)
        pro = ts.pro_api()
        # Optional third-party relay support (see docstring).
        http_url = os.environ.get("TUSHARE_HTTP_URL")
        if http_url:
            try:
                pro._DataApi__token = token         # noqa: SLF001
                pro._DataApi__http_url = http_url   # noqa: SLF001
                log.info("tushare client patched to use relay: %s", http_url)
            except AttributeError as e:
                log.warning("tushare relay patch failed (private attr "
                            "renamed?): %s — falling back to official URL", e)
        _tushare_client = pro
    except ImportError:
        log.debug("tushare not installed; skipping tushare backend")
        _tushare_client = None
    except Exception as e:
        log.warning("tushare init failed: %s", e)
        _tushare_client = None
    return _tushare_client


def _try_import_mootdx():
    """Return a mootdx Quotes client if installed and reachable; else None.

    mootdx talks the TDX (通达信) protocol to either a local TDX client
    or one of the public TDX servers. It's our offline-friendly backend:
    when network/eastmoney/tushare are all flaky, mootdx usually still
    works because its servers are independent of those upstream paths.

    OHLCV data only — mootdx doesn't have fundamentals.
    """
    global _mootdx_client
    if _mootdx_client is not _MISSING:
        return _mootdx_client
    try:
        from mootdx.quotes import Quotes  # type: ignore
        _mootdx_client = Quotes.factory(market="std")
    except ImportError:
        log.debug("mootdx not installed; skipping mootdx backend")
        _mootdx_client = None
    except Exception as e:
        log.warning("mootdx init failed: %s", e)
        _mootdx_client = None
    return _mootdx_client


def _to_ts_code(symbol: str) -> str:
    """6-digit code -> tushare ts_code format ('600519' -> '600519.SH').

    Tushare requires a suffix indicating the exchange. Rules mirror
    `_prefix_sh_sz_bj` above so the mapping is consistent.
    """
    s = normalize_symbol(symbol)
    if s.startswith(("60", "68", "51", "56", "58", "11", "13")):
        return f"{s}.SH"
    if s.startswith(("43", "83", "87", "88", "92")):
        return f"{s}.BJ"
    return f"{s}.SZ"


# ---------------------------------------------------------------------------
# OHLCV
# ---------------------------------------------------------------------------
def _prefix_sh_sz_bj(symbol: str) -> str:
    """Prepend 'sh' / 'sz' / 'bj' for sina/tencent style endpoints.

    Rules distilled from CSRC board prefixes:
      60/68/51/56/58/11/13  -> SSE (sh)
      00/30/15/16/17/18     -> SZSE (sz)
      43/83/87/88/92        -> BSE (bj)
    Anything else falls back to 'sz' — very rare and caller will see an
    error from the backend quickly enough.
    """
    s = normalize_symbol(symbol)
    if s.startswith(("60", "68", "51", "56", "58", "11", "13")):
        return f"sh{s}"
    if s.startswith(("43", "83", "87", "88", "92")):
        return f"bj{s}"
    return f"sz{s}"


_EAST_COL_MAP = {
    "日期": "date", "开盘": "open", "收盘": "close",
    "最高": "high", "最低": "low", "成交量": "volume",
    "成交额": "amount", "振幅": "amplitude",
    "涨跌幅": "pct_chg", "涨跌额": "chg", "换手率": "turnover",
}


def _fetch_ohlcv_east(ak, symbol: str, period: str, start: str, end: str,
                      adjust: str) -> pd.DataFrame:
    """Eastmoney backend — primary. Returns canonical DataFrame."""
    df = _retry_akshare(
        ak.stock_zh_a_hist,
        symbol=symbol, period=period,
        start_date=start, end_date=end, adjust=adjust,
    )
    if df is None or df.empty:
        return pd.DataFrame()
    return df.rename(columns={k: v for k, v in _EAST_COL_MAP.items()
                              if k in df.columns})


def _fetch_ohlcv_sina(ak, symbol: str, start: str, end: str,
                      adjust: str) -> pd.DataFrame:
    """Sina backend — daily only. Independent server, survives eastmoney outages."""
    fn = getattr(ak, "stock_zh_a_daily", None)
    if fn is None:
        # Older AkShare versions may not expose this endpoint.
        return pd.DataFrame()
    prefixed = _prefix_sh_sz_bj(symbol)
    df = _retry_akshare(
        fn,
        symbol=prefixed,
        start_date=start, end_date=end,
        adjust=adjust,
    )
    if df is None or df.empty:
        return pd.DataFrame()
    # Sina columns are already English: date, open, close, high, low, volume,
    # outstanding_share, turnover. 'amount' is absent — approximate it from
    # volume * close so downstream consumers that reference it still work.
    if "amount" not in df.columns and "volume" in df.columns and "close" in df.columns:
        df = df.copy()
        df["amount"] = df["volume"] * df["close"]
    return df


def _fetch_ohlcv_tushare(pro, symbol: str, period: str, start: str, end: str,
                         adjust: str) -> pd.DataFrame:
    """Tushare Pro backend — preferred when TUSHARE_TOKEN is configured.

    Uses the high-level `pro_bar` helper because it transparently
    composes daily bars + adjust factor for qfq/hfq, instead of forcing
    us to compose them ourselves from `pro.daily` + `pro.adj_factor`.
    """
    import tushare as ts  # local import: only run if tushare is in use
    ts_code = _to_ts_code(symbol)
    freq_map = {"daily": "D", "weekly": "W", "monthly": "M"}
    freq = freq_map.get(period, "D")
    adj = adjust if adjust in ("qfq", "hfq") else None

    df = _retry_akshare(
        ts.pro_bar,
        ts_code=ts_code, freq=freq,
        start_date=start, end_date=end,
        adj=adj, api=pro,
    )
    if df is None or df.empty:
        return pd.DataFrame()

    # Tushare columns: ts_code, trade_date, open, high, low, close,
    # pre_close, change, pct_chg, vol, amount.
    df = df.rename(columns={"trade_date": "date", "vol": "volume"})
    keep = ["date", "open", "high", "low", "close", "volume", "amount"]
    return df[[c for c in keep if c in df.columns]].copy()


def _fetch_ohlcv_mootdx(client, symbol: str, start: str, end: str,
                        adjust: str) -> pd.DataFrame:
    """Mootdx (TDX protocol) backend — daily bars only, no adjust support.

    Returns up to ~800 most recent daily bars in one call. We then
    filter to the requested [start, end] window. Adjust is ignored:
    TDX ships raw prices. If the caller asked for qfq/hfq we let them
    fall through to a backend that supports it (tushare/akshare).
    """
    if adjust in ("qfq", "hfq"):
        # mootdx can't deliver adjusted prices — let the chain try the
        # next backend rather than handing back uncomparable raw prices.
        return pd.DataFrame()

    s = normalize_symbol(symbol)
    market = 1 if s.startswith(("60", "68", "51", "56", "58", "11", "13")) else 0

    df = _retry_akshare(
        client.bars,
        symbol=s, frequency=9, market=market, offset=800,
    )
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.rename(columns={"datetime": "date", "vol": "volume"})
    if "date" not in df.columns:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    if start:
        df = df[df["date"] >= pd.to_datetime(start)]
    if end:
        df = df[df["date"] <= pd.to_datetime(end)]
    keep = ["date", "open", "high", "low", "close", "volume", "amount"]
    return df[[c for c in keep if c in df.columns]].copy()


def _ohlcv_backend_chain(period: str) -> list[tuple[str, Any]]:
    """Build an ordered list of (name, callable) backends for fetch_ohlcv.

    Order: tushare → mootdx → akshare-east → akshare-sina.
    mootdx is only initialised when tushare is unavailable — its server
    scan takes ~18 s and is pointless when tushare already works.
    """
    backends: list[tuple[str, Any]] = []

    pro = _try_import_tushare()
    if pro is not None:
        backends.append((
            "tushare",
            lambda s, p, st, e, a: _fetch_ohlcv_tushare(pro, s, p, st, e, a),
        ))
    else:
        # mootdx server scan (~18 s) only worth paying when tushare is absent.
        md = _try_import_mootdx()
        if md is not None and period == "daily":
            backends.append((
                "mootdx",
                lambda s, p, st, e, a: _fetch_ohlcv_mootdx(md, s, st, e, a),
            ))

    ak = _try_import_akshare()
    if ak is not None:
        backends.append((
            "akshare-east",
            lambda s, p, st, e, a: _fetch_ohlcv_east(ak, s, p, st, e, a),
        ))
        if period == "daily":
            backends.append((
                "akshare-sina",
                lambda s, p, st, e, a: _fetch_ohlcv_sina(ak, s, st, e, a),
            ))

    return backends


def fetch_ohlcv(
    symbol: str,
    period: str = "daily",
    start: str | None = None,
    end: str | None = None,
    adjust: str = "qfq",
    use_cache: bool = True,
) -> pd.DataFrame:
    """Fetch historical OHLCV bars with automatic backend failover.

    Backend chain (tries in order, stops at first non-empty result):
      1. tushare (when TUSHARE_TOKEN env var is set + tushare installed)
      2. mootdx (when mootdx installed; daily, raw prices only)
      3. akshare-east (eastmoney via akshare — broad coverage, flaky)
      4. akshare-sina (sina via akshare; daily only — independent server)

    Backends that aren't available are silently skipped, so users with
    only akshare installed get the original behavior unchanged.

    period: 'daily' | 'weekly' | 'monthly'  (weekly/monthly: tushare/east only)
    adjust: 'qfq' (前复权) | 'hfq' (后复权) | '' (不复权)
    """
    symbol = normalize_symbol(symbol)
    start = start or days_ago_str(365)
    end = end or today_str()

    cache_key = f"ohlcv_{symbol}_{period}_{start}_{end}_{adjust}"
    if use_cache:
        cached = cache_get(cache_key, ttl_seconds=3600)
        if cached is not None:
            return pd.DataFrame(cached)

    # DuckDB time-series cache: read first if available. Misses fall through
    # to the live backends; on success we write back so subsequent calls hit
    # local storage. Importing lazily so duckdb stays an optional dep.
    cache_db_mod: Any = None
    try:
        from . import cache_db as cache_db_mod   # noqa: F811
    except ImportError:
        pass
    if cache_db_mod is not None and use_cache:
        try:
            cached_df = cache_db_mod.get_ohlcv(
                symbol, period=period, start=start, end=end, adjust=adjust,
            )
            if cached_df is not None and not cached_df.empty:
                return cached_df
        except Exception as e:
            log.debug("DuckDB read failed for %s: %s; falling through", symbol, e)

    backends = _ohlcv_backend_chain(period)
    if not backends:
        raise RuntimeError(
            "No OHLCV backend available. Install at least one of: "
            "akshare, tushare (+ TUSHARE_TOKEN), mootdx."
        )

    df = pd.DataFrame()
    last_error: Exception | None = None

    for name, fn in backends:
        try:
            df = fn(symbol, period, start, end, adjust)
            if df is not None and not df.empty:
                log.info("OHLCV %s via %s (%d rows)", symbol, name, len(df))
                break
        except Exception as e:
            last_error = e
            log.warning("%s failed for %s: %s", name, symbol, e)
            continue

    if df.empty:
        if last_error is not None:
            raise last_error
        raise ValueError(f"No OHLCV data found for symbol {symbol!r}")

    # Guard: require both 'date' and 'close' columns; treat missing as a
    # bad response (some backends return metadata-only rows for invalid symbols).
    if "date" not in df.columns or "close" not in df.columns:
        log.warning(
            "OHLCV response for %s is missing required columns (got: %s); "
            "treating as empty", symbol, list(df.columns)
        )
        raise ValueError(
            f"OHLCV data for {symbol!r} has unexpected columns — "
            "symbol may not exist or the data source returned junk"
        )

    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    if use_cache:
        cache_set(cache_key, df.to_dict("records"))
        if cache_db_mod is not None:
            try:
                cache_db_mod.put_ohlcv(symbol, df, period=period, adjust=adjust)
            except Exception as e:
                log.debug("DuckDB write-back failed for %s: %s", symbol, e)
    return df


def fetch_index_ohlcv(index_code: str, start: str | None = None,
                      end: str | None = None) -> pd.DataFrame:
    """Index bars. index_code like 'sh000300' (HS300) or 'sh000001' (SSE).

    Same DuckDB-cache + AkShare-fallback shape as fetch_ohlcv, scaled
    down because indices have far fewer query patterns and no adjust.
    """
    # DuckDB read-through
    cache_db_mod: Any = None
    try:
        from . import cache_db as cache_db_mod   # noqa: F811
    except ImportError:
        pass
    if cache_db_mod is not None:
        try:
            cached = cache_db_mod.get_index_ohlcv(index_code, start=start, end=end)
            if cached is not None and not cached.empty:
                return cached
        except Exception as e:
            log.debug("DuckDB index read failed for %s: %s", index_code, e)

    ak = _try_import_akshare()
    if ak is None:
        return pd.DataFrame()
    try:
        df = _retry_akshare(ak.stock_zh_index_daily, symbol=index_code)
    except Exception as e:
        log.error("Index fetch failed %s: %s", index_code, e)
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    if cache_db_mod is not None:
        try:
            cache_db_mod.put_index_ohlcv(index_code, df)
        except Exception as e:
            log.debug("DuckDB index write-back failed for %s: %s", index_code, e)
    return df


# ---------------------------------------------------------------------------
# Realtime quote
# ---------------------------------------------------------------------------
def _fetch_quote_tushare(pro, symbol: str) -> dict[str, Any]:
    """Single-stock quote via tushare daily_basic (~0.7 s).

    One trading day stale for price, but pe/pb/mcap are daily snapshots
    — sufficient for signal generation and fundamentals scoring.
    """
    ts_code = _to_ts_code(symbol)
    try:
        basic = _retry_akshare(pro.daily_basic, ts_code=ts_code, limit=1)
        if basic is None or basic.empty:
            return {}
        row = basic.iloc[0]
        out: dict[str, Any] = {}
        # tushare daily_basic returns market cap in 万元; convert to 元
        # for consistency with AkShare spot (which returns 元).
        _MCAP_FIELDS = {"total_mcap", "float_mcap"}
        for src, dst in [
            ("close",         "price"),
            ("pct_chg",       "pct_chg"),
            ("pe_ttm",        "pe_ttm"),
            ("pb",            "pb"),
            ("ps_ttm",        "ps_ttm"),
            ("dv_ratio",      "dividend_yield"),
            ("total_mv",      "total_mcap"),
            ("circ_mv",       "float_mcap"),
            ("turnover_rate", "turnover"),
        ]:
            v = row.get(src)
            if v is not None and pd.notna(v):
                val = float(v)
                if dst in _MCAP_FIELDS:
                    val *= 10000   # 万元 → 元
                out[dst] = val
        return out
    except Exception as e:
        log.debug("tushare daily_basic quote failed for %s: %s", symbol, e)
        return {}


def fetch_realtime_quote(symbol: str) -> dict[str, Any]:
    """Snapshot quote. Cache 15 seconds.

    Fast path: tushare daily_basic (single stock, ~0.7 s).
    Fallback: akshare spot_em (bulk download of all A-shares, slow/flaky).
    """
    symbol = normalize_symbol(symbol)
    cache_key = f"quote_{symbol}"
    cached = cache_get(cache_key, ttl_seconds=15)
    if cached is not None:
        return cached

    pro = _try_import_tushare()
    if pro is not None:
        result = _fetch_quote_tushare(pro, symbol)
        if result:
            log.info("quote %s via tushare daily_basic", symbol)
            cache_set(cache_key, result)
            return result

    ak = _try_import_akshare()
    if ak is None:
        return {}
    try:
        df = _retry_akshare(ak.stock_zh_a_spot_em)
    except Exception as e:
        log.error("Realtime spot fetch failed: %s", e)
        return {}

    if df is None or df.empty:
        return {}
    df = df.rename(columns={"代码": "symbol", "名称": "name", "最新价": "price",
                            "涨跌幅": "pct_chg", "成交量": "volume",
                            "成交额": "amount", "换手率": "turnover",
                            "市盈率-动态": "pe_ttm", "市净率": "pb",
                            "总市值": "total_mcap", "流通市值": "float_mcap"})
    row = df[df["symbol"] == symbol]
    if row.empty:
        return {}
    result = row.iloc[0].to_dict()
    cache_set(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Fundamentals
# ---------------------------------------------------------------------------
def _fetch_fundamentals_tushare(pro, symbol: str) -> dict[str, Any]:
    """Pull rich fundamentals from tushare's fina_indicator + daily_basic
    + income statements.

    This is the function that turns the analysis card from "we know
    net_margin and debt_ratio" into "we know ROE, PE, PB, PEG, revenue
    YoY, net income YoY, dividend yield, market cap" — matching what
    `scripts/fundamental.py::score_*` actually expect.

    Returns a partial dict; missing fields are normal (e.g. newly listed
    companies have no YoY). Caller should merge with other sources.
    """
    ts_code = _to_ts_code(symbol)
    out: dict[str, Any] = {}

    # 1. Quarterly financial indicators — ROE, margins, debt, EPS, OCF
    try:
        ind = _retry_akshare(pro.fina_indicator, ts_code=ts_code)
        if ind is not None and not ind.empty:
            row = ind.iloc[0]   # most recent reporting period
            out["report_period"] = str(row.get("end_date", ""))
            for src, dst in [
                ("roe", "roe_ttm"),
                ("roe_dt", "roe_diluted"),
                ("grossprofit_margin", "gross_margin"),
                ("netprofit_margin", "net_margin"),
                ("debt_to_assets", "debt_ratio"),
                ("eps", "eps"),
                ("bps", "bps"),
                ("ocf_to_or", "operating_cf_to_revenue"),
                ("ocfps", "ocf_per_share"),
                ("current_ratio", "current_ratio"),
                ("quick_ratio", "quick_ratio"),
                ("inv_turn", "inventory_turnover"),
            ]:
                v = row.get(src)
                if v is not None and pd.notna(v):
                    out[dst] = float(v)
    except Exception as e:
        log.debug("tushare fina_indicator failed for %s: %s", symbol, e)

    # 2. Income statement — for YoY growth (tushare's fina_indicator
    # has *_yoy fields but they're inconsistently populated, so compute
    # from raw income statements which are reliable).
    #
    # The third-party relay ignores `limit` and may return 50+ rows with
    # duplicate end_dates across report_type variants. We must:
    #   a) drop duplicates (keep first = most recently filed variant)
    #   b) match the prior-year period by end_date string arithmetic
    #      instead of using iloc[4], which lands on the wrong quarter
    #      when duplicates shift the index.
    try:
        income = _retry_akshare(pro.income, ts_code=ts_code, limit=9)
        if income is not None and not income.empty:
            income = income.drop_duplicates(subset=["end_date"]).reset_index(drop=True)
            curr = income.iloc[0]
            curr_end = str(curr.get("end_date", ""))  # e.g. "20241231"
            if len(curr_end) == 8:
                # Build the matching period one year prior
                yago_end = str(int(curr_end[:4]) - 1) + curr_end[4:]  # "20231231"
                yago_rows = income[income["end_date"].astype(str) == yago_end]
                if not yago_rows.empty:
                    yago = yago_rows.iloc[0]
                    for src, dst in [("revenue", "revenue_growth_yoy"),
                                     ("n_income", "earnings_growth_yoy")]:
                        c = curr.get(src); y = yago.get(src)
                        if (c is not None and y is not None and pd.notna(c)
                                and pd.notna(y) and float(y) != 0):
                            out[dst] = (float(c) - float(y)) / abs(float(y)) * 100
    except Exception as e:
        log.debug("tushare income/yoy failed for %s: %s", symbol, e)

    # 3. Daily valuation snapshot — PE, PB, market cap (most recent trading day)
    try:
        basic = _retry_akshare(pro.daily_basic, ts_code=ts_code, limit=1)
        if basic is not None and not basic.empty:
            row = basic.iloc[0]
            _MCAP_FIELDS = {"total_mcap", "float_mcap"}
            for src, dst in [
                ("pe", "pe"), ("pe_ttm", "pe_ttm"),
                ("pb", "pb"),
                ("ps", "ps"), ("ps_ttm", "ps_ttm"),
                ("dv_ratio", "dividend_yield"),
                ("dv_ttm", "dividend_yield_ttm"),
                ("total_mv", "total_mcap"),  # tushare: 万元 → 元 below
                ("circ_mv", "float_mcap"),
                ("turnover_rate", "turnover"),
            ]:
                v = row.get(src)
                if v is not None and pd.notna(v):
                    val = float(v)
                    if dst in _MCAP_FIELDS:
                        val *= 10000   # 万元 → 元
                    out[dst] = val
    except Exception as e:
        log.debug("tushare daily_basic failed for %s: %s", symbol, e)

    return out


def fetch_fundamentals(symbol: str) -> dict[str, Any]:
    """Return a dict of fundamental ratios suitable for fundamental.py scoring.

    Fields (best-effort; missing keys default to None in consumers):
      name, industry, total_mcap, float_mcap,
      pe_ttm, pb, ps_ttm, dividend_yield,
      roe_ttm, roe_5y_avg, gross_margin, net_margin,
      debt_ratio, operating_cf_to_ni,
      revenue_growth_yoy, earnings_growth_yoy,
      pe_percentile_3y, pb_percentile_3y
    """
    symbol = normalize_symbol(symbol)
    cache_key = f"fundamentals_{symbol}"
    cached = cache_get(cache_key, ttl_seconds=6 * 3600)
    if cached is not None:
        return cached

    result: dict[str, Any] = {"symbol": symbol}

    # Layer 1: tushare (preferred). Has the richest schema by far.
    pro = _try_import_tushare()
    if pro is not None:
        try:
            ts_data = _fetch_fundamentals_tushare(pro, symbol)
            result.update(ts_data)
        except Exception as e:
            log.debug("tushare fundamentals path failed for %s: %s", symbol, e)

    # Layer 2: valuation overlay (pe/pb/mcap). Skip the expensive spot call
    # when tushare already populated all four fields via daily_basic.
    _valuation_keys = ("pe_ttm", "pb", "total_mcap", "float_mcap")
    if not all(k in result for k in _valuation_keys):
        try:
            spot = fetch_realtime_quote(symbol)
            for k in ("name", "industry"):
                if k in spot and k not in result:
                    result[k] = spot[k]
            for k in _valuation_keys:
                if k in spot and k not in result:
                    result[k] = spot[k]
        except Exception as e:
            log.debug("realtime spot for fundamentals failed for %s: %s", symbol, e)

    # Layer 3: akshare fallback for fields tushare/spot didn't fill in.
    # Skip the expensive call entirely if tushare already gave us the
    # core scoring inputs (roe + at least one margin).
    have_core = ("roe_ttm" in result
                 and ("net_margin" in result or "gross_margin" in result))
    if not have_core:
        ak = _try_import_akshare()
        if ak is not None:
            _augment_fundamentals_akshare(ak, symbol, result)

    cache_set(cache_key, result)
    return result


def _augment_fundamentals_akshare(ak, symbol: str, result: dict[str, Any]) -> None:
    """Best-effort fill of any fundamentals fields tushare/spot didn't supply.

    AkShare's stock_a_lg_indicator and stock_financial_abstract endpoints
    vary across versions, so each call is wrapped — missing data is fine.
    Mutates `result` in place; never raises.
    """
    try:
        ind = _retry_akshare(ak.stock_a_lg_indicator, symbol=symbol)
        if ind is not None and not ind.empty:
            latest = ind.iloc[-1]
            result.setdefault("trade_date", str(latest.get("trade_date", "")))
            for src, dst in [
                ("pe", "pe"), ("pe_ttm", "pe_ttm"), ("pb", "pb"),
                ("ps", "ps"), ("ps_ttm", "ps_ttm"),
                ("dv_ratio", "dividend_yield"),
                ("total_mv", "total_mcap"),
            ]:
                if dst in result:
                    continue
                if src in latest and pd.notna(latest[src]):
                    result[dst] = float(latest[src])
    except Exception as e:
        log.debug("stock_a_lg_indicator failed: %s", e)

    try:
        abs_df = _retry_akshare(ak.stock_financial_abstract, symbol=symbol)
        if abs_df is not None and not abs_df.empty:
            period_cols = [c for c in abs_df.columns if c not in ("选项", "指标")]
            if period_cols:
                latest_col = period_cols[0]
                for _, row in abs_df.iterrows():
                    metric = str(row.get("指标", ""))
                    val = row.get(latest_col)
                    if pd.isna(val):
                        continue
                    if "净资产收益率" in metric and "加权" in metric:
                        result.setdefault("roe_ttm", float(val))
                    elif "销售毛利率" in metric:
                        result.setdefault("gross_margin", float(val))
                    elif "销售净利率" in metric:
                        result.setdefault("net_margin", float(val))
                    elif "资产负债率" in metric:
                        result.setdefault("debt_ratio", float(val))
                    elif "营业收入" in metric and "同比" in metric:
                        result.setdefault("revenue_growth_yoy", float(val))
                    elif "净利润" in metric and "同比" in metric:
                        result.setdefault("earnings_growth_yoy", float(val))
    except Exception as e:
        log.debug("stock_financial_abstract failed: %s", e)


# ---------------------------------------------------------------------------
# Basic info
# ---------------------------------------------------------------------------
def _fetch_stock_info_tushare(pro, symbol: str) -> dict[str, Any]:
    """Name/industry/list_date from tushare stock_basic (single stock, fast)."""
    ts_code = _to_ts_code(symbol)
    try:
        df = _retry_akshare(
            pro.stock_basic,
            ts_code=ts_code,
            fields="ts_code,name,industry,list_date,market",
        )
        if df is None or df.empty:
            return {}
        row = df.iloc[0]
        return {dst: str(row[src])
                for src, dst in [("name", "name"), ("industry", "industry"),
                                  ("list_date", "list_date"), ("market", "market")]
                if src in row and pd.notna(row[src])}
    except Exception as e:
        log.debug("tushare stock_basic failed for %s: %s", symbol, e)
        return {}


def fetch_stock_info(symbol: str) -> dict[str, Any]:
    """Name, industry, list date, ST status etc."""
    symbol = normalize_symbol(symbol)
    cache_key = f"info_{symbol}"
    cached = cache_get(cache_key, ttl_seconds=24 * 3600)
    if cached is not None:
        return cached

    info: dict[str, Any] = {"symbol": symbol}

    pro = _try_import_tushare()
    if pro is not None:
        ts_info = _fetch_stock_info_tushare(pro, symbol)
        if ts_info:
            info.update(ts_info)
            log.info("stock_info %s via tushare stock_basic", symbol)
            cache_set(cache_key, info)
            return info

    ak = _try_import_akshare()
    if ak is None:
        return info
    try:
        df = _retry_akshare(ak.stock_individual_info_em, symbol=symbol)
    except Exception as e:
        log.debug("stock_individual_info_em failed %s: %s", symbol, e)
        return info
    if df is not None and not df.empty:
        for _, row in df.iterrows():
            key = str(row.get("item", ""))
            val = row.get("value")
            if pd.isna(val):
                continue
            mapping = {
                "股票代码": "symbol", "股票简称": "name", "行业": "industry",
                "上市时间": "list_date", "总市值": "total_mcap",
                "流通市值": "float_mcap", "总股本": "total_shares",
                "流通股": "float_shares",
            }
            if key in mapping:
                info[mapping[key]] = val
    cache_set(cache_key, info)
    return info


# ---------------------------------------------------------------------------
# Universe helpers
# ---------------------------------------------------------------------------
def fetch_hs300_members() -> list[str]:
    """HS300 index constituents."""
    ak = _try_import_akshare()
    if ak is None:
        return []
    cache_key = "hs300_members"
    cached = cache_get(cache_key, ttl_seconds=24 * 3600)
    if cached is not None:
        return cached
    try:
        df = _retry_akshare(ak.index_stock_cons, symbol="000300")
    except Exception as e:
        log.error("hs300 members fetch failed: %s", e)
        return []
    syms: list[str] = []
    if df is not None and not df.empty:
        col = "品种代码" if "品种代码" in df.columns else df.columns[0]
        syms = [normalize_symbol(x) for x in df[col].astype(str).tolist()]
    cache_set(cache_key, syms)
    return syms


def fetch_zz500_members() -> list[str]:
    ak = _try_import_akshare()
    if ak is None:
        return []
    cache_key = "zz500_members"
    cached = cache_get(cache_key, ttl_seconds=24 * 3600)
    if cached is not None:
        return cached
    try:
        df = _retry_akshare(ak.index_stock_cons, symbol="000905")
    except Exception as e:
        log.error("zz500 members fetch failed: %s", e)
        return []
    syms: list[str] = []
    if df is not None and not df.empty:
        col = "品种代码" if "品种代码" in df.columns else df.columns[0]
        syms = [normalize_symbol(x) for x in df[col].astype(str).tolist()]
    cache_set(cache_key, syms)
    return syms


# ---------------------------------------------------------------------------
# Trading calendar
# ---------------------------------------------------------------------------
def is_trading_day(date_str: str | None = None) -> bool:
    """Check if date is an A-share trading day."""
    from datetime import datetime
    date_str = date_str or today_str()
    d = datetime.strptime(date_str, "%Y%m%d")
    if d.weekday() >= 5:
        return False
    # For holidays we'd need the official calendar; AkShare has
    # tool_trade_date_hist_sina. Fall back to weekday-only if unavailable.
    ak = _try_import_akshare()
    if ak is None:
        return True  # optimistic
    cache_key = "trade_calendar"
    cached = cache_get(cache_key, ttl_seconds=7 * 24 * 3600)
    trade_days: set[str]
    if cached:
        trade_days = set(cached)
    else:
        try:
            df = _retry_akshare(ak.tool_trade_date_hist_sina)
            trade_days = {
                pd.to_datetime(x).strftime("%Y%m%d")
                for x in df["trade_date"].tolist()
            }
            cache_set(cache_key, list(trade_days))
        except Exception as e:
            log.warning("trade calendar unavailable: %s", e)
            return True
    return date_str in trade_days


# ---------------------------------------------------------------------------
# Capital flow & earnings forecast
# ---------------------------------------------------------------------------
def fetch_moneyflow(symbol: str, days: int = 5) -> dict[str, Any]:
    """Institutional money flow for a single stock (主力资金流向).

    主力净流入 = (超大单+大单) 买入额 − (超大单+大单) 卖出额  (单位: 万元)
    Requires tushare. Returns {} when unavailable.
    """
    symbol = normalize_symbol(symbol)
    cache_key = f"moneyflow_{symbol}_{days}"
    cached = cache_get(cache_key, ttl_seconds=3600)
    if cached is not None:
        return cached

    pro = _try_import_tushare()
    if pro is None:
        return {}
    ts_code = _to_ts_code(symbol)
    try:
        df = _retry_akshare(pro.moneyflow, ts_code=ts_code, limit=days)
        if df is None or df.empty:
            return {}
        df = df.head(days)   # relay may ignore limit; enforce here
        df["main_net"] = (
            (df["buy_elg_amount"].fillna(0) + df["buy_lg_amount"].fillna(0))
            - (df["sell_elg_amount"].fillna(0) + df["sell_lg_amount"].fillna(0))
        )
        result: dict[str, Any] = {
            "days": min(days, len(df)),
            "main_net_total": float(df["main_net"].sum()),
            "net_mf_total": float(df["net_mf_amount"].fillna(0).sum()),
            "positive_days": int((df["main_net"] > 0).sum()),
            "latest_date": str(df.iloc[0]["trade_date"]),
            "daily": df[["trade_date", "main_net", "net_mf_amount"]].to_dict("records"),
        }
        cache_set(cache_key, result)
        return result
    except Exception as e:
        log.warning("moneyflow fetch failed for %s: %s", symbol, e)
        return {}


def fetch_forecast(symbol: str) -> dict[str, Any]:
    """Latest earnings forecast / guidance (业绩预告). Requires tushare.

    type      — 预增/略增/续盈/扭亏/预减/略减/首亏/续亏
    p_change  — midpoint of expected net-profit YoY change (%)
    end_date  — reporting period the forecast covers
    """
    symbol = normalize_symbol(symbol)
    cache_key = f"forecast_{symbol}"
    cached = cache_get(cache_key, ttl_seconds=6 * 3600)
    if cached is not None:
        return cached

    pro = _try_import_tushare()
    if pro is None:
        return {}
    ts_code = _to_ts_code(symbol)
    try:
        df = _retry_akshare(pro.forecast, ts_code=ts_code, limit=3)
        if df is None or df.empty:
            return {}
        df = df.head(3)   # relay may ignore limit; enforce here
        df0 = df[df["update_flag"].astype(str) == "0"]
        row = df0.iloc[0] if not df0.empty else df.iloc[0]

        # Reject forecasts published more than 2 years ago — no longer actionable
        ann_date = str(row.get("ann_date", ""))
        if ann_date and len(ann_date) >= 4:
            from datetime import datetime
            if int(ann_date[:4]) < datetime.now().year - 2:
                log.debug("forecast for %s is stale (%s), skipping", symbol, ann_date)
                return {}

        p_min = row.get("p_change_min")
        p_max = row.get("p_change_max")
        p_change: float | None = None
        if pd.notna(p_min) and pd.notna(p_max):
            p_change = (float(p_min) + float(p_max)) / 2
        elif pd.notna(p_min):
            p_change = float(p_min)
        elif pd.notna(p_max):
            p_change = float(p_max)
        result: dict[str, Any] = {
            "type": str(row.get("type", "")),
            "p_change": p_change,
            "end_date": str(row.get("end_date", "")),
            "summary": str(row.get("summary", "")),
            "ann_date": str(row.get("ann_date", "")),
        }
        cache_set(cache_key, result)
        return result
    except Exception as e:
        log.warning("forecast fetch failed for %s: %s", symbol, e)
        return {}


def fetch_northbound_flow(days: int = 5) -> dict[str, Any]:
    """Recent northbound capital flow (北向资金, 沪深港通). Market-wide signal.

    north_money: daily net inflow in 万元. Positive = net buying A-shares.
    Requires tushare. Returns {} when unavailable.
    """
    cache_key = f"northbound_{days}"
    cached = cache_get(cache_key, ttl_seconds=3600)
    if cached is not None:
        return cached

    pro = _try_import_tushare()
    if pro is None:
        return {}
    try:
        # moneyflow_hsgt doesn't reliably support `limit`; use date window
        end = today_str()
        start = days_ago_str(days + 4)   # +4 covers weekends/holidays
        df = _retry_akshare(pro.moneyflow_hsgt, start_date=start, end_date=end)
        if df is None or df.empty:
            return {}
        df = df.head(days)
        df["north_money"] = pd.to_numeric(df["north_money"], errors="coerce")
        df = df.dropna(subset=["north_money"])
        result: dict[str, Any] = {
            "days": min(days, len(df)),
            "total": float(df["north_money"].sum()),
            "latest": float(df.iloc[0]["north_money"]),
            "positive_days": int((df["north_money"] > 0).sum()),
            "latest_date": str(df.iloc[0]["trade_date"]),
        }
        cache_set(cache_key, result)
        return result
    except Exception as e:
        log.warning("northbound flow fetch failed: %s", e)
        return {}


if __name__ == "__main__":
    # smoke test: python -m scripts.fetch_data [--code SYMBOL]
    import argparse, sys
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", default="600519", help="股票代码, e.g. 002463")
    args = parser.parse_args()
    sym = args.code
    print(f"Testing fetch for {sym}")
    try:
        df = fetch_ohlcv(sym, period="daily")
        print(f"OHLCV rows: {len(df)}; latest date: "
              f"{df['date'].max() if not df.empty else 'N/A'}")
    except Exception as e:
        print(f"OHLCV failed: {e}")
    try:
        q = fetch_realtime_quote(sym)
        print(f"Quote: {q.get('name', 'N/A')} price={q.get('price', 'N/A')}")
    except Exception as e:
        print(f"Quote failed: {e}")
