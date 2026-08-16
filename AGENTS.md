# AGENTS.md

TradingView webhook bot: receives TradingView (Pine Script) alerts as JSON at `POST /webhook`, runs risk checks + optional LLM, places market orders on Binance/Bybit/OKX/Hyperliquid futures, then attaches TP/SL and optional trailing stop. Pure Python, no packaging (no pyproject.toml), `requirements.txt` only.

## Commands

- Run: `python src/main.py` (uvicorn `server:app`, `HOST`/`PORT` from `.env`, default 0.0.0.0:8000)
- Tests: `pytest tests/ -q` — no test config, no fixtures/conftest; tests import repo modules directly
- No linter/typecheck config exists. `.venv` uses Python 3.14; README claims 3.10+.

## Env / config gotchas

- All settings come from `config.py` (pydantic-settings), loaded from `.env` at import time. `.env` is gitignored and present locally — never commit or log it; it contains exchange API keys.
- Tests are **not hermetic**: `risk.py` reads module-level `settings`, so tests assume default caps (`MAX_POSITION_SIZE_USDT=1000`, etc.). The local `.env` sets `MAX_POSITION_SIZE_USDT=10`, so `tests/test_risk.py::test_check_position_size` fails locally. Move `.env` aside (or use default caps) for a clean suite.
- `webhook_bot.db` is local SQLite state (orders, idempotency, risk cooldown) — gitignored, safe to delete.
- An exchange is enabled only if its API key env vars are non-empty (`config.Settings.exchange_enabled`), not by any separate flag.

## Architecture / pipeline

- `server.py` — FastAPI app and the whole webhook execution pipeline in `_execute_payload` (server.py:168): auth → idempotency → AI enhancer → risk checks (fixed order: allowlist, size cap, per-symbol cap, total exposure, leverage cap, daily-loss reject, cooldown, spread) → LLM advisor → `place_market_order` with retries → TP/SL → record trailing stop.
- `trailing_stop.py` runs its own background loop (`TRAILING_STOP_CHECK_INTERVAL_SECONDS`).
- `main.py` is a 10-line uvicorn launcher; almost all logic lives in `server.py`, `risk.py`, `db.py`, `tpsl.py`, `trailing_stop.py` (all flat at repo root, no `src/` package).
- Dashboard is a single static page `static/dashboard.html` (Tailwind CDN + Lightweight Charts), served by FastAPI.

## Exchange layer

- `exchanges/base.py` — `ExchangeBase` ABC; symbol mapping helpers are the subtle part: `ccxt_symbol()` (TradingView-style → CCXT), `tv_symbol()` (inverse), `canonical_symbol()` (→ `BASEUSDT` risk key). Invariant, enforced by `tests/test_hyperliquid.py::test_exposure_key_round_trip`: `tv_symbol(ccxt_symbol(s)) == canonical_symbol(s)` — keep it when editing mappings.
- `exchanges/registry.py` — module-level singleton registry built once; each connector caches its CCXT client. After changing live/testnet, call `reset_all_clients()` (or `ExchangeBase.reset_client()`); the dashboard Live/Testnet switch goes through `runtime_settings.set_use_testnet_override` (global override beats per-exchange env flags).
- `size_usdt` is notional (margin × leverage); orders are `create_order(..., type="market")` converted from notional via last price.

## Hyperliquid (exchanges/hyperliquid.py)

- Docs root: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api (official API reference — trust it over CCXT docs). Sitemap: `https://hyperliquid.gitbook.io/hyperliquid-docs/sitemap.xml`.
- Direct pages for concepts this bot touches:
  - Order placement incl. trigger orders, `stopLossPrice`/`takeProfitPrice`/`reduceOnly`, `reduceOnly` → `/for-developers/api/exchange-endpoint`
  - Account data: balances, `userFills` (has `closedPnl`), positions/exposure → `/for-developers/api/info-endpoint` and `/for-developers/api/info-endpoint/perpetuals`
  - TP/SL order semantics → `/trading/take-profit-and-stop-loss-orders-tp-sl`
  - Order types, margining, contract specs (tick/lot size) → `/trading/order-types`, `/trading/margining`, `/for-developers/api/tick-and-lot-size`
  - Error responses, rate limits, signing → `/for-developers/api/error-responses`, `/for-developers/api/rate-limits-and-user-limits`, `/for-developers/api/signing`
  - Testnet funds → `/onboarding/testnet-faucet`
