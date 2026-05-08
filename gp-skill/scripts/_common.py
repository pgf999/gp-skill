"""Shared helpers: paths, config loading, logging, lightweight cache."""
from __future__ import annotations
import json
import logging
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# ---- Load .env from project root (if present) --------------------------------
# Keeps credentials out of source control while making them available to all
# scripts without requiring python-dotenv as a dependency.
_env_file = Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())

# ---- Global socket timeout ------------------------------------------------
# Tushare / mootdx / requests don't all consistently expose a `timeout`
# parameter, and several internal calls go straight to urllib without
# one. A hung TCP read with no timeout can freeze the whole script
# indefinitely — exactly the failure mode users hit when a third-party
# tushare relay or a public TDX server is slow/dead.
#
# `socket.setdefaulttimeout` is process-global and applies to every
# socket created from now on, including ones inside libraries we don't
# control. 30 seconds is generous for honest-but-slow endpoints,
# aggressive enough that a dead connection fails fast and our
# `_retry_akshare` backoff (1s/2s/4s) gets a chance to retry or fall
# back to the next backend.
#
# Override via env var if a single backend genuinely needs longer.
_SOCKET_TIMEOUT = float(os.environ.get("GP_SKILL_SOCKET_TIMEOUT", "30"))
socket.setdefaulttimeout(_SOCKET_TIMEOUT)

# ---- Paths ----------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
LOG_DIR = DATA_DIR / "logs"

for d in (DATA_DIR, CACHE_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ---- Logging --------------------------------------------------------------
def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s | %(message)s")

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    fh = logging.FileHandler(LOG_DIR / "skill.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


log = get_logger("a-stock")

# ---- Config ---------------------------------------------------------------
_DEFAULT_RISK = CONFIG_DIR / "risk_conservative.yaml"


def load_risk_config() -> dict[str, Any]:
    """Load the user's active risk config.

    Priority:
      1. $GP_SKILL_RISK_CONFIG env var (absolute path)
      2. config/risk.yaml (user's chosen file, copied from a preset)
      3. config/risk_conservative.yaml (fall back to safest default)
    """
    env_path = os.environ.get("GP_SKILL_RISK_CONFIG")
    if env_path and Path(env_path).exists():
        path = Path(env_path)
    elif (CONFIG_DIR / "risk.yaml").exists():
        path = CONFIG_DIR / "risk.yaml"
    else:
        log.warning("No risk.yaml found, using conservative default")
        path = _DEFAULT_RISK

    with path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["_source_path"] = str(path)
    return cfg


def load_watchlist() -> list[str]:
    """Load user's watchlist symbols from config/watchlist.yaml."""
    path = CONFIG_DIR / "watchlist.yaml"
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    symbols = data.get("symbols", []) or []
    return [str(s).zfill(6) for s in symbols]


# ---- Disk-backed JSON cache ----------------------------------------------
def _cache_path(key: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
    return CACHE_DIR / f"{safe}.json"


def cache_get(key: str, ttl_seconds: int) -> Any | None:
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            blob = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if time.time() - blob.get("_ts", 0) > ttl_seconds:
        return None
    return blob.get("payload")


def cache_set(key: str, payload: Any) -> None:
    path = _cache_path(key)
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump({"_ts": time.time(), "payload": payload}, f,
                      ensure_ascii=False, default=str)
    except OSError as e:
        log.warning("cache_set failed for %s: %s", key, e)


# ---- Time/date utilities --------------------------------------------------
import datetime as _dt


def today_str() -> str:
    return _dt.date.today().strftime("%Y%m%d")


def days_ago_str(n: int) -> str:
    return (_dt.date.today() - _dt.timedelta(days=n)).strftime("%Y%m%d")


def is_trading_hours(now: _dt.datetime | None = None) -> bool:
    """Return True if now is during A-share continuous auction.

    Does NOT account for holidays; callers should check the trading calendar
    separately (via fetch_data.is_trading_day).
    """
    now = now or _dt.datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return (_dt.time(9, 30) <= t <= _dt.time(11, 30)) or \
           (_dt.time(13, 0) <= t <= _dt.time(14, 57))


def is_call_auction(now: _dt.datetime | None = None) -> bool:
    """Call auction windows where skill refuses to place orders."""
    now = now or _dt.datetime.now()
    t = now.time()
    return (_dt.time(9, 15) <= t <= _dt.time(9, 30)) or \
           (_dt.time(14, 57) <= t <= _dt.time(15, 0))


# ---- Symbol helpers -------------------------------------------------------
def normalize_symbol(sym: str) -> str:
    """Ensure symbol is a 6-digit string, zero-padded if needed."""
    s = str(sym).strip().upper()
    # strip common prefixes like SH/SZ/BJ
    for prefix in ("SH", "SZ", "BJ", "SH.", "SZ.", "BJ."):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    s = s.lstrip(".")
    if len(s) < 6:
        s = s.zfill(6)
    return s


def market_of(symbol: str) -> str:
    """Return SH / SZ / BJ / STAR / CHINEXT based on prefix."""
    s = normalize_symbol(symbol)
    if s.startswith(("60", "68", "11")):
        return "STAR" if s.startswith("688") else "SH"
    if s.startswith(("00", "30", "12")):
        return "CHINEXT" if s.startswith("300") else "SZ"
    if s.startswith(("43", "83", "87", "88", "92")):
        return "BJ"
    return "UNKNOWN"


def is_st(symbol: str, name: str | None = None) -> bool:
    """Best-effort ST detection by stock name. Call sites should also
    double-check via authoritative status fields from the data provider."""
    if not name:
        return False
    name = name.upper().replace(" ", "")
    return any(tag in name for tag in ("ST", "*ST", "S*ST", "退"))


# ---- Money formatting -----------------------------------------------------
def yuan(x: float) -> str:
    if x >= 1e8:
        return f"{x/1e8:.2f}亿"
    if x >= 1e4:
        return f"{x/1e4:.2f}万"
    return f"{x:.2f}"
