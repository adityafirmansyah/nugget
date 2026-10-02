#!/usr/bin/env python3
"""Nugget's predictor: a multi-parameter directional model with abstention.

This is the v1 parameter set. It is deliberately simple and interpretable,
because the point is not to be clever - it is to be MEASURABLE. Every parameter
is recorded with every prediction so cronjob #4 can later test whether adding
or removing one actually improves out-of-sample accuracy.

Parameters (all observable at decision time, no look-ahead):
  trend     : EMA12 vs EMA26
  position  : close vs SMA20, normalized by ATR
  momentum  : 24-bar rate of change
  rsi       : RSI14 distance from 50
  breadth   : higher-high / lower-low structure over 6 bars
  volume    : current volume vs 24-bar average (confirmation, not direction)

The model emits a score in [-1, +1] and abstains when |score| is below the
confidence bar, because a forced call on a flat market is noise, not a
prediction. Abstention is recorded as a legitimate outcome.
"""
from __future__ import annotations

import statistics

MODEL_VERSION = "v0.1-technical-multi-param"
NOISE_ATR_MULT = 0.5      # moves below this fraction of ATR14 are noise
ABSTAIN_BAR = 0.18        # |score| below this -> no_call


def ema(vals, period):
    if len(vals) < period:
        return None
    k = 2.0 / (period + 1)
    e = statistics.fmean(vals[:period])
    for v in vals[period:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))][-period:]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))][-period:]
    ag, al = statistics.fmean(gains), statistics.fmean(losses)
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return None
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
               abs(lows[i] - closes[i - 1])) for i in range(1, len(closes))][-period:]
    return statistics.fmean(trs)


def build_params():
    """The parameter set. Weights are v1 priors, to be replaced by measured ones.

    `session_sweep` and `dxy_proxy` are NEW indicators added after the v1 set
    was live. Both start at weight 0.0 (INERT): they are computed and recorded
    in every prediction's features_json/contributions so there is a full
    history to backtest against, but they contribute NOTHING to the score or
    the direction Nugget actually calls until job 4 (improve.py) runs its
    train/held-out gate and finds they measurably help - the same promotion
    discipline every other weight went through. Do not hand-raise these
    weights; let the gate earn it.
    """
    return {
        "trend": {"weight": 0.25, "lookback": [12, 26]},
        "position": {"weight": 0.20, "lookback": 20},
        "momentum": {"weight": 0.15, "lookback": 24},
        "rsi": {"weight": 0.20, "lookback": 14},
        "breadth": {"weight": 0.10, "lookback": 6},
        "volume_confirm": {"weight": 0.10, "lookback": 24},
        "session_sweep": {"weight": 0.0, "lookback": 8},
        "dxy_proxy": {"weight": 0.0, "lookback": 24},
    }


def session_sweep_signal(bars: list[dict]) -> int:
    """Liquidity-sweep microstructure: did price take out the Asian session's
    high/low and then reject back inside it?

    Institutional desks read this as a stop-run: price is pushed through a
    session extreme to trigger resting stops/liquidity, then reverses - the
    reversal, not the breakout, is the signal. Needs only OHLC we already
    fetch every hour; no new API call.

    Returns +1 (swept the low, closed back above -> bullish), -1 (swept the
    high, closed back below -> bearish), or 0 (no sweep / not enough bars).
    Asian session window is approximated as UTC 00:00-08:00 (Tokyo + most of
    the Singapore/HK morning) - a coarse but zero-dependency proxy.
    """
    if len(bars) < 32 or "open_time" not in bars[0]:
        return 0

    from datetime import datetime, timezone
    asian_bars = []
    for b in bars[-32:-1]:  # the window BEFORE the current/last bar
        hour = datetime.fromtimestamp(b["open_time"] / 1000, timezone.utc).hour
        if 0 <= hour < 8:
            asian_bars.append(b)
    if len(asian_bars) < 4:
        return 0

    asian_high = max(b["high"] for b in asian_bars)
    asian_low = min(b["low"] for b in asian_bars)
    last = bars[-1]

    if last["low"] < asian_low and last["close"] > asian_low:
        return 1
    if last["high"] > asian_high and last["close"] < asian_high:
        return -1
    return 0


def dxy_proxy_roc(dxy_bars: list[dict] | None, lookback: int = 24) -> float | None:
    """Rate-of-change on the EURUSDT proxy over `lookback` bars, or None.

    This is EUR direction, which is DOLLAR-INVERSE: EUR up usually means the
    dollar is weakening, which is a tailwind for dollar-priced gold. The sign
    flip happens in score(), not here - this function just reports what the
    proxy actually did.
    """
    if not dxy_bars or len(dxy_bars) <= lookback:
        return None
    closes = [b["close"] for b in dxy_bars]
    if closes[-(lookback + 1)] == 0:
        return None
    return (closes[-1] - closes[-(lookback + 1)]) / closes[-(lookback + 1)] * 100


