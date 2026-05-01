# Liquidity Hunter Bot

A market-maker-mindset trading bot that hunts liquidity pools instead of chasing direction. Scans all Binance USDT-M perpetuals, classifies positioning, identifies liquidation clusters, and generates conditional trade setups — paper-traded only in v1.

## Philosophy

> Don't chase price — hunt liquidity. The market always seeks the trapped side.

The bot does NOT use RSI, MACD, or retail indicators. It analyzes:
- **Liquidity pools** (where stops cluster)
- **Funding rate** (who's paying to hold)
- **Open Interest** (building or unwinding)
- **Long/Short ratio** (retail vs top-trader divergence)
- **Price action** (sweeps, rejections, structure)

## Architecture (10 Layers)

| # | Layer | Purpose |
|---|---|---|
| 1 | Scanner | Filter all USDT-M perpetuals by volume + OI + extremity signals |
| 2 | Data Collector | Pull detailed snapshots for shortlisted symbols |
| 3 | Liquidity Engine | Map liquidation clusters above/below price |
| 4 | Positioning Analyzer | Classify into 6 market states |
| 5 | Context Layer | 24h memory — same signal means different things |
| 6 | Regime Detector | Trending vs Range vs Volatile (ADX-based) |
| 7 | Decision Engine | Rule-based score 0–100 with regime adjustments |
| 8 | Trigger Confirmation | Volume spike + OI reaction + Rejection candle (2/3 needed) |
| 9 | Trade Generator | Conditional trade card with R:R ≥ 2.0 |
| 10 | Paper Executor | Simulated execution with slippage, spread, kill-switch |
| 11 | Backtest Engine | Walk-forward simulation on historical data |
| 12 | Telegram Alerter | Setup / trigger / exit notifications |

## Quick Start

### 1. Install
```bash
cd liquidity_hunter
python -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure
```bash
cp .env.example .env
# Edit .env with your Telegram credentials
```

Get Telegram credentials:
- Bot token: message [@BotFather](https://t.me/BotFather) → `/newbot`
- Chat ID: message your bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates`

### 3. Initialize database
```bash
python -m src.main init
```

### 4. Run

**Dashboard (recommended):**
```bash
python -m ui.app
```
Open http://localhost:8090

> Port 8090 is the default (avoiding conflict with Freqtrade UI on 8080).
> Change it via `ui.port` in `config.yaml` if you need a different port.

**CLI mode (no UI):**
```bash
python -m src.main bot          # Live paper-trading loop
python -m src.main scan         # One scan cycle, then exit
python -m src.main backtest --symbol BTCUSDT --days 60
```

## Configuration

All tunable parameters live in `config.yaml` — no magic numbers in code. Common knobs:

```yaml
scanner:
  min_quote_volume_24h_usd: 100_000_000
  funding_extreme_threshold: 0.0005
  top_n_to_monitor: 10

decision_engine:
  min_score_to_signal: 60
  min_score_full_size: 75

paper_executor:
  initial_capital_usd: 10_000
  risk_per_trade_pct: 0.01
  daily_max_loss_pct: 0.05
```

## Project Structure

```
liquidity_hunter/
├── config.yaml              # All tunable parameters
├── .env                     # Secrets (Telegram, DB URL)
├── requirements.txt
├── data/                    # SQLite DB lives here
├── logs/                    # Rotating log files
├── src/
│   ├── core/                # Config, DB models, logger
│   ├── exchange/            # Binance Futures client
│   ├── layers/              # The 10 bot layers
│   ├── backtest/            # Backtest engine
│   ├── alerts/              # Telegram alerter
│   └── main.py              # Orchestrator + CLI
├── ui/
│   ├── app.py               # NiceGUI dashboard
│   ├── theme.py             # Design system
│   └── components/          # Reusable widgets
└── tests/
```

## Database

SQLite by default — zero setup. Switch to PostgreSQL by changing `DATABASE_URL` in `.env`:

```env
# SQLite (default)
DATABASE_URL=sqlite+aiosqlite:///data/liquidity_hunter.db

# PostgreSQL (for VPS deployment)
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/liquidity_hunter
```

The schema uses no SQLite-specific types — same code works on both.

## Risk Management Rules

These are hard-coded and **cannot be bypassed**:

- Position size: 1–2% of equity per trade
- Min R:R: 2.0 (preferred 2.5+)
- Max concurrent trades: 3
- Daily kill-switch: -5% / day OR 3 consecutive losses
- No chasing: cancel if price runs > 0.3% past entry zone

## Roadmap

- **v1** (this) — Paper trading + Backtest + Dashboard + Telegram
- **v2** — Multi-timeframe confirmations, more aggressive backtest with funding/OI series
- **v3** — Auto-execute via Binance API (only after >100 paper trades and verified Sharpe > 1.0)
- **v4** — Move to VPS, add Postgres, add monitoring (Grafana)

## License

Personal use only. No warranty. Markets can wipe you out — paper trade until you have 100+ documented trades with a positive expectancy.
