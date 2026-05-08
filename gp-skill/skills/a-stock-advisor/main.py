"""
A股投资决策支持 Skill — 入口文件

OpenClaw Agent 调用此文件执行技能。
所有实现逻辑位于项目根目录的 scripts/ 包中；
此文件仅做路径引导和统一入口。

环境变量（可选）：
  TUSHARE_TOKEN     Tushare Pro token（推荐，提速 5-10x）
  TUSHARE_HTTP_URL  Tushare 中继地址（默认 http://jiaoch.site）
"""
from __future__ import annotations
import sys
import os
from pathlib import Path

# 将项目根目录加入 sys.path，使 scripts 包可被导入
_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

# 修复 Windows 终端中文编码
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def analyze(symbol: str) -> dict:
    """单股深度分析，返回 SignalCard.to_dict()。"""
    from scripts.signals import analyze_symbol
    card = analyze_symbol(symbol)
    return card.to_dict()


def recommend(top_n: int = 3, min_score: float = 0.55) -> list[dict]:
    """扫描自选股池，返回 Top N SignalCard.to_dict() 列表。"""
    from scripts.recommend import run
    return [c.to_dict() for c in run(top_n=top_n, min_score=min_score)]


def get_watchlist() -> list[str]:
    """返回当前自选股池的股票代码列表。"""
    from scripts._common import load_watchlist
    return load_watchlist()


def add_to_watchlist(symbol: str, note: str = "") -> str:
    """把股票加入自选股池，返回操作结果描述。"""
    import yaml
    from scripts._common import normalize_symbol
    symbol = normalize_symbol(symbol)
    wl_path = _ROOT / "config" / "watchlist.yaml"
    with wl_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    symbols = [str(s) for s in data.get("symbols", [])]
    if symbol in symbols:
        return f"{symbol} 已在自选股池中"
    data.setdefault("symbols", []).append(symbol)
    if note:
        data.setdefault("notes", {})[symbol] = note
    with wl_path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    return f"已添加 {symbol}" + (f"，备注：{note}" if note else "")


def remove_from_watchlist(symbol: str) -> str:
    """从自选股池移除股票，返回操作结果描述。"""
    import yaml
    from scripts._common import normalize_symbol
    symbol = normalize_symbol(symbol)
    wl_path = _ROOT / "config" / "watchlist.yaml"
    with wl_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    before = len(data.get("symbols", []))
    data["symbols"] = [s for s in data.get("symbols", []) if str(s) != symbol]
    data.get("notes", {}).pop(symbol, None)
    for g in (data.get("groups") or {}).values():
        if symbol in g:
            g.remove(symbol)
    with wl_path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    return f"已移除 {symbol}" if len(data.get("symbols", [])) < before else f"{symbol} 不在自选股池中"


# ── CLI 快速测试 ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse, json
    parser = argparse.ArgumentParser(description="A股 Skill 入口")
    sub = parser.add_subparsers(dest="cmd")

    p_analyze = sub.add_parser("analyze", help="分析单只股票")
    p_analyze.add_argument("symbol")

    p_rec = sub.add_parser("recommend", help="推荐自选股")
    p_rec.add_argument("--top", type=int, default=3)
    p_rec.add_argument("--min-score", type=float, default=0.55)

    p_wl = sub.add_parser("watchlist", help="查看自选股")
    p_add = sub.add_parser("add", help="加入自选股")
    p_add.add_argument("symbol")
    p_add.add_argument("--note", default="")
    p_rm = sub.add_parser("remove", help="移除自选股")
    p_rm.add_argument("symbol")

    args = parser.parse_args()
    if args.cmd == "analyze":
        print(json.dumps(analyze(args.symbol), ensure_ascii=False, indent=2, default=str))
    elif args.cmd == "recommend":
        results = recommend(top_n=args.top, min_score=args.min_score)
        for r in results:
            print(f"{r['symbol']} {r['name']} 综合分:{r['combined_score']:.3f} 动作:{r['action']}")
    elif args.cmd == "watchlist":
        print("\n".join(get_watchlist()))
    elif args.cmd == "add":
        print(add_to_watchlist(args.symbol, args.note))
    elif args.cmd == "remove":
        print(remove_from_watchlist(args.symbol))
    else:
        parser.print_help()
