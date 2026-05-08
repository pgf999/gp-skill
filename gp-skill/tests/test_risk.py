"""Risk gate tests. These are the MOST IMPORTANT tests in the suite,
because if risk breaks, real money can be lost.
"""
import datetime as dt

import pytest

from scripts import risk


CFG = {
    "account": {
        "total_capital": 100_000,
        "reserved_cash": 10_000,
        "max_single_trade": 10_000,
    },
    "position": {
        "max_single_stock_ratio": 0.10,
        "max_total_position_ratio": 0.50,
    },
    "trade_limits": {
        "max_daily_trades": 2,
        "cooldown_minutes": 30,
        "max_consecutive_up_days_allowed": 3,
    },
    "stop_loss": {
        "absolute_stop_loss": -0.05,
        "trailing_stop_loss": -0.08,
        "take_profit": 0.15,
    },
    "fees": {},
}


def _noon():
    # During trading hours so call-auction block doesn't kick in
    return dt.datetime.now().replace(hour=13, minute=30, second=0)


def _base_buy_kwargs(**over):
    base = dict(
        account_cash=100_000, account_equity=100_000,
        current_position_value=0, total_position_value=0,
        today_trade_count=0, last_trade_on_symbol_minutes_ago=None,
        quote={"pct_chg": 1.0, "name": "测试股"},
        stock_info={"name": "测试股"},
        consecutive_up_days=0,
        config=CFG, now=_noon(),
    )
    base.update(over)
    return base


# --- Hard rules -----------------------------------------------------------
def test_buy_rejected_on_st_name():
    d = risk.preview_buy("600001", price=10.0,
                        **_base_buy_kwargs(quote={"pct_chg": 1, "name": "*ST 某某"},
                                           stock_info={"name": "*ST 某某"}))
    assert not d.ok
    assert "st_blacklist" in d.reason_codes


def test_buy_rejected_on_limit_up():
    d = risk.preview_buy("600001", price=10.0,
                        **_base_buy_kwargs(
                            quote={"pct_chg": 9.95, "name": "某某股"}))
    assert not d.ok
    assert "limit_up" in d.reason_codes


def test_buy_rejected_during_call_auction():
    t = dt.datetime.now().replace(hour=9, minute=20)
    d = risk.preview_buy("600001", price=10.0,
                        **_base_buy_kwargs(now=t))
    assert not d.ok
    assert "call_auction" in d.reason_codes


def test_chinext_20pct_limit_respected():
    # 创业板股，涨幅 19.95% 应该也被拦
    d = risk.preview_buy("300001", price=10.0,
                        **_base_buy_kwargs(
                            quote={"pct_chg": 19.95, "name": "创业板股"}))
    assert not d.ok
    assert "limit_up" in d.reason_codes


def test_sell_rejected_on_limit_down():
    d = risk.preview_sell("600001", price=9.0,
                         holding_qty=100, avg_cost=10.0,
                         quote={"pct_chg": -9.95, "name": "某某股"},
                         stock_info={"name": "某某股"},
                         config=CFG, now=_noon())
    assert not d.ok
    assert "limit_down" in d.reason_codes


# --- Soft rules -----------------------------------------------------------
def test_buy_rejected_on_daily_limit():
    d = risk.preview_buy("600001", price=10.0,
                        **_base_buy_kwargs(today_trade_count=2))
    assert not d.ok
    assert "daily_trade_limit" in d.reason_codes


def test_buy_rejected_on_cooldown():
    d = risk.preview_buy("600001", price=10.0,
                        **_base_buy_kwargs(
                            last_trade_on_symbol_minutes_ago=5))
    assert not d.ok
    assert "cooldown" in d.reason_codes


def test_buy_rejected_on_chasing_up():
    d = risk.preview_buy("600001", price=10.0,
                        **_base_buy_kwargs(consecutive_up_days=5))
    assert not d.ok
    assert "chasing_up" in d.reason_codes


def test_buy_rejected_on_insufficient_cash():
    d = risk.preview_buy("600001", price=100.0,
                        **_base_buy_kwargs(account_cash=5_000))
    assert not d.ok
    assert "insufficient_cash" in d.reason_codes


def test_buy_respects_single_stock_cap():
    # Already have 10_000 in position, cap is 10% of 100_000 = 10_000 → no room
    d = risk.preview_buy("600001", price=10.0,
                        **_base_buy_kwargs(current_position_value=10_000))
    assert not d.ok
    assert "position_cap_reached" in d.reason_codes


def test_buy_respects_total_position_cap():
    # Total pos cap = 50% = 50_000; we have 50_000 → no room
    d = risk.preview_buy("600001", price=10.0,
                        **_base_buy_kwargs(total_position_value=50_000))
    assert not d.ok
    assert "total_cap_reached" in d.reason_codes


# --- Happy path -----------------------------------------------------------
def test_buy_ok_within_constraints():
    d = risk.preview_buy("600001", price=10.0,
                        **_base_buy_kwargs())
    assert d.ok
    assert d.suggested_qty > 0
    assert d.suggested_qty % 100 == 0
    # 10k single_trade cap at 10 yuan → 1000 shares max
    assert d.suggested_qty == 1000


def test_sell_ok_with_position():
    d = risk.preview_sell("600001", price=12.0,
                         holding_qty=100, avg_cost=10.0,
                         quote={"pct_chg": 2, "name": "某某股"},
                         stock_info={"name": "某某股"},
                         config=CFG, now=_noon())
    assert d.ok


def test_sell_rejected_without_position():
    d = risk.preview_sell("600001", price=12.0,
                         holding_qty=0, avg_cost=0,
                         quote={"pct_chg": 2},
                         config=CFG, now=_noon())
    assert not d.ok
    assert "no_position" in d.reason_codes


# --- Stop conditions -----------------------------------------------------
def test_stop_loss_trigger():
    # bought at 10, now 9.40 → -6% drop, stop_loss_pct is -5%
    triggers = risk.check_stop_conditions(
        "600001", price=9.40, avg_cost=10.0, config=CFG,
    )
    types = [t["type"] for t in triggers]
    assert "stop_loss" in types


def test_take_profit_trigger():
    # bought at 10, now 11.60 → +16%, take_profit is 15%
    triggers = risk.check_stop_conditions(
        "600001", price=11.60, avg_cost=10.0, config=CFG,
    )
    types = [t["type"] for t in triggers]
    assert "take_profit" in types


def test_trailing_stop_trigger():
    # high 12, now 11 → -8.3% drawdown, trailing is -8%
    triggers = risk.check_stop_conditions(
        "600001", price=11.0, avg_cost=10.0,
        highest_since_buy=12.0, config=CFG,
    )
    types = [t["type"] for t in triggers]
    assert "trailing_stop" in types


def test_no_trigger_when_normal():
    triggers = risk.check_stop_conditions(
        "600001", price=10.5, avg_cost=10.0,
        highest_since_buy=10.8, config=CFG,
    )
    assert triggers == []
