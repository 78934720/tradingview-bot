"""Functional Hyperliquid TESTNET tests (network): order placement, cancels, TP/SL.

These hit the Hyperliquid testnet API and are skipped when no private key is
configured (HYPERLIQUID_PRIVATE_KEY in .env). The global testnet override is
forced on, so a live account is never touched.

Testnet quirks handled here:
- BTC spot (BTC/USDC) is degenerate (whole-BTC lot sizes, no bids), so HYPE/USDC
  is used for spot execution; symbol resolution is still tested for BTCUSD.
- XAUUSD is not listed on testnet; resolution falls back to the perp mapping
  but the live XAU checks are skipped.
"""
from __future__ import annotations

import math
import time

import pytest

from src.config import settings
from src.exchanges.hyperliquid import HyperliquidExchange
from src.runtime_settings import set_use_testnet_override

pytestmark = pytest.mark.skipif(
    not (settings.hyperliquid_private_key or "").strip(),
    reason="HYPERLIQUID_PRIVATE_KEY not set",
)

SPOT_TICKER, SPOT_SYM = "HYPEUSD", "HYPE/USDC"
PERP_TICKER, PERP_SYM = "BTCUSD.P", "BTC/USDC:USDC"
MIN_ORDER_VALUE_USDC = 10.0


@pytest.fixture(autouse=True)
def _force_testnet():
    set_use_testnet_override(True)
    yield
    set_use_testnet_override(None)


@pytest.fixture(scope="module")
def ex() -> HyperliquidExchange:
    return HyperliquidExchange()


@pytest.fixture(scope="module")
def client(ex):
    client = ex._get_client()
    client.load_markets()
    return client


def _step(client, sym: str) -> float:
    return float(client.markets[sym]["precision"].get("amount") or 0) or 1e-6


def _amount(client, sym: str, notional_usd: float, price: float) -> float:
    """Base amount for a ~notional_usd order, rounded up to the market's step."""
    step = _step(client, sym)
    min_amount = float(client.markets[sym].get("limits", {}).get("amount", {}).get("min") or 0)
    amount = max(notional_usd / price, min_amount)
    return math.ceil(amount / step - 1e-9) * step


def _position_size(client, sym: str) -> float:
    for p in client.fetch_positions():
        if p.get("symbol") == sym:
            return abs(float(p.get("contracts") or 0))
    return 0.0


def _cancel_all(client, sym: str) -> None:
    try:
        ids = [o["id"] for o in client.fetch_open_orders(sym)]
        if ids:
            client.cancel_orders(ids, sym)
    except Exception:
        pass


def _close_perp(client, sym: str) -> None:
    size = _position_size(client, sym)
    if size:
        bid = float(client.fetch_order_book(sym, 1)["bids"][0][0])
        client.create_order(sym, "market", "sell", size, bid, {"reduceOnly": True})


def _ensure_spot_usdc(client, need: float) -> None:
    """HL keeps spot and perp USDC in separate wallets; top up spot from perp."""
    spot = client.fetch_balance({"type": "spot"})
    free = float(spot["USDC"]["free"]) if spot.get("USDC") else 0.0
    if free >= need:
        return
    swap = client.fetch_balance({"type": "swap"})
    available = float(swap["USDC"]["free"]) if swap.get("USDC") else 0.0
    assert available >= need, f"not enough testnet USDC: swap={available}, spot={free}"
    client.transfer("USDC", need, "swap", "spot")


def _spot_sell_back(client, sym: str, amount: float) -> float:
    """Marketable limit sell sized to the bid depth; returns the amount sold."""
    ob = client.fetch_order_book(sym, 5)
    bids = ob.get("bids") or []
    if not bids:
        return 0.0
    bid = float(bids[0][0])
    depth = sum(float(level[1]) for level in bids)
    sell_amt = min(amount, depth * 0.9)
    if sell_amt * bid < MIN_ORDER_VALUE_USDC:
        sell_amt = _amount(client, sym, MIN_ORDER_VALUE_USDC * 1.5, bid)
    if sell_amt > 0:
        order = client.create_order(sym, "limit", "sell", sell_amt, bid * 0.99)
        time.sleep(1)
        open_ids = [o["id"] for o in client.fetch_open_orders(sym)]
        if order["id"] in open_ids:
            client.cancel_orders([order["id"]], sym)
    return sell_amt


def test_resolve_symbol_spot_vs_perp(client, ex):
    assert ex.resolve_symbol("BTCUSD") == "BTC/USDC"
    assert ex.resolve_symbol("BTCUSDT") == "BTC/USDC"
    assert ex.resolve_symbol("BTCUSDC") == "BTC/USDC"
    assert ex.resolve_symbol("HYPEUSD") == "HYPE/USDC"
    assert ex.resolve_symbol("BTCUSD.P") == "BTC/USDC:USDC"
    assert ex.resolve_symbol("BTCUSDT.P") == "BTC/USDC:USDC"
    assert ex.resolve_symbol("BTC/USDC") == "BTC/USDC"
    assert ex.resolve_symbol("BTC/USDC:USDC") == "BTC/USDC:USDC"
    if "XAU/USDC:USDC" in client.markets:
        # no spot XAU on Hyperliquid -> falls back to the perp
        assert ex.resolve_symbol("XAUUSD") == "XAU/USDC:USDC"


