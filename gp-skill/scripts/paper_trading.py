"""Paper trading ledger. Keeps a portfolio in data/portfolio.json and a
newline-delimited trade log in data/trade_log.jsonl.

Design:
  * Dead simple: no DB, no dependencies beyond stdlib + yaml.
  * File-locked write (best effort) so concurrent LLM calls don't corrupt.
  * Uses A-share fee model: stamp tax 0.05% on sell, commission 0.025%
    (configurable), minimum 5 yuan per trade.
  * T+1: sells of shares bought today are rejected.
"""
from __future__ import annotations
import datetime as dt
import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from ._common import DATA_DIR, log, load_risk_config, normalize_symbol


PORTFOLIO_FILE = DATA_DIR / "portfolio.json"
TRADE_LOG = DATA_DIR / "trade_log.jsonl"


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------
def _load_portfolio() -> dict[str, Any]:
    if not PORTFOLIO_FILE.exists():
        cfg = load_risk_config()
        init_cash = float(cfg.get("account", {}).get("total_capital", 100_000))
        portfolio = {
            "cash": init_cash,
            "initial_capital": init_cash,
            "positions": {},          # symbol -> position dict
            "created_at": dt.datetime.now().isoformat(),
        }
        _save_portfolio(portfolio)
        return portfolio
    with PORTFOLIO_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def _save_portfolio(p: dict) -> None:
    # Atomic write: write tmp, rename.
    tmp = PORTFOLIO_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PORTFOLIO_FILE)


def _append_log(entry: dict) -> None:
    with TRADE_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Fees
# ---------------------------------------------------------------------------
def _commission(cost: float, cfg: dict) -> float:
    fees = cfg.get("fees", {}) or {}
    rate = float(fees.get("commission_rate", 0.00025))  # 0.025%
    min_fee = float(fees.get("commission_min", 5.0))
    return max(min_fee, cost * rate)


def _stamp_tax(cost: float, cfg: dict) -> float:
    fees = cfg.get("fees", {}) or {}
    rate = float(fees.get("stamp_tax_rate", 0.0005))  # 0.05% on sell only
    return cost * rate


def _transfer_fee(cost: float, cfg: dict, symbol: str) -> float:
    """Shanghai only: 0.001% both sides."""
    fees = cfg.get("fees", {}) or {}
    rate = float(fees.get("transfer_fee_rate", 0.00001))
    return cost * rate if symbol.startswith(("6", "688")) else 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_portfolio() -> dict[str, Any]:
    return _load_portfolio()


def current_position(symbol: str) -> dict[str, Any]:
    symbol = normalize_symbol(symbol)
    p = _load_portfolio()
    return p["positions"].get(symbol, {
        "symbol": symbol, "qty": 0, "avg_cost": 0.0,
        "highest_since_buy": 0.0, "bought_today_qty": 0,
    })


def execute_buy(symbol: str, price: float, qty: int,
                reason: str = "manual", config: dict | None = None) -> dict:
    """Execute a paper buy. Returns trade record."""
    symbol = normalize_symbol(symbol)
    cfg = config or load_risk_config()
    if qty <= 0 or qty % 100 != 0:
        return {"ok": False, "error": "qty must be a positive multiple of 100"}

    p = _load_portfolio()
    cost = price * qty
    fee = _commission(cost, cfg) + _transfer_fee(cost, cfg, symbol)
    total = cost + fee
    if total > p["cash"]:
        return {"ok": False,
                "error": f"insufficient cash: need {total:.2f}, "
                         f"have {p['cash']:.2f}"}

    pos = p["positions"].get(symbol, {
        "symbol": symbol, "qty": 0, "avg_cost": 0.0,
        "highest_since_buy": price, "bought_today_qty": 0,
        "first_bought_at": dt.datetime.now().isoformat(),
    })
    new_qty = pos["qty"] + qty
    new_avg = (pos["qty"] * pos["avg_cost"] + cost) / new_qty
    pos["qty"] = new_qty
    pos["avg_cost"] = new_avg
    pos["highest_since_buy"] = max(pos.get("highest_since_buy", 0), price)
    pos["bought_today_qty"] = pos.get("bought_today_qty", 0) + qty
    pos["last_buy_at"] = dt.datetime.now().isoformat()

    p["cash"] -= total
    p["positions"][symbol] = pos
    _save_portfolio(p)

    record = {
        "timestamp": dt.datetime.now().isoformat(),
        "action": "buy", "symbol": symbol,
        "price": price, "qty": qty, "cost": cost, "fee": fee,
        "reason": reason, "cash_after": p["cash"],
    }
    _append_log(record)
    return {"ok": True, **record}


