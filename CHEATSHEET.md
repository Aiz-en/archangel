# Archangel — Terminal Cheat Sheet

Quick reference for running and checking the bot. Every command is verified
against the current code. **Run the venv activation first; everything else
assumes it's active.**

```bash
cd ~/archangel_proj && source .venv/bin/activate
```

Three things to remember:

- **`archangel_live.db`** = strict main runner (the control experiment).
  **`archangel_shadow.db`** = relaxed shadow runner (the +30% evidence
  collector). Never mix them.
- **One writer per DB.** Starting a runner manually while the auto-start
  agent's copy is alive makes the second one refuse to start (a lock), rather
  than corrupt data — that's a feature.
- **Kill switch is `launchctl bootout …`** (stops tomorrow's auto-start).
  `pkill` only stops what's running right now.

---

## Daily check — is the bot running & healthy?

```bash
ps ax -o pid,command | grep live_runner | grep -v grep   # runners alive?
launchctl list | grep archangel                          # auto-start loaded?
tail -f logs/live_$(date +%Y%m%d).log                    # watch main (Ctrl-C to stop watching)
tail -f logs/shadow_$(date +%Y%m%d).log                  # watch shadow
```

## Results — P&L and the daily record

```bash
# Per-day summary (writes even on zero-trade days) — MAIN then SHADOW:
sqlite3 archangel_live.db   "SELECT day,trades,realized_pnl,ending_equity,watched FROM daily_summary ORDER BY day;"
sqlite3 archangel_shadow.db "SELECT day,trades,realized_pnl,ending_equity,watched FROM daily_summary ORDER BY day;"

# Every individual trade (main):
sqlite3 archangel_live.db "SELECT symbol,ROUND(entry_price,3),ROUND(exit_price,3),ROUND(pnl,2),exit_reason,substr(entry_time,1,16) FROM closed_trades ORDER BY entry_time;"

# Quick totals (main):
sqlite3 archangel_live.db "SELECT COUNT(*) trades, ROUND(SUM(pnl),2) pnl FROM closed_trades;"
```

## See the market right now

```bash
python code_base/screener.py --once                # who passes the full +70% screen now
python code_base/scanner.py                        # raw top-gainers feed (+70/+30/+10%)
python code_base/screener.py --once --min-change 30 # looser look
```

## Run a bot manually (if not using auto-start)

```bash
python code_base/live_runner.py                    # main: strict, +70%, idles off-hours
python code_base/live_runner.py --entry-mode relaxed --min-change 30 --db archangel_shadow.db
python code_base/live_runner.py --once             # one cycle then exit (spot check)
```

> Do not run these while the launchd agent is also running — the DB is locked
> to one writer.

## Test / replay (safe — uses a throwaway DB)

```bash
python code_base/live_runner.py --smoke            # offline self-test, no network
python code_base/live_runner.py --once --replay-today --ignore-hours --db /tmp/test.db
python code_base/backtest.py                       # 2 sweeps (1d +70%, 5d +100%) with stats
```

## The auto-start agent (launchd)

```bash
launchctl list | grep archangel                        # is it loaded?
launchctl kickstart gui/$(id -u)/com.archangel.trading # fire it now (test)
launchctl bootout gui/$(id -u)/com.archangel.trading   # STOP auto-start (kill switch)

# re-install after a bootout:
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.archangel.trading.plist

# wake the Mac before the bell (one-time, needs admin):
sudo pmset repeat wakeorpoweron MTWRF 08:20:00
```

## Emergency — stop everything now

```bash
pkill -INT -f live_runner.py    # graceful stop (flushes trades, flattens positions)
```

---

## Key flags (`live_runner.py`)

| Flag | Meaning |
|------|---------|
| `--entry-mode {strict,relaxed}` | Entry rules. `strict` = documented baseline (default); `relaxed` = fast-mover experiment |
| `--min-change PCT` | Screener minimum % change (default 70) |
| `--min-rvol X` | Screener minimum relative volume (default 5) |
| `--max-float SHARES` | Screener maximum float (default 20,000,000) |
| `--db PATH` | SQLite trade log (default `archangel_live.db`) |
| `--cash USD` | Starting paper cash (default 5000) |
| `--refresh SECONDS` | Seconds between cycles (default 30) |
| `--exit-after-close` | Exit after the session instead of idling (for managed runs) |
| `--no-eod-flatten` | Do not force-close positions before the bell |
| `--once` / `--smoke` / `--ignore-hours` / `--replay-today` | Single cycle / offline test / run off-hours / replay recent days |

Position sizing, stop, and take-profit are fixed strategy decisions
($1,000/position, −5% stop, +10% take-profit, max 3 concurrent) — see
`docs/trading_strategy_baseline.md`.
