"""Compose a daily report. Called after market close to summarize the
day: market state, portfolio PnL, triggered risk events, watchlist
signals. LLM may consume this as raw JSON then format it nicely.
"""
from __future__ import annotations
import datetime as dt
import json
from typing import Any

from . import fetch_data, paper_trading, risk, signals
from ._common import load_risk_config, load_watchlist, log


def daily_report(config: dict | None = None) -> dict[str, Any]:
    cfg = config or load_risk_config()
    today = dt.date.today().strftime("%Y-%m-%d")

    # 1. Market overview — HS300 as the benchmark reference
    hs300 = fetch_data.fetch_index_ohlcv("sh000300")
    market = {}
    if not hs300.empty:
        tail = hs300.tail(2)
        if len(tail) >= 2:
            prev, today_row = tail.iloc[0], tail.iloc[1]
            chg_pct = (today_row["close"] - prev["close"]) / prev["close"] * 100
            market = {
                "hs300_close": float(today_row["close"]),
                "hs300_change_pct": round(chg_pct, 2),
                "hs300_volume": float(today_row["volume"]),
            }

    # 2. Portfolio state
    portfolio = paper_trading.get_portfolio()
    positions = portfolio.get("positions", {})
    price_map: dict[str, float] = {}
    for sym in positions:
        try:
            q = fetch_data.fetch_realtime_quote(sym)
            if q.get("price"):
                price_map[sym] = float(q["price"])
        except Exception as e:
            log.warning("quote failed for %s: %s", sym, e)
            continue
    mtm = paper_trading.mark_to_market(price_map)

    # 3. Risk events — check stop conditions on every holding
    risk_events = []
    for sym, pos in positions.items():
        price = price_map.get(sym)
        if price is None:
            continue
        triggers = risk.check_stop_conditions(
            sym, price=price,
            avg_cost=pos["avg_cost"],
            highest_since_buy=pos.get("highest_since_buy"),
            config=cfg,
        )
        risk_events.extend(triggers)

    # 4. Watchlist scan — light pass (no fundamentals, just tech)
    watchlist_signals = []
    for sym in load_watchlist():
        try:
            card = signals.analyze_symbol(sym, config=cfg)
            if card.action in ("buy", "buy_strong", "sell", "sell_strong"):
                watchlist_signals.append({
                    "symbol": sym,
                    "name": card.name,
                    "action": card.action,
                    "score": card.combined_score,
                    "confidence": card.confidence,
                    "latest_close": card.latest_close,
                })
        except Exception as e:
            log.debug("watchlist scan skip %s: %s", sym, e)

    # 5. Trade count today
    today_trades = paper_trading.today_trade_count()

    return {
        "date": today,
        "market": market,
        "portfolio": {
            "cash": mtm["cash"],
            "position_value": mtm["position_value"],
            "equity": mtm["equity"],
            "total_return_pct": round(mtm["total_return_pct"] * 100, 2),
            "positions": mtm["positions"],
        },
        "risk_events": risk_events,
        "watchlist_signals": watchlist_signals,
        "today_trade_count": today_trades,
        "disclaimer": "本报告由 a-stock-advisor skill 生成，仅供参考，不构成投资建议。",
    }


if __name__ == "__main__":
    print(json.dumps(daily_report(), ensure_ascii=False, indent=2, default=str))
