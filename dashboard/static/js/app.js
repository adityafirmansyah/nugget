/**
 * Nugget Quantitative Trading Dashboard Frontend
 * Handles dynamic fetching, KPI updates, Chart.js rendering, and details modal.
 */

let currentFilter = { days: null, signal: 'all' };
let currentPredictions = [];
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
      } else if (p.notes.startsWith("Market in low-conviction chop")) {
        reasonSummary = "Market chop (Abstained)";
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
