#!/usr/bin/env python3
"""Nugget's hourly cycle: the body of cronjob #2.

Order of operations, and why it is this order:

  1. calendar gate   - if the market is closed, record a GAP and stop. No fetch,
                       no prediction, no grading. A closed market is not an
                       error and must never enter the promotion gate.
  2. claim the slot  - idempotency. A duplicate or retried fire is rejected by
                       UNIQUE(job, scheduled_slot_utc) before any work happens.
  3. grade first     - score the OLDEST ungraded predictions whose target bar has
                       closed. Deliberately does not assume hour N-1 ran: a
                       missed tick simply means nothing is graded this cycle.
  4. predict         - make the call for the NEXT bar, and write it down before
                       that bar exists.
  5. finish the slot - status + detail, so the gap watchdog can diff expected
                       slots against actual rows.

Grading is threshold-aware: a move smaller than the stored noise_threshold
(0.5 x ATR14 at forecast time) is NOISE and is excluded from the hit-rate, but
counted separately. "no_call" is a legitimate recorded outcome, not a failure.
"""
from __future__ import annotations

import json
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
JOB = "hourly_predict"
BASE_RATE_WINDOW = 100          # bars used for the always-up base rate


def current_slot(now: datetime | None = None) -> datetime:
    """The top-of-hour slot this run belongs to."""
    now = now or datetime.now(UTC)
    return now.replace(minute=0, second=0, microsecond=0)


def base_rate_from(candles: list[dict], window: int = BASE_RATE_WINDOW) -> float:
    """The always-up rate in this window. NOT 0.5 - that is the whole point."""
    closes = [c["close"] for c in candles[-window:]]
    if len(closes) < 2:
        return 0.5
    ups = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i - 1])
    return ups / (len(closes) - 1)


