"""
universe_scanner.py — 宽基指数选股 + 自动维护自选股池

流程（两阶段，控制耗时）：
  Stage 1 · 快速预筛（单次批量 API，~5秒）
    - 拉取沪深300 + 中证500成分股
    - 用 tushare daily_basic 批量获取当日 PE/PB/流通市值/换手率
    - 基本过滤：去 ST、去停牌、PE/PB 合理范围、市值 > 阈值
    - 随机抽样 + 保留高换手率候选，最多保留 DEEP_SCAN_LIMIT 只

  Stage 2 · 深度评分（每只约 15-20 秒）
    - 对 Stage 1 候选运行完整 analyze_symbol()
    - 取 combined_score >= 阈值 且 action in (buy, buy_strong) 的结果
    - 按分数排序，取 top N

  自动维护自选股池：
    - 将 top N 加入 watchlist.yaml（已有的不重复添加）
    - 将评分持续低于 EVICT_THRESHOLD 的旧条目移出（可配置是否启用）
"""
from __future__ import annotations

import random
import datetime as dt
from pathlib import Path
from typing import Any

import yaml

from ._common import (
    DATA_DIR, log, load_risk_config, load_watchlist, normalize_symbol,
    cache_get, cache_set,
)
from . import fetch_data, signals

# ── 可调参数 ─────────────────────────────────────────────────────────────────
DEEP_SCAN_LIMIT   = 40      # Stage 1 → Stage 2 最多送入几只（时间换精度）
TOP_ADD_N         = 5       # 每次最多新增几只进自选股
ADD_SCORE_MIN     = 0.60    # 进入自选股的最低综合分
EVICT_THRESHOLD   = 0.35    # 低于此分 + 无持仓 → 移出自选股（需 evict=True）
STAGE1_PE_MAX     = 60      # PE 上限（亏损股 PE<0 单独保留）
STAGE1_PB_MAX     = 8       # PB 上限
STAGE1_MCAP_MIN   = 5e9     # 流通市值下限（50亿）
STAGE1_TURNOVER_TOP = 60    # 换手率 top N 优先进入深度扫描


_WATCHLIST_PATH = Path(__file__).resolve().parent.parent / "config" / "watchlist.yaml"


# ─────────────────────────────────────────────────────────────────────────────
# 内部工具
# ─────────────────────────────────────────────────────────────────────────────
def _load_wl_data() -> dict:
    if not _WATCHLIST_PATH.exists():
        return {"symbols": []}
    with _WATCHLIST_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {"symbols": []}


