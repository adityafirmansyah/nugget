#!/usr/bin/env python3
"""Local Modular Dashboard Server for Nugget XAUUSD Research Agent.

Port: 3000
Organized directory structure:
- dashboard/templates/index.html
- dashboard/static/css/style.css
- dashboard/static/js/app.js
"""
from __future__ import annotations

import json
import mimetypes
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "nugget.db"
DASHBOARD_DIR = BASE_DIR / "dashboard"
TEMPLATES_DIR = DASHBOARD_DIR / "templates"
STATIC_DIR = DASHBOARD_DIR / "static"
PORT = 3000

UTC = timezone.utc
WIB = timezone(timedelta(hours=7))


def get_db():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def get_stats_data(days: int | None = None, signal_source: str | None = None) -> dict:
    con = get_db()
    where = ["o.prediction_id IS NOT NULL"]
    args = []

    if days is not None:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        where.append("p.created_utc >= ?")
        args.append(cutoff)

    if signal_source and signal_source != "all":
        where.append("p.signal_source = ?")
        args.append(signal_source)

    sql = f"""
        SELECT 
            p.id, p.created_wib, p.created_utc, p.horizon, p.direction, p.confidence,
            p.signal_source, p.ref_price, p.noise_threshold,
            o.graded_wib, o.graded_utc, o.realized_price, o.move, o.move_pct,
            o.realized_dir, o.is_noise, o.correct, o.base_rate, o.row_hash
        FROM predictions p
        JOIN outcomes o ON o.prediction_id = p.id
        WHERE {' AND '.join(where)}
        ORDER BY p.id ASC
    """
    rows = [dict(r) for r in con.execute(sql, args).fetchall()]

    pred_sql = "SELECT COUNT(*) FROM predictions"
    total_preds = con.execute(pred_sql).fetchone()[0]
    
    pending_sql = """
        SELECT COUNT(*) FROM predictions p
        LEFT JOIN outcomes o ON o.prediction_id = p.id
        WHERE o.prediction_id IS NULL
    """
    pending_preds = con.execute(pending_sql).fetchone()[0]

    graded_non_noise = [r for r in rows if r["is_noise"] == 0 and r["correct"] is not None]
    wins = [r for r in graded_non_noise if r["correct"] == 1]
    losses = [r for r in graded_non_noise if r["correct"] == 0]
    noise_rows = [r for r in rows if r["is_noise"] == 1]
    no_calls = [r for r in rows if r["direction"] == "no_call"]

    n_graded = len(graded_non_noise)
    hit_rate = round((len(wins) / n_graded * 100), 2) if n_graded > 0 else None
    
    gross_win_pts = sum(abs(r["move"]) for r in wins)
    gross_loss_pts = sum(abs(r["move"]) for r in losses)
    profit_factor = round(gross_win_pts / gross_loss_pts, 2) if gross_loss_pts > 0 else (99.0 if gross_win_pts > 0 else 1.0)
    net_points = round(sum(r["move"] * (1 if r["direction"] == "bullish" else -1) for r in graded_non_noise), 2)

    avg_base_rate = round(sum(r["base_rate"] for r in rows) / len(rows) * 100, 2) if rows else 48.0
    edge_pp = round(hit_rate - avg_base_rate, 2) if hit_rate is not None else None

    cum_pts = 0.0
    equity_series = []
    for r in graded_non_noise:
        pts = r["move"] if r["direction"] == "bullish" else -r["move"]
        cum_pts += pts
        equity_series.append({
            "id": r["id"],
            "time": r["graded_wib"][:16],
            "points": round(cum_pts, 2),
            "move": round(pts, 2),
            "correct": r["correct"]
        })

    recent_sql = """
        SELECT 
            p.id, p.created_wib, p.horizon, p.direction, p.confidence, p.ref_price, 
            p.noise_threshold, p.signal_source, p.notes, p.features_json, p.inputs_json,
            o.graded_wib, o.realized_price, o.move, o.is_noise, o.correct, o.base_rate
        FROM predictions p
        LEFT JOIN outcomes o ON o.prediction_id = p.id
        ORDER BY p.id DESC
        LIMIT 50
    """
    recent_rows = [dict(r) for r in con.execute(recent_sql).fetchall()]

    con.close()

    return {
        "kpis": {
            "total_predictions": total_preds,
            "pending_predictions": pending_preds,
            "total_outcomes": len(rows),
            "graded_non_noise": n_graded,
            "noise_filtered": len(noise_rows),
            "no_calls": len(no_calls),
            "hit_rate": hit_rate,
            "base_rate": avg_base_rate,
            "edge_pp": edge_pp,
            "profit_factor": profit_factor,
            "net_points": net_points,
            "gross_win_pts": round(gross_win_pts, 2),
            "gross_loss_pts": round(gross_loss_pts, 2),
            "verdict": "SIGNIFICANT" if (edge_pp and edge_pp > 3.0 and n_graded > 30) else ("COLLECTING_DATA" if n_graded < 30 else "NOT_SIGNIFICANT")
        },
        "equity_curve": equity_series,
        "recent_predictions": recent_rows,
        "server_time_wib": datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S WIB"),
        "filter": {"days": days or "all", "signal_source": signal_source or "all"}
    }


