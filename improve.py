#!/usr/bin/env python3
"""Job 4 - algorithm improvement, with the guardrails that stop self-destruction.

This is where systems like this usually die. Three specific traps, and what
stops each:

  1. HYPERPARAMETER SEARCH ON THE SAME DATA THAT GRADES IT
     -> the ledger is split: a TRAIN slice the search may use, and a HELD-OUT
        slice the search NEVER sees. Promotion is decided on the held-out slice.

  2. PROMOTING ON A POINT ESTIMATE
     -> promotion requires the held-out CI to EXCLUDE the baseline (the always-up
        rate), not merely a higher number. Best-of-N selection is corrected for:
        when N candidates were tried, the bar rises accordingly.

  3. SILENT DEGRADATION AFTER A LUCKY PROMOTION
     -> a live rollback trigger: if a promoted model underperforms the model it
        replaced by more than TOLERANCE over the next M graded bars, it reverts
        automatically. Nobody is watching this hourly.

What job 4 may NOT do: touch stats.py or the grading contract. It tunes
predictor.py weights only. The thing being guarded must not edit its own guard.
"""
from __future__ import annotations

import copy
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ledger                      # noqa: E402
import predictor                   # noqa: E402
import stats                       # noqa: E402

UTC = timezone.utc
WIB = timezone(timedelta(hours=7))
JOB = "improve"

HELD_OUT_FRACTION = 0.30     # never seen by the search
MIN_TRAIN_ROWS = 40
MIN_HELD_OUT_ROWS = 20
ROLLBACK_TOLERANCE_PP = 8.0  # underperform the prior incumbent by this much...
ROLLBACK_WINDOW = 30         # ...over this many graded bars -> auto-revert


# ---------------------------------------------------------------- data access

def load_graded(con) -> list[dict]:
    """Graded, non-noise predictions with the features they were made from.

    The stored features are what makes honest re-scoring possible: a candidate
    parameter set is evaluated on the SAME inputs the original call saw, so
    there is no look-ahead.
    """
    rows = con.execute(
        """SELECT p.id, p.features_json, p.signal_source, p.direction AS made_call,
                  o.realized_dir, o.correct, o.is_noise, o.base_rate, o.move
           FROM predictions p JOIN outcomes o ON o.prediction_id = p.id
           WHERE o.is_noise = 0
           ORDER BY p.id ASC""").fetchall()
    out = []
    for r in rows:
        try:
            feats = json.loads(r["features_json"])
        except Exception:
            continue
        if not isinstance(feats, dict) or not feats:
            continue
        out.append({"id": r["id"], "features": feats,
                    "realized_dir": r["realized_dir"],
                    "base_rate": r["base_rate"] or 0.5,
                    "signal_source": r["signal_source"]})
    return out


def split(rows: list[dict], held_out_fraction: float = HELD_OUT_FRACTION):
    """Chronological split. NEVER shuffled - a shuffled split leaks the future."""
    n = len(rows)
    cut = int(n * (1 - held_out_fraction))
    return rows[:cut], rows[cut:]


# --------------------------------------------------------------- evaluation

def evaluate(rows: list[dict], params: dict, abstain_bar: float) -> dict:
    """Re-score each stored prediction with `params` and score the calls."""
    wins = calls = 0
    base_sum = 0.0
    for r in rows:
        try:
            s, _ = predictor.score(r["features"], params)
        except Exception:
            continue
        if abs(s) < abstain_bar:
            continue
        direction = "bullish" if s > 0 else "bearish"
        calls += 1
        base_sum += r["base_rate"]
        if direction == r["realized_dir"]:
            wins += 1
    base = (base_sum / calls) if calls else 0.5
    verdict = stats.grading_verdict(wins, calls, base, label="candidate")
    verdict["coverage"] = round(calls / len(rows) * 100, 1) if rows else 0.0
    return verdict


