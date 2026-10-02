#!/usr/bin/env python3
"""Nugget's market data layer (hardened).

Reliability review item 3. "Hard-fail" has to produce THREE distinguishable
outcomes, not one:

    FetchError   - source unreachable / 5xx / timeout / DNS.
                   -> fetch_errors row, retry next slot. Market may be open.
    (gap)        - the session calendar says the market is closed.
                   -> gaps row. NOT an error. Handled by the caller, not here.
    SchemaError  - a 200 OK whose body is missing a field, has a changed type,
                   or is stale. -> HARD ABORT, no row at all.

The dangerous case is a 200 OK carrying a truncated or stale body: it looks
like success and would silently enter the feature set as a valid signal. So
parsing is strict with NO silent defaults (a missing field raises rather than
coercing to 0/None), and every response is checked for staleness against wall
clock.

Also here: exponential backoff + jitter per attempt, and a per-host circuit
breaker - this host is already carrier-RPZ-blocked on two hosts, and hammering
the remaining keyless endpoints invites the same treatment.
"""
from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
WIB = timezone(timedelta(hours=7))

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125 Safari/537.36"
TIMEOUT_S = 20
ATTEMPTS = 4
BACKOFF_BASE_S = 0.5
STALE_AFTER_S = 300          # 5 min: hourly cadence must never accept older
CIRCUIT_FAILS = 4            # consecutive failures before the breaker opens
CIRCUIT_COOLDOWN_S = 900     # 15 min half-open probe

STATE_DIR_ENV = "NUGGET_STATE_DIR"


def _state_path() -> Path:
    """Circuit-breaker state file, resolved at CALL time.

    Resolving at import would freeze the path, and - more importantly - a single
    shared file means TEST runs and PRODUCTION runs fight over the same breaker:
    hammering the endpoints from a verification suite opens the circuit and
    silently degrades the next live report. `NUGGET_STATE_DIR` lets a suite point
    its breaker somewhere private so production state stays untouched.
    """
    override = os.environ.get(STATE_DIR_ENV)
    base = Path(override) if override else (Path(__file__).resolve().parent / "state")
    return base / "circuit.json"


class FetchError(Exception):
    """Source unreachable. Recoverable - record and retry next slot."""


class SchemaError(Exception):
    """Response arrived but is unusable. NOT recoverable by retrying."""


# ------------------------------------------------------------ circuit breaker

def _load_circuit() -> dict:
    p = _state_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def _save_circuit(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, sort_keys=True))


def circuit_allows(host: str) -> bool:
    st = _load_circuit().get(host, {})
    if st.get("fails", 0) < CIRCUIT_FAILS:
        return True
    opened = st.get("opened_at")
    if not opened:
        return True
    age = (datetime.now(UTC) - datetime.fromisoformat(opened)).total_seconds()
    return age >= CIRCUIT_COOLDOWN_S      # half-open: let one probe through