def _save_wl_data(data: dict) -> None:
    with _WATCHLIST_PATH.open("w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _wl_symbols() -> list[str]:
    return [str(s).zfill(6) for s in _load_wl_data().get("symbols", [])]


# ─────────────────────────────────────────────────────────────────────────────
# Stage 1：批量预筛
# ─────────────────────────────────────────────────────────────────────────────
def _stage1_candidates(universe: list[str], cfg: dict) -> list[str]:
    """用 tushare daily_basic 批量拿基本指标，做粗筛，返回候选列表。"""

    # 尝试 tushare 批量接口（最快）
    candidates = _stage1_via_tushare(universe, cfg)
    if candidates:
        return candidates

    # 降级：直接对 universe 随机抽样（无基本面过滤，但至少能跑）
    log.warning("tushare batch unavailable, falling back to random sample")
    return random.sample(universe, min(DEEP_SCAN_LIMIT, len(universe)))


def _stage1_via_tushare(universe: list[str], cfg: dict) -> list[str]:
    """单次 daily_basic 批量拉取，返回过滤后候选（最多 DEEP_SCAN_LIMIT 只）。"""
    try:
        import tushare as ts  # type: ignore
        token = __import__("os").environ.get("TUSHARE_TOKEN", "")
        if not token:
            return []
        relay_url = __import__("os").environ.get("TUSHARE_HTTP_URL", "")
        if relay_url:
            try:
                from tushare.util import upass  # type: ignore
                upass.set_token(token)
                ts.set_token(token)
                import tushare.pro.data_pro as _dp  # type: ignore
                _dp.DATA_API_URL = relay_url + "/api"
            except Exception:
                ts.set_token(token)
        else:
            ts.set_token(token)
        pro = ts.pro_api()
    except Exception as e:
        log.debug("tushare init failed in stage1: %s", e)
        return []

    # 获取当日（或最近交易日）daily_basic
    from ._common import today_str
    trade_date = today_str()
    cache_key = f"daily_basic_{trade_date}"
    df_basic = cache_get(cache_key, ttl_seconds=6 * 3600)

    if df_basic is None:
        try:
            import pandas as pd
            raw = pro.daily_basic(trade_date=trade_date,
                                  fields="ts_code,pe,pb,float_share,turnover_rate,total_mv")
            if raw is None or raw.empty:
                # 今天可能还没数据（非交易日），取最近一期
                raw = pro.daily_basic(fields="ts_code,pe,pb,float_share,turnover_rate,total_mv",
                                      limit=5000)
            df_basic = raw
            cache_set(cache_key, df_basic.to_dict("records") if df_basic is not None else [])
        except Exception as e:
            log.warning("daily_basic fetch failed: %s", e)
            return []
    else:
        import pandas as pd
        df_basic = pd.DataFrame(df_basic)

    if df_basic is None or df_basic.empty:
        return []

    # 规范化代码（去掉 .SH/.SZ 后缀）
    import pandas as pd
    df_basic["symbol"] = df_basic["ts_code"].str.split(".").str[0]
    universe_set = set(universe)
    df = df_basic[df_basic["symbol"].isin(universe_set)].copy()

    # 数值转换
    for col in ["pe", "pb", "float_share", "turnover_rate", "total_mv"]:
        df[col] = pd.to_numeric(df.get(col, pd.Series(dtype=float)), errors="coerce")

    # 流通市值：tushare 单位是万元
    df["float_mcap"] = df["total_mv"] * 1e4  # 转为元

    # 过滤
    mask = (
        # 市值下限
        (df["float_mcap"] >= STAGE1_MCAP_MIN) &
        # PB 合理（排除负 PB）
        (df["pb"].fillna(999) > 0) &
        (df["pb"].fillna(999) < STAGE1_PB_MAX) &
        # PE：允许 PE<0（困境反转），但排除极端高 PE
        (df["pe"].fillna(0) < STAGE1_PE_MAX)
    )
    filtered = df[mask].copy()
    log.info("stage1: universe=%d → after filter=%d", len(universe), len(filtered))

    if filtered.empty:
        return []

    # 排序：先取换手率 top，再随机补足
    filtered = filtered.sort_values("turnover_rate", ascending=False)
    top_turn = filtered.head(STAGE1_TURNOVER_TOP)["symbol"].tolist()

    remainder = filtered[~filtered["symbol"].isin(top_turn)]["symbol"].tolist()
    random.shuffle(remainder)

    combined = (top_turn + remainder)[:DEEP_SCAN_LIMIT]
    log.info("stage1: selected %d candidates for deep scan", len(combined))
    return combined


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2：深度评分
# ─────────────────────────────────────────────────────────────────────────────
def _stage2_score(candidates: list[str], cfg: dict) -> list[dict]:
    """对候选股运行完整 analyze_symbol，返回满足条件的结果列表（按分数降序）。"""
    results = []
    total = len(candidates)
    for i, sym in enumerate(candidates, 1):
        try:
            log.info("deep scan [%d/%d] %s …", i, total, sym)
            card = signals.analyze_symbol(sym, config=cfg)
            if card.action in ("buy", "buy_strong") and card.combined_score >= ADD_SCORE_MIN:
                results.append({
                    "symbol": sym,
                    "name": card.name,
                    "score": card.combined_score,
                    "action": card.action,
                    "latest_close": card.latest_close,
                    "confidence": card.confidence,
                })
        except Exception as e:
            log.debug("deep scan skip %s: %s", sym, e)

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


# ─────────────────────────────────────────────────────────────────────────────
# 自动维护自选股池
# ─────────────────────────────────────────────────────────────────────────────
def _auto_add(top_stocks: list[dict]) -> list[str]:
    """把 top_stocks 里不在自选股池的股票加进去，返回新增列表。"""
    wl_data = _load_wl_data()
    existing = set(str(s).zfill(6) for s in wl_data.get("symbols", []))
    added = []
    for s in top_stocks[:TOP_ADD_N]:
        sym = normalize_symbol(s["symbol"])
        if sym not in existing:
            wl_data.setdefault("symbols", []).append(sym)
            wl_data.setdefault("notes", {})[sym] = (
                f"[自动加入 {dt.date.today()}] "
                f"{s.get('name','')} 综合分={s['score']:.3f}"
            )
            existing.add(sym)
            added.append(sym)
            log.info("watchlist +ADD %s (%s) score=%.3f", sym, s.get("name",""), s["score"])

    if added:
        _save_wl_data(wl_data)
    return added


def _auto_evict(portfolio_positions: set[str]) -> list[str]:
    """
    扫描自选股池，把评分持续偏低 + 无持仓的股票移出。
    为避免误删，仅移出有 [自动加入] 标记的条目。
    返回已移出的股票列表。
    """
    wl_data = _load_wl_data()
    notes = wl_data.get("notes", {}) or {}
    symbols = [str(s).zfill(6) for s in wl_data.get("symbols", [])]
    evicted = []
    cfg = load_risk_config()

    for sym in list(symbols):
        # 只自动移除有 [自动加入] 标记的
        note = notes.get(sym, "")
        if "[自动加入" not in note:
            continue
        # 持仓中的不移除
        if sym in portfolio_positions:
            continue
        try:
            card = signals.analyze_symbol(sym, config=cfg)
            if card.combined_score < EVICT_THRESHOLD:
                symbols.remove(sym)
                notes.pop(sym, None)
                evicted.append(sym)
                log.info("watchlist -EVICT %s score=%.3f", sym, card.combined_score)
        except Exception as e:
            log.debug("evict check skip %s: %s", sym, e)

    if evicted:
        wl_data["symbols"] = symbols
        wl_data["notes"] = notes
        _save_wl_data(wl_data)
    return evicted


# ─────────────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────────────
def run_universe_scan(
    cfg: dict | None = None,
    evict: bool = True,
    portfolio_positions: set[str] | None = None,
) -> dict[str, Any]:
    """
    完整的宇宙扫描流程。
    返回:
      {
        "scanned": int,           # Stage 1 候选数
        "qualified": list[dict],  # 通过 Stage 2 的股票
        "added": list[str],       # 新加入自选股的股票代码
        "evicted": list[str],     # 移出自选股的股票代码
      }
    """
    cfg = cfg or load_risk_config()
    portfolio_positions = portfolio_positions or set()

    log.info("=== universe_scan start ===")

    # 构建宇宙：沪深300 + 中证500（去重）
    hs300 = fetch_data.fetch_hs300_members()
    zz500 = fetch_data.fetch_zz500_members()
    universe = list(dict.fromkeys(hs300 + zz500))  # 去重保序
    # 排除已在自选股中的（避免重复评分）
    current_wl = set(_wl_symbols())
    universe_excl = [s for s in universe if s not in current_wl]
    log.info("universe: hs300=%d zz500=%d total=%d excl_watchlist=%d",
             len(hs300), len(zz500), len(universe), len(universe_excl))

    if not universe_excl:
        log.info("all universe members already in watchlist, skip scan")
        return {"scanned": 0, "qualified": [], "added": [], "evicted": []}

    # Stage 1
    candidates = _stage1_candidates(universe_excl, cfg)

    # Stage 2
    qualified = _stage2_score(candidates, cfg)
    log.info("universe_scan: %d candidates → %d qualified (score>=%.2f)",
             len(candidates), len(qualified), ADD_SCORE_MIN)

    # 自动加入
    added = _auto_add(qualified)

    # 自动移出（可选）
    evicted: list[str] = []
    if evict:
        evicted = _auto_evict(portfolio_positions)

    log.info("=== universe_scan done: +%d added, -%d evicted ===",
             len(added), len(evicted))
    return {
        "scanned": len(candidates),
        "qualified": qualified,
        "added": added,
        "evicted": evicted,
    }