def candidate_grid() -> list[dict]:
    """A SMALL, interpretable grid. A huge grid is how you overfit a backtest."""
    base = predictor.build_params()
    out = [{"name": "incumbent", "params": copy.deepcopy(base),
            "abstain_bar": predictor.ABSTAIN_BAR}]

    # trend-tilted
    p = copy.deepcopy(base); p["trend"]["weight"] = 0.40; p["rsi"]["weight"] = 0.10
    out.append({"name": "trend-heavy", "params": p, "abstain_bar": 0.18})

    # mean-reversion-tilted
    p = copy.deepcopy(base); p["rsi"]["weight"] = 0.35; p["momentum"]["weight"] = 0.05
    out.append({"name": "rsi-heavy", "params": p, "abstain_bar": 0.18})

    # position-led
    p = copy.deepcopy(base); p["position"]["weight"] = 0.35; p["volume_confirm"]["weight"] = 0.05
    out.append({"name": "position-heavy", "params": p, "abstain_bar": 0.18})

    # more selective (fewer, stronger calls)
    out.append({"name": "selective", "params": copy.deepcopy(base), "abstain_bar": 0.35})

    # more permissive
    out.append({"name": "permissive", "params": copy.deepcopy(base), "abstain_bar": 0.08})

    # session-sweep activated: give the liquidity-sweep microstructure signal
    # a real weight (carved from volume_confirm, its weakest-measured peer per
    # the dashboard contribution history) and see if the held-out gate agrees
    # it earns a place in the jury. Starts at 0.0 in the incumbent; this is
    # the FIRST candidate that ever tests it non-zero.
    p = copy.deepcopy(base)
    p["session_sweep"]["weight"] = 0.10
    p["volume_confirm"]["weight"] = 0.0
    out.append({"name": "sweep-activated", "params": p, "abstain_bar": 0.18})

    # dxy-proxy activated: same treatment for the EURUSDT-as-dollar-direction
    # cross-asset signal, carved from breadth.
    p = copy.deepcopy(base)
    p["dxy_proxy"]["weight"] = 0.10
    p["breadth"]["weight"] = 0.0
    out.append({"name": "dxy-activated", "params": p, "abstain_bar": 0.18})

    return out


# -------------------------------------------------------------------- search

def search(con, now: datetime | None = None, *, dry_run: bool = False) -> dict:
    now = now or datetime.now(UTC)
    slot = now.replace(minute=0, second=0, microsecond=0)
    slot_iso = slot.isoformat(timespec="seconds")

    if not dry_run and not ledger.claim_slot(con, JOB, slot_iso):
        return {"action": "duplicate_slot_rejected", "slot_utc": slot_iso}

    rows = load_graded(con)
    train, held = split(rows)

    if len(train) < MIN_TRAIN_ROWS or len(held) < MIN_HELD_OUT_ROWS:
        res = {"action": "insufficient_data",
               "train": len(train), "held_out": len(held),
               "need_train": MIN_TRAIN_ROWS, "need_held_out": MIN_HELD_OUT_ROWS,
               "note": "no promotion attempted; the held-out slice must be earned"}
        if not dry_run:
            ledger.finish_slot(con, JOB, slot_iso, "skipped", json.dumps(res)[:300])
        return res

    inc = ledger.get_incumbent(con)
    inc_params = (json.loads(inc["params_json"]) if inc
                  else predictor.build_params())
    inc_bar = (json.loads(inc["params_json"]).get("_abstain_bar",
               predictor.ABSTAIN_BAR) if inc else predictor.ABSTAIN_BAR)

    grid = candidate_grid()
    # ---- SEARCH happens on TRAIN ONLY
    scored = []
    for cand in grid:
        v = evaluate(train, cand["params"], cand["abstain_bar"])
        scored.append({"name": cand["name"], "params": cand["params"],
                       "abstain_bar": cand["abstain_bar"], "train": v})
    scored.sort(key=lambda c: (c["train"]["hit_rate"] or 0), reverse=True)
    best = scored[0]

    incumbent_held = evaluate(held, inc_params, inc_bar)
    # ---- PROMOTION is decided on HELD-OUT ONLY
    challenger_held = evaluate(held, best["params"], best["abstain_bar"])
    # multiple-comparisons: the bar rises with how many candidates we tried
    corrected = stats.best_of_n_significance(
        round((challenger_held["hit_rate"] or 0) / 100 * (challenger_held["n"] or 1)),
        challenger_held["n"] or 1, trials=len(grid), base_rate=challenger_held["baseline"] / 100)

    beats_incumbent = ((challenger_held["hit_rate"] or 0)
                       > (incumbent_held["hit_rate"] or 0))
    promote = (challenger_held["significant"] and beats_incumbent
               and corrected["significant"])

    evidence = {
        "train_n": len(train), "held_out_n": len(held),
        "candidates_tried": len(grid),
        "search_ranking": [{"name": c["name"], "train_hit": c["train"]["hit_rate"],
                            "train_n": c["train"]["n"]} for c in scored],
        "best_on_train": best["name"],
        "incumbent_held_out": incumbent_held,
        "challenger_held_out": challenger_held,
        "corrected_p": corrected["p_value_corrected"],
        "decision": "promote" if promote else "keep",
        "reason": ("held-out CI excludes baseline, beats incumbent, and survives "
                   "the best-of-N correction" if promote else
                   "does not clear the held-out bar"),
    }

    if promote and not dry_run:
        version = f"v0.2-{best['name']}"
        params = copy.deepcopy(best["params"])
        params["_abstain_bar"] = best["abstain_bar"]
        ledger.register_model(con, version, params, "challenger", evidence)
        ledger.promote_model(con, version, evidence, note=evidence["reason"])
        evidence["promoted_version"] = version

    if not dry_run:
        ledger.finish_slot(con, JOB, slot_iso, "ok", evidence["decision"])

    return {"action": "promoted" if promote else "kept_incumbent",
            "slot_utc": slot_iso, "evidence": evidence}


