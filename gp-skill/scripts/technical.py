"""Pure-function technical indicators. No IO, no side effects.

All functions accept a DataFrame with at least: date, open, high, low, close, volume.
Returns new columns rather than mutating.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Indicator primitives
# ---------------------------------------------------------------------------
def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n, min_periods=1).mean()


def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26,
         signal: int = 9) -> tuple[pd.Series, pd.Series, pd.Series]:
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    diff = ema_fast - ema_slow
    dea = ema(diff, signal)
    hist = (diff - dea) * 2
    return diff, dea, hist


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    avg_up = up.ewm(alpha=1 / n, adjust=False).mean()
    avg_down = down.ewm(alpha=1 / n, adjust=False).mean()
    rs = avg_up / avg_down.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)


def kdj(df: pd.DataFrame, n: int = 9, m1: int = 3,
        m2: int = 3) -> tuple[pd.Series, pd.Series, pd.Series]:
    low_n = df["low"].rolling(n, min_periods=1).min()
    high_n = df["high"].rolling(n, min_periods=1).max()
    rsv = (df["close"] - low_n) / (high_n - low_n).replace(0, np.nan) * 100
    rsv = rsv.fillna(50)
    k = rsv.ewm(alpha=1 / m1, adjust=False).mean()
    d = k.ewm(alpha=1 / m2, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j


def bollinger(close: pd.Series, n: int = 20,
              k: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = close.rolling(n, min_periods=1).mean()
    std = close.rolling(n, min_periods=1).std(ddof=0)
    upper = mid + k * std
    lower = mid - k * std
    return upper, mid, lower


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close_prev = df["close"].shift(1)
    tr = pd.concat([high - low,
                    (high - close_prev).abs(),
                    (low - close_prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume."""
    direction = np.sign(df["close"].diff()).fillna(0)
    return (direction * df["volume"]).cumsum()


# ---------------------------------------------------------------------------
# Composite analysis
# ---------------------------------------------------------------------------
@dataclass
class TechnicalSnapshot:
    """Structured result suitable for the signal engine to consume."""
    symbol: str
    latest_date: str
    latest_close: float
    # Trend
    ma5: float
    ma10: float
    ma20: float
    ma60: float
    ma120: float
    ma_alignment: str         # "bullish" | "bearish" | "mixed"
    trend_strength: float     # 0-1, based on ma spread
    # Momentum
    macd_diff: float
    macd_dea: float
    macd_hist: float
    macd_state: str           # "golden_cross" | "death_cross" | "above_zero" | "below_zero"
    rsi14: float
    rsi_state: str            # "oversold" | "overbought" | "neutral"
    kdj_k: float
    kdj_d: float
    kdj_j: float
    # Volatility
    boll_upper: float
    boll_mid: float
    boll_lower: float
    boll_width_pct: float     # (upper-lower)/mid, proxy for volatility
    atr14: float
    atr_pct: float            # atr/close
    # Volume
    vol_5d_avg: float
    vol_ratio: float          # today's volume / 5d avg
    volume_price_state: str   # "price_up_vol_up" etc
    # Warning flags
    consecutive_up_days: int
    consecutive_down_days: int
    near_52w_high: bool       # within 3%
    near_52w_low: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ma_alignment(closes: list[float]) -> str:
    ma5, ma10, ma20, ma60 = closes
    if ma5 > ma10 > ma20 > ma60:
        return "bullish"
    if ma5 < ma10 < ma20 < ma60:
        return "bearish"
    return "mixed"


def _rsi_state(value: float) -> str:
    if value <= 30:
        return "oversold"
    if value >= 70:
        return "overbought"
    return "neutral"


def _macd_state(diff: float, dea: float, diff_prev: float,
                dea_prev: float) -> str:
    if diff > dea and diff_prev <= dea_prev:
        return "golden_cross"
    if diff < dea and diff_prev >= dea_prev:
        return "death_cross"
    return "above_zero" if diff > 0 else "below_zero"


def _volume_price_state(price_up: bool, vol_up: bool) -> str:
    return f"price_{'up' if price_up else 'down'}_vol_{'up' if vol_up else 'down'}"


def _consecutive_days(closes: pd.Series, up: bool) -> int:
    diff = closes.diff()
    count = 0
    for v in diff.iloc[::-1]:
        if pd.isna(v):
            break
        if (up and v > 0) or (not up and v < 0):
            count += 1
        else:
            break
    return count


