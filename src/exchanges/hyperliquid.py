"""
Hyperliquid connector (CCXT).

Hyperliquid settles everything in USDC. Spot markets are BASE/USDC, perps are
BASE/USDC:USDC (prices quoted in USDT, quanto, no USDC/USDT conversion), so
USD-notional risk caps (*_USDT) map 1:1 to USDC.

TradingView tickers map as: spot (BTCUSD, BTCUSDT, BTCUSDC, ...) -> BASE/USDC;
perps carry the .P suffix (BTCUSD.P) -> BASE/USDC:USDC. resolve_symbol() is
the network-aware variant: it prefers the spot market and falls back to the
perp when no spot market exists (e.g. XAUUSD -> XAU/USDC:USDC).
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


class HyperliquidSigningClient(ccxt.hyperliquid):
    """ccxt.hyperliquid subclass with a corrected L1 signer.

    Stock ccxt (as of 4.5.x) signs the EIP-712 digest's hex *string* again
    (keccak over the ASCII hex) instead of the digest itself, so Hyperliquid
    recovers a garbage signer and rejects every signed request with
    "User or API Wallet <random> does not exist". Sign the digest itself,
    matching the official hyperliquid-python-sdk.
    """

    def sign_l1_action(self, action, nonce, vaultAddress=None, expiresAfter=None):
        is_testnet = self.safe_bool(self.options, "sandboxMode", False)
        phantom_agent = self.construct_phantom_agent(
            self.action_hash(action, vaultAddress, nonce, expiresAfter), is_testnet
        )
        digest = self.eth_encode_structured_data(
            {
                "chainId": 1337,
                "name": "Exchange",
                "verifyingContract": self.safe_string(self.options, "zeroAddress"),
                "version": "1",
            },
            {
                "Agent": [
                    {"name": "source", "type": "string"},
                    {"name": "connectionId", "type": "bytes32"},
                ],
            },
            phantom_agent,
        )
        # digest is b"\x19\x01" + domain separator + message hash; the signed
        # message is keccak256 of that entire pre-image
        signature = self.ecdsa(
            self.hash(digest, "keccak"), self.privateKey[-64:], "secp256k1", None
        )
        return {
            "r": "0x" + signature["r"],
            "s": "0x" + signature["s"],
            "v": self.sum(27, signature["v"]),
        }


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
            self._client = HyperliquidSigningClient(opts)
            # ccxt attaches its default builder to every order with a mixed-case
            # address; HL hashes the builder address lowercased, so the signature
            # never matches. Attach the same builder lowercased at 0% fee.
            self._client.options["approvedBuilderFee"] = True
            self._client.options["builderFee"] = False
            self._client.options["builder"] = "0x6530512a6c89c7cfcebc3ba7fcd9ada5f30827a6"
            if get_use_testnet(self.name()):
                self._client.set_sandbox_mode(True)
        return self._client

    def name(self) -> str:
        return "hyperliquid"

    def ccxt_symbol(self, symbol: str) -> str:
        """TradingView-style ticker -> HL CCXT symbol (offline, no markets lookup).

        Spot (no .P suffix): BTCUSD/BTCUSDT/BTCUSDC -> BTC/USDC.
        Perps (.P suffix, e.g. BTCUSD.P): -> BTC/USDC:USDC.
        Symbols already containing "/" are passed through unchanged.
        """
        s = symbol.upper().strip()
        if "/" in s:
            return s
        perp = s.endswith(".P")
        s = re.sub(r"\.P$", "", s)
        for suffix in ("USDT", "USDC", "USD"):
            if s.endswith(suffix):
                s = s[:-len(suffix)]
                break
        return f"{s}/USDC:USDC" if perp else f"{s}/USDC"

    def resolve_symbol(self, symbol: str) -> str:
        """Network-aware CCXT symbol: prefer the spot market (BTCUSD -> BTC/USDC),
        fall back to the perp when no spot market exists (XAUUSD -> XAU/USDC:USDC).
        Explicit CCXT symbols (contain "/") and .P tickers are returned as-is."""
        s = symbol.upper().strip()
        if "/" in s or s.endswith(".P"):
            return self.ccxt_symbol(s)
        client = self._get_client()
        client.load_markets()
        spot = self.ccxt_symbol(s)
        if spot in client.markets:
            return spot
        perp = spot.replace("/USDC", "/USDC:USDC", 1)
        return perp if perp in client.markets else spot

    def _is_spot_symbol(self, symbol: str) -> bool:
        """True if the CCXT symbol resolves to the spot market (no :QUOTE suffix)."""
        return ":" not in symbol

    def _reference_price(self, client: ccxt.Exchange, symbol: str, side: str = "buy") -> float:
        """Last/close price from the ticker, falling back to the order book (testnet
        spot often has no trades -> no ticker last). For a buy use the ask, for a
        sell the bid, else the mid."""
        try:
            ticker = client.fetch_ticker(symbol)
            price = float(ticker.get("last") or ticker.get("close") or 0)
            if price > 0:
                return price
        except Exception:
            pass
        return self._book_price(client, symbol, side)

    def _book_price(self, client: ccxt.Exchange, symbol: str, side: str = "buy") -> float:
        """Best book price for a market order: ask for a buy, bid for a sell.
        The ticker last can be stale (testnet spot), so book first."""
        bid, ask, mid = self.get_orderbook(symbol)
        if side == "sell" and bid:
            return bid
        if ask:
            return ask
        if bid:
            return bid
        return mid

    def balance_currency(self) -> str:
        """Hyperliquid collateral is USDC, not USDT."""
        return "USDC"

    def get_last_price(self, symbol: str) -> float:
        try:
            client = self._get_client()
            return self._reference_price(client, self.resolve_symbol(symbol))
        except Exception:
            return 0.0

    def get_orderbook(self, symbol: str, limit: int = 5) -> tuple[float, float, float]:
        try:
            client = self._get_client()
            ob = client.fetch_order_book(self.resolve_symbol(symbol), limit)
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
            sym = self.resolve_symbol(symbol)
            if self._is_spot_symbol(sym):
                return False  # spot has no leverage
            client.set_leverage(leverage, sym)
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
            sym = self.resolve_symbol(symbol)
            price = self._book_price(client, sym, side)
            if price <= 0:
                price = self._reference_price(client, sym, side)
            if price <= 0:
                return OrderResult(success=False, message="Could not get price")
            amount = float(client.amount_to_precision(sym, size_usdt / price))
            if amount <= 0:
                return OrderResult(success=False, message="Amount rounds to zero (market lot size too large)")
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
            sym = self.resolve_symbol(symbol)
            if self._is_spot_symbol(sym):
                return OrderResult(
                    success=False,
                    message="TP/SL trigger orders are not supported on Hyperliquid spot",
                )
            close_side = "sell" if side == "buy" else "buy"
            if stop_loss_price:
                client.create_order(sym, "market", close_side, amount_base, stop_loss_price, {"stopLossPrice": stop_loss_price, "reduceOnly": True})
            if take_profit_price:
                client.create_order(sym, "market", close_side, amount_base, take_profit_price, {"takeProfitPrice": take_profit_price, "reduceOnly": True})
            return OrderResult(success=True, message="TP/SL set")
        except Exception as e:
            return OrderResult(success=False, message=str(e))
