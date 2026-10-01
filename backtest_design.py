#!/usr/bin/env python3
"""Design calibration: what SHOULD Nugget's prediction loop actually grade on?

Follow-up to backtest_baseline.py. The raw next-bar direction target is a coin
flip (~50%), so an hourly "predict then prove" loop planted on that target would
be grading noise and job #4 would optimise randomness. This tests the three
designs that can fix it:

  1. threshold-aware grading  - only score moves that clear a fraction of ATR
  2. abstention               - allow "no call" when signal is weak; grade
                                coverage AND accuracy on the calls made
  3. horizon / time-of-day    - is anything predictable at daily horizon or in a
                                specific session (London/NY open)?
"""
from __future__ import annotations

import json
import statistics
import urllib.request
from datetime import datetime, timezone, timedelta

UA = "Mozilla/5.0 (X11; Linux x86_64) Chrome/125 Safari/537.36"
WIB = timezone(timedelta(hours=7))
UTC = timezone.utc


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


def klines(interval, limit=1000):
    u = (f"https://data-api.binance.vision/api/v3/klines"
         f"?symbol=PAXGUSDT&interval={interval}&limit={limit}")
    return [{"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
             "l": float(k[3]), "c": float(k[4]), "v": float(k[5])} for k in get(u)]


def atr_series(h, l, c, p=14):
    trs = [0.0]
    for i in range(1, len(c)):
        trs.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    out, run = [], []
    for i, tr in enumerate(trs):
        run.append(tr)
        if len(run) > p:
            run.pop(0)
        out.append(statistics.fmean(run) if run else 0.0)
    return out


