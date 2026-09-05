# Tradingbot — Defensive Binance SPOT Bot

Small Python bot for **Binance Spot only** (no futures, margin, or leverage).  
It uses a simple mean-reversion / micro-range idea on `BTCUSDT` (configurable) and **hard risk caps** that gate every order.

> **Honest risk notice:** Crypto trading can lose money quickly. This software does **not** guarantee profit. The soft ~$1/day figure is **metrics/logging only**, not a promise. Past behavior does not predict future results. Use at your own risk. Default mode is paper (`DRY_RUN=true`).

Target repo (upload when ready): https://github.com/kaveezh13-svg/Tradingbot

## Risk caps (enforced in code)

| Cap | Default | Behavior |
|-----|---------|----------|
| `MAX_POSITION_USD` | `50` | Rejects entries that would exceed this notional |
| `MAX_DAILY_LOSS_USD` | `2` | UTC calendar day; when realized PnL ≤ −cap, **new entries stop**; exits still allowed |
| `STOP_LOSS_PCT` | `0.01` (~1%) | Per-trade stop on every long entry |
| Spot-only | — | No shorting; SELL only to close a long |

Every proposed order goes through `RiskManager.approve()` before `ExchangeClient.place_order()`.

## Features

- Spot market orders only (via `python-binance` when live)
- `DRY_RUN=true` by default — public market data, simulated fills, no signed orders
- Fee-aware entry filter + cooldown between trades
- Secrets only via environment / `.env` (gitignored)
- Unit tests for the risk manager

## Setup

```bash
cd Tradingbot
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
# or: pip install -r requirements.txt && pip install -e .
cp .env.example .env
```

### API keys (live only)

1. Create a Binance API key with **Spot trading** enabled.
2. **Disable withdrawals** on the key (and restrict IP if possible).
3. Never enable futures/margin for this bot.
4. Put keys in `.env` — never commit `.env`.

```env
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
DRY_RUN=true
```

## How to run

### Dry-run (default — recommended first)

```bash
# status / caps
tradingbot status

# single evaluation tick (uses public Binance market data)
tradingbot once

# continuous loop
tradingbot run
```

Or without the console script:

```bash
python -m tradingbot.cli.main status
python -m tradingbot.cli.main once
python -m tradingbot.cli.main run
```

### Live spot (real money)

1. Confirm caps in `.env` (`MAX_POSITION_USD`, `MAX_DAILY_LOSS_USD`, `STOP_LOSS_PCT`).
2. Set `DRY_RUN=false` and valid API keys.
3. Optionally set `BINANCE_TESTNET=true` to use Binance spot testnet first.
4. Start with tiny size and watch logs:

```bash
DRY_RUN=false tradingbot run -v
```

## Tests

```bash
pytest -q
```

## Layout

```
src/tradingbot/
  config.py          # env / .env settings
  models.py          # orders, positions, risk decisions
  bot.py             # strategy → risk → exchange loop
  risk/manager.py    # hard caps (MUST approve every order)
  strategy/          # mean-reversion micro-range
  exchange/client.py # public dry-run + spot live client
  cli/main.py        # tradingbot run|once|status
tests/test_risk_manager.py
```

## Caveats

- Not financial advice; no profit guarantee.
- Dry-run fills ignore slippage and partial fills.
- Live mode uses market orders — expect fees and slippage.
- Daily loss is **realized** PnL for the UTC day (persisted in `state.json`).
- Only one open spot long at a time (no pyramiding).
- Network / exchange outages can skip ticks; the bot retries next interval.
- You are responsible for local laws, taxes, and Binance ToS.
- Some regions get HTTP 451 from `api.binance.com`; dry-run falls back to `data-api.binance.vision` and other public hosts. Live trading still requires a reachable Binance account/API from your network (VPN/compliant access as allowed by Binance ToS).

## License

MIT — use responsibly.
