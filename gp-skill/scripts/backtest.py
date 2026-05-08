"""Vectorized backtest engine for A-share daily strategies.

Usage:
  from scripts.backtest import run_backtest, Strategy, overfit_warning

  def my_strat(df):
      # df has columns: date, open, high, low, close, volume, ma20, ...
      # return Series of {-1, 0, +1} signals (buy=+1, sell=-1, hold=0)
      ...

  result = run_backtest(df, my_strat)
  print(result['metrics'])

Constraints the engine enforces (common pitfalls it avoids):
  * No look-ahead: today's signal uses data up to and including today,
    but the trade fills at next day's open.
  * T+1: a buy entered today can only be sold tomorrow or later.
  * Commission + stamp tax costs are charged.
  * Stop-loss and take-profit take precedence over signal exits.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Any

import numpy as np
import pandas as pd

from . import technical
from ._common import log


Strategy = Callable[[pd.DataFrame], pd.Series]


@dataclass
class BacktestConfig:
    initial_capital: float = 100_000.0
    commission_rate: float = 0.00025
    stamp_tax_rate: float = 0.0005  # sell only
    min_commission: float = 5.0
    slippage_pct: float = 0.001  # 0.1%
    stop_loss_pct: float = -0.08
    take_profit_pct: float = 0.25
    position_size: float = 1.0   # fraction of capital deployed per trade
    benchmark_df: pd.DataFrame | None = None


def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    d = df.sort_values("date").reset_index(drop=True).copy()
    # Attach common indicators so strategies can reference them
    d["ma5"] = technical.sma(d["close"], 5)
    d["ma10"] = technical.sma(d["close"], 10)
    d["ma20"] = technical.sma(d["close"], 20)
    d["ma60"] = technical.sma(d["close"], 60)
    diff, dea, hist = technical.macd(d["close"])
    d["macd_diff"], d["macd_dea"], d["macd_hist"] = diff, dea, hist
    d["rsi14"] = technical.rsi(d["close"], 14)
    return d


def run_backtest(df: pd.DataFrame, strategy: Strategy,
                 config: BacktestConfig | None = None) -> dict[str, Any]:
    """Simulate a strategy on a single symbol's daily bars.

    Returns {
      'equity_curve': DataFrame[date, equity, drawdown],
      'trades': DataFrame[...],
      'metrics': dict,
      'warnings': list[str]
    }
    """
    cfg = config or BacktestConfig()
    d = _prepare(df)
    if len(d) < 30:
        raise ValueError("Need at least 30 bars for meaningful backtest")

    # Generate signals BEFORE knowing future. strategy sees only rows 0..i
    # We'll call it once on the full frame; responsibility is on the strategy
    # to avoid future leakage (e.g., don't use .shift(-1) inside).
    signals = strategy(d).fillna(0).astype(int)
    if len(signals) != len(d):
        raise ValueError("strategy must return a Series aligned with input")

    cash = cfg.initial_capital
    shares = 0
    avg_cost = 0.0
    high_water = 0.0
    holding_days = 0
    trades = []
    equity_curve = []

    for i in range(len(d)):
        date = d["date"].iloc[i]
        close = d["close"].iloc[i]
        next_open = d["open"].iloc[i + 1] if i + 1 < len(d) else close

        # Mark to market for equity tracking
        equity = cash + shares * close
        if shares > 0:
            high_water = max(high_water, close)
            pnl_pct = (close - avg_cost) / avg_cost

            # Stop-loss / take-profit check (triggered on today's close, act tomorrow open)
            forced_exit = False
            exit_reason = None
            if pnl_pct <= cfg.stop_loss_pct:
                forced_exit = True; exit_reason = "stop_loss"
            elif pnl_pct >= cfg.take_profit_pct:
                forced_exit = True; exit_reason = "take_profit"

            if forced_exit and i + 1 < len(d) and holding_days >= 1:
                # Sell at next-day open (accounting for slippage against us)
                exec_price = next_open * (1 - cfg.slippage_pct)
                gross = exec_price * shares
                fee = max(cfg.min_commission, gross * cfg.commission_rate) \
                      + gross * cfg.stamp_tax_rate
                net = gross - fee
                cash += net
                trades.append({
                    "date": d["date"].iloc[i + 1], "action": "sell",
                    "qty": shares, "price": exec_price,
                    "pnl": net - (avg_cost * shares),
                    "reason": exit_reason,
                })
                shares = 0; avg_cost = 0; high_water = 0; holding_days = 0
                equity_curve.append({"date": date, "equity": equity})
                continue

        sig = signals.iloc[i]
        # Only act on next bar
        if i + 1 >= len(d):
            equity_curve.append({"date": date, "equity": equity})
            continue

        if sig > 0 and shares == 0 and next_open > 0:
            # BUY at next open with slippage against us (up)
            exec_price = next_open * (1 + cfg.slippage_pct)
            deploy = cash * cfg.position_size
            max_qty = int(deploy / (exec_price * 100)) * 100
            if max_qty >= 100:
                cost = max_qty * exec_price
                fee = max(cfg.min_commission, cost * cfg.commission_rate)
                cash -= (cost + fee)
                shares = max_qty
                avg_cost = exec_price
                high_water = exec_price
                holding_days = 0
                trades.append({
                    "date": d["date"].iloc[i + 1], "action": "buy",
                    "qty": shares, "price": exec_price, "pnl": 0.0,
                    "reason": "signal",
                })
        elif sig < 0 and shares > 0 and holding_days >= 1:
            # SELL at next open
            exec_price = next_open * (1 - cfg.slippage_pct)
            gross = exec_price * shares
            fee = max(cfg.min_commission, gross * cfg.commission_rate) \
                  + gross * cfg.stamp_tax_rate
            net = gross - fee
            cash += net
            trades.append({
                "date": d["date"].iloc[i + 1], "action": "sell",
                "qty": shares, "price": exec_price,
                "pnl": net - (avg_cost * shares),
                "reason": "signal",
            })
            shares = 0; avg_cost = 0; high_water = 0; holding_days = 0

        if shares > 0:
            holding_days += 1
        equity_curve.append({"date": date, "equity": equity})

    # Close any remaining position at last close
    if shares > 0:
        final_close = d["close"].iloc[-1]
        gross = final_close * shares
        fee = max(cfg.min_commission, gross * cfg.commission_rate) + gross * cfg.stamp_tax_rate
        cash += gross - fee
        trades.append({
            "date": d["date"].iloc[-1], "action": "sell",
            "qty": shares, "price": final_close,
            "pnl": (gross - fee) - (avg_cost * shares),
            "reason": "force_close",
        })
        shares = 0

    eq_df = pd.DataFrame(equity_curve)
    if eq_df.empty:
        return {"equity_curve": eq_df, "trades": pd.DataFrame(),
                "metrics": {}, "warnings": ["backtest produced no equity points"]}

    eq_df["equity"] = eq_df["equity"].astype(float)
    running_max = eq_df["equity"].cummax()
    eq_df["drawdown"] = (eq_df["equity"] - running_max) / running_max

    trades_df = pd.DataFrame(trades)

    metrics = _compute_metrics(eq_df, trades_df, cfg)
    warnings = overfit_warning(metrics)

    return {
        "equity_curve": eq_df,
        "trades": trades_df,
        "metrics": metrics,
        "warnings": warnings,
    }


def _compute_metrics(eq: pd.DataFrame, trades: pd.DataFrame,
                    cfg: BacktestConfig) -> dict:
    initial = cfg.initial_capital
    final = float(eq["equity"].iloc[-1])
    total_return = final / initial - 1

    days = max(1, (eq["date"].iloc[-1] - eq["date"].iloc[0]).days)
    years = days / 365.25
    annualized = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0

    # Daily returns
    daily = eq["equity"].pct_change().dropna()
    sharpe = np.sqrt(252) * daily.mean() / daily.std() if daily.std() > 0 else 0

    max_dd = float(eq["drawdown"].min())

    if len(trades) > 0:
        sells = trades[trades["action"] == "sell"]
        wins = sells[sells["pnl"] > 0]
        losses = sells[sells["pnl"] < 0]
        win_rate = len(wins) / max(1, len(sells))
        avg_win = wins["pnl"].mean() if len(wins) else 0
        avg_loss = losses["pnl"].mean() if len(losses) else 0
        profit_factor = (wins["pnl"].sum() / abs(losses["pnl"].sum())
                         if len(losses) else float("inf"))
        n_trades = len(sells)
    else:
        win_rate = avg_win = avg_loss = profit_factor = n_trades = 0

    return {
        "initial_capital": initial,
        "final_equity": round(final, 2),
        "total_return_pct": round(total_return * 100, 2),
        "annualized_return_pct": round(annualized * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "sharpe_ratio": round(float(sharpe), 2),
        "n_trades": int(n_trades),
        "win_rate_pct": round(win_rate * 100, 1),
        "avg_win": round(float(avg_win), 2),
        "avg_loss": round(float(avg_loss), 2),
        "profit_factor": round(float(profit_factor), 2)
            if profit_factor != float("inf") else None,
        "years": round(years, 2),
    }


def overfit_warning(metrics: dict) -> list[str]:
    """Heuristic flags for overfit results that demand suspicion."""
    w = []
    ar = metrics.get("annualized_return_pct", 0)
    mdd = metrics.get("max_drawdown_pct", 0)
    sh = metrics.get("sharpe_ratio", 0)
    wr = metrics.get("win_rate_pct", 0)
    n = metrics.get("n_trades", 0)

    if ar > 30:
        w.append(f"⚠️ 年化 {ar}% 偏高，样本外可能不复现")
    if mdd > -5 and n > 5:
        w.append(f"⚠️ 最大回撤仅 {mdd}%，要么样本太短，要么参数过拟合")
    if sh > 3:
        w.append(f"⚠️ 夏普 {sh} 高得可疑，实盘一般达不到这个水平")
    if wr > 80 and n > 5:
        w.append(f"⚠️ 胜率 {wr}% 过高，可能来自止损过松或滑点低估")
    if n < 10:
        w.append(f"⚠️ 仅 {n} 笔交易，样本太少，结论不可靠")
    return w


# ---------------------------------------------------------------------------
# Reference strategies (examples)
# ---------------------------------------------------------------------------
def ma_crossover_strategy(df: pd.DataFrame) -> pd.Series:
    """MA5 > MA20 buy, cross below sell. Classic trend-following."""
    sig = np.where(df["ma5"] > df["ma20"], 1, -1)
    return pd.Series(sig, index=df.index)


def rsi_mean_reversion_strategy(df: pd.DataFrame) -> pd.Series:
    """RSI < 30 buy, RSI > 70 sell. Mean-reversion; high false-signal rate."""
    sig = np.zeros(len(df))
    sig[df["rsi14"] < 30] = 1
    sig[df["rsi14"] > 70] = -1
    return pd.Series(sig, index=df.index)


def macd_trend_strategy(df: pd.DataFrame) -> pd.Series:
    """MACD zero-line crossing with MA20 filter."""
    long_ok = (df["macd_diff"] > 0) & (df["close"] > df["ma20"])
    short_ok = (df["macd_diff"] < 0) & (df["close"] < df["ma20"])
    sig = np.where(long_ok, 1, np.where(short_ok, -1, 0))
    return pd.Series(sig, index=df.index)


if __name__ == "__main__":
    # Quick smoke with synthetic data
    dates = pd.date_range("2022-01-01", periods=500)
    rng = np.random.default_rng(7)
    closes = 100 + np.cumsum(rng.standard_normal(500) * 1.2)
    df = pd.DataFrame({
        "date": dates, "open": closes * 0.995,
        "high": closes * 1.01, "low": closes * 0.99,
        "close": closes, "volume": rng.integers(1e6, 5e6, 500),
    })
    r = run_backtest(df, ma_crossover_strategy)
    print("Metrics:", r["metrics"])
    print("Warnings:", r["warnings"])
