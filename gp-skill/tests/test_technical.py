"""Technical indicator sanity checks."""
import numpy as np
import pandas as pd
import pytest

from scripts import technical


@pytest.fixture
def synthetic_df():
    rng = np.random.default_rng(42)
    dates = pd.date_range("2024-01-01", periods=200)
    closes = 100 + np.cumsum(rng.standard_normal(200) * 0.8)
    return pd.DataFrame({
        "date": dates,
        "open": closes * 0.995,
        "high": closes * 1.015,
        "low": closes * 0.985,
        "close": closes,
        "volume": rng.integers(1e6, 5e6, 200),
    })


def test_sma_values_match_rolling_mean(synthetic_df):
    close = synthetic_df["close"]
    s = technical.sma(close, 20)
    assert len(s) == len(close)
    # At index 19 (20 bars), should equal plain mean
    expected = close.iloc[0:20].mean()
    assert abs(s.iloc[19] - expected) < 1e-9


def test_macd_output_shape(synthetic_df):
    diff, dea, hist = technical.macd(synthetic_df["close"])
    assert len(diff) == len(dea) == len(hist) == len(synthetic_df)


def test_rsi_bounded(synthetic_df):
    r = technical.rsi(synthetic_df["close"])
    # Allow small float noise beyond [0,100]
    assert r.min() >= -0.001
    assert r.max() <= 100.001


def test_snapshot_extracts_all_fields(synthetic_df):
    snap = technical.snapshot(synthetic_df, "TEST")
    d = snap.to_dict()
    required = {"symbol", "latest_close", "ma5", "ma20", "macd_state",
                "rsi14", "atr14", "volume_price_state",
                "consecutive_up_days", "consecutive_down_days"}
    assert required.issubset(d.keys())


def test_score_bounded(synthetic_df):
    snap = technical.snapshot(synthetic_df, "TEST")
    s, pos, neg = technical.technical_score(snap)
    assert 0 <= s <= 1
    assert isinstance(pos, list) and isinstance(neg, list)


def test_snapshot_fails_on_short_history():
    df = pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=10),
        "open": [10]*10, "high": [10]*10, "low": [10]*10,
        "close": [10]*10, "volume": [1000]*10,
    })
    with pytest.raises(ValueError):
        technical.snapshot(df, "TEST")


def test_consecutive_up_days_counts_correctly():
    # close transitions: NaN, -1, +1, +1, +1, +1, +1
    # → 5 consecutive up-days at the tail (9→10→11→12→13→14)
    closes = [10, 9, 10, 11, 12, 13, 14]
    df = pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=len(closes)),
        "open": closes, "high": closes, "low": closes,
        "close": closes, "volume": [1e6] * len(closes),
    })
    # Can't call snapshot directly (needs >= 20 bars) — test the helper
    from scripts.technical import _consecutive_days
    assert _consecutive_days(df["close"], up=True) == 5
    assert _consecutive_days(df["close"], up=False) == 0
