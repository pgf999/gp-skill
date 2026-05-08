"""Fundamental scoring. Pure functions over a dict of ratios.

The scoring reflects a hybrid of Graham-style value + Lynch-style growth.
Every rule has a clear justification; adjust in config/risk.yaml if your
philosophy differs.
"""
from __future__ import annotations
from typing import Any


def _safe(d: dict, key: str, default: float | None = None) -> float | None:
    v = d.get(key)
    if v is None or v == "" or v == "-":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def score_quality(f: dict) -> tuple[float, list[str], list[str]]:
    """How 'good' is this business? ROE, margins, leverage."""
    score = 0.5
    pos: list[str] = []
    neg: list[str] = []

    roe = _safe(f, "roe_ttm")
    if roe is not None:
        if roe >= 15:
            score += 0.2; pos.append(f"ROE {roe:.1f}% 优秀 (≥15)")
        elif roe >= 10:
            score += 0.1; pos.append(f"ROE {roe:.1f}% 合格")
        elif roe < 5:
            score -= 0.15; neg.append(f"ROE {roe:.1f}% 偏低 (<5)")

    gm = _safe(f, "gross_margin")
    if gm is not None:
        if gm >= 40:
            score += 0.05; pos.append(f"毛利率 {gm:.1f}% 较高")
        elif gm < 15:
            score -= 0.05; neg.append(f"毛利率 {gm:.1f}% 较低")

    nm = _safe(f, "net_margin")
    if nm is not None:
        if nm >= 20:
            score += 0.1; pos.append(f"净利率 {nm:.1f}% 优秀 (≥20)，盈利能力强")
        elif nm >= 10:
            score += 0.03; pos.append(f"净利率 {nm:.1f}% 稳健")
        elif nm < 3:
            score -= 0.05; neg.append(f"净利率 {nm:.1f}% 偏薄")

    debt = _safe(f, "debt_ratio")
    if debt is not None:
        # Exclude financial sector where high leverage is normal
        if debt > 70:
            score -= 0.15; neg.append(f"资产负债率 {debt:.1f}% 偏高 (>70)")
        elif debt < 40:
            score += 0.05; pos.append(f"资产负债率 {debt:.1f}% 健康 (<40)")

    score = max(0.0, min(1.0, score))
    return score, pos, neg


def score_valuation(f: dict) -> tuple[float, list[str], list[str]]:
    """Cheap or expensive? Lower PE/PB = higher score, within reason."""
    score = 0.5
    pos: list[str] = []
    neg: list[str] = []

    pe = _safe(f, "pe_ttm") or _safe(f, "pe")
    pb = _safe(f, "pb")
    pe_pct = _safe(f, "pe_percentile_3y")
    pb_pct = _safe(f, "pb_percentile_3y")

    if pe is not None:
        if pe <= 0:
            score -= 0.1; neg.append(f"PE {pe:.1f} 为负 (亏损)")
        elif pe <= 15:
            score += 0.15; pos.append(f"PE {pe:.1f} 估值偏低")
        elif pe <= 25:
            score += 0.05; pos.append(f"PE {pe:.1f} 处于合理区间")
        elif pe <= 60:
            neg.append(f"PE {pe:.1f} 偏高，对成长预期依赖较大")
        else:
            score -= 0.15; neg.append(f"PE {pe:.1f} 估值偏高 (>60)")

    if pb is not None:
        if pb < 1.0:
            score += 0.1; pos.append(f"PB {pb:.2f} 破净")
        elif pb < 3:
            pass   # unremarkable
        elif pb > 8:
            score -= 0.1; neg.append(f"PB {pb:.2f} 偏高")
        else:
            neg.append(f"PB {pb:.2f} 偏高，需要 ROE 或护城河支撑")

    if pe_pct is not None:
        if pe_pct < 30:
            score += 0.1; pos.append(f"PE处于3年 {pe_pct:.0f}% 分位 (较低)")
        elif pe_pct > 80:
            score -= 0.1; neg.append(f"PE处于3年 {pe_pct:.0f}% 分位 (较高)")
    if pb_pct is not None:
        if pb_pct < 30:
            score += 0.05
        elif pb_pct > 80:
            score -= 0.05

    score = max(0.0, min(1.0, score))
    return score, pos, neg


