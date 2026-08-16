"""Tests for Hyperliquid connector symbol/currency mapping (no network)."""
from src.exchanges.base import ExchangeBase
from src.exchanges.binance import BinanceExchange
from src.exchanges.hyperliquid import HyperliquidExchange


def test_hl_ccxt_symbol_spot():
    ex = HyperliquidExchange()
    assert ex.ccxt_symbol("BTCUSD") == "BTC/USDC"
    assert ex.ccxt_symbol("BTCUSDT") == "BTC/USDC"
    assert ex.ccxt_symbol("BTCUSDC") == "BTC/USDC"
    assert ex.ccxt_symbol("ETHUSD") == "ETH/USDC"
    assert ex.ccxt_symbol("HYPEUSDC") == "HYPE/USDC"
    assert ex.ccxt_symbol("SOLUSD") == "SOL/USDC"
    assert ex.ccxt_symbol("XAUUSD") == "XAU/USDC"


def test_hl_ccxt_symbol_perp_suffix():
    ex = HyperliquidExchange()
    assert ex.ccxt_symbol("BTCUSD.P") == "BTC/USDC:USDC"
    assert ex.ccxt_symbol("BTCUSDT.P") == "BTC/USDC:USDC"
    assert ex.ccxt_symbol("ethusdt.p") == "ETH/USDC:USDC"
    assert ex.ccxt_symbol("XAUUSD.P") == "XAU/USDC:USDC"


def test_hl_ccxt_symbol_passthrough():
    ex = HyperliquidExchange()
    assert ex.ccxt_symbol("BTC/USDC:USDC") == "BTC/USDC:USDC"
    assert ex.ccxt_symbol("BTC/USDC") == "BTC/USDC"


def test_default_ccxt_symbol_keeps_usdt_quote():
    assert BinanceExchange().ccxt_symbol("BTCUSDT") == "BTC/USDT"


def test_tv_symbol_inverse():
    ex = HyperliquidExchange()
    assert ex.tv_symbol("BTC/USDC:USDC") == "BTCUSDT"
    assert ex.tv_symbol("ETH/USDT:USDT") == "ETHUSDT"
    assert ex.tv_symbol("BTC/USDC") == "BTCUSDT"
    assert ex.tv_symbol("BTCUSDT") == "BTCUSDT"


def test_canonical_symbol():
    ex = HyperliquidExchange()
    assert ex.canonical_symbol("BTCUSDT") == "BTCUSDT"
    assert ex.canonical_symbol("BTCUSD.P") == "BTCUSDT"
    assert ex.canonical_symbol("BTCUSDT.P") == "BTCUSDT"
    assert ex.canonical_symbol("SOLUSD") == "SOLUSDT"
    assert ex.canonical_symbol("HYPEUSDC") == "HYPEUSDT"
    assert ex.canonical_symbol("XAUUSD") == "XAUUSDT"
    assert BinanceExchange().canonical_symbol("BTCUSDT") == "BTCUSDT"


def test_exposure_key_round_trip():
    """canonical_symbol must match tv_symbol(ccxt_symbol(s)) so risk lookups align."""
    hl = HyperliquidExchange()
    for s in ("BTCUSDT", "BTCUSDT.P", "BTCUSD.P", "SOLUSD", "HYPEUSDC", "ETHUSDT.P", "XAUUSD", "BTCUSDC", "BTCUSD"):
        assert hl.tv_symbol(hl.ccxt_symbol(s)) == hl.canonical_symbol(s), s
    bn = BinanceExchange()
    assert bn.tv_symbol(bn.ccxt_symbol("BTCUSDT")) == bn.canonical_symbol("BTCUSDT")


def test_balance_currency():
    assert HyperliquidExchange().balance_currency() == "USDC"
    assert BinanceExchange().balance_currency() == "USDT"
