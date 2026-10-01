#!/usr/bin/env python3
"""CLI utility to query Nugget statistics across any time window.

Usage:
    python3 stats_query.py --period weekly
    python3 stats_query.py --period monthly
    python3 stats_query.py --period all
    python3 stats_query.py --days 30
    python3 stats_query.py --signal-source technical
    python3 stats_query.py --signal-source sentiment
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger
import stats

UTC = timezone.utc
WIB = timezone(timedelta(hours=7))


def query_window_statistics(
    days: int | None = None,
    signal_source: str | None = None,
    horizon: str | None = None,
) -> dict:
    con = ledger.connect()
    where = ["o.prediction_id IS NOT NULL"]
    args = []

    if days is not None:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        where.append("p.created_utc >= ?")
        args.append(cutoff)

    if signal_source is not None:
        where.append("p.signal_source = ?")
        args.append(signal_source)

    if horizon is not None:
        where.append("p.horizon = ?")
        args.append(horizon)

    sql = f"""
        SELECT 
            p.id, p.created_wib, p.horizon, p.direction, p.signal_source,
            p.ref_price, p.noise_threshold,
            o.graded_wib, o.realized_price, o.move, o.move_pct,
            o.realized_dir, o.is_noise, o.correct, o.base_rate
        FROM predictions p
        JOIN outcomes o ON o.prediction_id = p.id
        WHERE {' AND '.join(where)}
        ORDER BY p.id ASC
    """
    rows = [dict(r) for r in con.execute(sql, args).fetchall()]
    
    summary = stats.summarize(rows)
    summary["filters"] = {
        "days": days or "all_time",
        "signal_source": signal_source or "all",
        "horizon": horizon or "all",
    }
    
    # Detailed breakdowns
    if rows:
        summary["total_predictions"] = len(rows)
        summary["noise_count"] = sum(1 for r in rows if r["is_noise"] == 1)
        summary["graded_non_noise"] = sum(1 for r in rows if r["is_noise"] == 0 and r["correct"] is not None)
        summary["bullish_calls"] = sum(1 for r in rows if r["direction"] == "bullish")
        summary["bearish_calls"] = sum(1 for r in rows if r["direction"] == "bearish")
        summary["no_calls"] = sum(1 for r in rows if r["direction"] == "no_call")
        summary["total_net_points_move"] = round(sum(r["move"] for r in rows), 2)
        summary["avg_move_pts"] = round(sum(abs(r["move"]) for r in rows) / len(rows), 2)
    
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Query Nugget performance stats")
    parser.add_argument("--period", choices=["daily", "weekly", "monthly", "yearly", "all"], default="all")
    parser.add_argument("--days", type=int, help="Custom rolling window in days")
    parser.add_argument("--signal-source", choices=["technical", "sentiment", "ensemble"])
    parser.add_argument("--horizon", choices=["1h", "24h"])
    args = parser.parse_args()

    days_map = {
        "daily": 1,
        "weekly": 7,
        "monthly": 30,
        "yearly": 365,
        "all": None,
    }
    days = args.days if args.days is not None else days_map[args.period]

    res = query_window_statistics(days=days, signal_source=args.signal_source, horizon=args.horizon)
    print(json.dumps(res, indent=2))
