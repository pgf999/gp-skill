"""Nightly batch sync — refresh the local DuckDB cache against live data.

Run this after market close (suggested: 19:00 trading days) so the next
morning's analysis hits local storage instead of beating up the public
endpoints. The cache is incremental: each symbol's last cached date is
read from DuckDB, and only newer bars are fetched from the backend
chain.

Usage
-----
  # Refresh the watchlist (recommended default — quick, ~1-2 min)
  python -m scripts.sync_data --universe watchlist

  # Refresh HS300 (~5-10 min depending on backend)
  python -m scripts.sync_data --universe hs300

  # Refresh ZZ500 + HS300
  python -m scripts.sync_data --universe hs300 --universe zz500

  # Custom list
  python -m scripts.sync_data --symbols 600519,000858,300750

  # Force re-fetch from a specific date instead of incremental
  python -m scripts.sync_data --universe hs300 --since 2024-01-01

  # Skip indices (HS300/ZZ500 themselves) and only do member stocks
  python -m scripts.sync_data --universe hs300 --no-indices

Scheduling on Windows
---------------------
Open Task Scheduler -> Create Task:
  Trigger: Daily at 19:00, only on Mon-Fri
  Action: Start a program
    Program:   powershell.exe
    Arguments: -NoProfile -ExecutionPolicy Bypass -Command "cd 'C:\\Users\\YOU\\path\\gp-skill'; python -m scripts.sync_data --universe watchlist --universe hs300 *> data\\logs\\sync_$(Get-Date -Format yyyyMMdd).log"
  Conditions: Wake the computer if needed (optional)

The script is idempotent — running it twice in a row is harmless and
mostly free (everything's already cached).
"""
from __future__ import annotations
import argparse
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

from ._common import log, normalize_symbol, today_str
from . import fetch_data, cache_db


# ---------------------------------------------------------------------------
# Universe resolution
# ---------------------------------------------------------------------------
def _read_watchlist() -> list[str]:
    """Pull symbols from config/watchlist.yaml. Falls back to empty if
    the user hasn't created one yet — that's fine, just means nothing
    to sync from the watchlist source.
    """
    cfg = Path(__file__).resolve().parent.parent / "config" / "watchlist.yaml"
    if not cfg.exists():
        log.info("No config/watchlist.yaml found — skipping watchlist sync")
        return []
    try:
        import yaml   # type: ignore
    except ImportError:
        log.warning("PyYAML not installed; can't read watchlist.yaml")
        return []
    try:
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except Exception as e:
        log.error("Failed to parse watchlist.yaml: %s", e)
        return []
    syms: list[str] = []
    # Accept either {"symbols": [...]} or {"watchlist": [...]} or top-level list
    if isinstance(data, list):
        syms = data
    elif isinstance(data, dict):
        for k in ("symbols", "watchlist", "stocks"):
            if k in data and isinstance(data[k], list):
                syms = data[k]
                break
        # Also accept nested structure: {groups: {core: [...], satellite: [...]}}
        if not syms and "groups" in data and isinstance(data["groups"], dict):
            for v in data["groups"].values():
                if isinstance(v, list):
                    syms.extend(v)
    return [normalize_symbol(str(s).split("#")[0].strip()) for s in syms if s]


def _resolve_universe(names: list[str]) -> list[str]:
    """Translate ['hs300','watchlist'] etc. into a deduped symbol list."""
    out: list[str] = []
    seen: set[str] = set()

    def add(syms: Iterable[str]) -> None:
        for s in syms:
            ns = normalize_symbol(s)
            if ns and ns not in seen:
                seen.add(ns)
                out.append(ns)

    for name in names:
        n = name.lower()
        if n == "hs300":
            add(fetch_data.fetch_hs300_members())
        elif n == "zz500":
            add(fetch_data.fetch_zz500_members())
        elif n == "watchlist":
            add(_read_watchlist())
        elif n == "all":
            # "all" = HS300 + ZZ500. Truly all-A would be 5000+ symbols and
            # an hour-long sync; not the default. Add support if you really
            # want it: AkShare's stock_zh_a_spot_em returns the full list.
            add(fetch_data.fetch_hs300_members())
            add(fetch_data.fetch_zz500_members())
        else:
            log.warning("Unknown universe '%s' — skipped", name)
    return out