- Perps are **USDC-margined**, priced in USDT (quanto) — `*_USDT` risk caps map 1:1 to USDC. `balance_currency()` returns `"USDC"`, not `"USDT"`.
- CCXT symbols are `BASE/USDC:USDC`; `ccxt_symbol()` strips TradingView `.P` perp suffixes (`BTCUSDT.P`, `BTCUSD.P`) and accepts USDT/USDC/USD quote suffixes (`HYPEUSDC` → `HYPE/USDC:USDC`).
- Auth is `HYPERLIQUID_PRIVATE_KEY` (required; raw private key string, not a mnemonic); `HYPERLIQUID_WALLET_ADDRESS` optional. Testnet via `set_sandbox_mode(True)`.
- Daily PnL comes from `fetch_my_trades` `info.closedPnl`, cached 60 s per UTC day (`_PNL_CACHE`).
- TP/SL are **market trigger orders** (`{"stopLossPrice": ..., "reduceOnly": True}` / `{"takeProfitPrice": ...}`), not limit orders.

## PineScript (strategies/*.pine)

- Docs: https://www.tradingview.com/pine-script-docs/ (Pine v5 language reference — `strategy.entry/close/exit`, `alert_message`, `request.*` calls, `timeframe.change`). No sitemap (checked robots.txt → global `sitemap.xml` only lists the docs root; index the nav tree instead). The default docs are **v6**; repo scripts are `//@version=5`, so prefix URLs with `/pine-script-docs/v5/...`.
- Direct pages for concepts used by the strategies:
  - `strategy.entry/close/close_all/exit`, pyramiding, order placement → `/pine-script-docs/v5/concepts/strategies`
  - `alert_message`, `{{strategy.order.alert_message}}`, alert setup → `/pine-script-docs/v5/concepts/alerts`
  - `request.earnings/dividends/splits`, external data → `/pine-script-docs/v5/concepts/other-timeframes-and-data`
  - `timeframe.change`, MTF/HTF → `/pine-script-docs/v5/concepts/timeframes`; sessions/time → `/pine-script-docs/v5/concepts/sessions`, `/pine-script-docs/v5/concepts/time`
  - Execution model (`barstate.*`, calc on every tick) → `/pine-script-docs/v5/language/execution-model`
  - `input.*` (incl. the "Alert Message IDs" group) → `/pine-script-docs/v5/concepts/inputs`
  - Built-in function reference (`fun_strategy.entry`, `fun_ta.vwap`, …) → https://www.tradingview.com/pine-script-reference/v5/
- `strategies/` is **gitignored** (`*.pine`) — these files are not part of the repo, are not executed by the bot, and are TradingView-side only. Don't expect them in `git status`, and don't wire them into the bot.
- `vwap_strategy.pine` (1438 lines, the main strategy, v5) and `vwap_rhythm.pine` / `vwap_angle.pine` (supporting indicators) share the same anchor system (Session/Week/…/Year) and stdev-band VWAP structure. `vwap_strategy.pine` has MTF/HTF filters, hedge mode, pyramiding, equity protector, and limit entries at stdev bands.
- The strategy fires alerts via `alert_message = uuid_entrylong/uuid_exitlong/uuid_entryshort/uuid_exitshort` — opaque UUID strings set in the "Alert Message IDs" inputs group (TradingView alert setup is expected to wrap them in the JSON the webhook expects). The bot's webhook payload contract lives in `models.py`/README: `secret`, `exchange`, `symbol`, `side`, `size_usdt`, optional `leverage`, `take_profit`, `stop_loss`, `trailing_stop`, `trailing_activation_pct`, `request_id` (idempotency key).
- Pine scripts are edited as text only; there is no local compiler/backtester here.