def get_candles(interval: str = "1h", limit: int = 300) -> dict:
    """OHLCV bars for the self-hosted candlestick chart.

    Data comes from the SAME feed Nugget predicts on (PAXGUSDT), so every marker
    drawn on this chart lines up with the prices in her ledger. That is the whole
    point of the self-hosted chart: an unrelated feed would make the overlays lie.
    """
    sys.path.insert(0, str(BASE_DIR))
    import fetch_market as fm

    allowed = {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}
    if interval not in allowed:
        interval = "1h"
    limit = max(60, min(int(limit or 300), 1000))

    try:
        bars = fm.fetch_candles(interval, limit)
    except (fm.FetchError, fm.SchemaError) as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "bars": []}

    return {
        "ok": True,
        "interval": interval,
        "symbol": "XAUUSD (PAXGUSDT proxy)",
        "bars": [{
            # Lightweight Charts wants UNIX seconds
            "time": b["open_time"] // 1000,
            "open": b["open"], "high": b["high"],
            "low": b["low"], "close": b["close"],
            "volume": b["volume"],
        } for b in bars],
    }


def get_markers() -> dict:
    """Every graded prediction as a chart marker.

    This is the payoff of the self-hosted chart: Nugget's own calls, placed on the
    exact bar they were judged against, so her accuracy is visible instead of
    inferred from a table.
    """
    con = get_db()
    rows = con.execute("""
        SELECT p.id, p.created_wib, p.direction, p.ref_price, p.noise_threshold,
               p.signal_source, p.target_bar_utc,
               o.realized_price, o.move, o.is_noise, o.correct, o.graded_wib
        FROM predictions p
        LEFT JOIN outcomes o ON o.prediction_id = p.id
        ORDER BY p.id ASC
    """).fetchall()
    con.close()

    out = []
    for r in rows:
        graded = r["graded_wib"] is not None
        if r["is_noise"]:
            kind, label, pos = "noise", f"#{r['id']} NOISE", "inBar"
        elif r["correct"] == 1:
            kind, label, pos = "win", f"#{r['id']} WIN", "belowBar"
        elif r["correct"] == 0:
            kind, label, pos = "loss", f"#{r['id']} LOSS", "aboveBar"
        else:
            kind, label, pos = "pending", f"#{r['id']} PENDING", "inBar"

        out.append({
            "id": r["id"],
            "created_wib": r["created_wib"],
            "target_bar_utc": r["target_bar_utc"],
            "direction": r["direction"],
            "ref_price": r["ref_price"],
            "realized_price": r["realized_price"],
            "noise_threshold": r["noise_threshold"],
            "signal_source": r["signal_source"],
            "graded": graded,
            "kind": kind,
            "correct": r["correct"],
            "is_noise": r["is_noise"],
            "move": r["move"],
            "label": label,
            "position": pos,
        })
    return {"ok": True, "markers": out}


def get_live_price() -> dict:
    """Sub-minute spot tick for the live price strip."""
    sys.path.insert(0, str(BASE_DIR))
    import fetch_market as fm
    try:
        spot = fm.fetch_spot()
        proxies = fm.fetch_proxies()
        paxg = proxies["paxg_usd"]
        return {
            "ok": True,
            "spot": spot["price"],
            "updated_at": spot["updated_at"],
            "paxg": paxg,
            "basis": round(paxg - spot["price"], 2),
            "basis_pct": round((paxg - spot["price"]) / spot["price"] * 100, 3),
        }
    except (fm.FetchError, fm.SchemaError) as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # Serve index template
        if path == "/" or path == "/index.html":
            index_file = TEMPLATES_DIR / "index.html"
            if index_file.exists():
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(index_file.read_bytes())
                return
            else:
                self.send_response(404)
                self.end_headers()
                return

        # Serve static assets (CSS, JS)
        if path.startswith("/static/"):
            rel_path = path[len("/static/"):]
            file_path = STATIC_DIR / rel_path
            try:
                file_path.resolve().relative_to(STATIC_DIR.resolve())
            except ValueError:
                self.send_response(403)
                self.end_headers()
                return

            if file_path.exists() and file_path.is_file():
                mime, _ = mimetypes.guess_type(str(file_path))
                self.send_response(200)
                self.send_header("Content-Type", mime or "application/octet-stream")
                self.end_headers()
                self.wfile.write(file_path.read_bytes())
                return
            else:
                self.send_response(404)
                self.end_headers()
                return

        # API: /api/stats
        if path == "/api/stats":
            days = int(query["days"][0]) if "days" in query and query["days"][0].isdigit() else None
            signal = query.get("signal", [None])[0]
            data = get_stats_data(days=days, signal_source=signal)
            self._json(data)
            return

        # API: /api/candles
        if path == "/api/candles":
            interval = query.get("interval", ["1h"])[0]
            limit = query.get("limit", ["300"])[0]
            self._json(get_candles(interval, limit))
            return

        # API: /api/markers
        if path == "/api/markers":
            self._json(get_markers())
            return

        # API: /api/live
        if path == "/api/live":
            self._json(get_live_price())
            return

        self.send_response(404)
        self.end_headers()

    def _json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def log_message(self, format, *args):
        pass


def run_server():
    server = HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    print(f"🥇 Nugget Modular Dashboard running on http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down dashboard...")
        server.server_close()


if __name__ == "__main__":
    run_server()
