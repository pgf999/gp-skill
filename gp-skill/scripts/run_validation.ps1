# Runs pytest + end-to-end signal analysis on a fixed symbol set and writes
# everything to data/validation_report.txt. Intended for iterative dev:
# Claude edits code, you run this one command, Claude reads the report back.
#
# Usage:  cd gp-skill; .\scripts\run_validation.ps1
#
# NOTE: This script is ASCII-only on purpose. Windows PowerShell 5.1 reads
# .ps1 files as ANSI (Windows-1252) unless they carry a UTF-8 BOM, so any
# Chinese characters in the source would parse-error. The Python output IS
# Chinese (analysis cards) -- we force the console + file encoding to UTF-8
# below so that downstream output stays readable.

$ErrorActionPreference = "Continue"

# Load .env from project root so TUSHARE_TOKEN / TUSHARE_HTTP_URL are available.
$envFile = Join-Path $PSScriptRoot "..\\.env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([^#=\s]+)\s*=\s*(.*)$') {
            [System.Environment]::SetEnvironmentVariable($Matches[1], $Matches[2].Trim(), 'Process')
        }
    }
}

# Force UTF-8 everywhere so Python's Chinese stdout doesn't mojibake.
chcp 65001 > $null
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONIOENCODING = "utf-8"

$report = "data\validation_report.txt"
New-Item -ItemType Directory -Force -Path "data" | Out-Null
# Truncate the report file with explicit UTF-8 (no BOM).
Set-Content -Path $report -Value "" -Encoding utf8

function Append-Report($text) {
    Add-Content -Path $report -Value $text -Encoding utf8
}

function Log($msg) {
    Write-Host $msg
    Append-Report $msg
}

Log "========================================="
Log " gp-skill validation run"
Log " $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Log "========================================="
Log ""

# -----------------------------------------------------------------
# 1. pytest
# -----------------------------------------------------------------
Log "----- STEP 1/2: pytest -----"
$pytestOut = pytest tests/ -v --tb=short 2>&1 | Out-String
Write-Host $pytestOut
Append-Report $pytestOut
Log ""

# -----------------------------------------------------------------
# 2. end-to-end signals across boards
# -----------------------------------------------------------------
Log "----- STEP 2/2: live signal analysis -----"

# Symbols + ASCII-only notes (Chinese names live in the Python output, not here)
$symbols = @(
    @{sym="600519"; note="SH main board - Moutai (baseline reference)"},
    @{sym="300750"; note="ChiNext - CATL (20% limit)"},
    @{sym="688981"; note="STAR - SMIC (20% limit)"},
    @{sym="000568"; note="SZ main - Luzhou Laojiao (baijiu cross-check)"},
    @{sym="002594"; note="SZ midcap - BYD (high growth)"}
)

foreach ($item in $symbols) {
    Log ""
    Log "===== $($item.sym) | $($item.note) ====="
    $out = python -m scripts.signals $item.sym 2>&1 | Out-String
    Write-Host $out
    Append-Report $out
}

Log ""
Log "----- Cache state (DuckDB) -----"
$cacheOut = python -m scripts.cache_db 2>&1 | Out-String
Write-Host $cacheOut
Append-Report $cacheOut

Log ""
Log "----- Backend resolution -----"
Log "(scan the lines above for 'OHLCV <symbol> via <backend>' to see which"
Log " source actually answered each request - tushare > mootdx > akshare-east"
Log " > akshare-sina, missing tools fall through silently)"

Log ""
Log "========================================="
Log " Report written to: $report"
Log "========================================="
