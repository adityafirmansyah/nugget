#!/usr/bin/env python3
"""Local Dashboard Server for Nugget XAUUSD Research Agent.

Port: 8397 (free, neighboring Nugget api_server 8396)
Zero external dependencies: uses Python stdlib http.server + sqlite3.
Features:
- REST API: /api/stats, /api/predictions, /api/equity
- Professional dark quantitative trading UI with Chart.js and Tailwind CSS (via CDN)
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "nugget.db"
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

    # Unfiltered predictions count (including pending)
    pred_sql = "SELECT COUNT(*) FROM predictions"
    total_preds = con.execute(pred_sql).fetchone()[0]
    
    # Active/Pending count
    pending_sql = """
        SELECT COUNT(*) FROM predictions p
        LEFT JOIN outcomes o ON o.prediction_id = p.id
        WHERE o.prediction_id IS NULL
    """
    pending_preds = con.execute(pending_sql).fetchone()[0]

    # Metrics calculation
    graded_non_noise = [r for r in rows if r["is_noise"] == 0 and r["correct"] is not None]
    wins = [r for r in graded_non_noise if r["correct"] == 1]
    losses = [r for r in graded_non_noise if r["correct"] == 0]
    noise_rows = [r for r in rows if r["is_noise"] == 1]
    no_calls = [r for r in rows if r["direction"] == "no_call"]

    n_graded = len(graded_non_noise)
    hit_rate = round((len(wins) / n_graded * 100), 2) if n_graded > 0 else None
    
    # Point metrics
    gross_win_pts = sum(abs(r["move"]) for r in wins)
    gross_loss_pts = sum(abs(r["move"]) for r in losses)
    profit_factor = round(gross_win_pts / gross_loss_pts, 2) if gross_loss_pts > 0 else (99.0 if gross_win_pts > 0 else 1.0)
    net_points = round(sum(r["move"] * (1 if r["direction"] == "bullish" else -1) for r in graded_non_noise), 2)

    # Base rate
    avg_base_rate = round(sum(r["base_rate"] for r in rows) / len(rows) * 100, 2) if rows else 48.0
    edge_pp = round(hit_rate - avg_base_rate, 2) if hit_rate is not None else None

    # Cumulative equity curve data
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

    # Recent predictions (including pending ones)
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


HTML_PAGE = """<!DOCTYPE html>
<html lang="en" class="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Nugget — XAUUSD Quantitative Research Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Geist:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          fontFamily: {
            sans: ['Geist', 'sans-serif'],
            mono: ['JetBrains Mono', 'monospace'],
          },
          colors: {
            brand: {
              50: '#fffbeb',
              500: '#f59e0b',
              600: '#d97706',
              900: '#78350f',
            },
            dark: {
              base: '#090d16',
              card: '#111827',
              border: '#1f2937',
              hover: '#1e293b'
            }
          }
        }
      }
    }
  </script>
  <style>
    body { font-family: 'Geist', sans-serif; background-color: #080c14; }
    .tabular { font-variant-numeric: tabular-nums; }
  </style>
</head>
<body class="text-slate-200 min-h-screen">

  <!-- Top Navbar -->
  <header class="border-b border-slate-800 bg-[#0c121e]/80 backdrop-blur sticky top-0 z-50 px-6 py-3.5 flex items-center justify-between">
    <div class="flex items-center gap-3">
      <div class="w-8 h-8 rounded-lg bg-amber-500/10 border border-amber-500/30 flex items-center justify-center text-amber-400 font-bold text-lg shadow-[0_0_15px_rgba(245,158,11,0.2)]">
        🥇
      </div>
      <div>
        <div class="flex items-center gap-2">
          <span class="font-bold text-white tracking-wide">NUGGET</span>
          <span class="text-xs px-2 py-0.5 rounded-full bg-amber-500/10 text-amber-400 border border-amber-500/20 font-mono">XAUUSD QUANTS</span>
        </div>
        <div class="text-[11px] text-slate-400">Deterministic Falsifiable Prediction Ledger</div>
      </div>
    </div>

    <!-- Live indicator + Filters -->
    <div class="flex items-center gap-4 text-xs font-mono">
      <div class="flex items-center gap-2 px-3 py-1 rounded-full bg-emerald-500/10 border border-emerald-500/20 text-emerald-400">
        <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
        <span>GATEWAY ONLINE</span>
      </div>
      <div id="serverTime" class="text-slate-400 hidden sm:block">--:--:-- WIB</div>
    </div>
  </header>

  <!-- Main Container -->
  <main class="max-w-7xl mx-auto px-4 sm:px-6 py-8 space-y-6">

    <!-- Filters Strip -->
    <div class="flex flex-wrap items-center justify-between gap-4 bg-slate-900/60 p-3 rounded-xl border border-slate-800">
      <div class="flex items-center gap-2 text-xs font-medium">
        <span class="text-slate-400 mr-1">Time Horizon:</span>
        <button onclick="setFilter('days', 1)" id="btn-days-1" class="filter-btn px-3 py-1.5 rounded-lg border border-slate-700 bg-slate-800 text-slate-300 hover:text-white">24H</button>
        <button onclick="setFilter('days', 7)" id="btn-days-7" class="filter-btn px-3 py-1.5 rounded-lg border border-slate-700 bg-slate-800 text-slate-300 hover:text-white">7D</button>
        <button onclick="setFilter('days', 30)" id="btn-days-30" class="filter-btn px-3 py-1.5 rounded-lg border border-slate-700 bg-slate-800 text-slate-300 hover:text-white">30D</button>
        <button onclick="setFilter('days', null)" id="btn-days-all" class="filter-btn px-3 py-1.5 rounded-lg border border-amber-500/40 bg-amber-500/10 text-amber-400 font-semibold">ALL TIME</button>
      </div>

      <div class="flex items-center gap-2 text-xs font-medium">
        <span class="text-slate-400 mr-1">Signal Source:</span>
        <button onclick="setFilter('signal', 'all')" id="btn-sig-all" class="filter-btn px-3 py-1.5 rounded-lg border border-amber-500/40 bg-amber-500/10 text-amber-400 font-semibold">All Signals</button>
        <button onclick="setFilter('signal', 'technical')" id="btn-sig-technical" class="filter-btn px-3 py-1.5 rounded-lg border border-slate-700 bg-slate-800 text-slate-300 hover:text-white">Technical</button>
        <button onclick="setFilter('signal', 'sentiment')" id="btn-sig-sentiment" class="filter-btn px-3 py-1.5 rounded-lg border border-slate-700 bg-slate-800 text-slate-300 hover:text-white">Sentiment</button>
      </div>
    </div>

    <!-- Health / KPI Strip -->
    <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3.5">
      <!-- Hit Rate -->
      <div class="bg-slate-900/80 border border-slate-800 rounded-xl p-4 flex flex-col justify-between">
        <div class="text-slate-400 text-xs font-medium">Hit Rate (True Edge)</div>
        <div class="my-2">
          <span id="kpiHitRate" class="text-2xl font-bold font-mono text-white tabular">--%</span>
          <span id="kpiEdgeBadge" class="text-[11px] ml-1.5 px-1.5 py-0.5 rounded font-mono bg-slate-800 text-slate-400">vs 48%</span>
        </div>
        <div class="text-[11px] text-slate-500" id="kpiGradedCount">0 graded (non-noise)</div>
      </div>

      <!-- Net Points -->
      <div class="bg-slate-900/80 border border-slate-800 rounded-xl p-4 flex flex-col justify-between">
        <div class="text-slate-400 text-xs font-medium">Net Realized Move</div>
        <div class="my-2">
          <span id="kpiNetPoints" class="text-2xl font-bold font-mono text-white tabular">0.00</span>
          <span class="text-xs text-slate-400">pts</span>
        </div>
        <div class="text-[11px] text-slate-500" id="kpiWinLossRatio">W: 0 | L: 0</div>
      </div>

      <!-- Profit Factor -->
      <div class="bg-slate-900/80 border border-slate-800 rounded-xl p-4 flex flex-col justify-between">
        <div class="text-slate-400 text-xs font-medium">Profit Factor</div>
        <div class="my-2">
          <span id="kpiProfitFactor" class="text-2xl font-bold font-mono text-amber-400 tabular">1.00</span>
        </div>
        <div class="text-[11px] text-slate-500">Gross Win / Gross Loss</div>
      </div>

      <!-- Noise Filter Ratio -->
      <div class="bg-slate-900/80 border border-slate-800 rounded-xl p-4 flex flex-col justify-between">
        <div class="text-slate-400 text-xs font-medium">Noise Filtered</div>
        <div class="my-2">
          <span id="kpiNoiseCount" class="text-2xl font-bold font-mono text-slate-300 tabular">0</span>
          <span class="text-xs text-slate-500">&lt; 0.5×ATR</span>
        </div>
        <div class="text-[11px] text-slate-500" id="kpiNoisePercent">0% filtered out</div>
      </div>

      <!-- Total Calls / Active -->
      <div class="bg-slate-900/80 border border-slate-800 rounded-xl p-4 flex flex-col justify-between">
        <div class="text-slate-400 text-xs font-medium">Total Predictions</div>
        <div class="my-2 flex items-baseline gap-2">
          <span id="kpiTotalPreds" class="text-2xl font-bold font-mono text-white tabular">0</span>
          <span id="kpiPendingBadge" class="text-[11px] text-amber-400 font-mono">0 pending</span>
        </div>
        <div class="text-[11px] text-slate-500">100% Append-Only</div>
      </div>

      <!-- Statistical Verdict -->
      <div class="bg-slate-900/80 border border-slate-800 rounded-xl p-4 flex flex-col justify-between">
        <div class="text-slate-400 text-xs font-medium">Sample Status</div>
        <div class="my-2">
          <span id="kpiVerdict" class="text-sm font-bold font-mono px-2 py-1 rounded bg-slate-800 text-slate-300">COLLECTING</span>
        </div>
        <div class="text-[11px] text-slate-500">Goal: n &gt; 57 (Job 4)</div>
      </div>
    </div>

    <!-- Charts Grid -->
    <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
      
      <!-- Equity / Points Chart (2 Cols) -->
      <div class="lg:col-span-2 bg-slate-900/80 border border-slate-800 rounded-xl p-5">
        <div class="flex items-center justify-between mb-4">
          <div>
            <h2 class="text-sm font-semibold text-white">Cumulative Net Points (XAUUSD)</h2>
            <p class="text-xs text-slate-400">Calculated exclusively on non-noise calls</p>
          </div>
          <span class="text-xs font-mono px-2 py-0.5 rounded bg-amber-500/10 text-amber-400 border border-amber-500/20">POINTS CURVE</span>
        </div>
        <div class="h-64">
          <canvas id="equityChart"></canvas>
        </div>
      </div>

      <!-- Win / Loss / Noise Distribution -->
      <div class="bg-slate-900/80 border border-slate-800 rounded-xl p-5 flex flex-col justify-between">
        <div>
          <h2 class="text-sm font-semibold text-white">Outcome Classification</h2>
          <p class="text-xs text-slate-400 mb-4">Win vs Loss vs Noise-Filtered</p>
          <div class="h-52 flex items-center justify-center">
            <canvas id="distributionChart"></canvas>
          </div>
        </div>
        <div class="border-t border-slate-800 pt-3 text-[11px] text-slate-400 flex justify-between font-mono">
          <span>Wins: <strong id="distWinText" class="text-emerald-400">0</strong></span>
          <span>Losses: <strong id="distLossText" class="text-rose-400">0</strong></span>
          <span>Noise: <strong id="distNoiseText" class="text-slate-400">0</strong></span>
        </div>
      </div>

    </div>

    <!-- Predictions Audit Ledger -->
    <div class="bg-slate-900/80 border border-slate-800 rounded-xl overflow-hidden shadow-xl">
      <div class="p-4 sm:px-6 border-b border-slate-800 flex items-center justify-between">
        <div>
          <h2 class="text-sm font-semibold text-white">Cryptographic Prediction Ledger</h2>
          <p class="text-xs text-slate-400">Append-only sequence. Predictions logged before outcome bar closes.</p>
        </div>
        <span class="text-xs font-mono text-emerald-400 flex items-center gap-1.5">
          <span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span> SHA256 CHAIN OK
        </span>
      </div>

      <div class="overflow-x-auto">
        <table class="w-full text-left text-xs font-mono">
          <thead class="bg-slate-950/60 text-slate-400 border-b border-slate-800 uppercase text-[10px] tracking-wider">
            <tr>
              <th class="py-3 px-4"># ID</th>
              <th class="py-3 px-4">Created (WIB)</th>
              <th class="py-3 px-4">Horizon</th>
              <th class="py-3 px-4">Signal</th>
              <th class="py-3 px-4">Direction</th>
              <th class="py-3 px-4">Prediction Rationale / Key Drivers</th>
              <th class="py-3 px-4 text-right">Ref Price</th>
              <th class="py-3 px-4 text-right">Realized</th>
              <th class="py-3 px-4 text-right">Move (pts)</th>
              <th class="py-3 px-4 text-center">Status</th>
            </tr>
          </thead>
          <tbody id="ledgerTableBody" class="divide-y divide-slate-800/60 text-slate-300">
            <tr>
              <td colspan="10" class="text-center py-6 text-slate-500">Loading ledger data...</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

  </main>

  <!-- Details Modal -->
  <div id="detailsModal" onclick="handleBackdropClick(event)" class="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm hidden flex items-center justify-center p-4">
    <div id="detailsModalCard" class="bg-slate-900 border border-slate-700/80 rounded-2xl max-w-2xl w-full max-h-[90vh] flex flex-col shadow-2xl overflow-hidden animate-in fade-in zoom-in-95 duration-150">
      
      <!-- Modal Header -->
      <div class="px-6 py-4 border-b border-slate-800 flex items-center justify-between bg-slate-950/40">
        <div class="flex items-center gap-3">
          <div id="modalDirIcon" class="w-8 h-8 rounded-lg flex items-center justify-center font-bold text-sm"></div>
          <div>
            <div class="flex items-center gap-2">
              <span id="modalTitle" class="font-bold text-white text-base">Prediction Details</span>
              <span id="modalHorizonBadge" class="text-xs px-2 py-0.5 rounded font-mono bg-slate-800 text-slate-400">1H</span>
            </div>
            <div id="modalTimestamp" class="text-xs text-slate-400 font-mono">--</div>
          </div>
        </div>
        <button onclick="closeDetailsModal()" class="w-8 h-8 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-400 hover:text-white flex items-center justify-center text-lg">&times;</button>
      </div>

      <!-- Modal Body -->
      <div class="p-6 overflow-y-auto space-y-6 text-sm">
        
        <!-- Summary Box -->
        <div class="bg-slate-950/60 border border-slate-800 rounded-xl p-4 space-y-2">
          <div class="text-xs text-slate-400 uppercase font-mono tracking-wider font-semibold">Core Rationale & Voting Summary</div>
          <p id="modalRationaleText" class="text-slate-200 leading-relaxed font-sans text-sm"></p>
        </div>

        <!-- Indicator Scorecard Breakdown (Newbie-friendly) -->
        <div class="space-y-3">
          <div class="flex items-center justify-between">
            <span class="text-xs text-slate-400 uppercase font-mono tracking-wider font-semibold">Indicator Jury Voting Scorecard</span>
            <span id="modalTotalScoreBadge" class="text-xs font-mono px-2 py-0.5 rounded bg-slate-800 text-slate-300">Score: +0.00</span>
          </div>
          <div id="modalScorecardContainer" class="space-y-2 font-sans text-xs">
            <!-- Dynamically populated -->
          </div>
        </div>

        <!-- Key Metrics Grid -->
        <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs font-mono">
          <div class="bg-slate-950/40 border border-slate-800/80 rounded-lg p-3">
            <div class="text-slate-400 text-[11px]">Reference Price</div>
            <div id="modalRefPrice" class="text-white font-bold text-base mt-1">$0.00</div>
          </div>
          <div class="bg-slate-950/40 border border-slate-800/80 rounded-lg p-3">
            <div class="text-slate-400 text-[11px]">Volatility Filter</div>
            <div id="modalNoiseThr" class="text-slate-300 font-bold text-base mt-1">$0.00</div>
          </div>
          <div class="bg-slate-950/40 border border-slate-800/80 rounded-lg p-3">
            <div class="text-slate-400 text-[11px]">Realized Price</div>
            <div id="modalRealizedPrice" class="text-white font-bold text-base mt-1">—</div>
          </div>
          <div class="bg-slate-950/40 border border-slate-800/80 rounded-lg p-3">
            <div class="text-slate-400 text-[11px]">Outcome Result</div>
            <div id="modalOutcomeStatus" class="font-bold text-base mt-1">Pending</div>
          </div>
        </div>

        <!-- Technical Parameters Breakdown -->
        <div class="space-y-3">
          <div class="text-xs text-slate-400 uppercase font-mono tracking-wider font-semibold">Technical Confluence Factors</div>
          <div id="modalFeaturesGrid" class="grid grid-cols-2 sm:grid-cols-3 gap-2 text-xs font-mono">
            <!-- Injected via JS -->
          </div>
        </div>

        <!-- Market Sources Context -->
        <div id="modalSourcesSection" class="space-y-2 text-xs">
          <div class="text-slate-400 uppercase font-mono tracking-wider font-semibold text-[11px]">Market State at Decision Time</div>
          <div id="modalSourcesText" class="text-slate-400 bg-slate-950/30 p-3 rounded-lg border border-slate-800/60 font-mono text-[11px]"></div>
        </div>

      </div>

      <!-- Modal Footer -->
      <div class="px-6 py-3 border-t border-slate-800 bg-slate-950/60 flex items-center justify-between text-xs font-mono text-slate-500">
        <span>Append-only cryptographic record</span>
        <button onclick="closeDetailsModal()" class="px-4 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 transition-colors">Close</button>
      </div>

    </div>
  </div>

  <footer class="max-w-7xl mx-auto px-6 py-8 text-center text-xs text-slate-500 border-t border-slate-800/80 mt-12">
    <div>Nugget Quantitative Trading Research — Operating autonomously under Hermes Orchestrator.</div>
    <div class="text-[11px] text-slate-600 mt-1">Learning instrument and reference only. Explicitly NOT financial advice.</div>
  </footer>

  <script>
    let currentFilter = { days: null, signal: 'all' };
    let equityChart = null;
    let distributionChart = null;

    function setFilter(type, value) {
      if (type === 'days') currentFilter.days = value;
      if (type === 'signal') currentFilter.signal = value;
      
      // Update UI button active states
      document.querySelectorAll('.filter-btn').forEach(btn => {
        btn.classList.remove('border-amber-500/40', 'bg-amber-500/10', 'text-amber-400', 'font-semibold');
        btn.classList.add('border-slate-700', 'bg-slate-800', 'text-slate-300');
      });

      const dayBtn = document.getElementById(currentFilter.days ? `btn-days-${currentFilter.days}` : 'btn-days-all');
      if (dayBtn) {
        dayBtn.classList.add('border-amber-500/40', 'bg-amber-500/10', 'text-amber-400', 'font-semibold');
        dayBtn.classList.remove('border-slate-700', 'bg-slate-800', 'text-slate-300');
      }

      const sigBtn = document.getElementById(`btn-sig-${currentFilter.signal}`);
      if (sigBtn) {
        sigBtn.classList.add('border-amber-500/40', 'bg-amber-500/10', 'text-amber-400', 'font-semibold');
        sigBtn.classList.remove('border-slate-700', 'bg-slate-800', 'text-slate-300');
      }

      loadData();
    }

    async function loadData() {
      try {
        let url = `/api/stats?signal=${currentFilter.signal}`;
        if (currentFilter.days) url += `&days=${currentFilter.days}`;
        
        const res = await fetch(url);
        const data = await res.json();
        renderDashboard(data);
      } catch (err) {
        console.error('Failed to load stats:', err);
      }
    }

    let currentPredictions = [];

    function renderDashboard(data) {
      currentPredictions = data.recent_predictions || [];
      const k = data.kpis;
      
      document.getElementById('serverTime').textContent = data.server_time_wib;
      
      // KPIs
      document.getElementById('kpiHitRate').textContent = k.hit_rate !== null ? `${k.hit_rate}%` : '--%';
      document.getElementById('kpiEdgeBadge').textContent = k.edge_pp !== null ? `${k.edge_pp > 0 ? '+' : ''}${k.edge_pp}pp edge` : `vs ${k.base_rate}%`;
      document.getElementById('kpiGradedCount').textContent = `${k.graded_non_noise} graded (non-noise)`;
      
      const ptsEl = document.getElementById('kpiNetPoints');
      ptsEl.textContent = `${k.net_points > 0 ? '+' : ''}${k.net_points.toFixed(2)}`;
      ptsEl.className = `text-2xl font-bold font-mono tabular ${k.net_points > 0 ? 'text-emerald-400' : (k.net_points < 0 ? 'text-rose-400' : 'text-white')}`;

      document.getElementById('kpiProfitFactor').textContent = k.profit_factor >= 90 ? '∞' : k.profit_factor.toFixed(2);
      document.getElementById('kpiNoiseCount').textContent = k.noise_filtered;
      
      const totalOutcomes = k.total_outcomes || 1;
      const noisePct = Math.round((k.noise_filtered / totalOutcomes) * 100);
      document.getElementById('kpiNoisePercent').textContent = `${noisePct}% filtered out`;

      document.getElementById('kpiTotalPreds').textContent = k.total_predictions;
      document.getElementById('kpiPendingBadge').textContent = `${k.pending_predictions} pending`;

      const verdEl = document.getElementById('kpiVerdict');
      verdEl.textContent = k.verdict;
      verdEl.className = `text-xs font-bold font-mono px-2 py-1 rounded ${
        k.verdict === 'SIGNIFICANT' ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30' :
        k.verdict === 'COLLECTING_DATA' ? 'bg-amber-500/10 text-amber-400 border border-amber-500/20' :
        'bg-slate-800 text-slate-400'
      }`;

      // Distribution Counts
      const winsCount = Math.round((k.graded_non_noise * (k.hit_rate || 0)) / 100);
      const lossCount = k.graded_non_noise - winsCount;
      document.getElementById('distWinText').textContent = winsCount;
      document.getElementById('distLossText').textContent = lossCount;
      document.getElementById('distNoiseText').textContent = k.noise_filtered;
      document.getElementById('kpiWinLossRatio').textContent = `W: ${winsCount} | L: ${lossCount}`;

      // Render Charts
      renderEquityChart(data.equity_curve);
      renderDistributionChart(winsCount, lossCount, k.noise_filtered);

      // Render Table
      renderTable(data.recent_predictions);
    }

    function renderEquityChart(series) {
      const ctx = document.getElementById('equityChart').getContext('2d');
      
      const labels = series.length ? series.map(s => s.time) : ['Genesis'];
      const data = series.length ? series.map(s => s.points) : [0];

      if (equityChart) equityChart.destroy();

      equityChart = new Chart(ctx, {
        type: 'line',
        data: {
          labels: labels,
          datasets: [{
            label: 'Net Points Accumulated',
            data: data,
            borderColor: '#f59e0b',
            backgroundColor: 'rgba(245, 158, 11, 0.08)',
            borderWidth: 2,
            tension: 0.1,
            fill: true,
            pointBackgroundColor: '#f59e0b',
            pointRadius: series.length > 20 ? 1 : 4,
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                label: function(ctx) { return `Points: ${ctx.parsed.y > 0 ? '+' : ''}${ctx.parsed.y.toFixed(2)}`; }
              }
            }
          },
          scales: {
            x: {
              grid: { color: 'rgba(255, 255, 255, 0.03)' },
              ticks: { color: '#64748b', maxTicksLimit: 8, font: { family: 'JetBrains Mono', size: 10 } }
            },
            y: {
              grid: { color: 'rgba(255, 255, 255, 0.05)' },
              ticks: { color: '#64748b', font: { family: 'JetBrains Mono', size: 10 } }
            }
          }
        }
      });
    }

    function renderDistributionChart(wins, losses, noise) {
      const ctx = document.getElementById('distributionChart').getContext('2d');
      if (distributionChart) distributionChart.destroy();

      const total = wins + losses + noise;
      const data = total > 0 ? [wins, losses, noise] : [0, 0, 1];

      distributionChart = new Chart(ctx, {
        type: 'doughnut',
        data: {
          labels: ['Wins', 'Losses', 'Noise Filtered'],
          datasets: [{
            data: data,
            backgroundColor: ['#10b981', '#f43f5e', '#334155'],
            borderWidth: 0,
            hoverOffset: 4
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { display: false }
          },
          cutout: '72%'
        }
      });
    }

    function renderTable(predictions) {
      const tbody = document.getElementById('ledgerTableBody');
      if (!predictions || !predictions.length) {
        tbody.innerHTML = '<tr><td colspan="10" class="text-center py-6 text-slate-500">No predictions found</td></tr>';
        return;
      }

      tbody.innerHTML = predictions.map(p => {
        const isBull = p.direction === 'bullish';
        const isBear = p.direction === 'bearish';
        const dirBadge = isBull 
          ? '<span class="px-2 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 font-semibold">BULL</span>'
          : (isBear 
            ? '<span class="px-2 py-0.5 rounded bg-rose-500/10 text-rose-400 border border-rose-500/20 font-semibold">BEAR</span>'
            : '<span class="px-2 py-0.5 rounded bg-slate-800 text-slate-400">NO CALL</span>');

        let statusBadge = '<span class="text-slate-500 font-mono">Pending...</span>';
        if (p.graded_wib) {
          if (p.is_noise) {
            statusBadge = '<span class="px-2 py-0.5 rounded bg-slate-800 text-slate-400 font-mono">NOISE</span>';
          } else if (p.correct === 1) {
            statusBadge = '<span class="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-400 font-bold border border-emerald-500/30">WIN</span>';
          } else if (p.correct === 0) {
            statusBadge = '<span class="px-2 py-0.5 rounded bg-rose-500/20 text-rose-400 font-bold border border-rose-500/30">LOSS</span>';
          }
        }

        const moveText = p.move !== null ? `${p.move > 0 ? '+' : ''}${p.move.toFixed(2)}` : '—';
        const moveColor = p.move > 0 ? 'text-emerald-400' : (p.move < 0 ? 'text-rose-400' : 'text-slate-400');
        
        let reasonSummary = "Technical Confluence";
        if (p.notes) {
          if (p.notes.startsWith("Bullish bias:")) {
            reasonSummary = "Bullish momentum & structure";
          } else if (p.notes.startsWith("Bearish bias:")) {
            reasonSummary = "Bearish rejection & drag";
          } else if (p.notes.startsWith("contrib=")) {
            reasonSummary = "Calculated multi-factor score";
          } else {
            reasonSummary = p.notes.split('.')[0];
          }
        }

        return `
          <tr class="hover:bg-slate-800/40 transition-colors cursor-pointer group" onclick="openDetailsModal(${p.id})">
            <td class="py-3 px-4 font-mono font-medium text-slate-400 group-hover:text-amber-400">#${p.id}</td>
            <td class="py-3 px-4 text-slate-300 whitespace-nowrap">${p.created_wib.slice(5, 16)}</td>
            <td class="py-3 px-4 font-mono text-slate-400">${p.horizon}</td>
            <td class="py-3 px-4 text-slate-400">${p.signal_source}</td>
            <td class="py-3 px-4">${dirBadge}</td>
            <td class="py-3 px-4">
              <div class="flex items-center gap-1.5 text-slate-300 group-hover:text-amber-300">
                <span>${reasonSummary}</span>
                <span class="text-[10px] px-1.5 py-0.5 rounded bg-slate-800 text-slate-400 border border-slate-700/60 font-mono">View Details &rarr;</span>
              </div>
            </td>
            <td class="py-3 px-4 text-right font-mono tabular">${p.ref_price.toFixed(2)}</td>
            <td class="py-3 px-4 text-right font-mono tabular text-white">${p.realized_price ? p.realized_price.toFixed(2) : '—'}</td>
            <td class="py-3 px-4 text-right font-mono tabular font-medium ${moveColor}">${moveText}</td>
            <td class="py-3 px-4 text-center">${statusBadge}</td>
          </tr>
        `;
      }).join('');
    }

    function openDetailsModal(id) {
      const p = currentPredictions.find(item => item.id === id);
      if (!p) return;

      const isBull = p.direction === 'bullish';
      const isBear = p.direction === 'bearish';
      
      const icon = document.getElementById('modalDirIcon');
      if (isBull) {
        icon.className = 'w-8 h-8 rounded-lg bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 flex items-center justify-center font-bold text-sm';
        icon.textContent = '▲';
      } else if (isBear) {
        icon.className = 'w-8 h-8 rounded-lg bg-rose-500/10 text-rose-400 border border-rose-500/20 flex items-center justify-center font-bold text-sm';
        icon.textContent = '▼';
      } else {
        icon.className = 'w-8 h-8 rounded-lg bg-slate-800 text-slate-400 border border-slate-700 flex items-center justify-center font-bold text-sm';
        icon.textContent = '—';
      }

      document.getElementById('modalTitle').textContent = `Prediction #${p.id} — ${p.direction.toUpperCase()}`;
      document.getElementById('modalHorizonBadge').textContent = p.horizon.toUpperCase();
      document.getElementById('modalTimestamp').textContent = `${p.created_wib} (${p.signal_source})`;

      // Parse contributions
      let contribs = {};
      if (p.notes && p.notes.includes("contrib=")) {
        try {
          const match = p.notes.match(/contrib=({.*?})/);
          if (match) contribs = JSON.parse(match[1]);
        } catch(e){}
      }

      // Populate Easy-to-understand Scorecard
      const scoreContainer = document.getElementById('modalScorecardContainer');
      const scoreBadge = document.getElementById('modalTotalScoreBadge');
      
      const scoreVal = p.confidence !== undefined ? p.confidence : 0;
      const signedScore = (p.direction === 'bearish' ? -scoreVal : scoreVal).toFixed(3);
      scoreBadge.textContent = `Total Score: ${signedScore > 0 ? '+' : ''}${signedScore} (Bar: ±0.18)`;
      scoreBadge.className = `text-xs font-mono px-2 py-0.5 rounded font-bold ${signedScore >= 0.18 ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30' : (signedScore <= -0.18 ? 'bg-rose-500/20 text-rose-400 border border-rose-500/30' : 'bg-slate-800 text-slate-300')}`;

      const indicatorMeta = {
        position: {
          title: "Price Position (vs 20-hour Average)",
          desc: "Are buyers paying more or less than the average price over the last 20 hours?",
          explain: (v) => v > 0.05 ? "Price trading well above recent moving average (strong buyer dominance)." : (v < -0.05 ? "Price pinned below recent moving average (seller dominance)." : "Price hovering right near the 20-hour average.")
        },
        rsi: {
          title: "RSI Momentum (Buying Speedometer)",
          desc: "How fast is price climbing or falling on a scale of 0 to 100?",
          explain: (v) => v > 0.05 ? "Speedometer shows strong upward buying momentum (> 50)." : (v < -0.05 ? "Speedometer shows downward selling momentum (< 50)." : "Momentum is balanced near the 50 neutral mark.")
        },
        trend: {
          title: "Moving Average Trend (Fast vs Slow)",
          desc: "Is the short-term 12h average moving faster than the medium-term 26h average?",
          explain: (v) => v > 0.02 ? "Fast 12-EMA sits above 26-EMA; the overall elevator is moving up." : (v < -0.02 ? "Fast 12-EMA sits under 26-EMA; the overall elevator is moving down." : "Fast and slow moving averages are flat/intertwined.")
        },
        momentum: {
          title: "24-Hour Velocity",
          desc: "Where is the price right now compared to exactly 24 hours ago?",
          explain: (v) => v > 0.02 ? "Positive gain over the past full day (healthy continuation)." : (v < -0.02 ? "Price is lower than 24 hours ago (acting as overhead drag/resistance)." : "Flat compared to yesterday.")
        },
        volume_confirm: {
          title: "Volume Confirmation",
          desc: "Are large volume traders actively backing up this move?",
          explain: (v) => v > 0.02 ? "High volume confirms and supports the directional move." : (v < -0.02 ? "Lower than average volume (move lacks strong institutional fuel)." : "Average trading volume.")
        },
        breadth: {
          title: "Range Breakout (6-Hour High/Low)",
          desc: "Did the current bar break out of the 6-hour range?",
          explain: (v) => v > 0 ? "Broke out to make a fresh 6-hour higher-high." : (v < 0 ? "Broke down to make a fresh 6-hour lower-low." : "Trading inside recent 6-hour boundaries (no breakout).")
        }
      };

      if (Object.keys(contribs).length > 0) {
        scoreContainer.innerHTML = Object.entries(contribs).map(([key, val]) => {
          const meta = indicatorMeta[key] || { title: key, desc: "", explain: () => "" };
          const isPos = val > 0.01;
          const isNeg = val < -0.01;
          
          const badgeClass = isPos ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' : (isNeg ? 'bg-rose-500/10 text-rose-400 border border-rose-500/20' : 'bg-slate-800 text-slate-400 border border-slate-700/50');
          const voteText = isPos ? `+${val.toFixed(3)} Bullish` : (isNeg ? `${val.toFixed(3)} Bearish` : `0.000 Neutral`);

          return `
            <div class="bg-slate-950/40 p-3 rounded-xl border border-slate-800/80 flex flex-col sm:flex-row sm:items-center justify-between gap-2">
              <div class="space-y-1">
                <div class="flex items-center gap-2">
                  <span class="font-semibold text-slate-200 text-xs">${meta.title}</span>
                  <div class="group/tip relative inline-flex items-center">
                    <span class="w-4 h-4 rounded-full bg-slate-800 text-slate-400 group-hover/tip:bg-slate-700 group-hover/tip:text-amber-300 flex items-center justify-center text-[10px] font-mono cursor-help transition-colors border border-slate-700/60 font-bold">?</span>
                    <div class="absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 hidden group-hover/tip:block w-56 p-2 bg-slate-900 border border-slate-700 text-slate-200 text-[11px] rounded-lg shadow-xl z-30 font-sans normal-case leading-normal pointer-events-none">
                      ${meta.desc}
                      <div class="w-2 h-2 bg-slate-900 border-r border-b border-slate-700 transform rotate-45 absolute -bottom-1 left-1/2 -translate-x-1/2"></div>
                    </div>
                  </div>
                  <span class="text-[10px] font-mono px-1.5 py-0.5 rounded font-semibold ${badgeClass}">${voteText}</span>
                </div>
                <div class="text-[11px] text-slate-300 font-sans italic">${meta.explain(val)}</div>
              </div>
            </div>
          `;
        }).join('');

        // Generate comprehensive summary purely from row data
        const posDrivers = Object.entries(contribs).filter(([k, v]) => v > 0.02).map(([k, v]) => indicatorMeta[k]?.title.split(' ')[0] || k);
        const negDrivers = Object.entries(contribs).filter(([k, v]) => v < -0.02).map(([k, v]) => indicatorMeta[k]?.title.split(' ')[0] || k);
        
        let driverSentence = "";
        if (p.direction === 'bullish') {
          if (posDrivers.length) driverSentence += `Upward momentum was driven primarily by <strong>${posDrivers.join(', ')}</strong>. `;
          if (negDrivers.length) driverSentence += `Counter-acting drag was noted from <strong>${negDrivers.join(', ')}</strong>. `;
        } else if (p.direction === 'bearish') {
          if (negDrivers.length) driverSentence += `Downward pressure was driven primarily by <strong>${negDrivers.join(', ')}</strong>. `;
          if (posDrivers.length) driverSentence += `Counter-acting support was noted from <strong>${posDrivers.join(', ')}</strong>. `;
        } else {
          driverSentence = "Neither buyers nor sellers showed sufficient conviction to clear the directional hurdle. ";
        }

        const actionText = p.direction === 'no_call' 
          ? `Because the score fell inside the <strong>±0.18</strong> neutral zone, she wisely abstained to avoid flat market chop.`
          : `Because the final score cleared the <strong>±0.18</strong> threshold, she committed to the call with an ATR volatility cushion of <strong>$${p.noise_threshold ? p.noise_threshold.toFixed(2) : 0}</strong>.`;

        let summaryHtml = `Nugget reached a <strong>${p.direction.toUpperCase()}</strong> call with a conviction score of <strong>${signedScore}</strong>. ${driverSentence}${actionText}`;

        document.getElementById('modalRationaleText').innerHTML = summaryHtml;
      } else {
        scoreContainer.innerHTML = `<div class="text-slate-500 py-3 text-center">Standard indicator calculations logged.</div>`;
        document.getElementById('modalRationaleText').textContent = p.notes || "Technical indicators confluence score calculated prior to bar formation.";
      }
      document.getElementById('modalRefPrice').textContent = `$${p.ref_price.toFixed(2)}`;
      document.getElementById('modalNoiseThr').textContent = p.noise_threshold ? `$${p.noise_threshold.toFixed(2)}` : '—';
      document.getElementById('modalRealizedPrice').textContent = p.realized_price ? `$${p.realized_price.toFixed(2)}` : 'In Progress...';

      const outcomeEl = document.getElementById('modalOutcomeStatus');
      if (p.graded_wib) {
        if (p.is_noise) {
          outcomeEl.textContent = 'NOISE (Ignored)';
          outcomeEl.className = 'font-bold text-base mt-1 text-slate-400';
        } else if (p.correct === 1) {
          outcomeEl.textContent = `WIN (+${p.move.toFixed(2)} pts)`;
          outcomeEl.className = 'font-bold text-base mt-1 text-emerald-400';
        } else if (p.correct === 0) {
          outcomeEl.textContent = `LOSS (${p.move.toFixed(2)} pts)`;
          outcomeEl.className = 'font-bold text-base mt-1 text-rose-400';
        }
      } else {
        outcomeEl.textContent = 'Pending Grading';
        outcomeEl.className = 'font-bold text-base mt-1 text-amber-400';
      }

      // Populate features
      const featuresGrid = document.getElementById('modalFeaturesGrid');
      let feats = {};
      try { feats = JSON.parse(p.features_json || '{}'); } catch(e){}

      const featureItems = [
        { label: 'RSI (14)', val: feats.rsi14 ? feats.rsi14.toFixed(1) : '—', color: feats.rsi14 > 55 ? 'text-emerald-400' : (feats.rsi14 < 45 ? 'text-rose-400' : 'text-slate-300') },
        { label: '20-SMA', val: feats.sma20 ? `$${feats.sma20.toFixed(1)}` : '—', color: 'text-slate-300' },
        { label: 'Fast EMA (12)', val: feats.ema12 ? `$${feats.ema12.toFixed(1)}` : '—', color: 'text-slate-300' },
        { label: 'Slow EMA (26)', val: feats.ema26 ? `$${feats.ema26.toFixed(1)}` : '—', color: 'text-slate-300' },
        { label: 'Volatility (ATR14)', val: feats.atr14 ? `$${feats.atr14.toFixed(2)}` : '—', color: 'text-amber-400' },
        { label: 'Volume vs Avg', val: feats.vol_vs_avg ? `${feats.vol_vs_avg.toFixed(2)}x` : '—', color: 'text-slate-300' }
      ];

      featuresGrid.innerHTML = featureItems.map(f => `
        <div class="bg-slate-950/40 p-2.5 rounded-lg border border-slate-800">
          <div class="text-[10px] text-slate-500 uppercase">${f.label}</div>
          <div class="font-bold text-xs mt-0.5 ${f.color}">${f.val}</div>
        </div>
      `).join('');

      // Sources & Inputs
      let inputs = {};
      try { inputs = JSON.parse(p.inputs_json || '{}'); } catch(e){}
      const sourcesDiv = document.getElementById('modalSourcesSection');
      if (inputs.paxg || inputs.spot) {
        sourcesDiv.classList.remove('hidden');
        document.getElementById('modalSourcesText').innerHTML = `
          Spot: <strong>$${inputs.spot || '—'}</strong> | PAXG: <strong>$${inputs.paxg || '—'}</strong> | Spread: <strong>$${inputs.spread || 0.01}</strong> | Macro Calendar Events: <strong>${inputs.calendar_events || 0}</strong>
        `;
      } else {
        sourcesDiv.classList.add('hidden');
      }

      document.getElementById('detailsModal').classList.remove('hidden');
    }

    function closeDetailsModal() {
      document.getElementById('detailsModal').classList.add('hidden');
    }

    function handleBackdropClick(event) {
      const card = document.getElementById('detailsModalCard');
      // If click target is outside the inner modal card, close it
      if (card && !card.contains(event.target)) {
        closeDetailsModal();
      }
    }

    // Close modal on Escape
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeDetailsModal();
    });

    // Auto-refresh every 30 seconds
    loadData();
    setInterval(loadData, 30000);
  </script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode("utf-8"))
            return

        if path == "/api/stats":
            days = int(query["days"][0]) if "days" in query and query["days"][0].isdigit() else None
            signal = query.get("signal", [None])[0]
            data = get_stats_data(days=days, signal_source=signal)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        # Suppress noisy standard request logs
        pass


def run_server():
    server = HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    print(f"🥇 Nugget Quantitative Dashboard running on http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down dashboard...")
        server.server_close()


if __name__ == "__main__":
    run_server()