def snapshot(df: pd.DataFrame, symbol: str) -> TechnicalSnapshot:
    """Compute everything the signal engine needs in one pass."""
    if df.empty or len(df) < 20:
        raise ValueError(f"Need at least 20 bars for snapshot, got {len(df)}")

    # All the heavy lifting on a sorted copy
    d = df.sort_values("date").reset_index(drop=True).copy()
    close = d["close"]

    d["ma5"] = sma(close, 5)
    d["ma10"] = sma(close, 10)
    d["ma20"] = sma(close, 20)
    d["ma60"] = sma(close, 60)
    d["ma120"] = sma(close, 120)

    diff_, dea_, hist_ = macd(close)
    d["macd_diff"], d["macd_dea"], d["macd_hist"] = diff_, dea_, hist_

    d["rsi14"] = rsi(close, 14)
    k_, dk_, j_ = kdj(d)
    d["kdj_k"], d["kdj_d"], d["kdj_j"] = k_, dk_, j_

    up, mid, low = bollinger(close)
    d["boll_upper"], d["boll_mid"], d["boll_lower"] = up, mid, low

    d["atr14"] = atr(d, 14)

    d["vol_5d_avg"] = d["volume"].rolling(5, min_periods=1).mean()

    # 52-week proxy: last 240 trading days
    lookback = d.tail(240)
    high_52w = lookback["high"].max()
    low_52w = lookback["low"].min()

    last = d.iloc[-1]
    prev = d.iloc[-2] if len(d) >= 2 else last

    price_up = last["close"] > prev["close"]
    vol_up = last["volume"] > last["vol_5d_avg"]

    return TechnicalSnapshot(
        symbol=symbol,
        latest_date=str(last["date"])[:10],
        latest_close=float(last["close"]),
        ma5=float(last["ma5"]), ma10=float(last["ma10"]),
        ma20=float(last["ma20"]), ma60=float(last["ma60"]),
        ma120=float(last["ma120"]) if not pd.isna(last["ma120"]) else float("nan"),
        ma_alignment=_ma_alignment([last["ma5"], last["ma10"], last["ma20"], last["ma60"]]),
        trend_strength=min(1.0, abs(last["ma5"] - last["ma60"]) / last["ma60"] * 5)
            if last["ma60"] else 0.0,
        macd_diff=float(last["macd_diff"]),
        macd_dea=float(last["macd_dea"]),
        macd_hist=float(last["macd_hist"]),
        macd_state=_macd_state(last["macd_diff"], last["macd_dea"],
                               prev["macd_diff"], prev["macd_dea"]),
        rsi14=float(last["rsi14"]),
        rsi_state=_rsi_state(last["rsi14"]),
        kdj_k=float(last["kdj_k"]), kdj_d=float(last["kdj_d"]),
        kdj_j=float(last["kdj_j"]),
        boll_upper=float(last["boll_upper"]),
        boll_mid=float(last["boll_mid"]),
        boll_lower=float(last["boll_lower"]),
        boll_width_pct=float((last["boll_upper"] - last["boll_lower"])
                             / last["boll_mid"]) if last["boll_mid"] else 0.0,
        atr14=float(last["atr14"]) if not pd.isna(last["atr14"]) else 0.0,
        atr_pct=float(last["atr14"] / last["close"])
            if last["close"] and not pd.isna(last["atr14"]) else 0.0,
        vol_5d_avg=float(last["vol_5d_avg"]),
        vol_ratio=float(last["volume"] / last["vol_5d_avg"])
            if last["vol_5d_avg"] else 0.0,
        volume_price_state=_volume_price_state(price_up, vol_up),
        consecutive_up_days=_consecutive_days(close, up=True),
        consecutive_down_days=_consecutive_days(close, up=False),
        near_52w_high=bool(last["close"] >= high_52w * 0.97),
        near_52w_low=bool(last["close"] <= low_52w * 1.03),
    )


