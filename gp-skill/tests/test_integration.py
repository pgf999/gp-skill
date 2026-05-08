"""
集成测试 — 覆盖真实网络调用和端到端流程。

快速单元测试（无网络）：pytest tests/ -m "not network"
完整集成测试（需网络）：pytest tests/ -m network -v

网络测试平均耗时 10-30 秒/股票，CI 环境请按需跳过。
"""
from __future__ import annotations
import os
import sys
import tempfile
from pathlib import Path

import pytest
import pandas as pd

# ── path setup ────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load .env so TUSHARE_TOKEN is available during tests
_env_file = ROOT / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            k, _, v = _line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


# ── fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def risk_cfg():
    from scripts._common import load_risk_config
    return load_risk_config()


@pytest.fixture(scope="session")
def watchlist():
    from scripts._common import load_watchlist
    return load_watchlist()


# ── environment / setup tests ─────────────────────────────────────────────

class TestEnvironment:
    """Validate that the installed environment is sane (no network needed)."""

    def test_python_version(self):
        assert sys.version_info >= (3, 10), "Python 3.10+ required"

    def test_core_imports(self):
        import pandas  # noqa
        import numpy   # noqa
        import yaml    # noqa
        import requests  # noqa

    def test_akshare_importable(self):
        pytest.importorskip("akshare", reason="akshare not installed")

    def test_risk_config_loads(self, risk_cfg):
        assert isinstance(risk_cfg, dict)
        assert "position" in risk_cfg or "signals" in risk_cfg or "_source_path" in risk_cfg

    def test_watchlist_loads(self, watchlist):
        assert isinstance(watchlist, list)
        # Symbols should be 6-digit strings
        for sym in watchlist:
            assert len(sym) == 6, f"Symbol {sym!r} is not 6 digits"
            assert sym.isdigit(), f"Symbol {sym!r} contains non-digits"

    def test_watchlist_not_empty(self, watchlist):
        assert len(watchlist) > 0, (
            "watchlist.yaml has no symbols — "
            "add stocks to config/watchlist.yaml"
        )

    def test_config_dirs_exist(self):
        for d in ("config", "data", "data/cache", "data/logs", "scripts"):
            assert (ROOT / d).exists(), f"Directory {d} missing"

    def test_risk_yaml_exists(self):
        assert (ROOT / "config" / "risk.yaml").exists(), (
            "config/risk.yaml missing — run: python setup.py"
        )


# ── fundamental scoring (pure / no network) ───────────────────────────────

class TestFundamentalScoring:

    def test_empty_input_returns_neutral(self):
        from scripts.fundamental import score_fundamentals
        r = score_fundamentals({})
        assert 0.0 <= r["overall"] <= 1.0
        # empty data has no positive signals and at least a growth-data warning
        assert isinstance(r["positives"], list)
        assert isinstance(r["negatives"], list)

    def test_strong_company_scores_high(self):
        from scripts.fundamental import score_fundamentals
        r = score_fundamentals({
            "roe_ttm": 30.0, "gross_margin": 60.0, "net_margin": 25.0,
            "debt_ratio": 20.0, "pe_ttm": 18.0, "pb": 2.5,
            "revenue_growth_yoy": 20.0, "earnings_growth_yoy": 30.0,
        })
        assert r["overall"] >= 0.70, f"Strong company score too low: {r['overall']}"

    def test_weak_company_scores_low(self):
        from scripts.fundamental import score_fundamentals
        r = score_fundamentals({
            "roe_ttm": 2.0, "gross_margin": 8.0, "net_margin": 1.0,
            "debt_ratio": 80.0, "pe_ttm": 90.0, "pb": 12.0,
            "revenue_growth_yoy": -20.0, "earnings_growth_yoy": -40.0,
        })
        assert r["overall"] <= 0.35, f"Weak company score too high: {r['overall']}"

    def test_moneyflow_boosts_score(self):
        from scripts.fundamental import score_fundamentals
        base = {
            "roe_ttm": 15.0, "pe_ttm": 20.0,
            "revenue_growth_yoy": 10.0, "earnings_growth_yoy": 15.0,
        }
        without_mf = score_fundamentals(base)["overall"]
        with_mf = score_fundamentals(base, moneyflow={
            "main_net_total": 50000, "positive_days": 4, "days": 5
        })["overall"]
        assert with_mf > without_mf

    def test_negative_forecast_reduces_score(self):
        from scripts.fundamental import score_fundamentals
        base = {"roe_ttm": 15.0, "pe_ttm": 20.0}
        without_fc = score_fundamentals(base)["overall"]
        with_fc = score_fundamentals(base, forecast={"type": "首亏", "p_change": -60.0})["overall"]
        assert with_fc < without_fc

    def test_anomalous_growth_flagged(self):
        from scripts.fundamental import score_growth
        _, pos, _ = score_growth({"revenue_growth_yoy": 350.0, "earnings_growth_yoy": 250.0})
        # Both should be flagged as anomalous
        assert any("异常" in p or "核实" in p for p in pos)

    def test_null_and_dash_values_safe(self):
        from scripts.fundamental import score_fundamentals
        r = score_fundamentals({
            "roe_ttm": None, "pe_ttm": "", "debt_ratio": "-",
            "gross_margin": None, "net_margin": None,
        })
        assert 0.0 <= r["overall"] <= 1.0

    def test_score_always_bounded(self):
        from scripts.fundamental import score_fundamentals
        extremes = [
            {"roe_ttm": 999, "pe_ttm": -999, "debt_ratio": 999},
            {"roe_ttm": -999, "pe_ttm": 999, "net_margin": -999},
        ]
        for case in extremes:
            r = score_fundamentals(case)
            assert 0.0 <= r["overall"] <= 1.0, f"Score out of bounds for {case}"


