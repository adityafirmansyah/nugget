#!/usr/bin/env python3
"""Nugget's append-only prediction ledger (hardened).

Design contract (SOUL.md, non-negotiable):
  * Every prediction is written BEFORE the bar it is judged against exists.
  * Append-only. No UPDATE, no DELETE - enforced by SQLite triggers so it holds
    even against a bug in calling code.
  * A failed fetch is recorded as a failure. Nothing is carried forward.
  * Raw inputs are stored with every prediction so outcomes stay attributable.

Hardening added after the reliability review:
  * busy_timeout + retry-with-backoff (lock contention is a demonstrated failure
    mode in this codebase, not speculation)
  * cron_runs with UNIQUE(job, scheduled_slot_utc) so a duplicate or retried
    fire is REJECTED at insert, not merely idempotent by luck
  * gaps table - market-closed is neither an error nor a prediction
  * hash chain over predictions+outcomes, so "unmodified" is verifiable
    externally instead of trusting triggers that live in the same file
  * backup() for the nightly copy (single SSD = single point of failure)

Tables: predictions, outcomes, fetch_errors, gaps, cron_runs
"""
from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

WIB = timezone(timedelta(hours=7))
UTC = timezone.utc
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "nugget.db"

BUSY_TIMEOUT_MS = 15000
RETRY_ATTEMPTS = 6
RETRY_BASE_S = 0.25

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS predictions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    created_utc        TEXT    NOT NULL,
    created_wib        TEXT    NOT NULL,
    horizon            TEXT    NOT NULL,   -- '1h' | '24h'
    target_bar_utc     TEXT    NOT NULL,   -- the bar this call is judged against
    scheduled_slot_utc TEXT    NOT NULL,   -- the cron slot this belongs to
    direction          TEXT    NOT NULL,   -- 'bullish' | 'bearish' | 'no_call'
    confidence         REAL,
    ref_price          REAL    NOT NULL,
    model_version      TEXT    NOT NULL,
    params_json        TEXT    NOT NULL,
    features_json      TEXT    NOT NULL,
    inputs_json        TEXT    NOT NULL,
    signal_source      TEXT    NOT NULL,   -- 'technical' | 'sentiment' | 'ensemble'
    notes              TEXT,
    noise_threshold    REAL    NOT NULL,   -- NOT NULL: ungradable without it
    prev_hash          TEXT    NOT NULL,
    row_hash           TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS outcomes (
    prediction_id     INTEGER PRIMARY KEY,
    graded_utc        TEXT    NOT NULL,
    graded_wib        TEXT    NOT NULL,
    realized_price    REAL    NOT NULL,
    move              REAL    NOT NULL,
    move_pct          REAL    NOT NULL,
    realized_dir      TEXT    NOT NULL,
    is_noise          INTEGER NOT NULL,
    correct           INTEGER,            -- NULL when no_call or noise
    base_rate         REAL    NOT NULL,
    prev_hash         TEXT    NOT NULL,
    row_hash          TEXT    NOT NULL,
    FOREIGN KEY (prediction_id) REFERENCES predictions(id)
);

CREATE TABLE IF NOT EXISTS fetch_errors (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_utc  TEXT NOT NULL,
    occurred_wib  TEXT NOT NULL,
    source        TEXT NOT NULL,
    error         TEXT NOT NULL
);

-- Market closed: not an error, not a prediction. A recorded hole.
CREATE TABLE IF NOT EXISTS gaps (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_utc     TEXT NOT NULL UNIQUE,
    slot_wib     TEXT NOT NULL,
    reason       TEXT NOT NULL,           -- session_state() value
    recorded_utc TEXT NOT NULL
);

-- Idempotency: a duplicate/retried fire is rejected by the UNIQUE constraint.
CREATE TABLE IF NOT EXISTS cron_runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    job                TEXT NOT NULL,
    scheduled_slot_utc TEXT NOT NULL,
    started_utc        TEXT NOT NULL,
    completed_utc      TEXT,
    status             TEXT NOT NULL,     -- running|ok|failed|skipped
    detail             TEXT,
    UNIQUE(job, scheduled_slot_utc)
);

