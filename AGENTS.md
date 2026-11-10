# AGENTS.md

TradingView webhook bot: receives TradingView (Pine Script) alerts as JSON at `POST /webhook`, runs risk checks + optional LLM, places market orders on Binance/Bybit/OKX/Hyperliquid futures, then attaches TP/SL and optional trailing stop. Pure Python, no packaging (no pyproject.toml), `requirements.txt` only.

## Commands

- Run: `python src/main.py` (uvicorn `server:app`, `HOST`/`PORT` from `.env`, default 0.0.0.0:8000)
- Tests: `pytest tests/ -q` — no test config, no fixtures/conftest; tests import repo modules directly
- `tests/test_hyperliquid_testnet.py` hits the **Hyperliquid testnet API** (orders, cancels, TP/SL) and is skipped when `HYPERLIQUID_PRIVATE_KEY` is unset; it forces the testnet override itself, so it never touches a live account
- Testnet USDC (faucet funds) is **finite** and spot round trips on testnet burn it (degenerate books: ETH spot bid ~50 vs ask ~150, no XAU, BTC spot is whole-BTC lots). Prefer **perps** for testnet functional tests, and use only **minimal amounts** everywhere (just above the 10 USDC min order value).
- No linter/typecheck config exists. `.venv` uses Python 3.14; README claims 3.10+.
- Pine syntax gate: `tools/openpinescript` (one-time build: `cd tools/openpinescript && npm ci --ignore-scripts && npm run build`); it is not a TradingView-parity backtester — see Local Pine tooling.

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
- Symbol mapping (offline, `ccxt_symbol()`): **no `.P` suffix → spot `BASE/USDC`** (BTCUSD/BTCUSDT/BTCUSDC → `BTC/USDC`); **`.P` suffix → perp `BASE/USDC:USDC`**. `resolve_symbol()` is the network-aware variant: prefers the spot market, falls back to perp when no spot market exists (XAUUSD → `XAU/USDC:USDC`). Spot and perp share the `BASEUSDT` risk key.
- **Do NOT "fix" the signing**: `HyperliquidSigningClient` (subclass of `ccxt.hyperliquid`) is required. Stock ccxt 4.5.x signs `keccak(ascii-hex(EIP-712 digest))` (double hash) and attaches its default builder with a mixed-case address; both make HL recover a garbage signer → every request fails with `User or API Wallet <random> does not exist` (the address changes per call). The subclass signs the digest itself and `_get_client()` pre-lowers the builder address.
- Spot on Hyperliquid: **no TP/SL trigger orders yet** (HL: "Placing trigger orders is not enabled on spot trading yet"; `place_tp_sl_orders` returns `success=False` for spot), `reduceOnly` is rejected on spot, orders must be worth ≥ 10 USDC, and spot/perp USDC wallets are separate (`fetch_balance({"type": "spot"|"swap"})`, `transfer(code, amount, "swap", "spot")`). Testnet spot is degenerate: BTC/USDC has whole-BTC lot sizes (1.0 precision), XAU isn't listed, books are thin and prices decouple from perps (use the order book, not the ticker `last`, for market-order pricing) — the functional tests use ETH/USDC for spot execution.
- Auth is `HYPERLIQUID_PRIVATE_KEY` (required; raw private key string, not a mnemonic); `HYPERLIQUID_WALLET_ADDRESS` optional. Testnet via `set_sandbox_mode(True)`.
- Daily PnL comes from `fetch_my_trades` `info.closedPnl`, cached 60 s per UTC day (`_PNL_CACHE`).
- TP/SL are **market trigger orders** (`{"stopLossPrice": ..., "reduceOnly": True}` / `{"takeProfitPrice": ...}`), not limit orders; trigger prices are tick-rounded, and `fetch_open_orders` exposes them via `order["triggerPrice"]`.

## PineScript (indicators/ and strategies/)

- Docs: https://www.tradingview.com/pine-script-docs/ (Pine v5 language reference — `strategy.entry/close/exit`, `alert_message`, `request.*` calls, `timeframe.change`). No sitemap (checked robots.txt → global `sitemap.xml` only lists the docs root; index the nav tree instead). The default docs are **v6**; repo scripts are mostly `//@version=5`, so prefix URLs with `/pine-script-docs/v5/...` when needed. A local v6 docs corpus is in `tools/pinescriptv6` (see below) — use it as general Pine guidance, not v6-only.
- Direct pages for concepts used by the strategies:
  - `strategy.entry/close/close_all/exit`, pyramiding, order placement → `/pine-script-docs/v5/concepts/strategies`
  - `alert_message`, `{{strategy.order.alert_message}}`, alert setup → `/pine-script-docs/v5/concepts/alerts`
  - `request.earnings/dividends/splits`, external data → `/pine-script-docs/v5/concepts/other-timeframes-and-data`
  - `timeframe.change`, MTF/HTF → `/pine-script-docs/v5/concepts/timeframes`; sessions/time → `/pine-script-docs/v5/concepts/sessions`, `/pine-script-docs/v5/concepts/time`
  - Execution model (`barstate.*`, calc on every tick) → `/pine-script-docs/v5/language/execution-model`
  - `input.*` → `/pine-script-docs/v5/concepts/inputs`
  - Built-in function reference (`fun_strategy.entry`, `fun_ta.vwap`, …) → https://www.tradingview.com/pine-script-reference/v5/