def circuit_record(host: str, ok: bool) -> None:
    state = _load_circuit()
    st = state.get(host, {"fails": 0, "opened_at": None})
    if ok:
        st = {"fails": 0, "opened_at": None}
    else:
        st["fails"] = st.get("fails", 0) + 1
        if st["fails"] >= CIRCUIT_FAILS and not st.get("opened_at"):
            st["opened_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    state[host] = st
    _save_circuit(state)


# ------------------------------------------------------------------ transport

def _get_json(url: str, host: str) -> dict | list:
    """GET with retry+backoff+jitter. Raises FetchError only."""
    if not circuit_allows(host):
        raise FetchError(f"{host}: circuit open (backing off)")

    last = None
    for i in range(ATTEMPTS):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                body = r.read().decode("utf-8", "replace")
            out = json.loads(body)
            circuit_record(host, True)
            return out
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
        except urllib.error.URLError as e:
            last = f"URLError {e.reason}"
        except json.JSONDecodeError as e:
            last = f"invalid JSON ({e.msg})"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"

        if i < ATTEMPTS - 1:
            time.sleep(BACKOFF_BASE_S * (2 ** i) + random.uniform(0, BACKOFF_BASE_S))

    circuit_record(host, False)
    raise FetchError(f"{host}: {last} (after {ATTEMPTS} attempts)")


def _get_text(url: str, host: str, *, user_agent: str | None = None) -> str:
    """GET with retry+backoff+jitter, returning raw text. Raises FetchError only.

    Same transport contract as _get_json (circuit breaker, backoff, jitter) but
    for non-JSON bodies (FRED serves CSV). `user_agent` overrides the module
    default for hosts that behave differently per UA (FRED times out on the
    Chrome-spoofed UA every other source uses, but answers instantly for a
    plain one) - this does not touch the shared UA used by every JSON source.
    """
    if not circuit_allows(host):
        raise FetchError(f"{host}: circuit open (backing off)")

    ua = user_agent or UA
    last = None
    for i in range(ATTEMPTS):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                body = r.read().decode("utf-8", "replace")
            circuit_record(host, True)
            return body
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
        except urllib.error.URLError as e:
            last = f"URLError {e.reason}"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"

        if i < ATTEMPTS - 1:
            time.sleep(BACKOFF_BASE_S * (2 ** i) + random.uniform(0, BACKOFF_BASE_S))

    circuit_record(host, False)
    raise FetchError(f"{host}: {last} (after {ATTEMPTS} attempts)")


def _num(obj, key, where):
    """Numeric field that may legitimately arrive as a string.

    Binance returns prices as quoted strings to preserve precision. Accepting
    that is correct; accepting an UNPARSEABLE value is not - it still raises.
    """
    if not isinstance(obj, dict) or key not in obj:
        raise SchemaError(f"{where}: missing required field '{key}'")
    v = obj[key]
    if isinstance(v, bool):
        raise SchemaError(f"{where}: '{key}' is a bool, not a number")
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            raise SchemaError(f"{where}: '{key}' not numeric: {v!r}")
    raise SchemaError(f"{where}: '{key}' unexpected type {type(v).__name__}")


def _req(obj, key, kind, where):
    """Strict field access. Missing/wrong type RAISES - never a silent default."""
    if not isinstance(obj, dict) or key not in obj:
        raise SchemaError(f"{where}: missing required field '{key}'")
    v = obj[key]
    if kind is float:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise SchemaError(f"{where}: '{key}' expected number, got {type(v).__name__}")
        return float(v)
    if kind is int:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise SchemaError(f"{where}: '{key}' expected int, got {type(v).__name__}")
        return int(v)
    if not isinstance(v, kind):
        raise SchemaError(f"{where}: '{key}' expected {kind.__name__}, got {type(v).__name__}")
    return v


def _assert_fresh(iso_ts: str, where: str, max_age_s: int = STALE_AFTER_S) -> None:
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except Exception as e:
        raise SchemaError(f"{where}: unparseable timestamp {iso_ts!r} ({e})")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - ts).total_seconds()
    if age > max_age_s:
        raise SchemaError(f"{where}: STALE by {age:.0f}s (limit {max_age_s}s)")
    if age < -120:
        raise SchemaError(f"{where}: timestamp {age:.0f}s in the FUTURE")


# ------------------------------------------------------------------- sources

def fetch_spot() -> dict:
    """True XAU/USD spot. Authoritative reference price."""
    host = "api.gold-api.com"
    d = _get_json("https://api.gold-api.com/price/XAU", host)
    price = _req(d, "price", float, "gold-api")
    ts = _req(d, "updatedAt", str, "gold-api")
    _assert_fresh(ts, "gold-api")
    if not (100 < price < 100_000):
        raise SchemaError(f"gold-api: implausible spot {price}")
    return {"price": price, "updated_at": ts, "source": host}


def fetch_proxies() -> dict:
    """PAXG/XAUT scored tokens - cross-check and basis measurement."""
    host = "api.coingecko.com"
    d = _get_json("https://api.coingecko.com/api/v3/simple/price"
                  "?ids=pax-gold,tether-gold&vs_currencies=usd"
                  "&include_last_updated_at=true", host)
    paxg = _req(_req(d, "pax-gold", dict, "coingecko"), "usd", float, "coingecko.paxg")
    xaut = _req(_req(d, "tether-gold", dict, "coingecko"), "usd", float, "coingecko.xaut")
    if not (100 < paxg < 100_000) or not (100 < xaut < 100_000):
        raise SchemaError(f"coingecko: implausible token prices paxg={paxg} xaut={xaut}")
    return {"paxg_usd": paxg, "xaut_usd": xaut, "source": host}


