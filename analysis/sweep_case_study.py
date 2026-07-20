"""Sweep the three entry configs across every banked tape (watchlists 7/13-7/17 + SDOT).

Configs:
  A) strict gate     + is_9_ema_touch(EMA_9)            -- main runner today
  B) relaxed gate    + is_9_ema_touch(EMA_9)            -- shadow runner today
  C) case_study gate + is_ema_reversal_touch(E9,E12)    -- SDOT case-study spec

Fill model: market at trigger-bar close, 0.5% slip/side, TP +10% / SL -5%
(stop checked first), 15:55 flatten, no entries at/after 15:55, one open
position per symbol per config. Positions never span days.
Also logs, for config C, whether the NEXT 1m bar was the "nice to see"
engulf (green, close above the touch bar's open) -- evidence for his deferred
risk-value idea.
"""
import json
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "code_base"))

import pandas as pd
from ema import add_ema
from strategy import detect_setup, is_9_ema_touch, is_ema_reversal_touch

SCRATCH = HERE
CACHE = os.path.join(SCRATCH, "tape_cache")
os.makedirs(CACHE, exist_ok=True)

import sqlite3
watch_pairs = set()
for db in ("archangel_live.db", "archangel_shadow.db"):
    con = sqlite3.connect(os.path.join(HERE, "..", db))
    for day, watched in con.execute("SELECT day, watched FROM daily_summary"):
        for sym in filter(None, watched.split(",")):
            watch_pairs.add((sym, day))
    con.close()
watch_pairs.add(("SDOT", "2026-07-17"))
symbols = sorted({s for s, _ in watch_pairs})
print(f"{len(symbols)} symbols, {len(watch_pairs)} (symbol, watch-day) pairs")

def fetch(sym):
    path = os.path.join(CACHE, f"{sym}.json")
    if os.path.exists(path):
        return sym, json.load(open(path))
    import yfinance as yf
    out = {}
    try:
        t = yf.Ticker(sym)
        for iv in ("1m", "5m"):
            bars = t.history(period="5d", interval=iv)
            if bars.empty:
                return sym, None
            bars = add_ema(bars, [9, 12])
            out[iv] = [
                {"t": ts.isoformat(), "day": ts.strftime("%Y-%m-%d"),
                 "hm": ts.strftime("%H:%M"), "o": float(r.Open), "h": float(r.High),
                 "l": float(r.Low), "c": float(r.Close),
                 "e9": float(r.EMA_9), "e12": float(r.EMA_12)}
                for ts, r in bars.iterrows()]
    except Exception as e:
        print(f"  {sym}: fetch failed ({e})", file=sys.stderr)
        return sym, None
    json.dump(out, open(path, "w"))
    return sym, out

tapes = {}
with ThreadPoolExecutor(max_workers=4) as ex:
    for sym, data in ex.map(fetch, symbols):
        if data:
            tapes[sym] = data
print(f"fetched/cached {len(tapes)} tapes")

def to_df(recs):
    df = pd.DataFrame(recs)
    df.index = pd.to_datetime(df["t"].tolist())
    return df.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close",
                              "e9": "EMA_9", "e12": "EMA_12"})

CONFIGS = ("A_strict", "B_relaxed", "C_case_study")
MODE = {"A_strict": "strict", "B_relaxed": "relaxed", "C_case_study": "case_study"}