- Layout: `indicators/` holds TradingView-side indicators, `strategies/` holds strategies; neither is executed by the bot. Pine files are tracked in git (not ignored). The vwap strategies embed the webhook `secret` — keep it in sync with `WEBHOOK_SECRET` in `.env` and treat it as sensitive.
- `indicators/vwap.pine` is TradingView's own VWAP indicator and the canonical reference for the shared anchor/stdev-band structure. Its `[_vwap, _stdevUpper, _] = ta.vwap(src, isNewPeriod, 1)` (indicators/vwap.pine:67) is valid Pine v5 and must NOT be "fixed" for OpenPineScript (which only implements single-return `ta.vwap`; see Local Pine tooling).
- `indicators/vwap_angle.pine` / `indicators/vwap_rhythm.pine` are supporting indicators on the same anchor system; their logic is embedded (with plots omitted) as optional filters in `strategies/vwap_swing.pine`.
- `indicators/cot_index_v2.pine` / `cot_comm_net_v2.pine` / `cot_noncomm_net_v2.pine` / `cot_retail_net_v2.pine` are **Pine v6** (unlike the v5 strategies — user preference for these): reconstructions of the invite-only "COT ... by OTC" indicators. They import `TradingView/LibraryCOT/6` (`getCFTCCode()` + `COTTickerid()` to build `COT:<code>_<F|FO>_<metric>_<direction>` tickers) and request the weekly COT metrics at daily resolution with `nz(daily, weekly)` fallback so Friday releases show on the forming weekly bar. Pine has no `coalesce()` in v5 or v6 (CE10271) — use `nz()`.
- `indicators/true_seasonality.pine` and `indicators/valuation_tool.pine` are Pine v6 reconstructions of invite-only OTC indicators (screenshots live alongside them in `indicators/`).
- `strategies/vwap_strategy.pine` (v4.84, ~946 lines) is the accumulation/distribution strategy: core VWAP bounces/crosses/limit entries and TPs, optional HTF VWAP filter, equity protector, pyramiding; HeikinAshi/CVD were removed in the split.
- `strategies/vwap_swing.pine` (v1.00, ~1286 lines) is the swing variant: full HA/CVD core, optional vwap_angle filters (Alignment/Imbalance/Balance), optional vwap_rhythm filters (Not Extended/VWAP Side/Inside 1st Band), and a stop-loss (VWAP band or percent; band stop can track live or freeze at entry).
- `strategies/12-month-rule.pine` is a monthly sleeve-timing strategy.
- Alerts: `vwap_strategy.pine` / `vwap_swing.pine` build the webhook JSON in `msg()` / `msgEntryLong()` etc. with the same contract as the bot (`secret`, `exchange`, `symbol`, `side`, `size_usdt`, `request_id`; see `models.py`/README). `secret` must match `WEBHOOK_SECRET` in `.env`.
- Pine scripts are edited as text; use `tools/openpinescript` as a local syntax gate, but TradingView is the final compile/parity check — there is no local TradingView-parity backtester.

## Local Pine tooling

- `tools/openpinescript/` — OpenPineScript (GPL-3.0): a real Pine v1–v5 transpiler/runtime, used as a **local syntax gate**, not as a parity backtester. Build once: `cd tools/openpinescript && npm ci --ignore-scripts && npm run build` (Node ≥20; `--ignore-scripts` skips its `prepare` parser generation; `npm run build` regenerates the parsers and builds). Validate with `compileScript()` / `Session.compile()`; the CLI additionally needs an OHLCV CSV.
- Known OpenPineScript engine gaps (valid Pine it rejects; work around only inside the gate, never in the `.pine` files):
  - `ta.vwap` is single-return in OpenPineScript; the valid v5 tuple form is in `indicators/vwap.pine:67`.
  - `strategy.max_drawdown_percent` (used in the stats table) is not implemented.
  - With those two substitutions, both vwap strategies transpile as v5 (77 KB / 115 KB JS). Remaining diagnostics are syntax hints, not TradingView parity.
- `tools/pinescriptv6/` — markdown Pine reference corpus restructured for LLM/RAG use; treat it as **general Pine guidance, not v6-only**. Docs only, no code; license/redistribution is unclear, so reference use only.
- Both are nested git clones. Do not `git add` them as-is (git stages gitlinks); remove their `.git` to vendor them, or register submodules. Keep OpenPineScript a separate GPL-3.0 tool; do not copy its code into this repo without complying.