def test_spot_market_buy_tp_sl_sell_round_trip(client, ex):
    sym = ex.resolve_symbol(SPOT_TICKER)
    assert sym == SPOT_SYM, f"expected {SPOT_SYM}, got {sym}"
    # minimal amount: bought qty x bid must clear the 10 USDC min order value on the sell-back
    notional = 30.0
    _ensure_spot_usdc(client, notional * 1.1)
    buy = ex.place_market_order(SPOT_TICKER, "buy", notional)
    assert buy.success, buy.message
    try:
        time.sleep(1)
        fills = client.fetch_my_trades(sym, limit=5)
        bought = sum(float(t["amount"]) for t in fills if t["side"] == "buy")
        assert bought > 0, "spot buy produced no fills"
        assert not client.fetch_open_orders(sym), "unexpected open orders after spot buy"

        # TP/SL trigger orders are NOT supported on HL spot yet: the connector
        # must fail gracefully with a clear message (no raw API error, no retry)
        ob = client.fetch_order_book(sym, 2)
        mid = (float(ob["bids"][0][0]) + float(ob["asks"][0][0])) / 2
        res = ex.place_tp_sl_orders(
            sym, "buy", bought,
            take_profit_price=mid * 1.02, stop_loss_price=mid * 0.98,
        )
        assert not res.success
        assert "spot" in res.message.lower(), res.message
        assert not client.fetch_open_orders(sym), "TP/SL attempt left open orders"

        # sell back what the bid depth can absorb; rest of the cleanup is in finally
        sold = _spot_sell_back(client, sym, bought)
        assert sold > 0 or bought <= MIN_ORDER_VALUE_USDC, "spot sell-back could not be placed"
        assert not client.fetch_open_orders(sym), "open orders left after spot round trip"
    finally:
        _cancel_all(client, sym)


def test_perp_market_round_trip(client, ex):
    sym = ex.resolve_symbol(PERP_TICKER)
    assert sym == PERP_SYM
    buy = ex.place_market_order(PERP_TICKER, "buy", 10.0)
    assert buy.success, buy.message
    try:
        time.sleep(1)
        assert _position_size(client, sym) > 0, "perp buy left no position"
        _close_perp(client, sym)
        assert _position_size(client, sym) == 0, "perp position not closed"
        assert not client.fetch_open_orders(sym), "open orders left after perp round trip"
    finally:
        _cancel_all(client, sym)
        _close_perp(client, sym)


@pytest.mark.parametrize("ticker,sym", [(SPOT_TICKER, SPOT_SYM), (PERP_TICKER, PERP_SYM)])
def test_cancel_open_orders(client, ex, ticker, sym):
    price = float(client.fetch_order_book(sym, 1)["bids"][0][0])
    # HL rejects limit prices more than 80% away from the reference price
    low = price * 0.9
    amount = _amount(client, sym, MIN_ORDER_VALUE_USDC * 2, low)
    ids = []
    try:
        o1 = client.create_order(sym, "limit", "buy", amount, low)
        o2 = client.create_order(sym, "limit", "buy", amount, low * 0.9)
        ids = [o1["id"], o2["id"]]
        open_ids = [o["id"] for o in client.fetch_open_orders(sym)]
        assert all(i in open_ids for i in ids), f"limit orders not open: {open_ids}"
        client.cancel_orders(ids, sym)
        open_ids = [o["id"] for o in client.fetch_open_orders(sym)]
        assert not any(i in open_ids for i in ids), "orders still open after cancel"
    finally:
        _cancel_all(client, sym)


def test_perp_tp_sl_trigger_orders(client, ex):
    sym = ex.resolve_symbol(PERP_TICKER)
    buy = ex.place_market_order(PERP_TICKER, "buy", 10.0)
    assert buy.success, buy.message
    try:
        time.sleep(1)
        size = _position_size(client, sym)
        assert size > 0
        bid = float(client.fetch_order_book(sym, 2)["bids"][0][0])
        tp, sl = bid * 1.02, bid * 0.98
        res = ex.place_tp_sl_orders(
            sym, "buy", size,
            take_profit_price=tp, stop_loss_price=sl,
        )
        assert res.success, res.message
        time.sleep(1)
        triggers = [o for o in client.fetch_open_orders(sym) if o.get("triggerPrice")]
        assert len(triggers) == 2, f"expected 2 trigger orders, got {len(triggers)}"
        prices = {float(o["triggerPrice"]) for o in triggers}
        assert any(abs(tp - p) / tp < 0.01 for p in prices), prices
        assert any(abs(sl - p) / sl < 0.01 for p in prices), prices
        _cancel_all(client, sym)
        _close_perp(client, sym)
        assert _position_size(client, sym) == 0
    finally:
        _cancel_all(client, sym)
        _close_perp(client, sym)


def test_account_data_endpoints(client, ex):
    assert ex.balance_currency() == "USDC"
    spot_bal = client.fetch_balance({"type": "spot"})
    swap_bal = client.fetch_balance({"type": "swap"})
    assert spot_bal.get("USDC") is not None, "spot wallet missing USDC"
    assert swap_bal.get("USDC") is not None, "swap wallet missing USDC"
    assert isinstance(ex.fetch_daily_pnl_usdt(), float)
    assert isinstance(ex.fetch_total_exposure_usdt(), float)
    bid, ask, mid = ex.get_orderbook(SPOT_TICKER)
    assert 0 < bid < ask, f"bad orderbook {bid}/{ask}"
    assert ex.get_last_price(SPOT_TICKER) > 0
    assert ex.get_last_price(PERP_TICKER) > 0
    assert ex.get_last_price("BTCUSD") > 0  # degenerate spot market still prices via book
