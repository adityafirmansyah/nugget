#!/usr/bin/env python3
"""Calibration backtest for Nugget's hourly direction call.

Answers the only question that matters before building the prediction loop:
what accuracy is actually ACHIEVABLE, and what is the naive baseline?

Evaluates several candidate rules on real PAXGUSDT hourly candles with
walk-forward, next-bar direction as the target. Also measures the weekend
liquidity hole that will silently break an hourly cron.
"""
from __future__ import annotations

import json
import statistics
import urllib.request
from datetime import datetime, timezone, timedelta

UA = "Mozilla/5.0 (X11; Linux x86_64) Chrome/125 Safari/537.36"
WIB = timezone(timedelta(hours=7))


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


def klines(interval, limit=1000):
    u = (f"https://data-api.binance.vision/api/v3/klines"
         f"?symbol=PAXGUSDT&interval={interval}&limit={limit}")
    return [{"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
             "l": float(k[3]), "c": float(k[4]), "v": float(k[5])} for k in get(u)]


def ema(vals, p):
    k = 2.0 / (p + 1)
    e = statistics.fmean(vals[:p])
    for v in vals[p:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes, p=14):
    g = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))][-p:]
    l = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))][-p:]
    ag, al = statistics.fmean(g), statistics.fmean(l)
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def main():
    bars = klines("1h", 1000)
    print(f"hourly bars fetched: {len(bars)}")
    t0 = datetime.fromtimestamp(bars[0]['t'] / 1000, WIB)
    t1 = datetime.fromtimestamp(bars[-1]['t'] / 1000, WIB)
    print(f"window: {t0:%Y-%m-%d %H:%M} .. {t1:%Y-%m-%d %H:%M} WIB "
          f"({(t1 - t0).days} days)")

    c = [b["c"] for b in bars]
    h = [b["h"] for b in bars]
    l = [b["l"] for b in bars]
    v = [b["v"] for b in bars]

    # ---- weekend / low-liquidity hole
    wknd = [b for b in bars
            if datetime.fromtimestamp(b["t"] / 1000, timezone.utc).weekday() >= 5]
    zero_vol = [b for b in bars if b["v"] == 0]
    avg_v = statistics.fmean(v)
    thin = [b for b in bars if b["v"] < avg_v * 0.1]
    print(f"\n[LIQUIDITY] weekend bars: {len(wknd)}/{len(bars)} "
          f"({len(wknd)/len(bars)*100:.1f}%)")
    print(f"[LIQUIDITY] zero-volume bars: {len(zero_vol)}")
    print(f"[LIQUIDITY] bars under 10% of avg volume: {len(thin)} "
          f"({len(thin)/len(bars)*100:.1f}%)")
    print(f"[LIQUIDITY] avg hourly volume: {avg_v:.1f} PAXG")

    # ---- baselines
    ups = sum(1 for i in range(1, len(c)) if c[i] > c[i - 1])
    downs = sum(1 for i in range(1, len(c)) if c[i] < c[i - 1])
    flats = len(c) - 1 - ups - downs
    n = len(c) - 1
    print(f"\n[BASELINE] next-bar direction over {n} bars: "
          f"up={ups} ({ups/n*100:.1f}%)  down={downs} ({downs/n*100:.1f}%)  "
          f"flat={flats}")
    print(f"[BASELINE] always-up accuracy = {ups/n*100:.2f}%")

    results = {}

    def evaluate(name, fn, warmup=60):
        """fn(i) -> +1 / -1 / 0 (flat = no trade) using only data < i."""
        wins = trades = 0
        long_w = long_n = short_w = short_n = 0
        for i in range(warmup, n):
            sig = fn(i)
            if sig == 0:
                continue
            fut = c[i + 1] - c[i]
            trades += 1
            if sig > 0:
                long_n += 1
                if fut > 0:
                    long_w += 1; wins += 1
            else:
                short_n += 1
                if fut < 0:
                    short_w += 1; wins += 1
        acc = wins / trades * 100 if trades else 0.0
        results[name] = {
            "trades": trades, "accuracy": round(acc, 2),
            "coverage_pct": round(trades / (n - warmup) * 100, 1),
            "long_acc": round(long_w / long_n * 100, 2) if long_n else None,
            "short_acc": round(short_w / short_n * 100, 2) if short_n else None,
        }
        print(f"  {name:<34} acc={acc:6.2f}%  trades={trades:<5} "
              f"cov={trades/(n-warmup)*100:5.1f}%  "
              f"L={results[name]['long_acc']} S={results[name]['short_acc']}")

    print("\n[RULES] walk-forward, signal on bar i -> outcome bar i+1")
    evaluate("EMA12>EMA26 (trend)", lambda i: 1 if ema(c[:i + 1], 12) > ema(c[:i + 1], 26) else -1, 60)
    evaluate("close>SMA20", lambda i: 1 if c[i] > statistics.fmean(c[i - 20:i]) else -1, 30)
    evaluate("momentum(3) sign", lambda i: 1 if c[i] > c[i - 3] else -1, 10)
    evaluate("momentum(1) sign", lambda i: 1 if c[i] > c[i - 1] else -1, 10)
    evaluate("RSI14 >50 -> long", lambda i: 1 if rsi(c[:i + 1]) > 50 else -1, 30)
    evaluate("RSI14 mean-revert <30/>70",
             lambda i: 1 if rsi(c[:i + 1]) < 30 else (-1 if rsi(c[:i + 1]) > 70 else 0), 30)
    evaluate("vol breakout: close>prev high",
             lambda i: 1 if c[i] > h[i - 1] else (-1 if c[i] < l[i - 1] else 0), 10)
    evaluate("EMA12>EMA26 AND RSI>50 (confluence)",
             lambda i: 1 if (ema(c[:i + 1], 12) > ema(c[:i + 1], 26)
                             and rsi(c[:i + 1]) > 50) else -1, 60)

    print("\n[VERDICT vs always-up baseline] "
          f"baseline={ups/n*100:.2f}%  best_rule="
          f"{max(results.items(), key=lambda kv: kv[1]['accuracy'])[0]} "
          f"{max(r['accuracy'] for r in results.values()):.2f}%")

    json.dump(results, open("/home/aditf/projects/nugget/backtest_baseline.json", "w"),
              indent=2)
    print("\nwrote backtest_baseline.json")


if __name__ == "__main__":
    main()
