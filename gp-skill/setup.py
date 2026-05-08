#!/usr/bin/env python3
"""
gp-skill 一键安装向导 (cross-platform: Windows / macOS / Linux)

用法:
    python setup.py          # 完整安装（推荐首次使用）
    python setup.py --check  # 只验证当前环境，不做任何修改
"""
from __future__ import annotations
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Fix Windows console encoding so Chinese text doesn't crash.
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "config"
REQUIREMENTS = ROOT / "requirements.txt"


# ── helpers ────────────────────────────────────────────────────────────────

def _ok(msg: str) -> None:
    print(f"  ✓  {msg}")

def _warn(msg: str) -> None:
    print(f"  ⚠  {msg}")

def _err(msg: str) -> None:
    print(f"  ✗  {msg}")

def _ask(prompt: str, default: str = "") -> str:
    try:
        val = input(f"  {prompt}").strip()
        return val if val else default
    except (EOFError, KeyboardInterrupt):
        return default


# ── steps ──────────────────────────────────────────────────────────────────

def check_python() -> bool:
    if sys.version_info < (3, 10):
        _err(f"需要 Python 3.10+，当前版本 {sys.version.split()[0]}")
        return False
    _ok(f"Python {sys.version.split()[0]}")
    return True


def install_deps(quiet: bool = False) -> bool:
    print("\n[2/5] 安装 Python 依赖…")
    cmd = [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS)]
    if quiet:
        cmd.append("-q")
    try:
        subprocess.check_call(cmd)
        _ok("依赖安装完成")
        return True
    except subprocess.CalledProcessError as e:
        _err(f"pip install 失败: {e}")
        _warn("可手动运行: pip install -r requirements.txt")
        return False


def setup_env() -> bool:
    print("\n[3/5] 配置 API 密钥 (.env)…")
    env_file = ROOT / ".env"

    if env_file.exists():
        _ok(".env 已存在，跳过（如需修改请直接编辑该文件）")
        return True

    print()
    print("  Tushare 中继配置（可选，但强烈推荐）")
    print("  · 没有 Tushare → 自动回退到 AkShare（免费，但慢且数据少）")
    print("  · 有 Tushare Token → 数据更丰富、速度提升 5-10 倍")
    print()
    token = _ask("Tushare Token（直接回车跳过）: ")
    url = ""
    if token:
        url = _ask("中继地址 [默认: http://jiaoch.site]: ", "http://jiaoch.site")

    lines = []
    if token:
        lines.append(f"TUSHARE_TOKEN={token}")
        lines.append(f"TUSHARE_HTTP_URL={url}")
        _ok(f"Tushare 中继: {url}")
    else:
        lines.append("# TUSHARE_TOKEN=your_token_here")
        lines.append("# TUSHARE_HTTP_URL=http://jiaoch.site")
        _warn("未配置 Tushare，将使用 AkShare（速度较慢）")

    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _ok(f".env 已创建: {env_file}")
    return True


def setup_config() -> bool:
    print("\n[4/5] 初始化配置文件…")
    ok = True

    # risk.yaml
    risk_dst = CONFIG_DIR / "risk.yaml"
    if not risk_dst.exists():
        risk_src = CONFIG_DIR / "risk_conservative.yaml"
        shutil.copy(risk_src, risk_dst)
        _ok("config/risk.yaml 创建（保守档位）")
    else:
        _ok("config/risk.yaml 已存在")

    # watchlist.yaml
    wl_dst = CONFIG_DIR / "watchlist.yaml"
    if not wl_dst.exists():
        wl_src = CONFIG_DIR / "watchlist.example.yaml"
        shutil.copy(wl_src, wl_dst)
        _ok("config/watchlist.yaml 从示例创建")
        print()
        print("  ★ 请编辑 config/watchlist.yaml 把你关心的股票代码加进去！")
        print("    格式示例:")
        print("      symbols:")
        print('        - "600519"  # 贵州茅台')
        print('        - "300750"  # 宁德时代')
    else:
        _ok("config/watchlist.yaml 已存在")

    # data dirs
    for d in ("data", "data/cache", "data/logs"):
        (ROOT / d).mkdir(parents=True, exist_ok=True)

    return ok


def validate() -> bool:
    print("\n[5/5] 验证安装…")
    sys.path.insert(0, str(ROOT))
    passed = True

    # core imports
    for pkg in ("pandas", "numpy", "yaml", "requests"):
        try:
            __import__(pkg)
            _ok(f"{pkg} 可导入")
        except ImportError:
            _err(f"{pkg} 导入失败，请检查 pip install")
            passed = False

    # akshare
    try:
        import akshare  # noqa: F401
        _ok("akshare 可用")
    except ImportError:
        _warn("akshare 未安装（基础数据源缺失）")
        passed = False

    # tushare (optional)
    try:
        import tushare  # noqa: F401
        _ok("tushare 已安装（可选项）")
    except ImportError:
        _warn("tushare 未安装（将使用 AkShare 回退）")

    # config loading
    try:
        from scripts._common import load_risk_config, load_watchlist
        cfg = load_risk_config()
        wl = load_watchlist()
        _ok(f"risk.yaml 加载成功（来源: {Path(cfg['_source_path']).name}）")
        if wl:
            _ok(f"watchlist.yaml 已加载，共 {len(wl)} 只股票")
        else:
            _warn("watchlist.yaml 为空，请添加股票代码")
    except Exception as e:
        _err(f"配置加载失败: {e}")
        passed = False

    # tushare connectivity (optional)
    try:
        from scripts.fetch_data import _try_import_tushare
        pro = _try_import_tushare()
        if pro is not None:
            _ok("Tushare 中继连通")
        else:
            _warn("Tushare 未配置或不可用，将使用 AkShare")
    except Exception as e:
        _warn(f"Tushare 连通性检查失败: {e}")

    return passed


# ── main ───────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="gp-skill 安装向导")
    parser.add_argument("--check", action="store_true", help="只验证，不安装")
    parser.add_argument("--quiet", action="store_true", help="pip 静默模式")
    args = parser.parse_args()

    print("=" * 55)
    print("  gp-skill A股决策支持 — 安装向导")
    print("=" * 55)

    if args.check:
        print("\n【验证模式】只检查不修改\n")
        ok = check_python() and validate()
        sys.exit(0 if ok else 1)

    print()
    # step 1
    print("[1/5] 检查 Python 版本…")
    if not check_python():
        sys.exit(1)

    # steps 2-5
    install_deps(quiet=args.quiet)
    setup_env()
    setup_config()
    ok = validate()

    print()
    print("=" * 55)
    if ok:
        print("  安装完成！")
        print()
        print("  下一步：")
        print("  1. 编辑 config/watchlist.yaml 添加你关心的股票")
        print("  2. 用 Claude Code 打开此目录（claude .）")
        print("  3. 询问 Claude：'帮我分析 600519' 或 '推荐几只股票'")
    else:
        print("  安装部分完成，请修复上述错误后重试")
    print("=" * 55)


if __name__ == "__main__":
    main()