def score_growth(f: dict) -> tuple[float, list[str], list[str]]:
    """Revenue + earnings trajectory."""
    score = 0.5
    pos: list[str] = []
    neg: list[str] = []

    rev_g = _safe(f, "revenue_growth_yoy")
    eps_g = _safe(f, "earnings_growth_yoy")

    # Data gap is itself a negative — user should know we're flying partly blind
    if rev_g is None and eps_g is None:
        neg.append("成长数据缺失 (AkShare 未返回同比)，建议人工查阅最新季报")
        return score, pos, neg

    if rev_g is not None:
        if rev_g > 200:
            # Extreme growth often reflects restructuring/consolidation; cap the
            # score bonus and flag it so the analyst can verify.
            score += 0.15; pos.append(f"营收同比 +{rev_g:.0f}% 高速（幅度异常，建议核实是否含并购/重组）")
        elif rev_g > 30:
            score += 0.15; pos.append(f"营收同比 +{rev_g:.1f}% 高速")
        elif rev_g > 10:
            score += 0.05; pos.append(f"营收同比 +{rev_g:.1f}% 稳健")
        elif rev_g < -10:
            score -= 0.15; neg.append(f"营收同比 {rev_g:.1f}% 明显下滑")

    if eps_g is not None:
        if eps_g > 200:
            score += 0.15; pos.append(f"净利同比 +{eps_g:.0f}% 高速（幅度异常，建议核实是否含非经常性损益）")
        elif eps_g > 50:
            score += 0.15; pos.append(f"净利同比 +{eps_g:.1f}% 高速")
        elif eps_g > 10:
            score += 0.05
        elif eps_g < -30:
            score -= 0.2; neg.append(f"净利同比 {eps_g:.1f}% 暴跌")
        elif eps_g < 0:
            score -= 0.1; neg.append(f"净利同比 {eps_g:.1f}% 下滑")

    # PEG penalty
    pe = _safe(f, "pe_ttm") or _safe(f, "pe")
    if pe and pe > 0 and eps_g and eps_g > 0:
        peg = pe / eps_g
        if peg < 1:
            score += 0.05; pos.append(f"PEG {peg:.2f} (<1) 成长估值合理")
        elif peg > 2:
            score -= 0.05; neg.append(f"PEG {peg:.2f} (>2) 成长溢价过高")

    score = max(0.0, min(1.0, score))
    return score, pos, neg


def score_moneyflow(mf: dict) -> tuple[float, list[str], list[str]]:
    """Score based on institutional money flow (主力资金净流向, N-day window).

    main_net_total > 0 means institutions net-bought; < 0 means net-sold.
    Persistence (positive_days) amplifies the signal.
    """
    score = 0.5
    pos: list[str] = []
    neg: list[str] = []

    if not mf:
        return score, pos, neg

    main_net = mf.get("main_net_total")   # 万元
    positive_days = mf.get("positive_days", 0)
    days = mf.get("days", 5)

    if main_net is None:
        return score, pos, neg

    net_yi = main_net / 10000   # 亿元

    if main_net > 0:
        if positive_days >= days - 1:
            score += 0.2
            pos.append(f"主力近{days}日持续净流入 {net_yi:.2f}亿，机构持续加仓")
        else:
            score += 0.1
            pos.append(f"主力近{days}日净流入 {net_yi:.2f}亿")
    elif main_net < 0:
        if positive_days <= 1:
            score -= 0.2
            neg.append(f"主力近{days}日持续净流出 {abs(net_yi):.2f}亿，机构持续减仓")
        else:
            score -= 0.1
            neg.append(f"主力近{days}日净流出 {abs(net_yi):.2f}亿")

    return max(0.0, min(1.0, score)), pos, neg


