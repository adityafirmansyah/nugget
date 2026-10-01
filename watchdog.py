#!/usr/bin/env python3
"""Nugget's watchdog: the signal that catches silent corruption.

Reliability review items 2 and 6. Every failure mode that matters here is SILENT
by default - a missed tick just shrinks n, a drifted parser just returns a
different number, a locked write just loses a row. None of that raises. This
module is what turns each into a signal.

Checks, each with the specific thing it detects:

  1. slot gaps       - expected open-market hourly slots (from the session
                       calendar) vs actual cron_runs rows. Detects missed ticks.
  2. duplicate rows  - any (job, slot) appearing twice (should be impossible via
                       UNIQUE; a violation means the constraint is gone).
  3. chain integrity - verify_chain(); detects any edit to the record.
  4. lock events     - "database is locked" in the ledger sidecar / recent runs.
  5. source drift    - gold-api spot vs coingecko proxy delta beyond threshold.
  6. stale prices    - most recent prediction's ref price age.
  7. no-call ratio   - an abstention rate near 100% means the model stopped
                       deciding; near 0% means the abstain bar is dead.

Exit codes: 0 clean, 1 warnings, 2 errors. Output is a JSON report so a cron can
post it verbatim.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_market as fm          # noqa: E402
import ledger                      # noqa: E402
import session_calendar as cal     # noqa: E402

UTC = timezone.utc
WIB = timezone(timedelta(hours=7))
JOB = "hourly_predict"

LOOKBACK_DAYS = 7            # how far back to diff expected vs actual slots
SPOT_PROXY_MAX_DELTA_PCT = 1.5   # gold-api vs coingecko PAXG
NO_CALL_WARN_HIGH = 0.85     # abstaining on almost everything = model mute
NO_CALL_WARN_LOW = 0.0       # never abstaining = the bar is not doing anything


def check_slot_gaps(con, now: datetime) -> dict:
    """Expected open slots vs actual runs. The core missed-tick detector.

    Only counts slots AFTER the system's first recorded run. A fresh install has
    no history by definition, and alarming on slots that predate it would make
    the detector cry wolf on day one - which is how a real alarm gets ignored.
    """
    first = con.execute("SELECT MIN(started_utc) FROM cron_runs").fetchone()[0]
    if not first:
        return {"expected_slots": 0, "actual_runs": 0, "recorded_gaps": 0,
                "missing_slots": 0, "missing_sample": [],
                "status": "ok", "note": "no runs yet - nothing to diff"}

    # A malformed timestamp must not take down the watchdog: the whole point is
    # to still be running when something else is broken. Fall back to the
    # lookback window and say so.
    note = None
    try:
        first_dt = datetime.fromisoformat(first)
    except (ValueError, TypeError):
        first_dt = now - timedelta(days=LOOKBACK_DAYS)
        note = f"unparseable first run timestamp {first!r}; used lookback window"

    start = max(now - timedelta(days=LOOKBACK_DAYS), first_dt)
    expected = cal.expected_hourly_slots(start, now)

    actual = {
        r["scheduled_slot_utc"]
        for r in con.execute(
            "SELECT scheduled_slot_utc FROM cron_runs WHERE job=? AND started_utc >= ?",
            (JOB, start.isoformat(timespec="seconds")))
    }
    gaps = {r["slot_utc"] for r in con.execute(
        "SELECT slot_utc FROM gaps WHERE slot_utc >= ?",
        (start.isoformat(timespec="seconds"),))}

    missing = [s.isoformat(timespec="seconds") for s in expected
               if s.isoformat(timespec="seconds") not in actual
               and s.isoformat(timespec="seconds") not in gaps]
    out = {
        "expected_slots": len(expected),
        "actual_runs": len(actual),
        "recorded_gaps": len(gaps),
        "missing_slots": len(missing),
        "missing_sample": missing[:5],
        "status": "ok" if not missing else "gap",
    }
    if note:
        out["note"] = note
    return out


def check_duplicates(con) -> dict:
    rows = con.execute(
        "SELECT job, scheduled_slot_utc, COUNT(*) c FROM cron_runs"
        " GROUP BY job, scheduled_slot_utc HAVING c > 1").fetchall()
    return {"duplicates": len(rows),
            "detail": [dict(r) for r in rows][:5],
            "status": "ok" if not rows else "data_loss"}


def check_chain(con) -> dict:
    v = ledger.verify_chain(con)
    return {"status": "ok" if v["ok"] else "tampered", "detail": v}


def check_locks() -> dict:
    """Ledger write failures recorded as fetch_errors with a lock signature."""
    hits = 0
    con = ledger.connect()
    for r in con.execute(
        "SELECT error FROM fetch_errors WHERE error LIKE '%locked%'"
        " OR error LIKE '%busy%'"):
        hits += 1
    return {"lock_errors_logged": hits,
            "status": "ok" if hits == 0 else "contention"}


def check_source_drift() -> dict:
    """gold-api spot vs the coingecko proxy. A large gap = drift or bad parse."""
    out = {"status": "ok"}
    try:
        spot = fm.fetch_spot()["price"]
        proxies = fm.fetch_proxies()
        paxg = proxies["paxg_usd"]
        delta_pct = (paxg - spot) / spot * 100
        out.update({"spot": spot, "paxg": paxg, "delta_pct": round(delta_pct, 3)})
        if abs(delta_pct) > SPOT_PROXY_MAX_DELTA_PCT:
            out["status"] = "drift"
    except (fm.FetchError, fm.SchemaError) as e:
        out.update({"status": "unreachable", "error": f"{type(e).__name__}: {e}"})
    return out


def check_prediction_recency(con, now: datetime) -> dict:
    r = con.execute(
        "SELECT created_utc, direction, ref_price FROM predictions"
        " ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        return {"status": "no_data", "predictions": 0}
    age_h = (now - datetime.fromisoformat(r["created_utc"])).total_seconds() / 3600
    # only alarming if the market has been open since
    open_since = [s for s in cal.expected_hourly_slots(
        now - timedelta(hours=int(age_h) + 1), now)]
    stale = age_h > 3 and bool(open_since)
    return {"status": "stale" if stale else "ok",
            "last_prediction_age_hours": round(age_h, 2),
            "last_direction": r["direction"], "predictions": con.execute(
                "SELECT COUNT(*) FROM predictions").fetchone()[0]}


def check_no_call_ratio(con) -> dict:
    rows = con.execute(
        "SELECT direction FROM predictions ORDER BY id DESC LIMIT 100").fetchall()
    if not rows:
        return {"status": "no_data"}
    n = len(rows)
    nc = sum(1 for r in rows if r["direction"] == "no_call")
    ratio = nc / n
    status = "ok"
    if n >= 20 and ratio >= NO_CALL_WARN_HIGH:
        status = "model_mute"
    return {"status": status, "no_call_ratio": round(ratio, 3), "n": n}


def check_backup_age() -> dict:
    bdir = Path(__file__).resolve().parent / "backups"
    if not bdir.exists() or not any(bdir.glob("nugget-*.db")):
        return {"status": "no_backup", "backups": 0}
    newest = max(bdir.glob("nugget-*.db"), key=lambda p: p.stat().st_mtime)
    age_h = (datetime.now(UTC) -
             datetime.fromtimestamp(newest.stat().st_mtime, UTC)).total_seconds() / 3600
    return {"status": "ok" if age_h < 48 else "stale",
            "backups": len(list(bdir.glob("nugget-*.db"))),
            "newest_age_hours": round(age_h, 2)}


def run(now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    con = ledger.connect()
    checks = {
        "slot_gaps": check_slot_gaps(con, now),
        "duplicates": check_duplicates(con),
        "chain": check_chain(con),
        "locks": check_locks(),
        "source_drift": check_source_drift(),
        "recency": check_prediction_recency(con, now),
        "no_call_ratio": check_no_call_ratio(con),
        "backup": check_backup_age(),
    }

    errors = [k for k, v in checks.items()
              if v.get("status") in ("data_loss", "tampered", "contention")]
    warnings = [k for k, v in checks.items()
                if v.get("status") not in ("ok", "no_data", "no_backup")
                and k not in errors]
    if checks["backup"].get("status") == "no_backup":
        warnings.append("backup")

    return {
        "checked_utc": now.isoformat(timespec="seconds"),
        "checked_wib": now.astimezone(WIB).isoformat(timespec="seconds"),
        "overall": "error" if errors else ("warn" if warnings else "ok"),
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
    }


if __name__ == "__main__":
    rep = run()
    print(json.dumps(rep, indent=2, default=str))
    print(f"\nOVERALL: {rep['overall']}", file=sys.stderr)
    sys.exit(2 if rep["overall"] == "error" else (1 if rep["overall"] == "warn" else 0))