def technical_score(snap: TechnicalSnapshot) -> tuple[float, list[str], list[str]]:
    """Score 0-1 + list of positive/negative reasons.

    Kept intentionally lean; this isn't meant to replace judgment but to
    surface the most important factors so the caller (LLM or human) can
    weigh them.
    """
    score = 0.5
    pos: list[str] = []
    neg: list[str] = []

    # Trend: every branch should leave a narrative trail, including the
    # "mixed" case — a silent middle is the worst possible UX, because
    # the caller gets a 0.5 score with nothing to interpret.
    if snap.ma_alignment == "bullish":
        score += 0.15
        pos.append(f"均线多头排列 (MA5>{snap.ma10:.2f}>{snap.ma20:.2f}>{snap.ma60:.2f})")
    elif snap.ma_alignment == "bearish":
        score -= 0.15
        neg.append("均线空头排列，趋势向下")
    else:
        neg.append(f"均线方向不一 (MA5 {snap.ma5:.2f} / MA20 {snap.ma20:.2f} / "
                   f"MA60 {snap.ma60:.2f})，无明确趋势")

    # MACD — cover zero-line regime even without a cross
    if snap.macd_state == "golden_cross":
        score += 0.1
        pos.append(f"MACD金叉 (DIFF={snap.macd_diff:.3f}, DEA={snap.macd_dea:.3f})")
    elif snap.macd_state == "death_cross":
        score -= 0.1
        neg.append("MACD死叉，动能转弱")
    elif snap.macd_state == "above_zero":
        if snap.macd_hist > 0:
            score += 0.05
            pos.append(f"MACD 位于零轴上方且柱体为正，动能偏强")
        else:
            pos.append("MACD 位于零轴上方，多头结构未破")
    elif snap.macd_state == "below_zero":
        score -= 0.05
        neg.append(f"MACD 位于零轴下方 (DIFF={snap.macd_diff:.2f})，动能偏弱")

    # RSI
    if snap.rsi_state == "overbought":
        score -= 0.1
        neg.append(f"RSI超买 ({snap.rsi14:.1f})，短期追高风险")
    elif snap.rsi_state == "oversold":
        score += 0.05
        pos.append(f"RSI超卖 ({snap.rsi14:.1f})，可能有反弹机会")
    elif snap.rsi14 < 45:
        neg.append(f"RSI {snap.rsi14:.1f} 偏弱，买方力量不足")

    # Volume confirmation — include the "both down" case explicitly
    if snap.volume_price_state == "price_up_vol_up":
        score += 0.1
        pos.append(f"价涨量增 (量比 {snap.vol_ratio:.2f})")
    elif snap.volume_price_state == "price_up_vol_down":
        score -= 0.05
        neg.append("价涨量缩，上涨动能不足")
    elif snap.volume_price_state == "price_down_vol_up":
        score -= 0.05
        neg.append("价跌量增，抛压较重")
    elif snap.volume_price_state == "price_down_vol_down":
        # Neutral-positive: fading sellers, but no bottom confirmation yet
        neg.append(f"缩量下跌 (量比 {snap.vol_ratio:.2f})，抛压趋缓但尚无反转信号")

    # Consecutive days
    if snap.consecutive_up_days >= 5:
        score -= 0.1
        neg.append(f"已连涨 {snap.consecutive_up_days} 天，追涨风险高")
    if snap.consecutive_down_days >= 5:
        pos.append(f"已连跌 {snap.consecutive_down_days} 天，或接近超跌")

    # Trend strength — useful narrative even when middling
    if snap.trend_strength >= 0.4:
        pos.append(f"趋势强度 {snap.trend_strength:.2f}，主升浪特征")
    elif snap.trend_strength < 0.1:
        neg.append(f"趋势强度仅 {snap.trend_strength:.2f}，行情缺乏方向")

    # 52-week proximity
    if snap.near_52w_high and snap.rsi14 > 70:
        score -= 0.05
        neg.append("接近 52 周新高且 RSI 超买")
    if snap.near_52w_low and snap.macd_state == "golden_cross":
        score += 0.05
        pos.append("接近 52 周新低且 MACD 金叉，可能底部反转")

    score = max(0.0, min(1.0, score))
    return score, pos, neg


if __name__ == "__main__":
    # smoke test with synthetic data
    import datetime
    dates = pd.date_range("2024-01-01", periods=120)
    rng = np.random.default_rng(42)
    closes = 100 + np.cumsum(rng.standard_normal(120) * 0.5)
    df = pd.DataFrame({
        "date": dates,
        "open": closes - 0.5, "high": closes + 1, "low": closes - 1,
        "close": closes, "volume": rng.integers(1e6, 5e6, 120),
    })
    snap = snapshot(df, "TEST")
    sc, pos, neg = technical_score(snap)
    print(f"Score: {sc:.3f}")
    print("Positive:", pos)
    print("Negative:", neg)
