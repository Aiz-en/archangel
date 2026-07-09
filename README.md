# Archangel

An automated day-trading bot in Python. Archangel scans for low-float stocks making
big intraday moves, detects bull-flag continuation patterns on 5-minute charts, and
times entries on the 1-minute chart — currently running entirely in **paper-trading
mode** against real market data with locally simulated fills.

> **Disclaimer:** This is a personal research project, provided for educational
> purposes only. It is not financial advice, and nothing here is a recommendation to
> buy or sell any security. Day trading low-float momentum stocks is extremely risky.
> Use at your own risk.

## How it works

```
  scanner.py / screener.py     candidate symbols (today the backtest feeds
            │                  from the raw gainer feed; wiring the live
            ▼                  watchlist into a real-time loop is next)
        runner.py  ◀────────── yfinance 1m/5m bars
            │
            ├──▶ strategy.py      bull-flag detection + 9 EMA trigger
            ├──▶ paper_engine.py  simulated fills, positions, P&L
            └──▶ storage.py       SQLite trade log
```

1. **Screen** — `screener.py` polls Webull's top-gainers feed and keeps a live
   watchlist of symbols meeting all the strategy's selection criteria: up **+70%+
   on the day**, priced **$1–$20**, **≥1M shares** traded, **float ≤20M**, and
   **relative volume (RVOL) ≥5×** the daily average.
2. **Detect** — `strategy.py` looks for a bull flag on closed 5-minute bars: a pole
   of 3+ consecutive green candles, a pullback of 2–3 red candles, everything
   holding above the 12 EMA.
3. **Enter** — the 1-minute chart provides the trigger: a candle wick crossing
   through the 9 EMA. Entry is a limit order at the 9 EMA value with a fixed
   **−5% stop / +10% take-profit** bracket, $1,000 per position, max 3 concurrent.
4. **Simulate** — `paper_engine.py` fills orders against real OHLC ranges
   (limit-style: fills only if the bar's range crosses the price) and tracks
   positions, cash, and closed-trade P&L.
5. **Record** — `storage.py` persists every closed trade and equity snapshot to
   SQLite for later analysis.

`runner.py` is the glue that walks bars in time order and ties these stages
together. Today it runs over recent history through the backtest harness; the
real-time polling loop is the next milestone.

The full strategy specification lives in
[docs/trading_strategy_baseline.md](docs/trading_strategy_baseline.md).

## Data sources

| Role | Source | Notes |
|------|--------|-------|
| Symbol screening | Unofficial `webull` scraper | Same backend as the Webull app's gainers list; to be replaced by a paid source for live trading |
| Price bars | `yfinance` | Free, ~1–2 min delayed; 1m bars limited to ~7 days of history |
| Order execution | Official `webull-openapi-python-sdk` | Wired for auth today; live orders are a future phase |

## Setup

Requires **Python 3.13** — the Webull SDK rejects 3.14+, and the pinned
dependencies want a recent 3.x. On macOS: `brew install python@3.13`.

```bash
git clone https://github.com/Aiz-en/archangel.git
cd archangel
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Everything in the Usage section below runs with **no account or API keys** — the
gainers endpoint is unauthenticated and fills are simulated locally. `.env` is
only needed for the broker pieces: `webull_paper.py` (username/password of a
dedicated, unfunded Webull account — see the security notes in `.env.example`)
and `hello_webull.py` (App Key/Secret from an approved Webull OpenAPI
application):

```bash
cp .env.example .env   # then fill in your keys
```

## Usage

**Live screener dashboard** — refreshes every 30s during market hours:

```bash
python code_base/screener.py                # strict strategy criteria
python code_base/screener.py --once         # single snapshot, no loop
python code_base/screener.py --min-change 30 --ignore-hours   # looser, after hours
python code_base/screener.py --help         # every criterion has a flag
```

**Raw top-gainers scan:**

```bash
python code_base/scanner.py
```

**Backtest recent movers** — two sweeps (today's +70% intraday gainers, then the
past 5 days' +100% movers) over recent 1m/5m bars, printing win rate, expectancy,
and drawdown for each. Writes `archangel_backtest_*.db` scratch files to the
working directory; on quiet days a sweep may find no qualifying movers:

```bash
python code_base/backtest.py
```

Most modules also run standalone as smoke tests, e.g.
`python code_base/strategy.py` or `python code_base/paper_engine.py`.

## Project layout

```
code_base/
  screener.py      # live multi-criteria watchlist (the selection rules)
  scanner.py       # raw Webull top-gainers feed
  strategy.py      # bull-flag detection + 9 EMA trigger (pure functions)
  runner.py        # walks bars, ties detection to the paper engine
  paper_engine.py  # simulated portfolio: orders, fills, positions, P&L
  backtest.py      # multi-symbol harness + aggregate stats
  storage.py       # SQLite trade/equity persistence
  ema.py           # EMA helpers (trader-standard recursive form)
  webull_paper.py  # Webull paper-account broker (read-only scaffold)
  hello_webull.py  # Webull OpenAPI auth smoke test
  hello_quote.py   # yfinance data-fetch smoke test
  practice.py      # Python learning scratchpad (not part of the bot)
docs/              # strategy spec + Webull API research
```

## Status & roadmap

Working today: screening, detection, simulated fills, SQLite logging, and a
multi-symbol backtest harness. Early backtest samples are far too small to draw
conclusions from.

Next up:

- Live polling runner: poll bars during market hours, trade the watchlist in real
  time against a Webull paper account
- Order placement through the paper broker (currently read-only)
- Historical movers source, so backtests can cover more than the last ~7 days
- Time-interleaved backtesting (currently sequential per symbol)

## Caveats

- The screening feed uses an **unofficial** Webull library that scrapes consumer
  endpoints — it can break or be blocked at any time, and is used here only for
  personal research during the paper phase.
- yfinance bars are delayed a minute or two; fine for paper trading, not for live
  execution.
- The RVOL filter compares cumulative volume to the full-day average, so it reads
  low early in the session (documented in the strategy spec).