def main():
    h1 = klines("1h", 1000)
    d1 = klines("1d", 1000)
    print(f"hourly bars: {len(h1)}   daily bars: {len(d1)}")

    c = [b["c"] for b in h1]
    hh = [b["h"] for b in h1]
    ll = [b["l"] for b in h1]
    ts = [b["t"] for b in h1]
    a = atr_series(hh, ll, c, 14)

    # ================= 1. THRESHOLD-AWARE GRADING =================
    # A prediction is CORRECT if direction was right AND the move cleared the
    # threshold. Below threshold = "noise", excluded from the score entirely.
    print("\n" + "=" * 78)
    print("1. THRESHOLD-AWARE GRADING (signal: close > SMA20)")
    print("=" * 78)
    print(f"{'threshold':<26}{'graded':<9}{'noise%':<9}{'accuracy':<11}{'edge vs 50%'}")
    for mult in (0.0, 0.25, 0.5, 1.0, 2.0):
        wins = graded = noise = 0
        for i in range(30, len(c) - 1):
            sig = 1 if c[i] > statistics.fmean(c[i - 20:i]) else -1
            move = c[i + 1] - c[i]
            thr = a[i] * mult
            if mult > 0 and abs(move) <= thr:
                noise += 1
                continue
            graded += 1
            if (sig > 0 and move > 0) or (sig < 0 and move < 0):
                wins += 1
        total = graded + noise
        acc = wins / graded * 100 if graded else 0
        print(f"{mult:>4} x ATR14{'':<15}{graded:<9}{noise/total*100:>6.1f}%  "
              f"{acc:>7.2f}%    {acc-50:+.2f}pp")

    # ================= 2. ABSTENTION =================
    # Only call when |close - SMA20| is a large fraction of ATR (strong signal).
    print("\n" + "=" * 78)
    print("2. ABSTENTION — call only when signal strength clears a bar")
    print("=" * 78)
    print(f"{'strength bar':<26}{'calls':<9}{'coverage':<11}{'accuracy':<11}{'edge'}")
    for bar in (0.0, 0.25, 0.5, 1.0, 1.5):
        wins = calls = tested = 0
        for i in range(30, len(c) - 1):
            sma20 = statistics.fmean(c[i - 20:i])
            strength = abs(c[i] - sma20) / a[i] if a[i] else 0
            tested += 1
            if strength < bar:
                continue
            sig = 1 if c[i] > sma20 else -1
            move = c[i + 1] - c[i]
            calls += 1
            if (sig > 0 and move > 0) or (sig < 0 and move < 0):
                wins += 1
        acc = wins / calls * 100 if calls else 0
        print(f"{bar:>4} x ATR14{'':<15}{calls:<9}{calls/tested*100:>6.1f}%    "
              f"{acc:>7.2f}%   {acc-50:+.2f}pp")

    # ================= 3. TIME OF DAY =================
    print("\n" + "=" * 78)
    print("3. TIME OF DAY (next-bar direction by UTC hour, signal close>SMA20)")
    print("=" * 78)
    by_hour = {}
    for i in range(30, len(c) - 1):
        hr = datetime.fromtimestamp(ts[i] / 1000, UTC).hour
        sig = 1 if c[i] > statistics.fmean(c[i - 20:i]) else -1
        move = c[i + 1] - c[i]
        w, n = by_hour.get(hr, (0, 0))
        by_hour[hr] = (w + (1 if (sig > 0 and move > 0) or (sig < 0 and move < 0) else 0), n + 1)
    # label sessions
    def sess(hr):
        if 7 <= hr < 12:
            return "London"
        if 12 <= hr < 16:
            return "LDN+NY overlap"
        if 16 <= hr < 21:
            return "NY"
        return "Asia/off"
    rows = []
    for hr in sorted(by_hour):
        w, n = by_hour[hr]
        rows.append((hr, sess(hr), w / n * 100, n))
    for hr, s, acc, n in rows:
        bar = "#" * int((acc - 40) / 2)
        print(f"  {hr:02d}:00 UTC ({s:<16}) {acc:5.1f}%  n={n:<4} {bar}")

    print("\n  by session:")
    agg = {}
    for hr, s, acc, n in rows:
        w, tot = agg.get(s, (0, 0))
        agg[s] = (w + acc * n / 100 * n / n * 1, tot + n)
    for s in ("Asia/off", "London", "LDN+NY overlap", "NY"):
        if s not in agg:
            continue
        ws = sum(by_hour[hr][0] for hr, ss, _, _ in rows if ss == s)
        ns = sum(by_hour[hr][1] for hr, ss, _, _ in rows if ss == s)
        print(f"    {s:<18} {ws/ns*100:5.2f}%  n={ns}")

    # ================= 4. DAILY HORIZON =================
    print("\n" + "=" * 78)
    print("4. DAILY HORIZON (is direction predictable a day ahead?)")
    print("=" * 78)
    for win in (5, 10, 20):
        wins = n = 0
        for i in range(win + 30, len(h1) - 24):
            past = c[i] - c[i - win]
            fut = c[i + 24] - c[i]
            sig = 1 if past > 0 else -1
            n += 1
            if (sig > 0 and fut > 0) or (sig < 0 and fut < 0):
                wins += 1
        print(f"  past {win}h momentum -> next 24h direction: {wins/n*100:5.2f}%  n={n}")

    # daily bars
    dc = [b["c"] for b in d1]
    dups = sum(1 for i in range(1, len(dc)) if dc[i] > dc[i - 1])
    print(f"  daily-bar always-up baseline: {dups/(len(dc)-1)*100:5.2f}%  n={len(dc)-1}")
    wins = n = 0
    for i in range(30, len(dc) - 1):
        sma = statistics.fmean(dc[i - 20:i])
        sig = 1 if dc[i] > sma else -1
        fut = dc[i + 1] - dc[i]
        n += 1
        if (sig > 0 and fut > 0) or (sig < 0 and fut < 0):
            wins += 1
    print(f"  daily close>SMA20 -> next daily bar:     {wins/n*100:5.2f}%  n={n}")


if __name__ == "__main__":
    main()