-- Model registry. INTENTIONALLY MUTABLE - this is configuration, not the record.
-- The append-only guarantee covers predictions/outcomes, which is what makes the
-- track record trustworthy; the registry only says which model is live.
CREATE TABLE IF NOT EXISTS model_versions (
    version       TEXT PRIMARY KEY,
    created_utc   TEXT NOT NULL,
    params_json   TEXT NOT NULL,
    status        TEXT NOT NULL,      -- incumbent | challenger | retired
    promoted_utc  TEXT,
    retired_utc   TEXT,
    evidence_json TEXT,
    note          TEXT
);

-- Append-only enforcement, at the database level.
CREATE TRIGGER IF NOT EXISTS predictions_no_update
BEFORE UPDATE ON predictions
BEGIN SELECT RAISE(ABORT, 'predictions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS predictions_no_delete
BEFORE DELETE ON predictions
BEGIN SELECT RAISE(ABORT, 'predictions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS outcomes_no_update
BEFORE UPDATE ON outcomes
BEGIN SELECT RAISE(ABORT, 'outcomes is append-only'); END;
CREATE TRIGGER IF NOT EXISTS outcomes_no_delete
BEFORE DELETE ON outcomes
BEGIN SELECT RAISE(ABORT, 'outcomes is append-only'); END;

CREATE INDEX IF NOT EXISTS idx_pred_target ON predictions(target_bar_utc);
CREATE INDEX IF NOT EXISTS idx_pred_source ON predictions(signal_source);
CREATE INDEX IF NOT EXISTS idx_pred_model  ON predictions(model_version);
CREATE INDEX IF NOT EXISTS idx_pred_slot   ON predictions(scheduled_slot_utc);
"""


# --------------------------------------------------------------- connection

def connect(path: Path | str = None) -> sqlite3.Connection:
    """Open the ledger.

    `path=None` resolves DB_PATH at CALL time, not at import time. That matters
    for tests and for anything that needs to point the ledger at a sandbox:
    binding the default as a parameter default would freeze it at import.
    """
    if path is None:
        path = DB_PATH
    con = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_MS / 1000)
    con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


def with_retry(fn, attempts: int = RETRY_ATTEMPTS, base: float = RETRY_BASE_S,
               con: sqlite3.Connection | None = None):
    """Retry only on lock contention, with exponential backoff + jitter.

    `con` is optional but important: a failed write can leave an implicit
    transaction open, which HOLDS a lock and makes the next attempt fail too.
    Rolling back before each retry is what makes the retry actually able to
    succeed rather than spinning against our own stale lock.
    """
    last = None
    for i in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if con is not None:
                try:
                    con.rollback()
                except Exception:
                    pass
            msg = str(e).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            last = e
            if i == attempts - 1:
                break
            time.sleep(base * (2 ** i) + random.uniform(0, base))
    raise last


# ------------------------------------------------------------------- chain

def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _row_hash(prev_hash: str, payload: dict) -> str:
    return hashlib.sha256((prev_hash + _canonical(payload)).encode()).hexdigest()


def _last_hash(con) -> str:
    r = con.execute(
        "SELECT row_hash FROM predictions ORDER BY id DESC LIMIT 1").fetchone()
    return r["row_hash"] if r else "GENESIS"


def verify_chain(con) -> dict:
    """Recompute the whole chain. Any edit to any row breaks it.

    NOTE: the autoincrement `id` is excluded from the hashed payload - it does
    not exist until after the insert, so it was never part of the original hash.
    Including it here would make every chain fail verification by construction.
    """
    prev = "GENESIS"
    for r in con.execute("SELECT * FROM predictions ORDER BY id ASC"):
        payload = {k: r[k] for k in r.keys()
                   if k not in ("row_hash", "prev_hash", "id")}
        if r["prev_hash"] != prev:
            return {"ok": False, "table": "predictions", "id": r["id"],
                    "reason": "prev_hash mismatch"}
        if _row_hash(prev, payload) != r["row_hash"]:
            return {"ok": False, "table": "predictions", "id": r["id"],
                    "reason": "row_hash mismatch"}
        prev = r["row_hash"]

    o_prev = "GENESIS"
    for r in con.execute("SELECT * FROM outcomes ORDER BY prediction_id ASC"):
        # prediction_id IS part of the payload (it is known at write time), so it
        # stays in the re-hash.
        payload = {k: r[k] for k in r.keys() if k not in ("row_hash", "prev_hash")}
        if r["prev_hash"] != o_prev:
            return {"ok": False, "table": "outcomes", "id": r["prediction_id"],
                    "reason": "prev_hash mismatch"}
        if _row_hash(o_prev, payload) != r["row_hash"]:
            return {"ok": False, "table": "outcomes", "id": r["prediction_id"],
                    "reason": "row_hash mismatch"}
        o_prev = r["row_hash"]
    return {"ok": True, "head": prev}


# ---------------------------------------------------------------- writes

def now_both() -> tuple[str, str]:
    n = datetime.now(UTC)
    return (n.isoformat(timespec="seconds"),
            n.astimezone(WIB).isoformat(timespec="seconds"))


def claim_slot(con, job: str, scheduled_slot_utc: str) -> bool:
    """Idempotency gate. True = this slot is OURS to run; False = already ran.

    UNIQUE(job, scheduled_slot_utc) is what makes a duplicate or retried fire
    impossible to double-execute, rather than merely unlikely.
    """
    utc, _ = now_both()

    def _do():
        con.execute(
            "INSERT INTO cron_runs (job, scheduled_slot_utc, started_utc, status)"
            " VALUES (?,?,?,'running')", (job, scheduled_slot_utc, utc))
        con.commit()
        return True

    try:
        return with_retry(_do, con=con)
    except sqlite3.IntegrityError:
        # A rejected duplicate leaves an OPEN implicit transaction, which holds a
        # write lock. Without this rollback the guard itself would break the very
        # next write from any connection ("database is locked"). Reproduced.
        try:
            con.rollback()
        except Exception:
            pass
        return False


def finish_slot(con, job: str, scheduled_slot_utc: str, status: str,
                detail: str | None = None) -> None:
    utc, _ = now_both()

    def _do():
        con.execute(
            "UPDATE cron_runs SET completed_utc=?, status=?, detail=?"
            " WHERE job=? AND scheduled_slot_utc=?",
            (utc, status, detail, job, scheduled_slot_utc))
        con.commit()
    with_retry(_do, con=con)


def record_prediction(con, *, horizon, target_bar_utc, scheduled_slot_utc,
                      direction, confidence, ref_price, model_version, params,
                      features, inputs, signal_source, noise_threshold,
                      notes=None) -> int:
    """Append one prediction, chained. Returns its id. Never updates anything."""
    utc, wib = now_both()

    def _do():
        prev = _last_hash(con)
        payload = {
            "created_utc": utc, "created_wib": wib, "horizon": horizon,
            "target_bar_utc": target_bar_utc,
            "scheduled_slot_utc": scheduled_slot_utc, "direction": direction,
            "confidence": confidence, "ref_price": ref_price,
            "model_version": model_version, "params_json": _canonical(params),
            "features_json": _canonical(features), "inputs_json": _canonical(inputs),
            "signal_source": signal_source, "notes": notes,
            "noise_threshold": noise_threshold,
        }
        h = _row_hash(prev, payload)
        cur = con.execute(
            """INSERT INTO predictions
               (created_utc, created_wib, horizon, target_bar_utc,
                scheduled_slot_utc, direction, confidence, ref_price,
                model_version, params_json, features_json, inputs_json,
                signal_source, notes, noise_threshold, prev_hash, row_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (utc, wib, horizon, target_bar_utc, scheduled_slot_utc, direction,
             confidence, ref_price, model_version, _canonical(params),
             _canonical(features), _canonical(inputs), signal_source, notes,
             noise_threshold, prev, h))
        con.commit()
        return int(cur.lastrowid)

    return with_retry(_do, con=con)


def record_outcome(con, *, prediction_id, realized_price, move, move_pct,
                   is_noise, correct, base_rate) -> None:
    utc, wib = now_both()
    realized_dir = "bullish" if move > 0 else ("bearish" if move < 0 else "flat")

    def _do():
        r = con.execute(
            "SELECT row_hash FROM outcomes ORDER BY prediction_id DESC LIMIT 1"
        ).fetchone()
        prev = r["row_hash"] if r else "GENESIS"
        payload = {
            "prediction_id": prediction_id, "graded_utc": utc, "graded_wib": wib,
            "realized_price": realized_price, "move": move, "move_pct": move_pct,
            "realized_dir": realized_dir, "is_noise": 1 if is_noise else 0,
            "correct": correct, "base_rate": base_rate,
        }
        h = _row_hash(prev, payload)
        con.execute(
            """INSERT INTO outcomes
               (prediction_id, graded_utc, graded_wib, realized_price, move,
                move_pct, realized_dir, is_noise, correct, base_rate,
                prev_hash, row_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (prediction_id, utc, wib, realized_price, move, move_pct,
             realized_dir, 1 if is_noise else 0, correct, base_rate, prev, h))
        con.commit()
    with_retry(_do, con=con)


def record_fetch_error(con, source: str, error: str) -> None:
    utc, wib = now_both()

    def _do():
        con.execute(
            "INSERT INTO fetch_errors (occurred_utc, occurred_wib, source, error)"
            " VALUES (?,?,?,?)", (utc, wib, source, error))
        con.commit()
    with_retry(_do, con=con)


def record_gap(con, slot_utc: datetime, reason: str) -> None:
    """Market closed: record the hole. Not an error, not a prediction."""
    utc, _ = now_both()
    wib = slot_utc.astimezone(WIB).isoformat(timespec="seconds")

    def _do():
        con.execute(
            "INSERT OR IGNORE INTO gaps (slot_utc, slot_wib, reason, recorded_utc)"
            " VALUES (?,?,?,?)",
            (slot_utc.isoformat(timespec="seconds"), wib, reason, utc))
        con.commit()
    with_retry(_do, con=con)


def ungraded(con, before_utc: str | None = None):
    """Oldest ungraded predictions whose target bar has passed.

    Deliberately does NOT assume the previous hour ran - a missed tick simply
    means nothing is graded this cycle, which is logged, not silently skipped.
    """
    q = """SELECT p.* FROM predictions p
           LEFT JOIN outcomes o ON o.prediction_id = p.id
           WHERE o.prediction_id IS NULL"""
    args = []
    if before_utc:
        q += " AND p.target_bar_utc <= ?"
        args.append(before_utc)
    q += " ORDER BY p.target_bar_utc ASC"
    return con.execute(q, args).fetchall()


def get_incumbent(con) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM model_versions WHERE status='incumbent'"
        " ORDER BY promoted_utc DESC LIMIT 1").fetchone()


def register_model(con, version: str, params: dict, status: str,
                   evidence: dict | None = None, note: str | None = None) -> None:
    """Add or update a model version. The registry is deliberately mutable."""
    utc, _ = now_both()

    def _do():
        con.execute(
            """INSERT INTO model_versions
               (version, created_utc, params_json, status, evidence_json, note)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(version) DO UPDATE SET
                 params_json=excluded.params_json,
                 status=excluded.status,
                 evidence_json=excluded.evidence_json,
                 note=excluded.note""",
            (version, utc, _canonical(params), status,
             _canonical(evidence) if evidence else None, note))
        con.commit()
    with_retry(_do, con=con)


def promote_model(con, version: str, evidence: dict, note: str | None = None) -> None:
    """Make `version` the incumbent, retiring whatever was live."""
    utc, _ = now_both()

    def _do():
        con.execute(
            "UPDATE model_versions SET status='retired', retired_utc=?"
            " WHERE status='incumbent' AND version<>?", (utc, version))
        con.execute(
            "UPDATE model_versions SET status='incumbent', promoted_utc=?,"
            " evidence_json=?, note=? WHERE version=?",
            (utc, _canonical(evidence), note, version))
        con.commit()
    with_retry(_do, con=con)


def backup(dest: Path | None = None) -> Path:
    """Consistent online backup - safe while other processes are writing."""
    dest = dest or (BASE_DIR / "backups" /
                    f"nugget-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.db")
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(DB_PATH))
    dst = sqlite3.connect(str(dest))
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    return dest


if __name__ == "__main__":
    con = connect()
    print(f"ledger ready at {DB_PATH}")
    print(f"busy_timeout={con.execute('PRAGMA busy_timeout').fetchone()[0]}ms")
    for t in ("predictions", "outcomes", "fetch_errors", "gaps", "cron_runs"):
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:<14} rows={n}")
    print(f"chain verify: {verify_chain(con)}")