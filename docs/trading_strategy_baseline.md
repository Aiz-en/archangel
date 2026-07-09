# Trading Strategy Baseline

## Overview

Bull flag pattern trading on top daily gainers.

## Stock Selection

A candidate must clear **all** of these (the live screener, `code_base/screener.py`,
enforces them; defaults live in `ScreenCriteria`):

- **% change**: top gainer, **+70% or higher** on the day
- **Price**: **$1–$20** (low-priced parabolic range; avoids sub-$1 illiquid junk)
- **Volume**: **≥ 1M shares** traded today (absolute liquidity floor)
- **Float**: **≤ 20M shares** (low-float momentum names)
- **Relative volume (RVOL)**: **≥ 5×** average daily volume — i.e. today's
  volume is at least 5× the stock's ~3-month average

> RVOL caveat: the screener compares today's *cumulative* volume to the
> *full-day* average, so RVOL reads low early in the session and grows
> through the day. A time-of-day-adjusted RVOL is a future refinement.

## Timeframes

- 1-minute charts
- 5-minute charts

## Indicators

- 9 EMA (Exponential Moving Average)
- 12 EMA (Exponential Moving Average)

## Entry Rules

1. Identify **3 or more consecutive positive (green) candles** (the "pole")
2. Wait for a **pullback of 2-3 negative (red) candles**
3. Pullback candle must **cross/touch the 9 EMA**
4. Pullback must **NOT cross below the 12 EMA**
5. Entry price is at the **intersection of the candle and the 9 EMA**

## Exit Rules

- **Take profit**: +10% from entry price
- **Stop loss**: -5% from entry price

## Position Sizing

- **Fixed $1,000 per position** (does not scale with account growth for now)
- Max loss per trade: $50 (5% of $1,000)

## Concurrency

- **Max 3 concurrent open positions**
- **One position per ticker** at a time (no stacking entries on the same symbol)

## Timeframe Priority

- **5-minute chart is primary** for pattern detection: 3+ green candles (pole) + 2–3 red pullback candles
- **1-minute chart is used for entry timing**: once the 5m setup is valid, watch 1m for the wick-through-9-EMA trigger and execute on that bar

## "Touches 9 EMA" Definition

- A candle's **low (wick) crosses through the 9 EMA value** during that bar
- I.e., `bar.low <= 9_EMA <= bar.high` is sufficient — the body does not need to close near the EMA