def compute_features(bars: list[dict], dxy_bars: list[dict] | None = None) -> dict:
    """Technical feature set from OHLCV bars. Last bar = 'now'.

    `dxy_bars` is OPTIONAL and additive: every existing caller that passes
    only `bars` still gets back exactly the same keys it got before, plus the
    two new ones below with a safe None/0 default. This is what keeps old
    stored features_json (which never has these keys) scoring correctly
    through score()'s .get() lookups - this function and that one are the
    only two places that need to agree on the new keys' defaults.
    """
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    vols = [b["volume"] for b in bars]

    a = atr(highs, lows, closes, 14)
    e12, e26 = ema(closes, 12), ema(closes, 26)
    sma20 = statistics.fmean(closes[-20:])
    r = rsi(closes, 14)
    avg_vol = statistics.fmean(vols[-24:]) if len(vols) >= 24 else statistics.fmean(vols)

    return {
        "close": closes[-1],
        "ema12": e12,
        "ema26": e26,
        "sma20": sma20,
        "rsi14": r,
        "atr14": a,
        "roc24": (closes[-1] - closes[-25]) / closes[-25] * 100 if len(closes) > 25 else None,
        "mom1": (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) > 2 else None,
        "higher_high_6": closes[-1] > max(highs[-7:-1]) if len(highs) > 7 else None,
        "lower_low_6": closes[-1] < min(lows[-7:-1]) if len(lows) > 7 else None,
        "vol_vs_avg": (vols[-1] / avg_vol) if avg_vol else None,
        "atr_pct": (a / closes[-1] * 100) if a else None,
        "bars": len(bars),
        "session_sweep": session_sweep_signal(bars),
        "dxy_roc24": dxy_proxy_roc(dxy_bars, 24),
    }


def score(f: dict, params: dict | None = None) -> tuple[float, dict]:
    """Return (score in [-1,1], per-parameter contributions)."""
    params = params or build_params()
    contrib = {}

    p = params["trend"]
    if f["ema12"] is not None and f["ema26"] is not None and f["atr14"]:
        d = (f["ema12"] - f["ema26"]) / f["atr14"]
        contrib["trend"] = p["weight"] * max(-1.0, min(1.0, d))

    p = params["position"]
    if f["atr14"]:
        d = (f["close"] - f["sma20"]) / f["atr14"]
        contrib["position"] = p["weight"] * max(-1.0, min(1.0, d))

    p = params["momentum"]
    if f["roc24"] is not None:
        contrib["momentum"] = p["weight"] * max(-1.0, min(1.0, f["roc24"] / 0.5))

    p = params["rsi"]
    if f["rsi14"] is not None:
        contrib["rsi"] = p["weight"] * max(-1.0, min(1.0, (f["rsi14"] - 50) / 20))

    p = params["breadth"]
    if f["higher_high_6"] is not None:
        b = (1 if f["higher_high_6"] else 0) + (-1 if f["lower_low_6"] else 0)
        contrib["breadth"] = p["weight"] * b

    p = params["volume_confirm"]
    if f["vol_vs_avg"] is not None:
        direction = 1 if contrib.get("trend", 0) >= 0 else -1
        strength = max(-1.0, min(1.0, (f["vol_vs_avg"] - 1.0) / 1.0))
        contrib["volume_confirm"] = p["weight"] * direction * strength

    # New indicators (see build_params docstring: weight 0.0 until promoted).
    # params.get() tolerates an old stored params_json or an improve.py
    # candidate dict that predates these keys - defaulting to weight 0.0
    # reproduces "not in the jury yet" rather than raising KeyError.
    p = params.get("session_sweep", {"weight": 0.0})
    sweep = f.get("session_sweep")
    if sweep:
        contrib["session_sweep"] = p["weight"] * max(-1.0, min(1.0, float(sweep)))

    p = params.get("dxy_proxy", {"weight": 0.0})
    dxy_roc = f.get("dxy_roc24")
    if dxy_roc is not None:
        # EUR up ~ dollar down ~ gold tailwind, so the sign is INVERTED here.
        contrib["dxy_proxy"] = -p["weight"] * max(-1.0, min(1.0, dxy_roc / 1.0))

    total = sum(contrib.values())
    total = max(-1.0, min(1.0, total))
    return total, {k: round(v, 4) for k, v in contrib.items()}


