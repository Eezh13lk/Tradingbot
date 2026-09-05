# Tradingbot Futures — Binance USDT-M (Isolated)

Defensive Python bot for **Binance USDT-M perpetual futures** with **isolated margin only**, **max 10× leverage**, and hard risk gates on every order. Adapted from the spot Tradingbot mean-reversion / candle z-score rules (long **and** short).

> **Liquidation warning (read this):** Isolated 10× still amplifies risk. This bot **opens small** (~$5 notional) then **adds isolated margin** (toward $5 total) to widen liquidation distance **without** increasing size. Stops target ~$0.50 risk but are **not guaranteed** if the market gaps. Soft ~$1/day is **metrics/logging only**, not a promise. Default mode is paper (`DRY_RUN=true`). Use at your own risk.

## Isolated margin workflow (enforced in code)

| Step | Default | Meaning |
|------|---------|---------|
| 1. Open size | `OPEN_MARGIN_USD=0.5` × `LEVERAGE=10` | **Notional ≈ $5** (not $50) — this sets position size |
| 2. Add margin | up to `TARGET_MARGIN_USD=5` | After fill, `addIsolatedMargin` adds ~$4.5 — **size unchanged**; only widens liquidation distance |
| 3. Stop risk | `RISK_USD=0.5` (≈10% of target) | Mandatory SL: `$0.50 / $5 notional` ≈ **10%** adverse price |
| Cap | `MAX_DAILY_LOSS_USD=2` | UTC day; realized PnL ≤ −cap → **new entries stop**; exits still allowed |
| Mode | `MARGIN_TYPE=ISOLATED` | Cross margin is **rejected** |
| Leverage | hard max **10×** | Rejected above 10 |

### Open notional math

```
OPEN_MARGIN_USD = 0.5
LEVERAGE        = 10
open notional   = 0.5 × 10 = $5     ← position size is locked here
```

### Add-margin step (after fill)

```
TARGET_MARGIN_USD = 5
add amount        ≈ 5 − 0.5 = $4.5
→ total isolated margin ≈ $5
→ quantity / notional unchanged
→ liquidation farther away
```

### Stop risk (~$0.50)

```
RISK_USD        = 0.50   (or 10% of TARGET_MARGIN)
stop_loss_pct   = RISK_USD / open_notional = 0.50 / 5 = 0.10 (10% price)
planned loss    ≈ $5 × 10% = $0.50
```

Every proposed order goes through `RiskManager.approve()` before `FuturesExchangeClient.place_order()`. After an entry fill, the bot calls `add_isolated_margin()`.

## Strategy

- Candle close **z-score** mean reversion (lookback configurable)
- **Long** when oversold (`z ≤ -ENTRY_ZSCORE`); **short** when overbought (`z ≥ +ENTRY_ZSCORE`)
- Skip entries when lookback vol (`std/mean`) > `MAX_VOL_PCT`
- Fee-aware take-profit edge + cooldown after closed trades
- Analyse before every entry; risk manager must approve

## Setup

```bash
cd Tradingbot-futures
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
# or: pip install -r requirements.txt && pip install -e .
cp .env.example .env
```

### API keys (live only)

1. Create a Binance API key with **USDT-M Futures** trading enabled.
2. **Disable withdrawals** on the key (restrict IP if possible).
3. Put keys in `.env` — never commit `.env`.
4. Start with `DRY_RUN=true` until you understand the logs and caps.

```env
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
DRY_RUN=true
OPEN_MARGIN_USD=0.5
TARGET_MARGIN_USD=5
LEVERAGE=10
RISK_USD=0.5
MAX_DAILY_LOSS_USD=2
MARGIN_TYPE=ISOLATED
```

## How to run

### Dry-run (default — no keys required)

```bash
tradingbot-futures status
tradingbot-futures once
tradingbot-futures run
```

Or:

```bash
python -m tradingbot_futures.cli.main status
python -m tradingbot_futures.cli.main once
python -m tradingbot_futures.cli.main run
```

### Live futures (real money — dangerous)

1. Confirm caps in `.env`.
2. Set `DRY_RUN=false` and valid Futures-enabled keys (withdrawals OFF).
3. Optionally `BINANCE_TESTNET=true` for futures testnet first.
4. Bot sets **ISOLATED** margin and leverage ≤ 10 before orders, then tops up isolated margin after fill.

```bash
DRY_RUN=false tradingbot-futures run -v
```

## Tests

```bash
pytest -q
```

Risk math covered: open 0.5 × 10 → ~$5 notional; add-margin to $5; RISK_USD $0.50 → ~10% stop; daily halt; isolated-only; leverage cap.

## Layout

```
src/tradingbot_futures/
  config.py            # OPEN/TARGET margin, RISK_USD, derived stop_loss_pct
  models.py            # long/short positions, orders
  bot.py               # strategy → risk → exchange → addIsolatedMargin
  risk/manager.py      # hard caps (MUST approve every order)
  strategy/            # mean-reversion z-score (long & short)
  exchange/client.py   # public dry-run + futures live + add_isolated_margin
  cli/main.py          # tradingbot-futures run|once|status
tests/test_risk_manager.py
```

## Caveats

- Not financial advice; **no profit guarantee**.
- Futures amplify losses; isolated 10× can still liquidate (added margin helps but does not eliminate risk).
- Dry-run fills ignore slippage, partial fills, and funding.
- Live mode uses market orders — fees, slippage, and funding apply.
- Daily loss is **realized** PnL for the UTC day (`state.json`).
- One open position at a time (no pyramiding).
- Some regions block Binance hosts (HTTP 451); dry-run falls back through alternate `fapi` hosts and then **futures testnet public data** for paper ticks. Live trading needs reachable Futures API per Binance ToS.
- You are responsible for local laws, taxes, and Binance ToS.

## License

MIT — use responsibly.
