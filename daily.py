#!/usr/bin/env python3
"""Job 1 - daily pre-open prediction, and Job 5 - the grounded daily report.

Both are LLM-facing, so both get the same structural rule from the reliability
review: the LLM is a PROPOSER; a non-LLM validator is the COMMITTER.

  job 1: the daily call is produced by the SAME deterministic predictor as the
         hourly cycle (not by prose). The LLM's only role is to narrate the
         ledger row that already exists - it cannot invent the direction.

  job 5: every number in the report must resolve to a ledger row or the day's
         fetch. build_report() assembles those numbers from the ledger in code;
         the prose is generated around them and then checked with
         validate_grounding() - any figure not present in the evidence bundle
         REJECTS the report instead of posting it.

No trade advice, ever. Every reported rate carries n, CI and the base rate.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_market as fm          # noqa: E402
import ledger                      # noqa: E402
import predictor                   # noqa: E402
import session_calendar as cal     # noqa: E402
import stats                       # noqa: E402

UTC = timezone.utc
WIB = timezone(timedelta(hours=7))
JOB_DAILY = "daily_predict"
JOB_REPORT = "daily_report"

DISCLAIMER = ("Learning instrument and reference only - NOT financial advice. "
              "No trade execution. Numbers carry their uncertainty.")


# ------------------------------------------------------------------- helpers

def next_open(now: datetime) -> datetime:
    """The next session START at or after `now` (UTC).

    "Start" = the first open hour whose PREVIOUS hour was closed. During a
    running session this is the resume after the daily maintenance break.
    """
    probe = now
    for _ in range(24 * 8):
        probe = probe.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        if cal.is_open(probe):
            # walk back to the first open hour of this session block
            if not cal.is_open(probe - timedelta(hours=1)):
                return probe
    return probe


def daily_target(now: datetime) -> datetime:
    """The bar a daily call is judged against: the next OPEN instant >= now+24h.

    The target must itself be an open-market instant, because that is when the
    recorded price is meaningful. Two traps this avoids:
      * next_open + 24h lands back inside the daily maintenance break, since the
        break recurs at the same UTC hour each day
      * stepping into a weekend would otherwise grade against a closed market
    So: start at now+24h and advance to the first open hour (weekends included).
    """
    t = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=24)
    for _ in range(24 * 5):          # a long weekend is 2.5 days
        if cal.is_open(t):
            return t
        t += timedelta(hours=1)
    return t


def day_ahead_evidence(con, now: datetime) -> dict:
    """The evidence bundle for a daily call. Everything here is measured."""
    con_spot = None
    snap = fm.collect()
    candles = snap["candles"]
    dxy_bars = snap.get("dxy_proxy")
    feats = predictor.compute_features(candles, dxy_bars)
    p = predictor.predict(candles, dxy_bars=dxy_bars)
    base = stats.summarize([dict(r) for r in con.execute(
        """SELECT o.correct, o.is_noise, o.base_rate FROM outcomes o""")])

    # Real yield (US 10Y TIPS, DFII10): daily-cadence macro context, NOT a
    # jury vote - it updates once a business day, so folding it into the
    # hourly score would just re-score the same stale value 23 times. It is
    # surfaced here for the daily report's narrative only.
    real_yield = None
    try:
        real_yield = fm.fetch_real_yield()
    except (fm.FetchError, fm.SchemaError):
        pass  # soft-optional: a missing macro context must not block the call

    # macro events in the next 24h
    upcoming = []
    try:
        for e in snap.get("calendar") or []:
            dt = datetime.fromisoformat(e["date"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            if 0 <= (dt - now).total_seconds() <= 24 * 3600 and e["impact"] == "High":
                upcoming.append({"wib": dt.astimezone(WIB).strftime("%a %H:%M"),
                                 "ccy": e["country"], "title": e["title"],
                                 "forecast": e["forecast"], "previous": e["previous"]})
    except Exception:
        pass

    return {
        "now_utc": now.isoformat(timespec="seconds"),
        "now_wib": now.astimezone(WIB).isoformat(timespec="seconds"),
        "next_open_utc": next_open(now).isoformat(timespec="seconds"),
        "spot": snap["spot"]["price"],
        "paxg": (snap.get("proxies") or {}).get("paxg_usd"),
        "basis": (round((snap.get("proxies") or {}).get("paxg_usd", snap["spot"]["price"])
                        - snap["spot"]["price"], 2)),
        "features": feats,
        "direction": p["direction"],
        "score": p["score"],
        "confidence": p["confidence"],
        "contributions": p["contributions"],
        "noise_threshold": p["noise_threshold"],
        "model_version": p["model_version"],
        "track_record": base,
        "high_impact_24h": upcoming,
        "real_yield_10y": real_yield,
        "ledger_counts": {
            t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("predictions", "outcomes", "gaps", "fetch_errors")},
        "source_status": snap["source_status"],
    }


# --------------------------------------------------------------- job 1

def run_daily(now: datetime | None = None, *, dry_run: bool = False) -> dict:
    """Daily pre-open prediction. Deterministic call, recorded before the day."""
    now = now or datetime.now(UTC)
    slot = now.replace(minute=0, second=0, microsecond=0)
    slot_iso = slot.isoformat(timespec="seconds")
    con = ledger.connect()

    if not dry_run and not ledger.claim_slot(con, JOB_DAILY, slot_iso):
        return {"action": "duplicate_slot_rejected", "slot_utc": slot_iso}

    try:
        ev = day_ahead_evidence(con, now)
    except (fm.FetchError, fm.SchemaError) as e:
        if not dry_run:
            ledger.record_fetch_error(con, "daily", f"{type(e).__name__}: {e}")
            ledger.finish_slot(con, JOB_DAILY, slot_iso, "failed", str(e)[:300])
        return {"action": "fetch_failed", "error": f"{type(e).__name__}: {e}"}

    target = daily_target(now)
    pid = None
    if not dry_run:
        notes = ev.get("reason") or f"daily; contrib={json.dumps(ev['contributions'])}"
        pid = ledger.record_prediction(
            con, horizon="24h",
            target_bar_utc=target.isoformat(timespec="seconds"),
            scheduled_slot_utc=slot_iso,
            direction=ev["direction"], confidence=ev["confidence"],
            ref_price=ev["spot"], model_version=ev["model_version"],
            params=predictor.build_params(), features=ev["features"],
            inputs={"spot": ev["spot"], "paxg": ev["paxg"], "basis": ev["basis"],
                    "source_status": ev["source_status"],
                    "high_impact_24h": ev["high_impact_24h"]},
            signal_source="technical", noise_threshold=ev["noise_threshold"],
            notes=notes)
        ledger.finish_slot(con, JOB_DAILY, slot_iso, "ok", ev["direction"])

    return {"action": "predicted", "slot_utc": slot_iso, "prediction_id": pid,
            "evidence": ev}


# --------------------------------------------------------------- job 5

def build_report_evidence(con, now: datetime) -> dict:
    """The ONLY source of numbers allowed in job 5's report."""
    ev = day_ahead_evidence(con, now)
    rows = [dict(r) for r in con.execute(
        """SELECT o.correct, o.is_noise, o.base_rate, p.direction, p.signal_source
           FROM outcomes o JOIN predictions p ON p.id = o.prediction_id""")]
    ev["track_record"] = stats.summarize(rows)
    ev["chain"] = ledger.verify_chain(con)
    ev["yesterday"] = [
        {k: r[k] for k in ("prediction_id", "realized_dir", "move", "move_pct",
                           "is_noise", "correct")}
        for r in con.execute(
            """SELECT * FROM outcomes ORDER BY prediction_id DESC LIMIT 5""")]
    ev["open_today"] = [s.isoformat(timespec="seconds") for s in
                        cal.expected_hourly_slots(now, now + timedelta(hours=24))][:3]
    return ev


