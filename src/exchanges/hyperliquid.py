"""
Hyperliquid connector (CCXT).

Hyperliquid perps are USDC-margined with prices quoted in USDT (quanto, no
USDC/USDT conversion applied), so USD-notional risk caps (*_USDT) map 1:1 to
USDC. CCXT exposes perps as BASE/USDC:USDC; the TradingView-style BASEUSDT
symbols must be mapped accordingly (TradingView HL perps may also carry a
.P suffix, e.g. BTCUSD.P).
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Optional

import ccxt

from src.config import settings
from src.exchanges.base import ExchangeBase, OrderResult
from src.runtime_settings import get_use_testnet

_PNL_CACHE: dict[str, list] = {}
_PNL_CACHE_TTL_SECONDS = 60


class HyperliquidExchange(ExchangeBase):
    def __init__(self) -> None:
        self._client: Optional[ccxt.Exchange] = None

    def reset_client(self) -> None:
        self._client = None

    def _get_client(self) -> ccxt.Exchange:
        if self._client is None:
            opts = {"privateKey": settings.hyperliquid_private_key, "enableRateLimit": True}
            if settings.hyperliquid_wallet_address:
                opts["walletAddress"] = settings.hyperliquid_wallet_address
            self._client = ccxt.hyperliquid(opts)
            if get_use_testnet(self.name()):
                self._client.set_sandbox_mode(True)
        return self._client

    def name(self) -> str:
        return "hyperliquid"

    def ccxt_symbol(self, symbol: str) -> str:
        """TradingView-style (BTCUSDT, BTCUSDT.P, BTCUSD.P) -> HL perp symbol (BTC/USDC:USDC)."""
        s = symbol.upper().strip()
        if "/" in s:
            return s
        s = re.sub(r"\.P$", "", s)
        for suffix in ("USDT", "USDC", "USD"):
            if s.endswith(suffix):
                return s[:-len(suffix)] + "/USDC:USDC"
        return s + "/USDC:USDC"

    def balance_currency(self) -> str:
        """Hyperliquid collateral is USDC, not USDT."""
        return "USDC"

    def get_last_price(self, symbol: str) -> float:
        try:
            client = self._get_client()
            ticker = client.fetch_ticker(self.ccxt_symbol(symbol))
            return float(ticker.get("last") or ticker.get("close") or 0)
        except Exception:
            return 0.0

    def get_orderbook(self, symbol: str, limit: int = 5) -> tuple[float, float, float]:
        try:
            client = self._get_client()
            ob = client.fetch_order_book(self.ccxt_symbol(symbol), limit)
            bids = ob.get("bids") or []
            asks = ob.get("asks") or []
            bid = float(bids[0][0]) if bids else 0.0
            ask = float(asks[0][0]) if asks else 0.0
            mid = (bid + ask) / 2.0 if (bid and ask) else 0.0
            return bid, ask, mid
        except Exception:
            return 0.0, 0.0, 0.0

    def fetch_daily_pnl_usdt(self) -> float:
        """Today's realized PnL (settled in USDC, ~USD) from userFills closedPnl, cached for 60s."""
        day_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cached = _PNL_CACHE.get(day_key)
        if cached is not None and time.time() - cached[1] < _PNL_CACHE_TTL_SECONDS:
            return cached[0]
        try:
            client = self._get_client()
            today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
            trades = client.fetch_my_trades(since=int(today_start))
            total = 0.0
            for t in trades or []:
                closed_pnl = (t.get("info") or {}).get("closedPnl")
                if closed_pnl is not None:
                    total += float(closed_pnl)
            _PNL_CACHE[day_key] = [total, time.time()]
            for stale in [k for k in _PNL_CACHE if k != day_key]:
                del _PNL_CACHE[stale]
            return total
        except Exception:
            return 0.0

    def fetch_total_exposure_usdt(self) -> float:
        try:
            client = self._get_client()
            positions = client.fetch_positions()
            total = 0.0
            for p in positions or []:
                if p.get("contracts") and float(p.get("contracts", 0) or 0) != 0:
                    notional = abs(float(p.get("notional", 0) or 0)) or abs(float(p.get("contractSize", 0) or 0) * float(p.get("contracts", 0) or 0) * float(p.get("markPrice") or p.get("last", 0) or 0))
                    total += notional
            return total
        except Exception:
            return 0.0

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        try:
            client = self._get_client()
            client.set_leverage(leverage, self.ccxt_symbol(symbol))
            return True
        except Exception:
            return False

    def place_market_order(
        self,
        symbol: str,
        side: str,
        size_usdt: float,
        leverage: Optional[int] = None,
    ) -> OrderResult:
        try:
            client = self._get_client()
            client.load_markets()
            sym = self.ccxt_symbol(symbol)
            ticker = client.fetch_ticker(sym)
            price = float(ticker.get("last") or ticker.get("close") or 1)
            if price <= 0:
                return OrderResult(success=False, message="Could not get price")
            amount = size_usdt / price
            order = client.create_order(
                symbol=sym,
                type="market",
                side=side,
                amount=amount,
                price=price,
                params={},
            )
            order_id = order.get("id") or order.get("clientOrderId") or ""
            return OrderResult(success=True, order_id=str(order_id), message="Order placed")
        except Exception as e:
            return OrderResult(success=False, message=str(e))

    def place_tp_sl_orders(
        self,
        symbol: str,
        side: str,
        amount_base: float,
        take_profit_price: Optional[float] = None,
        stop_loss_price: Optional[float] = None,
    ) -> OrderResult:
        """Place TP/SL as market trigger orders (Hyperliquid only has USDC collateral; prices are USD)."""
        if not take_profit_price and not stop_loss_price:
            return OrderResult(success=True, message="No TP/SL to set")
        try:
            client = self._get_client()
            client.load_markets()
            sym = self.ccxt_symbol(symbol)
            close_side = "sell" if side == "buy" else "buy"
            if stop_loss_price:
                client.create_order(sym, "market", close_side, amount_base, stop_loss_price, {"stopLossPrice": stop_loss_price, "reduceOnly": True})
            if take_profit_price:
                client.create_order(sym, "market", close_side, amount_base, take_profit_price, {"takeProfitPrice": take_profit_price, "reduceOnly": True})
            return OrderResult(success=True, message="TP/SL set")
        except Exception as e:
            return OrderResult(success=False, message=str(e))
