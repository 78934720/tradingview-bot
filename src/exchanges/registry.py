"""
Exchange registry: returns enabled exchange by name.
"""
from __future__ import annotations

from typing import Dict, Optional

from src.config import settings
from src.exchanges.base import ExchangeBase
from src.exchanges.binance import BinanceExchange
from src.exchanges.bybit import BybitExchange
from src.exchanges.hyperliquid import HyperliquidExchange
from src.exchanges.okx import OKXExchange

_registry: Dict[str, ExchangeBase] = {}


def _init_registry() -> None:
    if _registry:
        return
    if settings.exchange_enabled("binance"):
        _registry["binance"] = BinanceExchange()
    if settings.exchange_enabled("bybit"):
        _registry["bybit"] = BybitExchange()
    if settings.exchange_enabled("okx"):
        _registry["okx"] = OKXExchange()
    if settings.exchange_enabled("hyperliquid"):
        _registry["hyperliquid"] = HyperliquidExchange()


def get_exchange(name: str) -> Optional[ExchangeBase]:
    _init_registry()
    return _registry.get(name.lower())


def get_all_exchanges() -> Dict[str, ExchangeBase]:
    _init_registry()
    return dict(_registry)


def reset_all_clients() -> None:
    """Clear cached API clients for all exchanges (e.g. after switching live/testnet)."""
    _init_registry()
    for ex in _registry.values():
        ex.reset_client()