def explain_prediction(features: dict, contributions: dict, direction: str, score: float, noise_thr: float) -> str:
    """Generate a clean, human-readable rationale for the prediction."""
    if direction == "no_call":
        return f"Market in low-conviction chop (|score|={abs(score):.2f} < 0.18 abstain threshold). Volatility filter: ${noise_thr:.2f}."
    
    # Sort strongest contributing parameters
    drivers = sorted(contributions.items(), key=lambda kv: abs(kv[1]), reverse=True)
    top_pos = [f"{k} (+{v:.2f})" for k, v in drivers if v > 0.03]
    top_neg = [f"{k} ({v:.2f})" for k, v in drivers if v < -0.03]
    
    parts = []
    close = features.get("close", 0)
    sma20 = features.get("sma20", 0)
    rsi14 = features.get("rsi14", 50)
    ema12 = features.get("ema12", 0)
    ema26 = features.get("ema26", 0)

    if direction == "bullish":
        reasons = []
        if close > sma20:
            reasons.append(f"Price (${close:.1f}) holding above 20-SMA (${sma20:.1f})")
        if ema12 > ema26:
            reasons.append("Fast EMA12 above EMA26")
        if rsi14 > 50:
            reasons.append(f"RSI bullish momentum ({rsi14:.1f})")
        if features.get("higher_high_6"):
            reasons.append("Formed higher high over 6h")
        
        main_story = ". ".join(reasons) if reasons else "Confluence of technical momentum indicators"
        parts.append(f"Bullish bias: {main_story}.")
    else:
        reasons = []
        if close < sma20:
            reasons.append(f"Price (${close:.1f}) rejected below 20-SMA (${sma20:.1f})")
        if ema12 < ema26:
            reasons.append("Fast EMA12 under EMA26")
        if rsi14 < 50:
            reasons.append(f"RSI bearish momentum ({rsi14:.1f})")
        if features.get("lower_low_6"):
            reasons.append("Formed lower low over 6h")
        
        main_story = ". ".join(reasons) if reasons else "Confluence of technical momentum indicators"
        parts.append(f"Bearish bias: {main_story}.")

    if top_pos:
        parts.append(f"Key drivers: {', '.join(top_pos)}.")
    if top_neg:
        parts.append(f"Drag/headwinds: {', '.join(top_neg)}.")
    
    return " ".join(parts)


def predict(bars: list[dict], params: dict | None = None,
           dxy_bars: list[dict] | None = None) -> dict:
    """Full decision: direction, confidence, features, contributions.

    `dxy_bars` is optional; omitting it just means the dxy_proxy feature is
    None (and, since its weight defaults to 0.0, contributes nothing).
    """
    features = compute_features(bars, dxy_bars)
    s, contrib = score(features, params)

    if abs(s) < ABSTAIN_BAR:
        direction = "no_call"
    else:
        direction = "bullish" if s > 0 else "bearish"

    noise_thr = round(NOISE_ATR_MULT * (features["atr14"] or 0), 4)
    reason = explain_prediction(features, contrib, direction, s, noise_thr)

    return {
        "direction": direction,
        "score": round(s, 4),
        "confidence": round(min(1.0, abs(s)), 4),
        "features": features,
        "contributions": contrib,
        "model_version": MODEL_VERSION,
        "params": params or build_params(),
        # the threshold this call will be graded against - stored at forecast time
        "noise_threshold": noise_thr,
        "reason": reason,
    }


if __name__ == "__main__":
    import json
    import urllib.request

    UA = "Mozilla/5.0 (X11; Linux x86_64) Chrome/125 Safari/537.36"
    url = ("https://data-api.binance.vision/api/v3/klines"
           "?symbol=PAXGUSDT&interval=1h&limit=200")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    raw = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
    bars = [{"open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
             "close": float(k[4]), "volume": float(k[5])} for k in raw]

    out = predict(bars)
    print(f"MODEL {out['model_version']}")
    print(f"  direction   : {out['direction']}")
    print(f"  score       : {out['score']:+.4f}")
    print(f"  confidence  : {out['confidence']:.4f}")
    print(f"  noise_thr   : {out['noise_threshold']}")
    print(f"  contributions:")
    for k, v in sorted(out["contributions"].items(), key=lambda x: -abs(x[1])):
        print(f"    {k:<16} {v:+.4f}")
    print(f"  key features: RSI={out['features']['rsi14']:.2f} "
          f"ATR={out['features']['atr14']:.2f} "
          f"close_vs_sma20={((out['features']['close']-out['features']['sma20'])/out['features']['sma20']*100):+.3f}%")