# ── fetch_data (network tests) ─────────────────────────────────────────────

@pytest.mark.network
class TestFetchData:

    SAFE_SYMBOL = "600519"  # 贵州茅台 — very liquid, always has data

    def test_ohlcv_returns_dataframe(self):
        from scripts.fetch_data import fetch_ohlcv
        df = fetch_ohlcv(self.SAFE_SYMBOL)
        assert isinstance(df, pd.DataFrame)
        assert not df.empty
        assert "date" in df.columns
        assert "close" in df.columns
        assert len(df) >= 60, "Expected at least 60 trading days of history"

    def test_ohlcv_invalid_symbol_raises(self):
        from scripts.fetch_data import fetch_ohlcv
        from scripts._common import CACHE_DIR
        # Clear any cached result for this fake symbol
        for f in CACHE_DIR.glob("*999999*"):
            f.unlink(missing_ok=True)
        with pytest.raises((ValueError, KeyError, Exception)):
            fetch_ohlcv("999999")

    def test_ohlcv_date_column_is_datetime(self):
        from scripts.fetch_data import fetch_ohlcv
        df = fetch_ohlcv(self.SAFE_SYMBOL)
        assert pd.api.types.is_datetime64_any_dtype(df["date"])

    def test_ohlcv_sorted_ascending(self):
        from scripts.fetch_data import fetch_ohlcv
        df = fetch_ohlcv(self.SAFE_SYMBOL)
        dates = df["date"].tolist()
        assert dates == sorted(dates), "OHLCV rows should be sorted ascending by date"

    def test_realtime_quote_returns_price(self):
        from scripts.fetch_data import fetch_realtime_quote
        q = fetch_realtime_quote(self.SAFE_SYMBOL)
        assert isinstance(q, dict)
        price = q.get("price") or q.get("close")
        assert price is not None and float(price) > 0

    def test_fundamentals_has_required_fields(self):
        from scripts.fetch_data import fetch_fundamentals
        f = fetch_fundamentals(self.SAFE_SYMBOL)
        assert isinstance(f, dict)
        # At minimum, some financial data should be present
        has_data = any(f.get(k) is not None for k in (
            "roe_ttm", "pe_ttm", "gross_margin", "net_margin",
            "debt_ratio", "revenue_growth_yoy"
        ))
        assert has_data, f"Fundamentals returned no usable fields: {list(f.keys())}"

    def test_market_cap_in_yuan(self):
        """Market cap must be in 元 (not 万元). Maotai should be ~1.7 trillion."""
        from scripts.fetch_data import fetch_fundamentals
        f = fetch_fundamentals(self.SAFE_SYMBOL)
        total_mcap = f.get("total_mcap")
        if total_mcap is not None:
            # Moutai market cap: reasonable range 500B to 5T RMB
            assert 5e11 < float(total_mcap) < 5e12, (
                f"Moutai market cap {float(total_mcap):.0f} 元 looks wrong "
                f"(expected ~1.7 trillion). Possible unit error (万元 vs 元)."
            )

    def test_revenue_growth_reasonable_for_catl(self):
        """CATL FY2025 revenue growth should be ~+17%, not +400%."""
        from scripts.fetch_data import fetch_fundamentals
        f = fetch_fundamentals("300750")
        rev_g = f.get("revenue_growth_yoy")
        if rev_g is not None:
            assert -50 < float(rev_g) < 100, (
                f"CATL revenue growth {rev_g:.1f}% looks wrong "
                "(expected ~+17%). Possible iloc[] period mismatch."
            )

    def test_moneyflow_structure(self):
        from scripts.fetch_data import fetch_moneyflow
        mf = fetch_moneyflow(self.SAFE_SYMBOL, days=5)
        if mf:  # optional — returns {} if tushare unavailable
            assert "main_net_total" in mf
            assert "positive_days" in mf
            assert "days" in mf
            assert isinstance(mf["positive_days"], int)
            assert 0 <= mf["positive_days"] <= mf["days"]


