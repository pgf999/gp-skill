"""Paper trading ledger tests. Uses a temp data dir so production
portfolio isn't touched.
"""
import json
import os
import shutil
from pathlib import Path

import pytest

# Starting cash for every test. Must be big enough for the largest buy in
# the suite: 200 shares of 600519 at 1700 ≈ 340k, plus headroom.
STARTING_CASH = 1_000_000


# Point DATA_DIR at a temp location BEFORE importing paper_trading
@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    from scripts import _common
    monkeypatch.setattr(_common, "DATA_DIR", tmp_path)
    # Also patch module-level constants in paper_trading
    from scripts import paper_trading
    monkeypatch.setattr(paper_trading, "DATA_DIR", tmp_path)
    monkeypatch.setattr(paper_trading, "PORTFOLIO_FILE",
                        tmp_path / "portfolio.json")
    monkeypatch.setattr(paper_trading, "TRADE_LOG",
                        tmp_path / "trade_log.jsonl")
    # Pre-seed portfolio.json so _load_portfolio doesn't fall back to
    # risk.yaml (which caps starting cash at 100k — too small for 600519 lots).
    seed = {
        "cash": STARTING_CASH,
        "initial_capital": STARTING_CASH,
        "positions": {},
        "created_at": "2026-04-22T00:00:00",
    }
    (tmp_path / "portfolio.json").write_text(
        json.dumps(seed, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    yield
    # cleanup happens via tmp_path fixture


CFG = {
    "account": {"total_capital": STARTING_CASH},
    "fees": {
        "commission_rate": 0.00025, "commission_min": 5.0,
        "stamp_tax_rate": 0.0005, "transfer_fee_rate": 0.00001,
    },
}


def test_initial_portfolio_has_cash():
    from scripts import paper_trading
    p = paper_trading.get_portfolio()
    assert p["cash"] > 0
    assert p["positions"] == {}


def test_buy_deducts_cash_and_records_position():
    from scripts import paper_trading
    result = paper_trading.execute_buy("600519", price=1600.0, qty=100,
                                      config=CFG)
    assert result["ok"], result
    p = paper_trading.get_portfolio()
    assert "600519" in p["positions"]
    assert p["positions"]["600519"]["qty"] == 100
    # Cash went down by ~cost+fee (commission ≥ 5, SH transfer fee ~1.6)
    assert p["cash"] < STARTING_CASH
    assert STARTING_CASH - p["cash"] >= 160_000  # at least the raw cost


def test_t_plus_1_blocks_same_day_sell():
    from scripts import paper_trading
    paper_trading.execute_buy("600519", price=1600.0, qty=100, config=CFG)
    result = paper_trading.execute_sell("600519", price=1650.0, qty=100,
                                        config=CFG)
    assert not result["ok"]
    assert "T+1" in result["error"]


def test_t_plus_1_released_after_reset():
    from scripts import paper_trading
    paper_trading.execute_buy("600519", price=1600.0, qty=100, config=CFG)
    paper_trading.reset_today_buys()
    result = paper_trading.execute_sell("600519", price=1650.0, qty=100,
                                        config=CFG)
    assert result["ok"], result


def test_buy_with_insufficient_cash_fails():
    from scripts import paper_trading
    result = paper_trading.execute_buy("600519", price=1000.0, qty=100_000,
                                      config=CFG)
    assert not result["ok"]


def test_buy_with_invalid_qty_fails():
    from scripts import paper_trading
    result = paper_trading.execute_buy("600519", price=100.0, qty=50,
                                      config=CFG)
    assert not result["ok"]


def test_sell_all_closes_position():
    from scripts import paper_trading
    paper_trading.execute_buy("600519", price=1600.0, qty=100, config=CFG)
    paper_trading.reset_today_buys()
    r = paper_trading.execute_sell("600519", price=1650.0, qty=100, config=CFG)
    assert r["ok"]
    p = paper_trading.get_portfolio()
    assert "600519" not in p["positions"]


def test_partial_sell_updates_position():
    from scripts import paper_trading
    paper_trading.execute_buy("600519", price=1600.0, qty=200, config=CFG)
    paper_trading.reset_today_buys()
    r = paper_trading.execute_sell("600519", price=1620.0, qty=100, config=CFG)
    assert r["ok"]
    p = paper_trading.get_portfolio()
    assert p["positions"]["600519"]["qty"] == 100


def test_avg_cost_updated_on_multiple_buys():
    from scripts import paper_trading
    paper_trading.execute_buy("600519", price=1500.0, qty=100, config=CFG)
    paper_trading.execute_buy("600519", price=1700.0, qty=100, config=CFG)
    p = paper_trading.get_portfolio()
    pos = p["positions"]["600519"]
    assert pos["qty"] == 200
    # Avg cost should be between the two prices (near 1600, ignoring fees)
    assert 1590 <= pos["avg_cost"] <= 1610


def test_trade_log_appended():
    from scripts import paper_trading
    paper_trading.execute_buy("600519", price=1600.0, qty=100, config=CFG)
    assert paper_trading.TRADE_LOG.exists()
    lines = paper_trading.TRADE_LOG.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["action"] == "buy"
    assert rec["symbol"] == "600519"


def test_today_trade_count():
    from scripts import paper_trading
    assert paper_trading.today_trade_count() == 0
    paper_trading.execute_buy("600519", price=1600.0, qty=100, config=CFG)
    assert paper_trading.today_trade_count() == 1