def run_symbol(sym, data):
    m1, m5 = to_df(data["1m"]), to_df(data["5m"])
    trades = {c: [] for c in CONFIGS}
    for day in sorted(m1["day"].unique()):
        d1 = m1[m1["day"] == day]
        armed_cache = {}
        busy = {c: None for c in CONFIGS}
        rows = list(d1.iterrows())
        for k, (ts, r) in enumerate(rows):
            hm = ts.strftime("%H:%M")
            if hm >= "15:55":
                break
            bar_close = ts + pd.Timedelta(minutes=1)
            n_closed = int((m5.index + pd.Timedelta(minutes=5) <= bar_close).sum())
            for cfg in CONFIGS:
                if busy[cfg] is not None and ts <= busy[cfg]:
                    continue
                key = (cfg, n_closed)
                if key not in armed_cache:
                    closed5 = m5.iloc[:n_closed]
                    armed_cache[key] = (len(closed5) >= 3 and
                                        detect_setup(closed5, mode=MODE[cfg]) is not None)
                if not armed_cache[key]:
                    continue
                if cfg == "C_case_study":
                    fire = is_ema_reversal_touch(r.Low, r.High, (r.EMA_9, r.EMA_12))
                else:
                    fire = is_9_ema_touch(r.Low, r.High, r.EMA_9)
                if not fire:
                    continue
                fill = r.Close * 1.005
                if fill <= 0:
                    continue
                shares = math.floor(1000 / fill)
                if shares < 1:
                    continue
                tp, sl = fill * 1.10, fill * 0.95
                xts, xpx, how = None, None, None
                for ts2, r2 in rows[k + 1:]:
                    if r2.Low <= sl:
                        xts, xpx, how = ts2, sl * 0.995, "STOP"; break
                    if r2.High >= tp:
                        xts, xpx, how = ts2, tp, "TP"; break
                    if ts2.strftime("%H:%M") >= "15:55":
                        xts, xpx, how = ts2, r2.Close * 0.995, "EOD"; break
                if xts is None:
                    ts2, r2 = rows[-1]
                    xts, xpx, how = ts2, r2.Close * 0.995, "EOD"
                pnl = (xpx - fill) * shares
                engulf = None
                if cfg == "C_case_study" and k + 1 < len(rows):
                    nb = rows[k + 1][1]
                    engulf = bool(nb.Close > nb.Open and nb.Close > r.Open)
                trades[cfg].append(dict(sym=sym, day=day, ints=ts.strftime("%H:%M"),
                                        px=fill, xts=xts.strftime("%H:%M"), how=how,
                                        pnl=pnl, engulf=engulf,
                                        watched=(sym, day) in watch_pairs))
                busy[cfg] = xts
    return trades

all_trades = {c: [] for c in CONFIGS}
for sym in sorted(tapes):
    tr = run_symbol(sym, tapes[sym])
    for c in CONFIGS:
        all_trades[c].extend(tr[c])

def agg(rows):
    n = len(rows)
    w = sum(1 for t in rows if t["pnl"] > 0)
    pnl = sum(t["pnl"] for t in rows)
    wr = f"{w/n*100:.0f}%" if n else "-"
    return f"{n:4d} trades  {w}W/{n-w}L  WR {wr:>4}  P&L {pnl:+9.2f}"

print("\n" + "=" * 74)
print("TOTALS — watch-day tapes only (symbol traded only on its watchlist day)")
print("=" * 74)
for c in CONFIGS:
    print(f"  {c:14s} {agg([t for t in all_trades[c] if t['watched']])}")
print("\nTOTALS — all 5 sessions of every tape (more samples, off-watch days incl.)")
for c in CONFIGS:
    print(f"  {c:14s} {agg(all_trades[c])}")

print("\n" + "=" * 74)
print("C (case_study) — watch-day trades")
print("=" * 74)
for t in sorted((t for t in all_trades["C_case_study"] if t["watched"]),
                key=lambda t: (t["day"], t["ints"])):
    print(f"  {t['day']} {t['sym']:5s} in {t['ints']} @ {t['px']:7.3f} -> "
          f"{t['how']:4s} {t['xts']}  {t['pnl']:+8.2f}  engulf_next={t['engulf']}")

print("\nEngulf-next split for C, all sessions:")
for flag in (True, False):
    rows = [t for t in all_trades["C_case_study"] if t["engulf"] is flag]
    print(f"  engulf={str(flag):5s} {agg(rows)}")

json.dump(all_trades, open(os.path.join(SCRATCH, "sweep_results.json"), "w"),
          default=str)
print("\nsaved sweep_results.json")