def _numbers(text: str) -> list[tuple[float, int]]:
    """Every number in `text` with its stated decimal precision.

    Precision matters: a report that rounds 0.2335 to "0.23" is making the SAME
    claim, so the comparison has to be numeric, not textual.
    """
    out = []
    for m in re.finditer(r"-?\d+\.?\d*", text):
        s = m.group(0)
        try:
            v = float(s)
        except ValueError:
            continue
        dec = len(s.split(".")[1]) if "." in s else 0
        out.append((v, dec))
    return out


def validate_grounding(prose: str, evidence: dict) -> dict:
    """Reject a report containing a figure that is not in the evidence.

    Comparison is NUMERIC with the prose's stated precision, so "4177.00" is
    accepted against an evidence value of 4177.0 and "0.23" against 0.2335 -
    rounding is not fabrication. A value with no near-match in the evidence
    bundle is fabrication, and rejects the whole report.
    """
    ev_nums = [v for v, _ in _numbers(json.dumps(evidence, default=str))]
    ungrounded = []
    for v, dec in _numbers(prose):
        tol = 0.5 * (10 ** -dec) if dec else 0.0
        hit = any(abs(e - v) <= tol or round(e, dec) == round(v, dec)
                  for e in ev_nums)
        if not hit and dec == 0:
            # bare small integers (counts) and years are structural, not claims
            hit = (0 <= v <= 12) or (2024 <= v <= 2035)
        if not hit:
            ungrounded.append(f"{v:g}")
    return {"ok": not ungrounded, "ungrounded": ungrounded[:20],
            "count": len(ungrounded)}


