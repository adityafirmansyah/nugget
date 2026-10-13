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


def get_active_technical(con) -> dict | None:
    """Return the oldest open (ungraded) directional technical prediction, if any."""
    r = con.execute("""
        SELECT p.* FROM predictions p
        LEFT JOIN outcomes o ON o.prediction_id = p.id
        WHERE o.prediction_id IS NULL
          AND p.signal_source = 'technical'
          AND p.horizon = '1h'
          AND p.direction IN ('bullish', 'bearish')
        ORDER BY p.id ASC LIMIT 1
    """).fetchone()
    return dict(r) if r else None


def evaluate_barrier_trade(active: dict, candles: list[dict], current_spot: float, now: datetime) -> dict:
    """Evaluate open directional prediction against TP / Cut Loss / 24h Time-stop."""
    ref = active["ref_price"]
    direction = active["direction"]
    created_utc = datetime.fromisoformat(active["created_utc"])
    entry_slot_ts = int(created_utc.replace(minute=0, second=0, microsecond=0).timestamp())

    try:
        features = json.loads(active["features_json"])
    except Exception:
        features = {}
    try:
        inputs = json.loads(active["inputs_json"])
    except Exception:
        inputs = {}
    try:
        params = json.loads(active["params_json"])
    except Exception:
        params = {}

    atr14 = features.get("atr14")
    if not atr14:
        thr = active.get("noise_threshold")
        atr14 = (thr / predictor.NOISE_ATR_MULT) if thr else 8.5

    tp_mult = params.get("tp_atr_mult", predictor.TP_ATR_MULT)
    sl_mult = params.get("sl_atr_mult", predictor.SL_ATR_MULT)

    tp_price = inputs.get("tp_price")
    sl_price = inputs.get("sl_price")
    if tp_price is None or sl_price is None:
        tp_price, sl_price = predictor.calculate_barriers(ref, atr14, direction, tp_mult, sl_mult)

    relevant_bars = [c for c in candles if (c["open_time"] // 1000) >= entry_slot_ts]
    completed_bars = [c for c in candles[:-1] if (c["open_time"] // 1000) >= entry_slot_ts]

    for i, b in enumerate(relevant_bars):
        b_high = b["high"]
        b_low = b["low"]

        if direction == "bullish":
            hit_tp = b_high >= tp_price
            hit_sl = b_low <= sl_price
        else:  # bearish
            hit_tp = b_low <= tp_price
            hit_sl = b_high >= sl_price

        if hit_tp and hit_sl:
            # Conservative rule: dual touch treated as SL hit
            return {
                "status": "concluded",
                "exit_reason": "sl_hit",
                "exit_price": sl_price,
                "bars_held": max(1, i),
                "tp_price": tp_price,
                "sl_price": sl_price,
                "ref_price": ref,
                "direction": direction,
            }
        elif hit_tp:
            return {
                "status": "concluded",
                "exit_reason": "tp_hit",
                "exit_price": tp_price,
                "bars_held": max(1, i),
                "tp_price": tp_price,
                "sl_price": sl_price,
                "ref_price": ref,
                "direction": direction,
            }
        elif hit_sl:
            return {
                "status": "concluded",
                "exit_reason": "sl_hit",
                "exit_price": sl_price,
                "bars_held": max(1, i),
                "tp_price": tp_price,
                "sl_price": sl_price,
                "ref_price": ref,
                "direction": direction,
            }

    # Time-stop check (24 completed bars or 24 hours)
    elapsed_seconds = (now - created_utc).total_seconds()
    bars_held = len(completed_bars)

    if elapsed_seconds >= (predictor.MAX_HOLDING_HOURS * 3600) or bars_held >= predictor.MAX_HOLDING_HOURS:
        exit_price = current_spot
        return {
            "status": "concluded",
            "exit_reason": "time_stop",
            "exit_price": exit_price,
            "bars_held": bars_held,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "ref_price": ref,
            "direction": direction,
        }

    return {
        "status": "holding",
        "bars_held": bars_held,
        "max_bars": predictor.MAX_HOLDING_HOURS,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "ref_price": ref,
        "current_price": current_spot,
        "direction": direction,
    }


def grade_due(con, candles: list[dict], now: datetime) -> list[dict]:
    """Grade every non-barrier prediction whose target bar has closed. Returns summaries."""
    by_time = {c["open_time"] // 1000: c for c in candles}
    base = base_rate_from(candles)
    results = []

    for row in ledger.ungraded(con, before_utc=now.isoformat(timespec="seconds")):
        if row["signal_source"] == "technical" and row["direction"] in ("bullish", "bearish"):
            continue

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
    out: dict[str, object] = {"slot_utc": slot_iso, "slot_wib": slot.astimezone(WIB).isoformat(timespec="seconds")}

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

        # ---- 5. evaluate active technical trade (TP / SL / Time-stop) ----
        active = get_active_technical(con)

        if active:
            ref_spot = snap["spot"]["price"]
            barrier_eval = evaluate_barrier_trade(active, candles, ref_spot, now)

            if barrier_eval["status"] == "holding":
                # Position is still in progress: do NOT open a new prediction!
                entry_ref = barrier_eval["ref_price"]
                curr = barrier_eval["current_price"]
                if barrier_eval["direction"] == "bullish":
                    pnl = curr - entry_ref
                else:
                    pnl = entry_ref - curr
                pnl_pct = (pnl / entry_ref * 100) if entry_ref else 0.0

                if not dry_run:
                    ledger.finish_slot(con, JOB, slot_iso, "ok",
                                       f"holding #{active['id']} ({barrier_eval['bars_held']}h/{barrier_eval['max_bars']}h)")

                out["action"] = "holding"
                out["holding"] = {
                    "id": active["id"],
                    "direction": active["direction"],
                    "entry_price": entry_ref,
                    "current_price": curr,
                    "tp_price": barrier_eval["tp_price"],
                    "sl_price": barrier_eval["sl_price"],
                    "bars_held": barrier_eval["bars_held"],
                    "max_bars": barrier_eval["max_bars"],
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 4),
                }
                return out

            elif barrier_eval["status"] == "concluded":
                exit_price = barrier_eval["exit_price"]
                exit_reason = barrier_eval["exit_reason"]
                ref = barrier_eval["ref_price"]
                move = exit_price - ref
                move_pct = (move / ref * 100) if ref else 0.0
                thr = active["noise_threshold"]

                if exit_reason == "tp_hit":
                    correct = 1
                    is_noise = False
                elif exit_reason == "sl_hit":
                    correct = 0
                    is_noise = False
                else:  # time_stop
                    is_noise = abs(move) <= thr
                    if is_noise:
                        correct = None
                    else:
                        up = move > 0
                        correct = 1 if (active["direction"] == "bullish") == up else 0

                base = base_rate_from(candles)
                if not dry_run:
                    ledger.record_outcome(
                        con, prediction_id=active["id"], realized_price=exit_price,
                        move=round(move, 4), move_pct=round(move_pct, 4),
                        is_noise=is_noise, correct=correct, base_rate=round(base, 6))

                graded_item = {
                    "id": active["id"],
                    "status": "graded",
                    "direction": active["direction"],
                    "move": round(move, 2),
                    "move_pct": round(move_pct, 4),
                    "noise": is_noise,
                    "correct": correct,
                    "exit_reason": exit_reason,
                    "exit_price": exit_price,
                    "bars_held": barrier_eval["bars_held"],
                }
                graded.append(graded_item)
                out["graded"] = graded
                # Previous trade is now concluded, fall through to predict step!

        # ---- 6. predict the next bar -------------------------------------
        ref = snap["spot"]["price"]
        p = predictor.predict(candles, dxy_bars=snap.get("dxy_proxy"), ref_price=ref)
        if p["direction"] in ("bullish", "bearish"):
            target = slot + timedelta(hours=predictor.MAX_HOLDING_HOURS)
        else:
            target = slot + timedelta(hours=1)

        inputs = {
            "spot": ref,
            "spot_updated": snap["spot"]["updated_at"],
            "paxg": (snap.get("proxies") or {}).get("paxg_usd"),
            "ticker_last": (snap.get("ticker") or {}).get("last"),
            "spread": (snap.get("ticker") or {}).get("spread"),
            "source_status": snap["source_status"],
            "calendar_events": len(snap.get("calendar") or []),
            "tp_price": p.get("tp_price"),
            "sl_price": p.get("sl_price"),
            "tp_atr_mult": p.get("tp_atr_mult"),
            "sl_atr_mult": p.get("sl_atr_mult"),
            "max_holding_hours": p.get("max_holding_hours"),
        }
        pid = None
        if not dry_run:
            notes = p.get("reason") or f"contrib={json.dumps(p['contributions'])}"
            if p.get("tp_price") and p.get("sl_price"):
                notes += f" | TP: ${p['tp_price']:.2f} · SL: ${p['sl_price']:.2f}"
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
            "confidence": p["confidence"],
            "bullish_confidence": p.get("bullish_confidence"),
            "bearish_confidence": p.get("bearish_confidence"),
            "ref_price": ref,
            "tp_price": p.get("tp_price"),
            "sl_price": p.get("sl_price"),
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

    if a == "holding":
        h = r.get("holding", {}) or {}
        dir_str = h.get("direction", "")
        dir_badge = "🟢 **Bullish**" if dir_str == "bullish" else "🔴 **Bearish**"
        pnl = h.get("pnl", 0.0)
        pnl_pct = h.get("pnl_pct", 0.0)
        pnl_sign = "+" if pnl > 0 else ""

        entry = h.get("entry_price", 0.0)
        curr = h.get("current_price", 0.0)
        tp = h.get("tp_price", 0.0)
        sl = h.get("sl_price", 0.0)

        lines = [
            f"**Hourly cycle** — {str(r.get('slot_wib'))[:16]} WIB",
            "",
            f"⏳ **Position In Progress** — Holding #{h.get('id')} ({h.get('bars_held')}h / {h.get('max_bars')}h)",
            f"• **Direction** — {dir_badge}",
            f"• **Entry** — ${entry:.2f}  ·  **Current** — ${curr:.2f} ({pnl_sign}{pnl:.2f} pts / {pnl_sign}{pnl_pct:.2f}%)",
            f"• **Take Profit** — ${tp:.2f}  ·  **Cut Loss** — ${sl:.2f}",
            "",
            "_Target barrier not yet reached. No new prediction this hour._",
        ]
        return "\n".join(lines)

    p = r.get("prediction", {}) or {}
    graded = [g for g in r.get("graded", []) if g.get("status") == "graded"]

    score = p.get("score", 0.0)
    p_bull = p.get("bullish_confidence")
    p_bear = p.get("bearish_confidence")
    if p_bull is None or p_bear is None:
        p_bull = round(0.5 + 0.5 * score, 4)
        p_bear = round(0.5 - 0.5 * score, 4)

    dir_str = p.get("direction", "no_call")
    if dir_str == "bullish":
        verdict_badge = "🟢 **Bullish**"
    elif dir_str == "bearish":
        verdict_badge = "🔴 **Bearish**"
    else:
        verdict_badge = "⚪ **No Call** (Abstained / Neutral)"

    lines = [f"**Hourly cycle** — {str(r.get('slot_wib'))[:16]} WIB", ""]

    if graded:
        lines.append("🎯 **Trade Concluded**")
        for g in graded:
            reason = g.get("exit_reason", "")
            if reason == "tp_hit":
                mark = "✅ **WIN (Take Profit Hit)**"
            elif reason == "sl_hit":
                mark = "❌ **LOSS (Cut Loss Hit)**"
            elif reason == "time_stop":
                sub = "WIN" if g.get("correct") == 1 else ("LOSS" if g.get("correct") == 0 else "NOISE")
                mark = f"⏰ **TIME-STOP (24h Close)** ({sub})"
            elif g.get("noise"):
                mark = "NOISE (excluded)"
            elif g.get("correct") == 1:
                mark = "WIN"
            elif g.get("correct") == 0:
                mark = "LOSS"
            else:
                mark = "no-call"

            exit_p = g.get("exit_price")
            exit_str = f"Exit ${exit_p:.2f} · " if exit_p is not None else ""
            lines.append(f"• #{g['id']} {g['direction'].upper()} → {exit_str}{mark} (Move {g['move']:+.2f} pts)")
        lines.append("")

    lines += [
        f"**New Prediction** — {verdict_badge}",
        f"• **Confidence** — 🐂 **{(p_bull or 0.0)*100:.1f}% Bullish** · 🐻 **{(p_bear or 0.0)*100:.1f}% Bearish** (Conviction: {p.get('confidence', 0):.2f})",
        f"• **Entry Price** — ${p.get('ref_price')}",
    ]
    if p.get("tp_price") and p.get("sl_price"):
        lines.append(f"• **Take Profit** — ${p.get('tp_price'):.2f} (1.5× ATR)  ·  **Cut Loss** — ${p.get('sl_price'):.2f} (1.0× ATR)")
    lines.append(f"• **Noise Threshold** — ${p.get('noise_threshold')}")

    if p.get("reason"):
        lines.append(f"• **Rationale** — {p.get('reason')}")

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