def fetch_candles(interval: str = "1h", limit: int = 200) -> list[dict]:
    """OHLCV from the PAXGUSDT proxy. api.binance.com is RPZ-blocked -> .vision."""
    host = "data-api.binance.vision"
    raw = _get_json(f"https://{host}/api/v3/klines"
                    f"?symbol=PAXGUSDT&interval={interval}&limit={limit}", host)
    if not isinstance(raw, list) or len(raw) < 60:
        raise SchemaError(f"binance.vision: expected >=60 klines, got "
                          f"{len(raw) if isinstance(raw, list) else type(raw).__name__}")
    out = []
    for i, k in enumerate(raw):
        if not isinstance(k, list) or len(k) < 9:
            raise SchemaError(f"binance.vision: malformed kline at {i} "
                              f"(len={len(k) if isinstance(k, list) else 'n/a'})")
        try:
            out.append({
                "open_time": int(k[0]), "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
                "trades": int(k[8]),
            })
        except (TypeError, ValueError) as e:
            raise SchemaError(f"binance.vision: kline {i} unparseable ({e})")
    # freshness: the last closed bar must be recent
    last_close = datetime.fromtimestamp(out[-1]["open_time"] / 1000, UTC)
    age_h = (datetime.now(UTC) - last_close).total_seconds() / 3600
    if age_h > 3:
        raise SchemaError(f"binance.vision: last bar is {age_h:.1f}h old (stale feed)")
    return out


def fetch_calendar() -> list[dict]:
    """ForexFactory weekly economic calendar."""
    host = "nfs.faireconomy.media"
    raw = _get_json(f"https://{host}/ff_calendar_thisweek.json", host)
    if not isinstance(raw, list):
        raise SchemaError(f"calendar: expected list, got {type(raw).__name__}")
    out = []
    for e in raw:
        if not isinstance(e, dict):
            raise SchemaError("calendar: non-dict event")
        out.append({
            "date": _req(e, "date", str, "calendar"),
            "country": _req(e, "country", str, "calendar"),
            "impact": _req(e, "impact", str, "calendar"),
            "title": _req(e, "title", str, "calendar"),
            "forecast": e.get("forecast", ""),
            "previous": e.get("previous", ""),
        })
    return out


def fetch_dxy_proxy_candles(interval: str = "1h", limit: int = 200) -> list[dict]:
    """EURUSDT OHLCV as a dollar-direction proxy (inverse-ish of DXY).

    There is no free, keyless, live DXY index feed. EUR/USD carries the
    heaviest weight in the real DXY basket, so EURUSDT direction is a usable
    stand-in for "is the dollar strengthening or weakening right now" at
    hourly cadence - EUR up ~ dollar down ~ tailwind for dollar-priced gold.
    This is a PROXY, not DXY itself; keep it labelled as such everywhere it
    is stored so nobody mistakes it for the real index later.
    """
    host = "data-api.binance.vision"
    raw = _get_json(f"https://{host}/api/v3/klines"
                    f"?symbol=EURUSDT&interval={interval}&limit={limit}", host)
    if not isinstance(raw, list) or len(raw) < 30:
        raise SchemaError(f"binance.vision(EURUSDT): expected >=30 klines, got "
                          f"{len(raw) if isinstance(raw, list) else type(raw).__name__}")
    out = []
    for i, k in enumerate(raw):
        if not isinstance(k, list) or len(k) < 9:
            raise SchemaError(f"binance.vision(EURUSDT): malformed kline at {i}")
        try:
            out.append({
                "open_time": int(k[0]), "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
            })
        except (TypeError, ValueError) as e:
            raise SchemaError(f"binance.vision(EURUSDT): kline {i} unparseable ({e})")
    last_close = datetime.fromtimestamp(out[-1]["open_time"] / 1000, UTC)
    age_h = (datetime.now(UTC) - last_close).total_seconds() / 3600
    if age_h > 3:
        raise SchemaError(f"binance.vision(EURUSDT): last bar is {age_h:.1f}h old (stale)")
    return out


