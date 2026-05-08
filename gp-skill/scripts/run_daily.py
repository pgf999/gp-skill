"""
每日收盘后自动执行：
  1. 根据信号引擎扫描自选股，执行模拟买卖
  2. 生成中文日报，写入 data/logs/daily_YYYYMMDD.md
  3. 输出到终端（供任务计划程序日志捕获）

运行方式（手动测试）：
  python scripts/run_daily.py

任务计划程序命令：
  python scripts/run_daily.py --date today
"""
from __future__ import annotations
import sys
import os
import json
import datetime as dt
import argparse
from pathlib import Path

# ── 编码修复（Windows 终端 / 任务计划程序）────────────────────────────────
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# ── 路径 ─────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts._common import load_risk_config, load_watchlist, log, DATA_DIR
from scripts import fetch_data, paper_trading, risk, signals, daily_report
from scripts import universe_scanner


# ═══════════════════════════════════════════════════════════════════════════
# 一、执行买卖操作
# ═══════════════════════════════════════════════════════════════════════════
def run_trades(cfg: dict) -> list[dict]:
    """扫描信号，执行买入 / 止损止盈卖出，返回操作记录列表。"""
    trades = []
    watchlist = load_watchlist()
    if not watchlist:
        log.warning("watchlist 为空，跳过交易")
        return trades

    portfolio = paper_trading.get_portfolio()
    cash = portfolio["cash"]
    positions = portfolio["positions"]

    # ── 风控参数 ──────────────────────────────────────────────────────────
    max_single_ratio = cfg.get("position", {}).get("max_single_stock_ratio", 0.20)
    max_total_ratio  = cfg.get("position", {}).get("max_total_position_ratio", 0.80)
    max_single_trade = cfg.get("account", {}).get("max_single_trade", 50000)
    initial_capital  = cfg.get("account", {}).get("total_capital", 500000)
    reserved_cash    = cfg.get("account", {}).get("reserved_cash", 50000)
    max_daily_trades = cfg.get("trade_limits", {}).get("max_daily_trades", 3)

    today_count = paper_trading.today_trade_count()

    # ── 先处理止损 / 止盈（已持仓）────────────────────────────────────────
    for sym, pos in list(positions.items()):
        if today_count >= max_daily_trades:
            break
        try:
            q = fetch_data.fetch_realtime_quote(sym)
            price = float(q.get("price") or 0)
        except Exception:
            continue
        if price <= 0:
            continue

        triggers = risk.check_stop_conditions(
            sym, price=price,
            avg_cost=pos["avg_cost"],
            highest_since_buy=pos.get("highest_since_buy"),
            config=cfg,
        )
        for t in triggers:
            qty = pos["qty"] - pos.get("bought_today_qty", 0)
            if qty <= 0:
                continue
            result = paper_trading.execute_sell(
                sym, price, qty,
                reason=t.get("reason", "stop_triggered"),
                config=cfg,
            )
            if result.get("ok"):
                today_count += 1
                trades.append({
                    "action": "sell", "symbol": sym,
                    "price": price, "qty": qty,
                    "reason": t.get("reason", ""),
                    "pnl": result.get("realized_pnl", 0),
                })
                log.info("SELL %s x%d @ %.2f  reason=%s  pnl=%.2f",
                         sym, qty, price, t.get("reason", ""), result.get("realized_pnl", 0))

    # ── 再处理买入信号 ─────────────────────────────────────────────────────
    buy_threshold = cfg.get("signals", {}).get("buy_threshold", 0.60)

    for sym in watchlist:
        if today_count >= max_daily_trades:
            break
        if sym in positions:          # 已持仓，不重复买入
            continue
        if cash - reserved_cash < 5000:
            break

        try:
            card = signals.analyze_symbol(sym, config=cfg)
        except Exception as e:
            log.warning("analyze %s failed: %s", sym, e)
            continue

        if card.action not in ("buy", "buy_strong"):
            continue
        if card.combined_score < buy_threshold:
            continue
        if not card.latest_close or card.latest_close <= 0:
            continue

        # 计算买入金额
        budget = min(
            initial_capital * max_single_ratio,
            max_single_trade,
            cash - reserved_cash,
        )
        # 总仓位限制
        mtm = paper_trading.mark_to_market({
            s: fetch_data.fetch_realtime_quote(s).get("price", 0)
            for s in positions
        })
        if (mtm["position_value"] + budget) / initial_capital > max_total_ratio:
            log.info("总仓位接近上限，跳过买入 %s", sym)
            continue

        price = card.latest_close
        qty = int(budget // (price * 100)) * 100
        if qty <= 0:
            continue

        result = paper_trading.execute_buy(
            sym, price, qty,
            reason=f"score={card.combined_score:.3f}",
            config=cfg,
        )
        if result.get("ok"):
            today_count += 1
            cash = result["cash_after"]
            trades.append({
                "action": "buy", "symbol": sym,
                "name": card.name,
                "price": price, "qty": qty,
                "score": card.combined_score,
                "reason": card.action,
            })
            log.info("BUY  %s(%s) x%d @ %.2f  score=%.3f",
                     sym, card.name, qty, price, card.combined_score)

    # ── T+1 重置（模拟今日收盘）──────────────────────────────────────────
    paper_trading.reset_today_buys()

    return trades


# ═══════════════════════════════════════════════════════════════════════════
# 二、生成日报 Markdown
# ═══════════════════════════════════════════════════════════════════════════
def format_report(report_data: dict, trades: list[dict],
                  scan_result: dict | None = None) -> str:
    d = report_data
    date_str = d.get("date", dt.date.today().isoformat())
    mkt = d.get("market", {})
    pf  = d.get("portfolio", {})
    risk_events = d.get("risk_events", [])
    wl_signals  = d.get("watchlist_signals", [])

    # 账户
    equity   = pf.get("equity", 0)
    initial  = pf.get("initial_capital", 500000) or 500000
    ret_pct  = (equity - initial) / initial * 100 if initial else 0
    cash     = pf.get("cash", 0)
    pos_val  = pf.get("position_value", 0)

    # 持仓表格
    pos_rows = ""
    for p in pf.get("positions", []):
        pnl_pct = p.get("unrealized_pnl_pct", 0) * 100
        sign = "+" if pnl_pct >= 0 else ""
        pos_rows += (
            f"| {p['symbol']} | {p['qty']:,} | "
            f"{p['avg_cost']:.2f} | {p['current_price']:.2f} | "
            f"{sign}{p['unrealized_pnl']:.0f} | {sign}{pnl_pct:.2f}% |\n"
        )
    if not pos_rows:
        pos_rows = "| — | — | — | — | — | — |\n"

    # 今日操作
    if trades:
        trade_lines = []
        for t in trades:
            if t["action"] == "buy":
                trade_lines.append(
                    f"- 🟢 买入 {t['symbol']}({t.get('name','')}) "
                    f"{t['qty']}股 @ {t['price']:.2f}  "
                    f"（信号分={t.get('score',0):.3f}）"
                )
            else:
                pnl = t.get('pnl', 0)
                sign = "+" if pnl >= 0 else ""
                trade_lines.append(
                    f"- 🔴 卖出 {t['symbol']} "
                    f"{t['qty']}股 @ {t['price']:.2f}  "
                    f"原因：{t.get('reason','')}  "
                    f"盈亏 {sign}{pnl:.0f}元"
                )
        trade_section = "\n".join(trade_lines)
    else:
        trade_section = "无"

    # 风控
    if risk_events:
        risk_lines = [f"- ⚠️ {e.get('symbol','')} {e.get('reason','')}" for e in risk_events]
        risk_section = "\n".join(risk_lines)
    else:
        risk_section = "无"

    # 自选股信号
    if wl_signals:
        sig_lines = []
        for s in wl_signals:
            icon = "🟢" if "buy" in s["action"] else "🔴"
            sig_lines.append(
                f"- {icon} {s['symbol']}({s['name']})  "
                f"动作={s['action']}  分={s['score']:.3f}  "
                f"收盘={s['latest_close']:.2f}"
            )
        sig_section = "\n".join(sig_lines)
    else:
        sig_section = "无明显买卖信号"

    # 明日关注（取 buy 信号前 3）
    watch_candidates = [s for s in wl_signals if "buy" in s["action"]][:3]
    if watch_candidates:
        watch_lines = [
            f"{i+1}. {s['symbol']}({s['name']}) — 信号分 {s['score']:.3f}，收盘 {s['latest_close']:.2f}"
            for i, s in enumerate(watch_candidates)
        ]
        watch_section = "\n".join(watch_lines)
    else:
        watch_section = "暂无明显机会，保持观望"

    hs300_str = (
        f"{mkt['hs300_close']:.2f}点，"
        f"{'涨' if mkt['hs300_change_pct'] >= 0 else '跌'}"
        f"{abs(mkt['hs300_change_pct']):.2f}%"
        if mkt else "数据获取失败"
    )

    ret_sign = "+" if ret_pct >= 0 else ""

    # 宇宙扫描区块
    scan_result = scan_result or {}
    added_syms   = scan_result.get("added", [])
    evicted_syms = scan_result.get("evicted", [])
    qualified    = scan_result.get("qualified", [])
    scanned_n    = scan_result.get("scanned", 0)

    if scanned_n:
        scan_header = f"本次扫描 HS300+ZZ500 共 {scanned_n} 只候选，发现 {len(qualified)} 只达标"
    else:
        scan_header = "今日未执行宇宙扫描（dry-run 或手动跳过）"

    if added_syms:
        added_lines = "\n".join(
            f"- ➕ {s['symbol']}({s.get('name','')})  分={s['score']:.3f}  "
            f"收盘={s.get('latest_close',0):.2f}"
            for s in qualified if s["symbol"] in added_syms
        )
    else:
        added_lines = "无新增"

    evicted_lines = (
        "- 🗑 " + "、".join(evicted_syms)
        if evicted_syms else "无移出"
    )

    report = f"""# 📊 A股虚拟盘日报 · {date_str}

## 【市场概况】
沪深300：{hs300_str}

## 【账户总览】
| 项目 | 金额 |
|------|------|
| 总资产 | {equity:,.0f} 元 |
| 持仓市值 | {pos_val:,.0f} 元 |
| 可用现金 | {cash:,.0f} 元 |
| 初始资金 | {initial:,.0f} 元 |
| 累计收益 | {ret_sign}{equity - initial:,.0f} 元（{ret_sign}{ret_pct:.2f}%） |

## 【持仓明细】
| 股票 | 持仓量 | 成本价 | 现价 | 浮盈亏(元) | 浮盈亏% |
|------|--------|--------|------|-----------|---------|
{pos_rows}
## 【今日操作】
{trade_section}

## 【风控提醒】
{risk_section}

## 【自选股信号】
{sig_section}

## 【选股雷达（HS300+ZZ500）】
{scan_header}

**新加入自选股：**
{added_lines}

**移出自选股：**
{evicted_lines}

## 【明日关注】
{watch_section}

---
*本报告由 a-stock-advisor skill 自动生成，仅供参考，不构成投资建议。*
*生成时间：{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*
"""
    return report


# ═══════════════════════════════════════════════════════════════════════════
# 三、主入口
# ═══════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="每日收盘后执行虚拟交易 + 生成日报")
    parser.add_argument("--dry-run", action="store_true", help="仅生成日报，不执行交易")
    parser.add_argument("--no-scan", action="store_true", help="跳过宇宙扫描（节省时间）")
    args = parser.parse_args()

    today = dt.date.today()
    log.info("=== run_daily start  date=%s  dry_run=%s ===", today, args.dry_run)

    cfg = load_risk_config()

    # ── Step 1：宇宙扫描（扩充自选股）──────────────────────────────────────
    scan_result: dict = {"scanned": 0, "qualified": [], "added": [], "evicted": []}
    if not args.no_scan and not args.dry_run:
        log.info("starting universe scan (HS300 + ZZ500) …")
        portfolio = paper_trading.get_portfolio()
        held = set(portfolio.get("positions", {}).keys())
        scan_result = universe_scanner.run_universe_scan(cfg=cfg, evict=True,
                                                         portfolio_positions=held)

    # ── Step 2：执行买卖 ─────────────────────────────────────────────────────
    if args.dry_run:
        trades = []
        log.info("dry-run: 跳过交易")
    else:
        trades = run_trades(cfg)
        log.info("今日共执行 %d 笔交易", len(trades))

    # ── Step 3：生成日报数据 ─────────────────────────────────────────────────
    report_data = daily_report.daily_report(config=cfg)

    # 格式化
    md = format_report(report_data, trades, scan_result=scan_result)

    # 写文件
    log_dir = DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    out_path = log_dir / f"daily_{today.strftime('%Y%m%d')}.md"
    out_path.write_text(md, encoding="utf-8")
    log.info("日报已写入: %s", out_path)

    # 打印到终端（任务计划程序捕获）
    print(md)

    log.info("=== run_daily done ===")


if __name__ == "__main__":
    main()