# ----------------------------------------------------------------- rollback

def check_rollback(con, now: datetime | None = None, *, dry_run: bool = False) -> dict:
    """Auto-revert a promoted model that is underperforming the one it replaced."""
    inc = ledger.get_incumbent(con)
    if not inc:
        return {"action": "no_incumbent"}
    ev = json.loads(inc["evidence_json"]) if inc["evidence_json"] else {}
    prior = ev.get("incumbent_held_out", {})
    if not prior:
        return {"action": "no_prior_baseline", "version": inc["version"]}

    promoted_at = inc["promoted_utc"]
    rows = con.execute(
        """SELECT o.correct, o.is_noise, o.base_rate
           FROM outcomes o JOIN predictions p ON p.id = o.prediction_id
           WHERE o.graded_utc >= ? AND o.is_noise = 0 AND o.correct IS NOT NULL
           ORDER BY o.prediction_id ASC LIMIT ?""",
        (promoted_at, ROLLBACK_WINDOW)).fetchall()

    if len(rows) < ROLLBACK_WINDOW:
        return {"action": "insufficient_post_promotion_data",
                "have": len(rows), "need": ROLLBACK_WINDOW, "version": inc["version"]}

    cur = stats.summarize([dict(r) for r in rows])
    prior_rate = prior.get("hit_rate")
    gap = (prior_rate or 0) - (cur["hit_rate"] or 0)
    should_revert = gap > ROLLBACK_TOLERANCE_PP

    if should_revert and not dry_run:
        con.execute("UPDATE model_versions SET status='retired', retired_utc=?"
                    " WHERE version=?",
                    (now.isoformat(timespec="seconds") if now else
                     datetime.now(UTC).isoformat(timespec="seconds"), inc["version"]))
        con.commit()

    return {"action": "reverted" if should_revert else "held",
            "version": inc["version"], "current_hit_rate": cur["hit_rate"],
            "prior_hit_rate": prior_rate, "gap_pp": round(gap, 2),
            "tolerance_pp": ROLLBACK_TOLERANCE_PP}


def run(now: datetime | None = None, *, dry_run: bool = False) -> dict:
    con = ledger.connect()
    rb = check_rollback(con, now, dry_run=dry_run)
    s = search(con, now, dry_run=dry_run)
    return {"rollback": rb, "search": s}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    print(json.dumps(run(dry_run=a.dry_run), indent=2, default=str))