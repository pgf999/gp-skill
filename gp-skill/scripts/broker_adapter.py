"""Broker adapter — ABSTRACT INTERFACE ONLY. Default mode is disabled.

To actually trade with real money, the user must:
  1. Set config/risk.yaml: trade.execution_mode = "live"
  2. Export broker-specific env vars (see concrete adapters below)
  3. Install broker SDK (xtquant for QMT, futu-api for Futu)
  4. Confirm each order with the exact string "确认"

We deliberately do not ship a working adapter; the risk of shipping one
that silently succeeds (and loses the user real money on a bad signal)
is too high. The user must wire in their own credentials and test on
their specific broker first.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ._common import log, load_risk_config


@dataclass
class OrderRequest:
    symbol: str
    side: str          # "buy" | "sell"
    qty: int
    price: float       # 0 means market order
    order_type: str = "limit"  # "limit" | "market"


@dataclass
class OrderResult:
    ok: bool
    order_id: str | None
    filled_qty: int
    filled_price: float
    error: str | None = None


class BrokerAdapter(ABC):
    """All broker integrations must subclass this."""

    @abstractmethod
    def connect(self) -> bool: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def get_balance(self) -> dict[str, Any]: ...

    @abstractmethod
    def get_positions(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def place_order(self, order: OrderRequest) -> OrderResult: ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool: ...

    @abstractmethod
    def get_order_status(self, order_id: str) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Disabled default (refuses all trades)
# ---------------------------------------------------------------------------
class DisabledAdapter(BrokerAdapter):
    """The default adapter. Never places an order."""

    def connect(self) -> bool:
        log.info("Broker adapter is DISABLED (paper trading only)")
        return True

    def disconnect(self) -> None:
        pass

    def get_balance(self) -> dict[str, Any]:
        raise NotImplementedError(
            "Live broker is disabled. Set config/risk.yaml: "
            "trade.execution_mode = 'live' and configure a real adapter."
        )

    def get_positions(self) -> list[dict[str, Any]]:
        raise NotImplementedError("Live broker is disabled.")

    def place_order(self, order: OrderRequest) -> OrderResult:
        return OrderResult(
            ok=False, order_id=None, filled_qty=0, filled_price=0.0,
            error="Live trading disabled. This is a safety feature, not a bug. "
                  "To enable: set config/risk.yaml: trade.execution_mode='live' "
                  "and wire in a concrete broker adapter."
        )

    def cancel_order(self, order_id: str) -> bool:
        return False

    def get_order_status(self, order_id: str) -> dict[str, Any]:
        return {"status": "disabled"}


# ---------------------------------------------------------------------------
# Scaffolds for real adapters — user must fill in
# ---------------------------------------------------------------------------
class QMTAdapter(BrokerAdapter):
    """For 国金 QMT / 华泰 MATIC style miniQMT.

    Prerequisites:
      * miniQMT.exe running and logged in
      * xtquant installed: pip install xtquant
      * Your account must be white-listed for the trading interface
    """

    def __init__(self, account: str, host: str = "127.0.0.1",
                 port: int = 58610):
        self.account = account
        self.host = host
        self.port = port
        self._xt_trader = None

    def connect(self) -> bool:
        try:
            from xtquant import xttrader  # type: ignore
        except ImportError:
            log.error("xtquant not installed. pip install xtquant")
            return False
        # TODO: user-specific session setup
        # self._xt_trader = xttrader.XtQuantTrader(...)
        # self._xt_trader.connect()
        raise NotImplementedError(
            "QMTAdapter is a scaffold. Fill in connect() per your broker's "
            "xtquant setup. See miniQMT docs."
        )

    def disconnect(self) -> None:
        if self._xt_trader:
            self._xt_trader.stop()
            self._xt_trader = None

    def get_balance(self) -> dict[str, Any]:
        raise NotImplementedError("QMTAdapter.get_balance: not wired up")

    def get_positions(self) -> list[dict[str, Any]]:
        raise NotImplementedError("QMTAdapter.get_positions: not wired up")

    def place_order(self, order: OrderRequest) -> OrderResult:
        raise NotImplementedError(
            "QMTAdapter.place_order is a scaffold. When implementing: "
            "use xtquant order_stock, handle sync vs async fills, "
            "ensure failure does not get retried (may double-fill)."
        )

    def cancel_order(self, order_id: str) -> bool:
        raise NotImplementedError("QMTAdapter.cancel_order: not wired up")

    def get_order_status(self, order_id: str) -> dict[str, Any]:
        raise NotImplementedError("QMTAdapter.get_order_status: not wired up")


class FutuAdapter(BrokerAdapter):
    """For 富途 OpenAPI (港股/美股/部分沪深港通).

    Prerequisites:
      * FutuOpenD running and logged in
      * futu-api installed: pip install futu-api
      * RSA key configured if needed
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 11111):
        self.host = host
        self.port = port
        self._quote_ctx = None
        self._trade_ctx = None

    def connect(self) -> bool:
        try:
            import futu as ft  # type: ignore
        except ImportError:
            log.error("futu-api not installed. pip install futu-api")
            return False
        raise NotImplementedError(
            "FutuAdapter is a scaffold. See "
            "https://openapi.futunn.com/futu-api-doc for setup."
        )

    def disconnect(self) -> None:
        pass

    def get_balance(self) -> dict[str, Any]:
        raise NotImplementedError

    def get_positions(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def place_order(self, order: OrderRequest) -> OrderResult:
        raise NotImplementedError

    def cancel_order(self, order_id: str) -> bool:
        raise NotImplementedError

    def get_order_status(self, order_id: str) -> dict[str, Any]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def get_broker(config: dict | None = None) -> BrokerAdapter:
    """Return the configured broker adapter. Default: DisabledAdapter."""
    cfg = config or load_risk_config()
    trade = cfg.get("trade", {}) or {}
    mode = trade.get("execution_mode", "paper").lower()
    if mode != "live":
        return DisabledAdapter()

    broker_type = (trade.get("broker") or "").lower()
    if broker_type == "qmt":
        account = trade.get("qmt_account", "")
        if not account:
            raise ValueError("live mode=qmt requires trade.qmt_account")
        return QMTAdapter(account=account)
    if broker_type == "futu":
        return FutuAdapter()

    raise ValueError(
        f"Unknown broker type: {broker_type!r}. "
        "Set trade.broker to 'qmt' or 'futu', or extend broker_adapter.py."
    )
