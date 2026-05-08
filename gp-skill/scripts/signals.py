"""Signal engine: combine technical + fundamental into an actionable
recommendation.

The LLM (via SKILL.md) is expected to call `analyze_symbol(symbol)` and
then render the returned dict as a human-friendly analysis card. The LLM
should ADD qualitative context around these numbers, not replace them.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any

import pandas as pd

from . import fetch_data, technical, fundamental
from ._common import log, normalize_symbol, load_risk_config


@dataclass
class SignalCard:
    symbol: str
    name: str
    latest_close: float
    latest_date: str
    # Scores (0-1)
    technical_score: float
    fundamental_score: float
    combined_score: float
    confidence: float         # 0-1, how much we trust the inputs
    # Actionable
    action: str               # "buy_strong" | "buy" | "hold" | "sell" | "sell_strong" | "skip"
    buy_zone_low: float | None
    buy_zone_high: float | None
    stop_loss_price: float | None
    take_profit_price: float | None
    suggested_position_pct: float  # fraction of account equity
    holding_horizon: str      # "short" | "medium" | "long"
    # Transparency
    positives: list[str] = field(default_factory=list)
    negatives: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Meta-advisory: surfaces non-obvious judgment calls the raw score misses.
    # Example: fundamentals strong but technicals weak (classic left-side
    # value setup) — a pure 'hold' verdict would strip out that nuance, so
    # we write it here for the LLM to carry into the analysis card.
    advisory: list[str] = field(default_factory=list)
    # Raw inputs for the LLM to quote
    technical_snapshot: dict[str, Any] | None = None
    fundamentals: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_WEIGHTS_DEFAULT = {"technical": 0.45, "fundamental": 0.55}


def _combined_weights(cfg: dict) -> dict:
    w = cfg.get("signals", {}).get("weights", {}) or {}
    return {**_WEIGHTS_DEFAULT, **w}


def _action_from_score(score: float, cfg: dict) -> str:
    sig = cfg.get("signals", {}) or {}
    strong_buy = float(sig.get("strong_buy_threshold", 0.75))
    buy = float(sig.get("buy_threshold", 0.60))
    sell = float(sig.get("sell_threshold", 0.40))
    strong_sell = float(sig.get("strong_sell_threshold", 0.25))
    if score >= strong_buy:
        return "buy_strong"
    if score >= buy:
        return "buy"
    if score <= strong_sell:
        return "sell_strong"
    if score <= sell:
        return "sell"
    return "hold"


def _estimate_confidence(snap, fund: dict, n_bars: int) -> float:
    """Low confidence when inputs are thin."""
    c = 1.0
    if n_bars < 60:
        c -= 0.3
    elif n_bars < 120:
        c -= 0.1
    if not fund or "roe_ttm" not in fund:
        c -= 0.2
    if abs(snap.rsi14 - 50) > 30:  # extreme readings can be noisy
        c -= 0.1
    return max(0.1, min(1.0, c))


def _derive_price_levels(snap: technical.TechnicalSnapshot,
                        fund_score: float, cfg: dict) -> dict:
    close = snap.latest_close
    atr_val = snap.atr14 or (close * 0.02)

    # Buy zone: between MA20 and latest close (if trend is up)
    buy_low = min(snap.ma20, close * 0.98)
    buy_high = close

    # Stop loss: max(ATR*2 drawdown, configured absolute SL)
    sl_cfg = cfg.get("stop_loss", {})
    abs_sl = abs(float(sl_cfg.get("absolute_stop_loss", -0.05)))
    sl_pct_based = close * (1 - abs_sl)
    sl_atr_based = close - 2 * atr_val
    stop_loss = min(sl_pct_based, sl_atr_based)  # wider (more lenient)

    # Take profit: configured
    tp_ratio = float(sl_cfg.get("take_profit", 0.20))
    take_profit = close * (1 + tp_ratio)

    return {
        "buy_zone_low": round(buy_low, 2),
        "buy_zone_high": round(buy_high, 2),
        "stop_loss_price": round(stop_loss, 2),
        "take_profit_price": round(take_profit, 2),
    }


def _suggest_position_pct(action: str, score: float, cfg: dict) -> float:
    pos_cfg = cfg.get("position", {})
    cap = float(pos_cfg.get("max_single_stock_ratio", 0.2))
    if action == "buy_strong":
        return cap
    if action == "buy":
        return cap * 0.6
    return 0.0


def _holding_horizon(fund_score: float, trend_strength: float) -> str:
    if fund_score >= 0.65 and trend_strength >= 0.4:
        return "long"    # >= 6 months
    if trend_strength >= 0.3:
        return "medium"  # 1-6 months
    return "short"       # < 1 month


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------
def analyze_symbol(symbol: str, *, config: dict | None = None,
                   lookback_days: int = 250) -> SignalCard:
    """Run the full analysis pipeline on one symbol.

    Raises ValueError if the symbol has insufficient data.
    """
    symbol = normalize_symbol(symbol)
    cfg = config or load_risk_config()

    # 1. Data
    df = fetch_data.fetch_ohlcv(symbol, period="daily")
    if df.empty:
        raise ValueError(f"No OHLCV data for {symbol}")
    if len(df) > lookback_days:
        df = df.tail(lookback_days)

    quote = fetch_data.fetch_realtime_quote(symbol)
    info = fetch_data.fetch_stock_info(symbol)
    fund = fetch_data.fetch_fundamentals(symbol)
    # Merge spot fields into fundamentals for scoring
    for k in ("pe_ttm", "pb", "total_mcap", "float_mcap"):
        if k in quote and k not in fund:
            fund[k] = quote[k]

    # Capital flow + earnings guidance (tushare only; absent = empty dict)
    mf_data = fetch_data.fetch_moneyflow(symbol) or {}
    fc_data = fetch_data.fetch_forecast(symbol) or {}
    nb_data = fetch_data.fetch_northbound_flow() or {}

    # 2. Technical snapshot + score
    snap = technical.snapshot(df, symbol)
    t_score, t_pos, t_neg = technical.technical_score(snap)

    # 3. Fundamental score (includes moneyflow + forecast when available)
    f_result = fundamental.score_fundamentals(
        fund,
        moneyflow=mf_data if mf_data else None,
        forecast=fc_data if fc_data else None,
    )
    f_score = f_result["overall"]

    # 4. Combined
    w = _combined_weights(cfg)
    combined = w["technical"] * t_score + w["fundamental"] * f_score

    action = _action_from_score(combined, cfg)
    confidence = _estimate_confidence(snap, fund, len(df))
    levels = _derive_price_levels(snap, f_score, cfg)
    pos_pct = _suggest_position_pct(action, combined, cfg)
    horizon = _holding_horizon(f_score, snap.trend_strength)

    # Collect warnings (structural stuff the LLM should flag)
    warnings: list[str] = []
    name = fund.get("name") or info.get("name") or quote.get("name", "")
    if name and any(tag in str(name).upper() for tag in ("ST", "*ST", "退")):
        warnings.append(f"⚠️ {name} 属于 ST/*ST/退市警示股，风险极高，不建议下单")
    if snap.consecutive_up_days >= 5:
        warnings.append(f"⚠️ 已连涨 {snap.consecutive_up_days} 天，追涨风险高")
    if snap.near_52w_high and snap.rsi14 > 70:
        warnings.append("⚠️ 接近 52 周新高且 RSI 超买，短期回调概率大")
    float_mcap = fund.get("float_mcap")
    if float_mcap and float(float_mcap) < 5e9:  # < 50 亿
        warnings.append(f"⚠️ 流通市值 {float(float_mcap)/1e8:.1f} 亿，小盘股波动剧烈")

    # Meta-advisory: surface divergence between fundamental and technical views
    # that a single composite score otherwise erases. These are *judgment* cues,
    # not alarm bells — the LLM should carry them verbatim into its narrative.
    advisory: list[str] = []

    # Northbound capital (market-wide context, stock-agnostic)
    if nb_data:
        nb_days = nb_data.get("days", 5)
        nb_total = nb_data.get("total", 0)
        nb_pos = nb_data.get("positive_days", 0)
        nb_yi = nb_total / 10000
        if nb_total > 0 and nb_pos >= nb_days - 1:
            advisory.append(
                f"ℹ️ 北向资金近{nb_days}日持续净流入 {nb_yi:.0f}亿，外资整体偏多"
            )
        elif nb_total < 0 and nb_pos <= 1:
            advisory.append(
                f"ℹ️ 北向资金近{nb_days}日持续净流出 {abs(nb_yi):.0f}亿，外资整体偏空"
            )
    divergence = f_score - t_score
    if action == "hold" and abs(divergence) >= 0.25:
        if divergence >= 0.25:
            # Classic deep-value / left-side setup: good company, ugly chart.
            advisory.append(
                f"ℹ️ 基本面 ({f_score:.2f}) 显著优于技术面 ({t_score:.2f})："
                "估值/质量具吸引力但趋势未转。若信奉左侧分批建仓，可等待企稳信号"
                "（如 MA20 金叉、MACD 转正、缩量止跌）后轻仓试探；切勿追涨或一次满仓。"
            )
        else:
            # Momentum strong, fundamentals soft — a trading-only setup.
            advisory.append(
                f"ℹ️ 技术面 ({t_score:.2f}) 显著优于基本面 ({f_score:.2f})："
                "短线动能强但基本面支撑不足。适合作为趋势交易标的严守止损；"
                "不宜按价值投资长期持有，动能一旦转弱须及时离场。"
            )

    return SignalCard(
        symbol=symbol,
        name=name or "",
        latest_close=snap.latest_close,
        latest_date=snap.latest_date,
        technical_score=round(t_score, 3),
        fundamental_score=round(f_score, 3),
        combined_score=round(combined, 3),
        confidence=round(confidence, 3),
        action=action,
        buy_zone_low=levels["buy_zone_low"],
        buy_zone_high=levels["buy_zone_high"],
        stop_loss_price=levels["stop_loss_price"],
        take_profit_price=levels["take_profit_price"],
        suggested_position_pct=round(pos_pct, 3),
        holding_horizon=horizon,
        positives=t_pos + f_result["positives"],
        negatives=t_neg + f_result["negatives"],
        warnings=warnings,
        advisory=advisory,
        technical_snapshot=snap.to_dict(),
        fundamentals={
            **fund,
            **({"moneyflow": mf_data} if mf_data else {}),
            **({"forecast": fc_data} if fc_data else {}),
            **({"northbound": nb_data} if nb_data else {}),
        },
    )


def scan_universe(symbols: list[str], *, top_n: int = 10,
                  min_score: float = 0.55,
                  config: dict | None = None) -> list[SignalCard]:
    """Batch-evaluate and return top N by combined score.

    Symbols that fail to analyze (bad code, network error, no data) are
    skipped with a WARNING log. A summary is printed at the end so the
    caller can see how many symbols were successfully evaluated.
    """
    results: list[SignalCard] = []
    failed: list[tuple[str, str]] = []

    for s in symbols:
        try:
            card = analyze_symbol(s, config=config)
            if card.combined_score >= min_score:
                results.append(card)
        except Exception as e:
            log.warning("scan_universe: skipping %s — %s", s, e)
            failed.append((s, str(e)))

    if failed:
        log.warning(
            "scan_universe: %d/%d symbols failed analysis: %s",
            len(failed), len(symbols),
            ", ".join(f"{s}({msg[:40]})" for s, msg in failed),
        )

    results.sort(key=lambda x: x.combined_score, reverse=True)
    return results[:top_n]


if __name__ == "__main__":
    import sys, json
    sym = sys.argv[1] if len(sys.argv) > 1 else "600519"
    try:
        card = analyze_symbol(sym)
        print(json.dumps(card.to_dict(), ensure_ascii=False, indent=2,
                         default=str))
    except Exception as e:
        print(f"Analysis failed: {e}")
