"""
自选股扫描 + 投资推荐

从 config/watchlist.yaml 读取股票池，批量跑 analyze_symbol，
按综合评分排序，输出 Top N 的结构化分析卡。

用法（Claude Code 内部调用，也可直接运行）：
    python -m scripts.recommend              # 默认 Top 3
    python -m scripts.recommend --top 5
    python -m scripts.recommend --min-score 0.60
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

# Windows CP-1252 terminals can't print Chinese characters without this.
# reconfigure is Python 3.7+; the 'errors=replace' fallback ensures we
# never crash even on exotic terminal encodings.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._common import load_watchlist, log
from scripts.signals import scan_universe, SignalCard

_ACTION_CN = {
    "buy_strong": "★★ 强烈买入",
    "buy":        "★  建议买入",
    "hold":       "   持观望",
    "sell":       "▼  建议卖出",
    "sell_strong": "▼▼ 强烈卖出",
    "skip":       "   跳过",
}

_HORIZON_CN = {
    "short":  "短线 (<1月)",
    "medium": "中线 (1-6月)",
    "long":   "长线 (>6月)",
}


def run(top_n: int = 3, min_score: float = 0.55) -> list[SignalCard]:
    """扫描 watchlist，返回 Top N SignalCard（按综合分降序）。"""
    symbols = load_watchlist()
    if not symbols:
        print("watchlist 为空，请先配置 config/watchlist.yaml")
        print("示例:")
        print("  symbols:")
        print('    - "600519"  # 贵州茅台')
        print('    - "300750"  # 宁德时代')
        return []

    print(f"正在扫描 {len(symbols)} 只股票，请稍候…\n")
    cards = scan_universe(symbols, top_n=top_n, min_score=min_score)

    if not cards:
        print(f"watchlist 中没有综合分 >= {min_score} 的标的。")
        print("可能原因：")
        print("  · 整体市场弱势，所有票综合分偏低")
        print(f"  · 可降低 --min-score 阈值重新扫描（当前 {min_score}）")
        print("  · 可扩充 config/watchlist.yaml 纳入更多标的")
        print("  · 检查 data/logs/skill.log 查看是否有数据拉取错误")

    return cards


def format_card(card: SignalCard, rank: int) -> str:
    """把 SignalCard 渲染成可读的文字卡片。"""
    sep = "─" * 52
    action_str = _ACTION_CN.get(card.action, card.action)
    horizon_str = _HORIZON_CN.get(card.holding_horizon, card.holding_horizon)

    lines: list[str] = [
        "",
        f"╔{'═'*50}╗",
        f"║  No.{rank}  {card.symbol} {card.name:<30}║",
        f"╚{'═'*50}╝",
        f"  动作: {action_str}   综合分: {card.combined_score:.3f}   置信度: {card.confidence:.0%}",
        f"  收盘: {card.latest_close}   日期: {card.latest_date}   持仓周期: {horizon_str}",
        f"  技术面: {card.technical_score:.3f}   基本面: {card.fundamental_score:.3f}",
        sep,
    ]

    if card.positives:
        lines.append("  【正面信号】")
        for p in card.positives[:6]:
            lines.append(f"    + {p}")

    if card.negatives:
        lines.append("  【风险点】")
        for n in card.negatives[:4]:
            lines.append(f"    - {n}")

    if card.warnings:
        lines.append("  【警告】")
        for w in card.warnings:
            lines.append(f"    {w}")

    if card.advisory:
        lines.append("  【判断补充】")
        for a in card.advisory:
            lines.append(f"    {a}")

    lines.append(sep)
    lines.append(
        f"  买入区间: {card.buy_zone_low} ~ {card.buy_zone_high}   "
        f"止损: {card.stop_loss_price}   止盈: {card.take_profit_price}"
    )
    lines.append(
        f"  建议仓位: {card.suggested_position_pct:.0%}  "
        f"（基于 config/risk.yaml 中的 max_single_stock_ratio）"
    )

    return "\n".join(lines)


def print_summary(cards: list[SignalCard]) -> None:
    """打印精简排行榜表头。"""
    print(f"{'排名':<4} {'代码':<8} {'名称':<12} {'综合分':<8} {'动作':<14} {'技术':<7} {'基本面':<7}")
    print("─" * 68)
    for i, c in enumerate(cards, 1):
        action_str = _ACTION_CN.get(c.action, c.action)
        print(
            f"{i:<4} {c.symbol:<8} {c.name:<12} {c.combined_score:<8.3f} "
            f"{action_str:<14} {c.technical_score:<7.3f} {c.fundamental_score:<7.3f}"
        )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="自选股扫描 — 推荐当前 watchlist 中最值得关注的标的"
    )
    parser.add_argument("--top", type=int, default=3, metavar="N",
                        help="返回前 N 名（默认 3）")
    parser.add_argument("--min-score", type=float, default=0.55, metavar="F",
                        help="最低综合分阈值（默认 0.55）")
    parser.add_argument("--summary-only", action="store_true",
                        help="只输出排行榜，不展开详细分析卡")
    args = parser.parse_args()

    cards = run(top_n=args.top, min_score=args.min_score)
    if not cards:
        sys.exit(0)

    print(f"\n{'='*52}")
    print(f"  自选股推荐  —  Top {len(cards)} / {len(load_watchlist())} 只")
    print(f"{'='*52}\n")

    print_summary(cards)

    if not args.summary_only:
        for i, card in enumerate(cards, 1):
            print(format_card(card, i))
            print()

    print("─" * 52)
    print("⚠ 以上为量化模型输出，不构成投资建议。")
    print("  实际操作前请结合市场环境、个人风险承受能力综合判断。")
    print("─" * 52)


if __name__ == "__main__":
    main()