def run_report(now: datetime | None = None, *, dry_run: bool = False,
               prose: str | None = None, evidence: dict | None = None) -> dict:
    """Gather evidence, optionally validate supplied prose, and record the run.

    `evidence` lets a caller validate prose against the exact bundle it was
    written from, instead of a fresh fetch that may have moved.
    """
    now = now or datetime.now(UTC)
    slot = now.replace(minute=0, second=0, microsecond=0)
    slot_iso = slot.isoformat(timespec="seconds")
    con = ledger.connect()

    if not dry_run and not ledger.claim_slot(con, JOB_REPORT, slot_iso):
        return {"action": "duplicate_slot_rejected", "slot_utc": slot_iso}

    try:
        ev = build_report_evidence(con, now)
    except (fm.FetchError, fm.SchemaError) as e:
        if not dry_run:
            ledger.record_fetch_error(con, "report", f"{type(e).__name__}: {e}")
            ledger.finish_slot(con, JOB_REPORT, slot_iso, "failed", str(e)[:300])
        return {"action": "fetch_failed", "error": f"{type(e).__name__}: {e}"}

    out = {"action": "evidence_ready", "slot_utc": slot_iso, "evidence": ev,
           "disclaimer": DISCLAIMER}

    if prose is not None:
        # Validate against the SAME bundle the prose was written from. Callers
        # that already hold an evidence bundle (the normal path - the writer
        # reads the evidence, writes prose, hands both back) pass it in, so a
        # live price tick between write and validate cannot spuriously reject
        # an accurate report.
        check_ev = evidence if evidence is not None else ev
        g = validate_grounding(prose, check_ev)
        out["grounding"] = g
        if not g["ok"]:
            out["action"] = "report_rejected"
            out["reason"] = "ungrounded numbers: " + ", ".join(g["ungrounded"])
            if not dry_run:
                ledger.finish_slot(con, JOB_REPORT, slot_iso, "failed",
                                   out["reason"][:300])
            return out
        out["action"] = "report_ok"
        out["prose"] = prose

    if not dry_run:
        ledger.finish_slot(con, JOB_REPORT, slot_iso, "ok", out["action"])
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", choices=["daily", "report"], default="daily")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    fn = run_daily if a.job == "daily" else run_report
    print(json.dumps(fn(dry_run=a.dry_run), indent=2, default=str))