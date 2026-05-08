"""Risk gatekeeper. This module decides whether any buy/sell proposal
passes the user's configured constraints PLUS the hard-coded safety rules.

Design rules:
  * All thresholds live in config/risk.yaml (except the "hard" ones listed below).
  * Functions return a structured RiskDecision; never raise for "not allowed".
  * Hard rules (never configurable) — enforced here so an LLM cannot talk
    itself into overriding them:
      - No buy on ST / *ST / delisting.
      - No buy when symbol is currently at limit-up.
      - No sell when symbol is currently at limit-down.
      - No order during call-auction windows.
      - Every live-mode order requires explicit 2nd confirmation (handled
        by the caller, but we surface this status).

Callers:
  - preview_buy(...) / preview_sell(...) return RiskDecision.
  - Combine with scripts.paper_trading.execute_* or broker_adapter for live.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any

from ._common import (
    load_risk_config, log, normalize_symbol, is_call_auction, is_st as _is_st_name,
)


# ---------------------------------------------------------------------------
# Decision envelope
# ---------------------------------------------------------------------------
@dataclass
class RiskDecision:
    ok: bool
    action: str                       # "buy" | "sell" | "skip"
    symbol: str
    reason_codes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    # If ok, these suggest an order for the caller; never a guarantee.
    suggested_qty: int = 0
    suggested_price: float = 0.0
    position_pct_after: float = 0.0
    requires_confirmation: bool = True   # live mode only

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _fail(symbol: str, action: str, code: str, reason: str) -> RiskDecision:
    return RiskDecision(
        ok=False, action=action, symbol=symbol,
        reason_codes=[code], reasons=[reason],
    )


# ---------------------------------------------------------------------------
# Hard rules (cannot be overridden by config)
# ---------------------------------------------------------------------------
def _hard_block_buy(
    symbol: str, quote: dict, stock_info: dict, now=None,
) -> RiskDecision | None:
    """Return a failure decision if any hard rule blocks the buy, else None."""
    # Call auction: volatile and cancels, refuse.
    if is_call_auction(now):
        return _fail(symbol, "buy", "call_auction",
                     "处于集合竞价时段 (9:15-9:30 或 14:57-15:00)，拒绝下单")

    name = stock_info.get("name") or quote.get("name", "")
    if _is_st_name(symbol, name):
        return _fail(symbol, "buy", "st_blacklist",
                     f"{name} 属 ST/*ST/退市整理，默认黑名单不下单")

    # Check limit-up: pct_chg near +10% (or +20%/+30% depending on board).
    pct = quote.get("pct_chg")
    if pct is not None:
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            pct = None
    if pct is not None:
        # Distinguish by board
        from ._common import market_of
        board = market_of(symbol)
        limits = {"SH": 10.0, "SZ": 10.0, "CHINEXT": 20.0, "STAR": 20.0, "BJ": 30.0}
        up_threshold = limits.get(board, 10.0) - 0.1
        if pct >= up_threshold:
            return _fail(symbol, "buy", "limit_up",
                         f"当前涨幅 {pct:.2f}% 接近或已涨停 ({up_threshold+0.1}%)，"
                         f"T+1 风险过高，拒绝下单")
    return None


def _hard_block_sell(
    symbol: str, quote: dict, stock_info: dict, now=None,
) -> RiskDecision | None:
    if is_call_auction(now):
        return _fail(symbol, "sell", "call_auction",
                     "处于集合竞价时段，拒绝下单")
    pct = quote.get("pct_chg")
    if pct is not None:
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            pct = None
    if pct is not None:
        from ._common import market_of
        board = market_of(symbol)
        limits = {"SH": -10.0, "SZ": -10.0, "CHINEXT": -20.0, "STAR": -20.0, "BJ": -30.0}
        down_threshold = limits.get(board, -10.0) + 0.1
        if pct <= down_threshold:
            return _fail(symbol, "sell", "limit_down",
                         f"当前跌幅 {pct:.2f}% 触及或接近跌停，卖单难成交，建议撤单等反弹")
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def preview_buy(
    symbol: str,
    price: float,
    *,
    account_cash: float,
    account_equity: float,
    current_position_value: float = 0.0,
    total_position_value: float = 0.0,
    today_trade_count: int = 0,
    last_trade_on_symbol_minutes_ago: float | None = None,
    quote: dict | None = None,
    stock_info: dict | None = None,
    consecutive_up_days: int = 0,
    config: dict | None = None,
    now=None,
) -> RiskDecision:
    """Check whether a buy order should be allowed.

    Parameters are deliberately verbose to keep this testable without mocks.
    """
    symbol = normalize_symbol(symbol)
    quote = quote or {}
    stock_info = stock_info or {}
    cfg = config or load_risk_config()

    # --- Hard rules -------------------------------------------------------
    blocked = _hard_block_buy(symbol, quote, stock_info, now=now)
    if blocked:
        return blocked

    # --- Soft (configurable) rules ---------------------------------------
    acct = cfg.get("account", {})
    pos = cfg.get("position", {})
    tl = cfg.get("trade_limits", {})
    sl = cfg.get("stop_loss", {})

    max_single_trade = float(acct.get("max_single_trade", 10_000))
    max_single_ratio = float(pos.get("max_single_stock_ratio", 0.2))
    max_total_ratio = float(pos.get("max_total_position_ratio", 0.8))
    reserved_cash = float(acct.get("reserved_cash", 0))

    max_daily = int(tl.get("max_daily_trades", 5))
    cooldown_min = int(tl.get("cooldown_minutes", 10))
    max_consec_up = int(tl.get("max_consecutive_up_days_allowed", 4))

    if today_trade_count >= max_daily:
        return _fail(symbol, "buy", "daily_trade_limit",
                     f"今日已交易 {today_trade_count} 笔，达到上限 {max_daily}")

    if last_trade_on_symbol_minutes_ago is not None \
            and last_trade_on_symbol_minutes_ago < cooldown_min:
        return _fail(
            symbol, "buy", "cooldown",
            f"距上次操作 {symbol} 仅 {last_trade_on_symbol_minutes_ago:.1f} 分钟，"
            f"冷静期 {cooldown_min} 分钟未到"
        )

    if consecutive_up_days > max_consec_up:
        return _fail(
            symbol, "buy", "chasing_up",
            f"{symbol} 已连涨 {consecutive_up_days} 天，超过追涨阈值 "
            f"{max_consec_up}，拒绝买入"
        )

    # Size the order: minimum of max_single_trade and what position-sizing allows.
    position_cap_value = account_equity * max_single_ratio
    room_in_position = max(0.0, position_cap_value - current_position_value)
    total_cap_value = account_equity * max_total_ratio
    room_total = max(0.0, total_cap_value - total_position_value)
    cash_available = max(0.0, account_cash - reserved_cash)

    budget = min(max_single_trade, room_in_position, room_total, cash_available)
    if budget < price * 100:  # must buy at least 1 lot (100 shares)
        if cash_available < price * 100:
            return _fail(symbol, "buy", "insufficient_cash",
                         f"可用现金 {cash_available:.0f} 不足以买入 1 手 "
                         f"({price * 100:.0f})")
        if room_in_position < price * 100:
            return _fail(symbol, "buy", "position_cap_reached",
                         f"该股持仓已达上限 (当前 {current_position_value:.0f}, "
                         f"上限 {position_cap_value:.0f})")
        if room_total < price * 100:
            return _fail(symbol, "buy", "total_cap_reached",
                         f"总仓位将超限 (当前 {total_position_value:.0f}, "
                         f"上限 {total_cap_value:.0f})")
        return _fail(symbol, "buy", "budget_too_small",
                     f"综合风控后预算仅 {budget:.0f} 元，低于 1 手金额")

    lots = int(budget / (price * 100))
    qty = lots * 100
    cost = qty * price
    position_after = current_position_value + cost
    total_after = total_position_value + cost

    reasons = [
        f"现金充足：可用 {cash_available:.0f} 元",
        f"仓位空间：该股持仓将变为 {position_after:.0f} / "
        f"{position_cap_value:.0f}",
        f"总仓占比：{total_after / account_equity * 100:.1f}% "
        f"(上限 {max_total_ratio * 100:.0f}%)",
        f"本笔金额：{cost:.0f} 元 (单笔上限 {max_single_trade:.0f})",
    ]

    return RiskDecision(
        ok=True, action="buy", symbol=symbol,
        reason_codes=["passed"], reasons=reasons,
        suggested_qty=qty, suggested_price=round(price, 2),
        position_pct_after=position_after / account_equity
            if account_equity else 0.0,
        requires_confirmation=True,
    )


def preview_sell(
    symbol: str,
    price: float,
    *,
    holding_qty: int,
    avg_cost: float,
    highest_since_buy: float | None = None,
    today_trade_count: int = 0,
    last_trade_on_symbol_minutes_ago: float | None = None,
    quote: dict | None = None,
    stock_info: dict | None = None,
    config: dict | None = None,
    reason: str = "manual",
    now=None,
) -> RiskDecision:
    """Decide whether to allow a sell proposal, and whether it's forced
    (e.g., stop-loss is effectively a 'recommended' sell, not a gate)."""
    symbol = normalize_symbol(symbol)
    quote = quote or {}
    stock_info = stock_info or {}
    cfg = config or load_risk_config()

    if holding_qty <= 0:
        return _fail(symbol, "sell", "no_position",
                     f"{symbol} 当前无持仓，无法卖出")

    blocked = _hard_block_sell(symbol, quote, stock_info, now=now)
    if blocked:
        return blocked

    tl = cfg.get("trade_limits", {})
    max_daily = int(tl.get("max_daily_trades", 5))
    cooldown_min = int(tl.get("cooldown_minutes", 10))

    if today_trade_count >= max_daily and reason not in ("stop_loss", "forced"):
        return _fail(symbol, "sell", "daily_trade_limit",
                     f"今日已交易 {today_trade_count} 笔，非强制卖出不允许")
    if last_trade_on_symbol_minutes_ago is not None \
            and last_trade_on_symbol_minutes_ago < cooldown_min \
            and reason not in ("stop_loss", "forced"):
        return _fail(symbol, "sell", "cooldown",
                     f"冷静期未到 (仅 {last_trade_on_symbol_minutes_ago:.1f} 分钟)")

    # Compute pnl ratio to annotate
    pnl_pct = (price - avg_cost) / avg_cost if avg_cost else 0.0

    reasons = [
        f"持仓 {holding_qty} 股 @ {avg_cost:.2f}，当前 {price:.2f}",
        f"浮动盈亏 {pnl_pct * 100:+.2f}%",
    ]
    return RiskDecision(
        ok=True, action="sell", symbol=symbol,
        reason_codes=["passed"], reasons=reasons,
        suggested_qty=holding_qty, suggested_price=round(price, 2),
        requires_confirmation=True,
    )


def check_stop_conditions(
    symbol: str,
    price: float,
    avg_cost: float,
    highest_since_buy: float | None = None,
    config: dict | None = None,
) -> list[dict[str, Any]]:
    """Return a list of triggered stop conditions.

    Each item: {type: "stop_loss" | "take_profit" | "trailing_stop",
                ratio, message}

    This doesn't block; it SIGNALS to the caller that a sell should be
    proposed. The caller decides what to do (paper trade auto-close,
    live mode ask user).
    """
    cfg = config or load_risk_config()
    sl = cfg.get("stop_loss", {})
    pnl_pct = (price - avg_cost) / avg_cost if avg_cost else 0.0

    triggers: list[dict[str, Any]] = []

    abs_sl = float(sl.get("absolute_stop_loss", -0.05))
    if pnl_pct <= abs_sl:
        triggers.append({
            "type": "stop_loss",
            "ratio": pnl_pct,
            "message": f"{symbol} 触发绝对止损 ({pnl_pct * 100:.2f}% <= "
                       f"{abs_sl * 100:.2f}%)，建议卖出"
        })

    tp = float(sl.get("take_profit", 0.20))
    if pnl_pct >= tp:
        triggers.append({
            "type": "take_profit",
            "ratio": pnl_pct,
            "message": f"{symbol} 触发止盈目标 ({pnl_pct * 100:.2f}% >= "
                       f"{tp * 100:.0f}%)，建议减仓或设置移动止盈",
        })

    trail = float(sl.get("trailing_stop_loss", -0.08))
    if highest_since_buy is not None and highest_since_buy > 0:
        drawdown = (price - highest_since_buy) / highest_since_buy
        if drawdown <= trail:
            triggers.append({
                "type": "trailing_stop",
                "ratio": drawdown,
                "message": f"{symbol} 从高点 {highest_since_buy:.2f} 回撤 "
                           f"{drawdown * 100:.2f}%，触发移动止损 "
                           f"({trail * 100:.2f}%)，建议卖出",
            })

    return triggers


if __name__ == "__main__":
    # smoke
    import json
    cfg = load_risk_config()
    d = preview_buy(
        "600519", price=1700.0,
        account_cash=100_000, account_equity=200_000,
        current_position_value=0, total_position_value=0,
        quote={"pct_chg": 2.1, "name": "贵州茅台"},
        stock_info={"name": "贵州茅台"},
        consecutive_up_days=1, config=cfg,
    )
    print(json.dumps(d.to_dict(), ensure_ascii=False, indent=2))