# ---------------------------------------------------------------------------
# Per-symbol sync
# ---------------------------------------------------------------------------
def _sync_one_ohlcv(symbol: str, *, since: str | None,
                    period: str = "daily", adjust: str = "qfq") -> dict:
    """Fetch + cache OHLCV for one symbol. Returns a small status dict.

    Strategy: if --since is given, refetch from there. Otherwise read
    DuckDB's latest cached date and fetch from the day after. New
    listings or empty cache → fetch a full year by default.
    """
    if since:
        start = since.replace("-", "")
    else:
        last = cache_db.latest_cached_date(symbol, period=period, adjust=adjust)
        if last:
            # Re-fetch from the last cached date so we overwrite a possibly
            # incomplete final bar (intraday close would have written a
            # partial row earlier).
            start = last
        else:
            # Cold start: pull a full year for new symbols
            from datetime import timedelta
            start = (datetime.now() - timedelta(days=400)).strftime("%Y%m%d")
    end = today_str()

    t0 = time.time()
    try:
        df = fetch_data.fetch_ohlcv(
            symbol, period=period, start=start, end=end,
            adjust=adjust, use_cache=False,   # bypass JSON cache; force fresh
        )
    except Exception as e:
        return {"symbol": symbol, "ok": False, "error": str(e), "rows": 0,
                "elapsed_s": round(time.time() - t0, 2)}

    rows = 0 if df is None else len(df)
    # fetch_ohlcv with use_cache=False still writes back to DuckDB via the
    # cache_db.put_ohlcv call inside it — so we don't need to write again.
    return {"symbol": symbol, "ok": True, "rows": rows, "start": start,
            "end": end, "elapsed_s": round(time.time() - t0, 2)}


def _sync_indices() -> list[dict]:
    """Refresh HS300 and ZZ500 index OHLCV (used as backtest benchmarks)."""
    results = []
    for code in ("sh000300", "sh000905"):
        t0 = time.time()
        try:
            df = fetch_data.fetch_index_ohlcv(code)
            results.append({"index_code": code, "ok": True,
                            "rows": len(df) if df is not None else 0,
                            "elapsed_s": round(time.time() - t0, 2)})
        except Exception as e:
            results.append({"index_code": code, "ok": False, "error": str(e),
                            "elapsed_s": round(time.time() - t0, 2)})
    return results


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Nightly cache sync for gp-skill",
    )
    parser.add_argument("--universe", action="append", default=[],
                        help="One of: hs300, zz500, watchlist, all "
                             "(repeatable; deduped)")
    parser.add_argument("--symbols", default=None,
                        help="Comma-separated symbols to sync (overrides "
                             "--universe if given)")
    parser.add_argument("--since", default=None,
                        help="Force re-fetch from YYYY-MM-DD instead of "
                             "incremental")
    parser.add_argument("--adjust", default="qfq",
                        choices=["qfq", "hfq", ""],
                        help="Price adjust mode (default: qfq)")
    parser.add_argument("--no-indices", action="store_true",
                        help="Skip HS300/ZZ500 index OHLCV refresh")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process at most N symbols (testing)")
    args = parser.parse_args(argv)

    # cache_db must be available — that's the whole point of this script.
    if not cache_db.is_available():
        print("ERROR: DuckDB not available. Install with: "
              "pip install duckdb", file=sys.stderr)
        return 2

    # Resolve symbols
    if args.symbols:
        symbols = [normalize_symbol(s) for s in args.symbols.split(",") if s]
    else:
        if not args.universe:
            args.universe = ["watchlist"]   # sensible default
        symbols = _resolve_universe(args.universe)

    if args.limit:
        symbols = symbols[: args.limit]

    if not symbols:
        print("Nothing to sync (empty symbol list).", file=sys.stderr)
        return 1

    # Pre-flight stats
    pre = cache_db.stats()
    print(f"=== gp-skill sync starting at "
          f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
    print(f"Cache before: {pre.get('ohlcv_rows', 0):,} OHLCV rows / "
          f"{pre.get('distinct_symbols', 0)} symbols / "
          f"{pre.get('size_mb', 0)} MB")
    print(f"Syncing {len(symbols)} symbols, "
          f"adjust={args.adjust!r}, since={args.since or 'incremental'}")
    print()

    ok = 0
    fail = 0
    total_rows = 0
    start_t = time.time()

    for i, sym in enumerate(symbols, 1):
        res = _sync_one_ohlcv(sym, since=args.since, adjust=args.adjust)
        if res["ok"]:
            ok += 1
            total_rows += res["rows"]
            print(f"[{i}/{len(symbols)}] {sym}  +{res['rows']} rows  "
                  f"({res['elapsed_s']}s)")
        else:
            fail += 1
            print(f"[{i}/{len(symbols)}] {sym}  FAIL: {res.get('error','?')[:80]}")

    if not args.no_indices:
        print()
        print("Refreshing index benchmarks...")
        for r in _sync_indices():
            tag = "OK " if r["ok"] else "FAIL"
            print(f"  {tag} {r['index_code']}  rows={r.get('rows', 0)}  "
                  f"({r['elapsed_s']}s)")

    cache_db.vacuum()
    post = cache_db.stats()
    elapsed = round(time.time() - start_t, 1)

    print()
    print(f"=== sync done in {elapsed}s ===")
    print(f"Symbols: {ok} ok / {fail} fail / {len(symbols)} total")
    print(f"Rows synced: +{total_rows:,}")
    print(f"Cache after:  {post.get('ohlcv_rows', 0):,} OHLCV rows / "
          f"{post.get('distinct_symbols', 0)} symbols / "
          f"{post.get('size_mb', 0)} MB")
    print(f"Latest date in cache: {post.get('latest_ohlcv_date')}")

    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