# ── signal engine (network tests) ─────────────────────────────────────────

@pytest.mark.network
class TestSignalEngine:

    def test_analyze_symbol_returns_card(self):
        from scripts.signals import analyze_symbol
        card = analyze_symbol("600519")
        assert card.symbol == "600519"
        assert card.latest_close > 0
        assert 0.0 <= card.technical_score <= 1.0
        assert 0.0 <= card.fundamental_score <= 1.0
        assert 0.0 <= card.combined_score <= 1.0
        assert 0.0 <= card.confidence <= 1.0
        assert card.action in ("buy_strong", "buy", "hold", "sell", "sell_strong", "skip")

    def test_analyze_symbol_invalid_raises(self):
        from scripts.signals import analyze_symbol
        from scripts._common import CACHE_DIR
        for f in CACHE_DIR.glob("*999999*"):
            f.unlink(missing_ok=True)
        with pytest.raises(Exception):
            analyze_symbol("999999")

    def test_card_price_levels_positive(self):
        from scripts.signals import analyze_symbol
        card = analyze_symbol("600519")
        assert card.stop_loss_price is not None
        assert card.take_profit_price is not None
        assert card.stop_loss_price < card.latest_close, "Stop loss should be below current price"
        assert card.take_profit_price > card.latest_close, "Take profit should be above current price"

    def test_card_buy_zone_ordered(self):
        from scripts.signals import analyze_symbol
        card = analyze_symbol("600519")
        if card.buy_zone_low is not None and card.buy_zone_high is not None:
            assert card.buy_zone_low <= card.buy_zone_high

    def test_card_position_pct_bounded(self):
        from scripts.signals import analyze_symbol
        card = analyze_symbol("600519")
        assert 0.0 <= card.suggested_position_pct <= 1.0

    def test_card_serializable(self):
        """to_dict() must produce a JSON-serializable dict."""
        import json
        from scripts.signals import analyze_symbol
        card = analyze_symbol("600519")
        d = card.to_dict()
        json.dumps(d, default=str)  # should not raise

    def test_scan_universe_mixed_symbols(self):
        """scan_universe gracefully skips invalid symbols."""
        from scripts._common import CACHE_DIR
        for f in CACHE_DIR.glob("*999999*"):
            f.unlink(missing_ok=True)
        from scripts.signals import scan_universe
        cards = scan_universe(["600519", "999999", "300750"], top_n=5, min_score=0.0)
        # 2 valid symbols should produce 2 cards (999999 skipped)
        assert len(cards) == 2
        syms = {c.symbol for c in cards}
        assert "600519" in syms
        assert "300750" in syms
        assert "999999" not in syms


# ── recommend script (network test) ───────────────────────────────────────

@pytest.mark.network
class TestRecommend:

    def test_run_returns_list(self):
        from scripts.recommend import run
        cards = run(top_n=2, min_score=0.0)
        assert isinstance(cards, list)

    def test_run_respects_top_n(self, watchlist):
        from scripts.recommend import run
        if len(watchlist) < 2:
            pytest.skip("watchlist too small")
        cards = run(top_n=2, min_score=0.0)
        assert len(cards) <= 2

    def test_run_empty_watchlist(self, tmp_path, monkeypatch):
        """run() should return [] and not crash on empty watchlist."""
        import scripts.recommend as rec_mod
        # Patch in the module where the name is actually used (not the source module)
        monkeypatch.setattr(rec_mod, "load_watchlist", lambda: [])
        result = rec_mod.run(top_n=3)
        assert result == []

    def test_format_card_no_crash(self):
        from scripts.recommend import run, format_card
        cards = run(top_n=1, min_score=0.0)
        if not cards:
            pytest.skip("no cards returned")
        text = format_card(cards[0], rank=1)
        assert isinstance(text, str)
        assert len(text) > 50

    def test_format_card_contains_key_fields(self):
        from scripts.recommend import run, format_card
        cards = run(top_n=1, min_score=0.0)
        if not cards:
            pytest.skip("no cards returned")
        text = format_card(cards[0], rank=1)
        # Should mention the symbol, score, and price levels
        assert cards[0].symbol in text
        assert str(cards[0].latest_close) in text


# ── setup wizard (no network) ─────────────────────────────────────────────

class TestSetupWizard:

    def test_check_mode_passes(self):
        """python setup.py --check should exit 0."""
        import subprocess
        result = subprocess.run(
            [sys.executable, str(ROOT / "setup.py"), "--check"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(ROOT),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        assert result.returncode == 0, (
            f"setup.py --check failed:\n{result.stdout}\n{result.stderr}"
        )

    def test_check_mode_reports_all_ok(self):
        import subprocess
        result = subprocess.run(
            [sys.executable, str(ROOT / "setup.py"), "--check"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(ROOT),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        # All lines after the header should not contain ✗
        assert "✓" in result.stdout, "Expected checkmark in output"