def grade_due(con, candles: list[dict], now: datetime) -> list[dict]:
    """Grade every prediction whose target bar has closed. Returns summaries."""
    by_time = {c["open_time"] // 1000: c for c in candles}
    base = base_rate_from(candles)
    results = []

    for row in ledger.ungraded(con, before_utc=now.isoformat(timespec="seconds")):
        target = datetime.fromisoformat(row["target_bar_utc"])
        # the bar that closes at `target` opens one hour earlier
        bar_open = int((target - timedelta(hours=1)).timestamp())
        bar = by_time.get(bar_open)
        if bar is None:
            results.append({"id": row["id"], "status": "bar_not_available"})
            continue

        ref = row["ref_price"]
        realized = bar["close"]
        move = realized - ref
        move_pct = move / ref * 100 if ref else 0.0
        thr = row["noise_threshold"]
        is_noise = abs(move) <= thr

        if row["direction"] == "no_call" or is_noise:
            correct = None
        else:
            up = move > 0
            correct = 1 if (row["direction"] == "bullish") == up else 0

        ledger.record_outcome(
            con, prediction_id=row["id"], realized_price=realized, move=round(move, 4),
            move_pct=round(move_pct, 4), is_noise=is_noise, correct=correct,
            base_rate=round(base, 6))
        results.append({
            "id": row["id"], "status": "graded", "direction": row["direction"],
            "move": round(move, 2), "noise": is_noise, "correct": correct,
        })
    return results


def run(now: datetime | None = None, *, dry_run: bool = False) -> dict:
    now = now or datetime.now(UTC)
    slot = current_slot(now)
    slot_iso = slot.isoformat(timespec="seconds")
    out = {"slot_utc": slot_iso, "slot_wib": slot.astimezone(WIB).isoformat(timespec="seconds")}

    # ---- 1. calendar gate -------------------------------------------------
    state = cal.session_state(now)
    out["session_state"] = state
    if state != "open":
        if not dry_run:
            con = ledger.connect()
            ledger.record_gap(con, slot, state)
        out["action"] = "gap"
        out["reason"] = state
        return out

    # ---- 2. claim the slot ------------------------------------------------
    con = ledger.connect()
    if not dry_run and not ledger.claim_slot(con, JOB, slot_iso):
        out["action"] = "duplicate_slot_rejected"
        return out

    try:
        # ---- 3. fetch -----------------------------------------------------
        try:
            snap = fm.collect()
        except (fm.FetchError, fm.SchemaError) as e:
            if not dry_run:
                ledger.record_fetch_error(con, "collect", f"{type(e).__name__}: {e}")
                ledger.finish_slot(con, JOB, slot_iso, "failed", str(e)[:300])
            out["action"] = "fetch_failed"
            out["error"] = f"{type(e).__name__}: {e}"
            return out

        for src, st in snap["source_status"].items():
            if st != "ok" and not dry_run:
                ledger.record_fetch_error(con, src, st)

        candles = snap["candles"]

        # ---- 4. grade what is due ----------------------------------------
        graded = grade_due(con, candles, now) if not dry_run else []
        out["graded"] = graded

        # ---- 5. predict the next bar -------------------------------------
        p = predictor.predict(candles, dxy_bars=snap.get("dxy_proxy"))
        target = slot + timedelta(hours=1)
        ref = snap["spot"]["price"]
        inputs = {
            "spot": ref,
            "spot_updated": snap["spot"]["updated_at"],
            "paxg": (snap.get("proxies") or {}).get("paxg_usd"),
            "ticker_last": (snap.get("ticker") or {}).get("last"),
            "spread": (snap.get("ticker") or {}).get("spread"),
            "source_status": snap["source_status"],
            "calendar_events": len(snap.get("calendar") or []),
        }
        pid = None
        if not dry_run:
            notes = p.get("reason") or f"contrib={json.dumps(p['contributions'])}"
            pid = ledger.record_prediction(
                con, horizon="1h",
                target_bar_utc=target.isoformat(timespec="seconds"),
                scheduled_slot_utc=slot_iso,
                direction=p["direction"], confidence=p["confidence"],
                ref_price=ref, model_version=p["model_version"],
                params=p["params"], features=p["features"], inputs=inputs,
                signal_source="technical", noise_threshold=p["noise_threshold"],
                notes=notes)
            ledger.finish_slot(con, JOB, slot_iso, "ok",
                               f"graded={len([g for g in graded if g['status']=='graded'])} "
                               f"predicted={p['direction']}")

        out["action"] = "predicted"
        out["prediction"] = {
            "id": pid, "direction": p["direction"], "score": p["score"],
            "confidence": p["confidence"], "ref_price": ref,
            "target_bar_utc": target.isoformat(timespec="seconds"),
            "noise_threshold": p["noise_threshold"],
            "contributions": p["contributions"],
            "reason": p.get("reason"),
        }
        return out

    except Exception as e:
        if not dry_run:
            try:
                ledger.finish_slot(con, JOB, slot_iso, "failed", f"{type(e).__name__}: {e}"[:300])
            except Exception:
                pass
        out["action"] = "error"
        out["error"] = f"{type(e).__name__}: {e}"
        return out


def format_hourly_delivery(r: dict) -> str:
    """Render the hourly cycle result as the Discord message.

    Returns an EMPTY STRING for the silent cases (market closed, duplicate slot).
    The cron runner suppresses delivery on empty stdout, so returning "" is what
    keeps closed-market hours out of the channel entirely - the ledger still
    records the gap, the channel just stays quiet.

    Kept here rather than in the cron script so the delivery contract is
    unit-testable without running the scheduler.
    """
    a = r.get("action")

    if a in ("gap", "duplicate_slot_rejected"):
        return ""

    if a in ("fetch_failed", "error"):
        return (f"⚠️ **Nugget hourly cycle FAILED** ({r.get('slot_wib')})\n"
                f"`{a}`: {r.get('error')}")

    p = r.get("prediction", {}) or {}
    graded = [g for g in r.get("graded", []) if g.get("status") == "graded"]

    lines = [
        f"**Hourly cycle** — {str(r.get('slot_wib'))[:16]} WIB",
        "",
        f"**Call** — `{p.get('direction')}` "
        f"(score {p.get('score')}, conf {p.get('confidence')})",
        f"**Ref price** — ${p.get('ref_price')}  ·  "
        f"noise threshold ${p.get('noise_threshold')}",
    ]

    if graded:
        lines += ["", "**Graded**"]
        for g in graded:
            if g.get("noise"):
                mark = "NOISE (excluded)"
            elif g.get("correct") == 1:
                mark = "WIN"
            elif g.get("correct") == 0:
                mark = "LOSS"
            else:
                mark = "no-call"
            lines.append(f"• #{g['id']} {g['direction']} → "
                         f"move {g['move']:+.2f} · **{mark}**")
    else:
        lines += ["", "_No predictions due for grading this hour._"]

    return "\n".join(lines)


def report(con) -> dict:
    """Honest summary: hit-rate with n, CI, and the base rate it must beat."""
    rows = [dict(r) for r in con.execute(
        """SELECT o.correct, o.is_noise, o.base_rate, p.signal_source
           FROM outcomes o JOIN predictions p ON p.id = o.prediction_id""")]
    summary = stats.summarize(rows)
    summary["chain"] = ledger.verify_chain(con)
    summary["counts"] = {
        t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("predictions", "outcomes", "gaps", "fetch_errors", "cron_runs")
    }
    return summary


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()

    if a.report:
        con = ledger.connect()
        print(json.dumps(report(con), indent=2, default=str))
    else:
        r = run(dry_run=a.dry_run)
        print(json.dumps(r, indent=2, default=str))