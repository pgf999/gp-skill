#!/usr/bin/env bash
# gp-skill — macOS / Linux Installer
# 用法: bash install.sh
# 支持: macOS 12+, Ubuntu 20.04+, Debian 11+

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'

ok()   { echo -e "  ${GREEN}[OK]${NC}  $1"; }
warn() { echo -e "  ${YELLOW}[!!]${NC}  $1"; }
err()  { echo -e "  ${RED}[XX]${NC}  $1"; }
step() { echo -e "\n${CYAN}[$1]${NC} $2"; }

echo ""
echo -e "${CYAN}======================================================"
echo -e "  gp-skill A股决策支持 — macOS/Linux 安装脚本"
echo -e "======================================================${NC}"
echo ""

# ── Step 1: Python check ─────────────────────────────────────────────────
step "1/4" "检查 Python 版本…"
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" --version 2>&1 | awk '{print $2}')
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
            PYTHON="$cmd"
            ok "Python $ver ($cmd)"
            break
        fi
    fi
done
if [ -z "$PYTHON" ]; then
    err "未找到 Python 3.10+，请先安装"
    warn "macOS:  brew install python@3.12"
    warn "Ubuntu: sudo apt install python3.12 python3.12-venv"
    exit 1
fi

# ── Step 2: pip install ──────────────────────────────────────────────────
step "2/4" "安装 Python 依赖（akshare / tushare / pandas 等）…"
"$PYTHON" -m pip install -r "$ROOT/requirements.txt" -q
ok "依赖安装完成"

# ── Step 3: .env config ──────────────────────────────────────────────────
step "3/4" "配置 API 密钥…"
ENV_FILE="$ROOT/.env"
if [ -f "$ENV_FILE" ]; then
    ok ".env 已存在，跳过（如需修改请直接编辑 .env 文件）"
else
    echo ""
    echo "  Tushare 中继配置（可选，推荐配置）"
    echo "  没有 Tushare -> 自动回退到 AkShare 免费接口（速度较慢）"
    echo "  有 Tushare  -> 数据更全，速度提升 5-10 倍"
    echo ""
    read -rp "  Tushare Token（直接回车跳过）: " TOKEN
    TOKEN="${TOKEN// /}"

    if [ -n "$TOKEN" ]; then
        read -rp "  中继地址 [默认: http://jiaoch.site]: " URL
        URL="${URL// /}"
        [ -z "$URL" ] && URL="http://jiaoch.site"
        printf "TUSHARE_TOKEN=%s\nTUSHARE_HTTP_URL=%s\n" "$TOKEN" "$URL" > "$ENV_FILE"
        ok ".env 已创建（Tushare 中继: $URL）"
    else
        printf "# TUSHARE_TOKEN=your_token_here\n# TUSHARE_HTTP_URL=http://jiaoch.site\n" > "$ENV_FILE"
        warn "未配置 Tushare，将使用 AkShare（速度较慢）"
    fi
fi

# ── Step 4: config files ─────────────────────────────────────────────────
step "4/4" "初始化配置文件…"

if [ ! -f "$ROOT/config/risk.yaml" ]; then
    cp "$ROOT/config/risk_conservative.yaml" "$ROOT/config/risk.yaml"
    ok "config/risk.yaml 已创建（保守风控档位）"
else
    ok "config/risk.yaml 已存在"
fi

if [ ! -f "$ROOT/config/watchlist.yaml" ]; then
    cp "$ROOT/config/watchlist.example.yaml" "$ROOT/config/watchlist.yaml"
    ok "config/watchlist.yaml 从示例创建"
    warn "请编辑 config/watchlist.yaml，加入你关心的股票！"
else
    ok "config/watchlist.yaml 已存在"
fi

mkdir -p "$ROOT/data/cache" "$ROOT/data/logs"

# ── Final validation ─────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}[验证]${NC} 运行安装检查…"
"$PYTHON" "$ROOT/setup.py" --check

echo ""
echo -e "${GREEN}======================================================"
echo -e "  安装完成！"
echo ""
echo "  下一步："
echo "  1. 编辑 config/watchlist.yaml 添加你关心的股票"
echo "  2. 在此目录打开 Claude Code："
echo -e "       ${YELLOW}claude .${NC}"
echo "  3. 对 Claude 说：'帮我分析 600519' 或 '推荐几只股票'"
echo -e "======================================================${GREEN}${NC}"
echo ""
