# gp-skill — Windows PowerShell Installer
# 用法: .\install.ps1
# 支持: Windows 10/11, Windows Server 2019+, PowerShell 5.1+

#Requires -Version 5.1
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Definition

function Write-OK   { param($msg) Write-Host "  [OK]  $msg" -ForegroundColor Green }
function Write-WARN { param($msg) Write-Host "  [!!]  $msg" -ForegroundColor Yellow }
function Write-ERR  { param($msg) Write-Host "  [XX]  $msg" -ForegroundColor Red }
function Write-Step { param($n,$msg) Write-Host "`n[$n] $msg" -ForegroundColor Cyan }

Write-Host ""
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host "  gp-skill A股决策支持 — Windows 安装脚本" -ForegroundColor Cyan
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host ""

# ── Step 1: Python check ──────────────────────────────────────────────────
Write-Step "1/4" "检查 Python 版本…"
try {
    $pyver = python --version 2>&1
    if ($pyver -match "Python (\d+)\.(\d+)") {
        $major = [int]$Matches[1]; $minor = [int]$Matches[2]
        if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 10)) {
            Write-ERR "需要 Python 3.10+，当前版本: $pyver"
            Write-WARN "请从 https://python.org 下载安装最新版 Python"
            exit 1
        }
        Write-OK "$pyver"
    } else {
        Write-ERR "无法识别 Python 版本: $pyver"
        exit 1
    }
} catch {
    Write-ERR "未找到 python 命令，请先安装 Python 3.10+"
    Write-WARN "下载地址: https://python.org/downloads"
    exit 1
}

# ── Step 2: pip install ───────────────────────────────────────────────────
Write-Step "2/4" "安装 Python 依赖（akshare / tushare / pandas 等）…"
try {
    python -m pip install -r "$ROOT\requirements.txt" -q
    Write-OK "依赖安装完成"
} catch {
    Write-ERR "pip install 失败: $_"
    Write-WARN "可手动运行: python -m pip install -r requirements.txt"
    exit 1
}

# ── Step 3: .env config ───────────────────────────────────────────────────
Write-Step "3/4" "配置 API 密钥…"
$envFile = "$ROOT\.env"
if (Test-Path $envFile) {
    Write-OK ".env 已存在，跳过（如需修改请直接编辑 .env 文件）"
} else {
    Write-Host ""
    Write-Host "  Tushare 中继配置（可选，推荐配置）" -ForegroundColor White
    Write-Host "  没有 Tushare -> 自动回退到 AkShare 免费接口（速度较慢）" -ForegroundColor Gray
    Write-Host "  有 Tushare  -> 数据更全，速度提升 5-10 倍" -ForegroundColor Gray
    Write-Host ""
    $token = Read-Host "  Tushare Token（直接回车跳过）"
    $token = $token.Trim()

    if ($token) {
        $url = Read-Host "  中继地址 [默认: http://jiaoch.site]"
        $url = $url.Trim()
        if (-not $url) { $url = "http://jiaoch.site" }

        "TUSHARE_TOKEN=$token`nTUSHARE_HTTP_URL=$url" | Out-File -FilePath $envFile -Encoding utf8
        Write-OK ".env 已创建（Tushare 中继: $url）"
    } else {
        "# TUSHARE_TOKEN=your_token_here`n# TUSHARE_HTTP_URL=http://jiaoch.site" | Out-File -FilePath $envFile -Encoding utf8
        Write-WARN "未配置 Tushare，将使用 AkShare（速度较慢）"
    }
}

# ── Step 4: config files ──────────────────────────────────────────────────
Write-Step "4/4" "初始化配置文件…"
$configDir = "$ROOT\config"

$riskDst = "$configDir\risk.yaml"
if (-not (Test-Path $riskDst)) {
    Copy-Item "$configDir\risk_conservative.yaml" $riskDst
    Write-OK "config/risk.yaml 已创建（保守风控档位）"
} else {
    Write-OK "config/risk.yaml 已存在"
}

$wlDst = "$configDir\watchlist.yaml"
if (-not (Test-Path $wlDst)) {
    Copy-Item "$configDir\watchlist.example.yaml" $wlDst
    Write-OK "config/watchlist.yaml 从示例创建"
    Write-WARN "请编辑 config\watchlist.yaml，加入你关心的股票！"
} else {
    Write-OK "config/watchlist.yaml 已存在"
}

# data dirs
foreach ($d in @("data", "data\cache", "data\logs")) {
    New-Item -ItemType Directory -Force -Path "$ROOT\$d" | Out-Null
}

# ── Final validation ──────────────────────────────────────────────────────
Write-Host ""
Write-Host "[验证] 运行安装检查…" -ForegroundColor Cyan
$env:PYTHONIOENCODING = "utf-8"
python "$ROOT\setup.py" --check

Write-Host ""
Write-Host "======================================================" -ForegroundColor Green
Write-Host "  安装完成！" -ForegroundColor Green
Write-Host ""
Write-Host "  下一步：" -ForegroundColor White
Write-Host "  1. 编辑 config\watchlist.yaml 添加你关心的股票"
Write-Host "  2. 在此目录打开 Claude Code："
Write-Host "       claude ." -ForegroundColor Yellow
Write-Host "  3. 对 Claude 说：'帮我分析 600519' 或 '推荐几只股票'"
Write-Host "======================================================" -ForegroundColor Green
Write-Host ""
