#!/usr/bin/env python3
"""XAUUSD market snapshot collector — proof-of-pipeline for the Nugget trading profile.

Pulls every feed Nugget's crons will need, computes the technical feature set the
prediction algorithm will consume, and prints one snapshot block.

Feeds (all keyless, all verified reachable from this host):
  spot   : api.gold-api.com            (XAU/USD spot)
  cross  : api.coingecko.com           (PAXG / XAUT scored gold tokens)
  candles: data-api.binance.vision     (PAXGUSDT klines -> OHLCV)
  macro  : nfs.faireconomy.media       (ForexFactory weekly econ calendar)
"""
from __future__ import annotations

import json
import statistics
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125 Safari/537.36"
TIMEOUT = 20
WIB = timezone(timedelta(hours=7))


def get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = statistics.fmean(gains[-period:])
    al = statistics.fmean(losses[-period:])
    if al == 0:
        return 100.0
    rs = ag / al
    return 100.0 - (100.0 / (1.0 + rs))


def ema(vals, period):
    if len(vals) < period:
        return None
    k = 2.0 / (period + 1)
    e = statistics.fmean(vals[:period])
    for v in vals[period:]:
        e = v * k + e * (1 - k)
    return e


def atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    return statistics.fmean(trs[-period:])


def gold_candles(interval="1h", limit=200):
    u = (f"https://data-api.binance.vision/api/v3/klines"
         f"?symbol=PAXGUSDT&interval={interval}&limit={limit}")
    raw = get(u)
    out = []
    for k in raw:
        out.append({
            "open_time": int(k[0]),
            "open": float(k[1]), "high": float(k[2]),
            "low": float(k[3]), "close": float(k[4]),
            "volume": float(k[5]), "trades": int(k[8]),
        })
    return out


def snapshot():
    s = {"collected_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "collected_at_wib": datetime.now(WIB).isoformat(timespec="seconds")}
    errors = []

    # ---- spot
    try:
        g = get("https://api.gold-api.com/price/XAU")
        s["spot_xauusd"] = g["price"]
        s["spot_updated"] = g.get("updatedAt")
    except Exception as e:
        errors.append(f"gold-api: {e}")

    # ---- scored tokens (cross-check + basis)
    try:
        c = get("https://api.coingecko.com/api/v3/simple/price"
                "?ids=pax-gold,tether-gold&vs_currencies=usd&include_last_updated_at=true")
        s["paxg_usd"] = c["pax-gold"]["usd"]
        s["xaut_usd"] = c["tether-gold"]["usd"]
    except Exception as e:
        errors.append(f"coingecko: {e}")

    # ---- 24h ticker (session high/low/volume)
    try:
        t = get("https://data-api.binance.vision/api/v3/ticker/24hr?symbol=PAXGUSDT")
        s["ticker"] = {
            "last": float(t["lastPrice"]), "bid": float(t["bidPrice"]),
            "ask": float(t["askPrice"]), "high_24h": float(t["highPrice"]),
            "low_24h": float(t["lowPrice"]), "chg_pct_24h": float(t["priceChangePercent"]),
            "volume_24h": float(t["volume"]),
        }
        s["spread_usd"] = round(float(t["askPrice"]) - float(t["bidPrice"]), 4)
    except Exception as e:
        errors.append(f"ticker: {e}")

    # ---- candles + features
    try:
        c1h = gold_candles("1h", 200)
        closes = [c["close"] for c in c1h]
        highs = [c["high"] for c in c1h]
        lows = [c["low"] for c in c1h]
        vols = [c["volume"] for c in c1h]
        last = c1h[-1]
        prev = c1h[-2]
        s["candle_last"] = {"close_time_wib": datetime.fromtimestamp(
            (last["open_time"] // 1000) + 3600, WIB).isoformat(timespec="minutes"),
            "o": last["open"], "h": last["high"], "l": last["low"], "c": last["close"]}
        s["features"] = {
            "sma20": round(statistics.fmean(closes[-20:]), 2),
            "sma50": round(statistics.fmean(closes[-50:]), 2),
            "ema12": round(ema(closes, 12), 2),
            "ema26": round(ema(closes, 26), 2),
            "macd": round((ema(closes, 12) or 0) - (ema(closes, 26) or 0), 2),
            "rsi14": round(rsi(closes, 14), 2),
            "atr14": round(atr(highs, lows, closes, 14), 2),
            "range_24h": round(max(highs[-24:]) - min(lows[-24:]), 2),
            "close_vs_sma20_pct": round((closes[-1] - statistics.fmean(closes[-20:]))
                                        / statistics.fmean(closes[-20:]) * 100, 3),
            "mom_1h_pct": round((last["close"] - prev["close"]) / prev["close"] * 100, 3),
            "mom_24h_pct": round((closes[-1] - closes[-25]) / closes[-25] * 100, 3),
            "vol_vs_avg": round(vols[-1] / statistics.fmean(vols[-24:]), 3),
            "higher_high_6": last["high"] > max(highs[-6:-1]),
            "lower_low_6": last["low"] < min(lows[-6:-1]),
            "bars": len(c1h),
        }
        s["atr_pct"] = round(atr(highs, lows, closes, 14) / closes[-1] * 100, 3)
    except Exception as e:
        errors.append(f"candles: {e}")

    # ---- basis: PAXG token vs true spot
    if "paxg_usd" in s and "spot_xauusd" in s:
        s["paxg_basis_usd"] = round(s["paxg_usd"] - s["spot_xauusd"], 2)
        s["paxg_basis_pct"] = round(s["paxg_basis_usd"] / s["spot_xauusd"] * 100, 3)

    # ---- macro calendar (next 48h, high/medium impact only)
    try:
        cal = get("https://nfs.faireconomy.media/ff_calendar_thisweek.json")
        now = datetime.now(timezone.utc)
        upcoming = []
        for e in cal:
            if e.get("impact") not in ("High", "Medium"):
                continue
            try:
                dt = datetime.fromisoformat(e["date"])
            except Exception:
                continue
            if 0 <= (dt - now).total_seconds() <= 48 * 3600:
                upcoming.append({
                    "wib": dt.astimezone(WIB).strftime("%a %H:%M"),
                    "ccy": e.get("country"), "impact": e.get("impact"),
                    "title": e.get("title"), "forecast": e.get("forecast"),
                    "previous": e.get("previous"),
                })
        s["macro_next_48h"] = sorted(upcoming, key=lambda x: x["wib"])
    except Exception as e:
        errors.append(f"calendar: {e}")

    s["errors"] = errors
    return s


if __name__ == "__main__":
    snap = snapshot()
    print(json.dumps(snap, indent=2))
    sys.exit(1 if snap["errors"] else 0)