def execute_sell(symbol: str, price: float, qty: int,
                 reason: str = "manual", config: dict | None = None) -> dict:
    """Execute a paper sell. Enforces T+1 on shares bought today."""
    symbol = normalize_symbol(symbol)
    cfg = config or load_risk_config()
    p = _load_portfolio()
    pos = p["positions"].get(symbol)
    if not pos or pos["qty"] < qty:
        return {"ok": False,
                "error": f"insufficient position: have {pos['qty'] if pos else 0}, "
                         f"want {qty}"}
    # T+1 check
    sellable = pos["qty"] - pos.get("bought_today_qty", 0)
    if qty > sellable:
        return {"ok": False,
                "error": f"T+1 limit: only {sellable} shares are sellable today "
                         f"(today-bought {pos.get('bought_today_qty', 0)} locked)"}

    gross = price * qty
    fee = (_commission(gross, cfg)
           + _stamp_tax(gross, cfg)
           + _transfer_fee(gross, cfg, symbol))
    net = gross - fee

    avg = pos["avg_cost"]
    realized_pnl = (price - avg) * qty - fee
    pos["qty"] -= qty
    if pos["qty"] == 0:
        # Close out
        p["positions"].pop(symbol)
    else:
        p["positions"][symbol] = pos

    p["cash"] += net
    _save_portfolio(p)

    record = {
        "timestamp": dt.datetime.now().isoformat(),
        "action": "sell", "symbol": symbol,
        "price": price, "qty": qty, "gross": gross, "fee": fee,
        "net": net, "realized_pnl": realized_pnl,
        "reason": reason, "cash_after": p["cash"],
    }
    _append_log(record)
    return {"ok": True, **record}


def reset_today_buys() -> None:
    """Call at market close (or start of next trading day) to unlock T+1."""
    p = _load_portfolio()
    for sym, pos in p["positions"].items():
        pos["bought_today_qty"] = 0
    _save_portfolio(p)


def mark_to_market(price_map: dict[str, float]) -> dict[str, Any]:
    """Compute current equity given a {symbol: current_price} dict."""
    p = _load_portfolio()
    position_value = 0.0
    details = []
    for sym, pos in p["positions"].items():
        price = price_map.get(sym)
        if price is None:
            continue
        value = price * pos["qty"]
        cost = pos["avg_cost"] * pos["qty"]
        details.append({
            "symbol": sym, "qty": pos["qty"],
            "avg_cost": pos["avg_cost"], "current_price": price,
            "market_value": value, "unrealized_pnl": value - cost,
            "unrealized_pnl_pct": (value - cost) / cost if cost else 0,
        })
        position_value += value
        # Update high watermark
        pos["highest_since_buy"] = max(
            pos.get("highest_since_buy", 0), price
        )
    _save_portfolio(p)
    equity = p["cash"] + position_value
    return {
        "cash": p["cash"],
        "position_value": position_value,
        "equity": equity,
        "initial_capital": p.get("initial_capital", equity),
        "total_return_pct": (equity - p.get("initial_capital", equity))
            / p.get("initial_capital", equity) if p.get("initial_capital") else 0,
        "positions": details,
    }


def today_trade_count() -> int:
    """How many trades executed today (for risk gate)."""
    today = dt.date.today().isoformat()
    count = 0
    if TRADE_LOG.exists():
        with TRADE_LOG.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("timestamp", "").startswith(today):
                    count += 1
    return count


def last_trade_on_symbol(symbol: str) -> dict | None:
    symbol = normalize_symbol(symbol)
    if not TRADE_LOG.exists():
        return None
    last = None
    with TRADE_LOG.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("symbol") == symbol:
                last = rec
    return last


if __name__ == "__main__":
    print(json.dumps(get_portfolio(), ensure_ascii=False, indent=2))