def score_forecast(fc: dict) -> tuple[float, list[str], list[str]]:
    """Score based on earnings guidance / forecast (业绩预告).

    type 字段来自管理层公告，前向性强: 预增/扭亏 = 利多; 预减/续亏 = 利空.
    """
    score = 0.5
    pos: list[str] = []
    neg: list[str] = []

    if not fc:
        return score, pos, neg

    fc_type = fc.get("type", "")
    p_change = fc.get("p_change")   # 净利润同比变化% 中值

    POSITIVE_TYPES = {"预增", "略增", "续盈", "扭亏", "减亏"}
    NEGATIVE_TYPES = {"预减", "略减", "首亏", "续亏"}

    if fc_type in POSITIVE_TYPES:
        if p_change is not None and p_change > 50:
            score += 0.2
            pos.append(f"业绩预告【{fc_type}】净利同比 +{p_change:.0f}%，超预期增长")
        elif p_change is not None:
            score += 0.1
            pos.append(f"业绩预告【{fc_type}】净利同比 +{p_change:.0f}%")
        else:
            score += 0.1
            pos.append(f"业绩预告【{fc_type}】")
    elif fc_type in NEGATIVE_TYPES:
        if p_change is not None and p_change < -50:
            score -= 0.2
            neg.append(f"业绩预告【{fc_type}】净利同比 {p_change:.0f}%，业绩大幅下滑")
        elif p_change is not None:
            score -= 0.1
            neg.append(f"业绩预告【{fc_type}】净利同比 {p_change:.0f}%")
        else:
            score -= 0.1
            neg.append(f"业绩预告【{fc_type}】")

    return max(0.0, min(1.0, score)), pos, neg


def score_fundamentals(f: dict, *,
                       moneyflow: dict | None = None,
                       forecast: dict | None = None) -> dict[str, Any]:
    """Combine sub-scores with optional capital flow and forecast signals.

    Base weights (no flow data): quality 0.40, valuation 0.30, growth 0.30
    With moneyflow + forecast:   quality 0.35, valuation 0.25, growth 0.25,
                                 moneyflow 0.10, forecast 0.05
    """
    q, qp, qn = score_quality(f)
    v, vp, vn = score_valuation(f)
    g, gp, gn = score_growth(f)
    mf_score, mfp, mfn = score_moneyflow(moneyflow or {})
    fc_score, fcp, fcn = score_forecast(forecast or {})

    have_mf = bool(moneyflow)
    have_fc = bool(forecast)

    if have_mf and have_fc:
        overall = 0.35*q + 0.25*v + 0.25*g + 0.10*mf_score + 0.05*fc_score
    elif have_mf:
        overall = 0.37*q + 0.27*v + 0.26*g + 0.10*mf_score
    elif have_fc:
        overall = 0.38*q + 0.28*v + 0.29*g + 0.05*fc_score
    else:
        overall = 0.40*q + 0.30*v + 0.30*g

    pos = ([f"[质量] {m}" for m in qp]
           + [f"[估值] {m}" for m in vp]
           + [f"[成长] {m}" for m in gp]
           + [f"[资金] {m}" for m in mfp]
           + [f"[预期] {m}" for m in fcp])
    neg = ([f"[质量] {m}" for m in qn]
           + [f"[估值] {m}" for m in vn]
           + [f"[成长] {m}" for m in gn]
           + [f"[资金] {m}" for m in mfn]
           + [f"[预期] {m}" for m in fcn])

    return {
        "overall": overall,
        "quality": q,
        "valuation": v,
        "growth": g,
        "moneyflow": mf_score,
        "forecast": fc_score,
        "positives": pos,
        "negatives": neg,
    }


if __name__ == "__main__":
    sample = {
        "roe_ttm": 18.5, "gross_margin": 45.0, "net_margin": 12.0,
        "debt_ratio": 35.0, "pe_ttm": 22.0, "pb": 3.5,
        "revenue_growth_yoy": 18.0, "earnings_growth_yoy": 25.0,
    }
    print(score_fundamentals(sample))