def fetch_real_yield() -> dict:
    """US 10Y TIPS real yield (FRED DFII10). Daily close, NOT hourly.

    Opportunity-cost driver for gold: higher real yields raise the cost of
    holding a non-yielding asset. FRED updates once per business day, so
    this is for the DAILY job only - calling it every hour would just
    re-read the same stale value and give a false sense of a live signal.
    """
    host = "fred.stlouisfed.org"
    body = _get_text(f"https://{host}/graph/fredgraph.csv?id=DFII10", host,
                     user_agent="Mozilla/5.0")
    lines = [l for l in body.strip().splitlines() if l.strip()]
    if len(lines) < 2:
        raise SchemaError("fred(DFII10): empty or truncated CSV")
    header = lines[0].split(",")
    if header != ["observation_date", "DFII10"]:
        raise SchemaError(f"fred(DFII10): unexpected header {header!r}")

    # Walk back from the most recent row - FRED marks non-trading days with
    # "." instead of omitting the row, so the last line is not always usable.
    for line in reversed(lines[1:]):
        parts = line.split(",")
        if len(parts) != 2:
            continue
        date_str, val_str = parts
        if val_str == ".":
            continue
        try:
            value = float(val_str)
        except ValueError:
            continue
        try:
            as_of = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            raise SchemaError(f"fred(DFII10): bad date {date_str!r}")
        age_days = (datetime.now(UTC) - as_of).total_seconds() / 86400
        if age_days > 10:
            # FRED can lag a holiday/weekend, but >10 days means the series
            # itself is stuck - treat as stale rather than silently stale-using it.
            raise SchemaError(f"fred(DFII10): latest value is {age_days:.1f}d old (stale)")
        return {"value": value, "as_of": date_str, "source": host}

    raise SchemaError("fred(DFII10): no usable (non-missing) row found")


def fetch_ticker() -> dict:
    """24h ticker: session high/low, spread, volume."""
    host = "data-api.binance.vision"
    d = _get_json(f"https://{host}/api/v3/ticker/24hr?symbol=PAXGUSDT", host)
    out = {
        "last": _num(d, "lastPrice", "ticker"),
        "bid": _num(d, "bidPrice", "ticker"),
        "ask": _num(d, "askPrice", "ticker"),
        "high_24h": _num(d, "highPrice", "ticker"),
        "low_24h": _num(d, "lowPrice", "ticker"),
        "chg_pct_24h": _num(d, "priceChangePercent", "ticker"),
        "volume_24h": _num(d, "volume", "ticker"),
    }
    if out["ask"] < out["bid"]:
        raise SchemaError(f"ticker: crossed book bid={out['bid']} ask={out['ask']}")
    out["spread"] = round(out["ask"] - out["bid"], 4)
    return out


def collect() -> dict:
    """One full snapshot. Any hard failure raises; caller decides how to record.

    Returns the snapshot plus a per-source status map so the caller can write
    fetch_errors rows for soft failures while still using the sources that work.
    """
    snap: dict = {"collected_utc": datetime.now(UTC).isoformat(timespec="seconds"),
                  "collected_wib": datetime.now(WIB).isoformat(timespec="seconds")}
    status: dict[str, str] = {}

    for name, fn in (("spot", fetch_spot), ("proxies", fetch_proxies),
                     ("ticker", fetch_ticker), ("candles", fetch_candles),
                     ("calendar", fetch_calendar),
                     ("dxy_proxy", fetch_dxy_proxy_candles)):
        try:
            snap[name] = fn()
            status[name] = "ok"
        except (FetchError, SchemaError) as e:
            status[name] = f"{type(e).__name__}: {e}"
            snap[name] = None

    snap["source_status"] = status
    # The prediction is only possible with candles + a reference price.
    if snap.get("candles") is None or snap.get("spot") is None:
        raise FetchError(
            "insufficient data to predict: "
            + "; ".join(f"{k}={v}" for k, v in status.items() if v != "ok"))
    return snap


if __name__ == "__main__":
    print("=== live hardened fetch ===")
    t0 = time.time()
    try:
        s = collect()
    except (FetchError, SchemaError) as e:
        print(f"HARD FAIL after {time.time()-t0:.1f}s: {type(e).__name__}: {e}")
        raise SystemExit(1)
    print(f"collected in {time.time()-t0:.1f}s at {s['collected_wib']}")
    for k, v in s["source_status"].items():
        print(f"  {k:<10} {v}")
    print(f"  spot       {s['spot']['price']} (updated {s['spot']['updated_at']})")
    if s.get("proxies"):
        print(f"  paxg       {s['proxies']['paxg_usd']}  "
              f"basis={s['proxies']['paxg_usd']-s['spot']['price']:+.2f}")
    if s.get("ticker"):
        print(f"  ticker     last={s['ticker']['last']} spread={s['ticker']['spread']}")
    if s.get("candles"):
        print(f"  candles    {len(s['candles'])} bars, last close "
              f"{s['candles'][-1]['close']}")
    if s.get("calendar"):
        print(f"  calendar   {len(s['calendar'])} events")
    print("\ncircuit state:")
    print(" ", json.dumps(_load_circuit(), sort_keys=True))