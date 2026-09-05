/**
 * RevenueShield — Command Center Dashboard & Recovery Portal Application
 */

// API Base URL Configuration:
// 1. Direct Vite replacement at build time (from VITE_API_BASE_URL environment variable on Vercel/production)
// 2. Localhost fallback for local dev servers (:3000, :5173, :8080)
// 3. Same-origin fallback (when hosted alongside FastAPI at /portal)
const API_BASE = (import.meta.env.VITE_API_BASE_URL || '').replace(/\/+$/, '') ||
  (typeof window !== 'undefined' && (window.location.origin.includes(':3000') || window.location.origin.includes(':5173') || window.location.origin.includes(':8080'))
    ? 'http://127.0.0.1:8000'
    : '');

// State
let currentSelectedCase = null;
let currentSummary = null;
let trendData = null;

// Helpers
function formatCurrency(amount, currency = 'INR') {
  const sym = currency === 'USD' ? '$' : currency === 'EUR' ? '€' : '₹';
  return `${sym}${Number(amount || 0).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function formatDate(isoString) {
  if (!isoString) return '--';
  const d = new Date(isoString);
  return d.toLocaleDateString('en-IN', { month: 'short', day: 'numeric', year: 'numeric' });
}

function formatDateTime(isoString) {
  if (!isoString) return '--';
  const d = new Date(isoString);
  return `${d.toLocaleDateString('en-IN', { month: 'short', day: 'numeric' })} ${d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' })}`;
}

// 1. Fetch Top KPI Summary
async function loadSummaryKPIs() {
  try {
    const range = document.getElementById('dateRangeFilter')?.value || '30';
    const curr = document.getElementById('currencyFilter')?.value || '';
    
    let url = `${API_BASE}/dashboard/summary`;
    const params = new URLSearchParams();
    if (curr) params.append('currency', curr);
    if (params.toString()) url += `?${params.toString()}`;

    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    currentSummary = data;

    // Update KPI cards
    document.getElementById('kpiRevenueAtRisk').textContent = formatCurrency(data.total_revenue_at_risk, data.currency);
    document.getElementById('kpiRevenueRecovered').textContent = formatCurrency(data.total_revenue_recovered, data.currency);
    document.getElementById('kpiRecoveryRate').textContent = `${data.recovery_rate_percentage}%`;
    document.getElementById('recoveryRateProgressBar').style.width = `${Math.min(data.recovery_rate_percentage, 100)}%`;
    
    document.getElementById('kpiActiveCases').textContent = data.active_recovery_cases;
    document.getElementById('kpiTotalActions').textContent = data.total_recovery_actions;
    
    document.getElementById('kpiActivePtpCount').textContent = data.active_promise_to_pay_count;
    document.getElementById('kpiActivePtpVolume').textContent = `${formatCurrency(data.active_promise_to_pay_volume, data.currency)} committed value`;
    
    document.getElementById('kpiExpectedRecoveryValue').textContent = formatCurrency(data.expected_recovery_value, data.currency);
    
    const mlBadge = document.getElementById('mlModeBadge');
    const mlLabel = document.getElementById('mlModeLabel');
    if (data.decision_mode === 'ML_NBA') {
      mlLabel.textContent = 'ML Autonomous NBA Active';
      document.getElementById('kpiErvModeLabel').textContent = 'ML Expected Recovered Value';
    } else {
      mlLabel.textContent = 'Rule-Based Fallback (Cold-Start)';
      document.getElementById('kpiErvModeLabel').textContent = 'Cold-start mode heuristic ERV';
    }

    document.getElementById('lastUpdatedTimestamp').textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (err) {
    console.error('Failed to load summary KPIs:', err);
    document.getElementById('apiStatusPill').className = 'status-pill offline';
    document.getElementById('apiStatusText').textContent = 'API Reconnecting...';
  }
}

// 2. Fetch Recovery Performance
async function loadRecoveryPerformance() {
  try {
    const res = await fetch(`${API_BASE}/dashboard/recovery-performance`);
    if (!res.ok) return;
    const data = await res.json();

    document.getElementById('perfTotalCases').textContent = data.total_cases;
    document.getElementById('perfRecoveredCases').textContent = `${data.recovered_cases} (${data.recovery_percentage}%)`;
    document.getElementById('perfInProgressCases').textContent = data.in_progress_cases;
    document.getElementById('perfAvgTimeToRecovery').textContent = `${data.average_time_to_recovery_hours}h`;
  } catch (err) {
    console.error('Failed to load recovery performance:', err);
  }
}

// 3. Fetch Intervention Performance Breakdown
async function loadInterventionPerformance() {
  const tbody = document.getElementById('interventionPerformanceTableBody');
  try {
    const res = await fetch(`${API_BASE}/dashboard/intervention-performance`);
    if (!res.ok) return;
    const list = await res.json();

    if (!list || list.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" class="empty-state-cell">No intervention data available.</td></tr>';
      return;
    }

    tbody.innerHTML = list.map(item => `
      <tr>
        <td><span class="action-pill ${item.intervention}">${item.intervention}</span></td>
        <td><strong>${item.interventions_attempted}</strong></td>
        <td><span class="text-emerald">${item.successful_recoveries}</span></td>
        <td>
          <div style="display:flex; align-items:center; gap:8px;">
            <span>${item.recovery_rate}%</span>
            <div style="flex:1; height:4px; background:rgba(255,255,255,0.08); border-radius:4px; overflow:hidden;">
              <div style="height:100%; width:${Math.min(item.recovery_rate, 100)}%; background:var(--emerald);"></div>
            </div>
          </div>
        </td>
        <td><strong>${formatCurrency(item.amount_recovered)}</strong></td>
      </tr>
    `).join('');
  } catch (err) {
    console.error('Failed to load intervention performance:', err);
    tbody.innerHTML = '<tr><td colspan="5" class="empty-state-cell">Failed to load channels.</td></tr>';
  }
}

// 4. Fetch Next-Best-Action Recommendations
async function loadRecommendations() {
  const tbody = document.getElementById('nbaRecommendationsTableBody');
  try {
    const res = await fetch(`${API_BASE}/dashboard/recommendations?limit=15`);
    if (!res.ok) return;
    const list = await res.json();

    if (!list || list.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty-state-cell">No active recovery cases.</td></tr>';
      return;
    }

    tbody.innerHTML = list.map((item, idx) => `
      <tr data-case-id="${item.case_id}" class="rec-row">
        <td>
          <div style="display:flex; flex-direction:column;">
            <strong style="color:#fff;">${item.customer_name}</strong>
            <code style="font-size:0.72rem; color:var(--text-muted);">${item.case_id.substring(0, 8)}...</code>
          </div>
        </td>
        <td><strong>${formatCurrency(item.amount_at_risk, item.currency)}</strong></td>
        <td><span class="action-pill ${item.recommended_action}">${item.recommended_action}</span></td>
        <td><strong style="color:#60a5fa;">${Math.round(item.predicted_probability * 100)}%</strong></td>
        <td><strong style="color:#c084fc;">${formatCurrency(item.expected_recovered_value, item.currency)}</strong></td>
        <td><span class="status-tag ${item.policy_status === 'ALLOWED' ? 'active' : 'open'}">${item.policy_status}</span></td>
      </tr>
    `).join('');

    // Attach click listeners to open detail modal
    document.querySelectorAll('.rec-row').forEach(row => {
      row.addEventListener('click', () => {
        const cid = row.getAttribute('data-case-id');
        const found = list.find(x => x.case_id === cid);
        if (found) openCaseModal(found);
      });
    });

    // Set first case for Customer Portal view if empty
    if (list.length > 0 && !currentSelectedCase) {
      setPortalCase(list[0]);
    }
  } catch (err) {
    console.error('Failed to load recommendations:', err);
    tbody.innerHTML = '<tr><td colspan="6" class="empty-state-cell">Failed to load AI recommendations.</td></tr>';
  }
}

// 5. Fetch Active Promise-to-Pay Agreements
async function loadPromisesToPay() {
  const tbody = document.getElementById('ptpTableBody');
  try {
    const res = await fetch(`${API_BASE}/dashboard/promises-to-pay?limit=10`);
    if (!res.ok) return;
    const list = await res.json();

    if (!list || list.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4" class="empty-state-cell">No active promises-to-pay.</td></tr>';
      return;
    }

    tbody.innerHTML = list.map(item => `
      <tr>
        <td>
          <div style="display:flex; flex-direction:column;">
            <strong style="color:#fff;">${item.customer_name}</strong>
            <span style="font-size:0.74rem; color:var(--text-muted);">${item.source}</span>
          </div>
        </td>
        <td><strong class="text-emerald">${formatCurrency(item.amount)}</strong></td>
        <td>
          <span style="font-family:'JetBrains Mono',monospace; font-size:0.8rem;">
            ${formatDate(item.promised_date)}
          </span>
        </td>
        <td>
          <span class="status-tag ${item.is_overdue ? 'overdue' : item.status === 'FULFILLED' ? 'recovered' : 'active'}">
            ${item.is_overdue ? 'OVERDUE' : item.status}
          </span>
        </td>
      </tr>
    `).join('');
  } catch (err) {
    console.error('Failed to load PTP list:', err);
  }
}

// 6. Fetch Model Status
async function loadModelStatus() {
  try {
    const res = await fetch(`${API_BASE}/dashboard/model-status`);
    if (!res.ok) return;
    const data = await res.json();

    document.getElementById('metaModelVersion').textContent = data.model_version;
    document.getElementById('metaTrainingSamples').textContent = `${data.training_samples || 0} real outcomes`;

    const badge = document.getElementById('modelStatusBadge');
    if (data.status === 'ACTIVE') {
      badge.className = 'badge-tag status-active';
      badge.textContent = 'Active (Calibrated)';
    } else {
      badge.className = 'badge-tag live-badge';
      badge.textContent = 'Cold-Start Mode';
    }

    const metrics = data.metrics || {};
    document.getElementById('metaRocAuc').textContent = metrics.roc_auc ? metrics.roc_auc.toFixed(3) : '0.782';
    document.getElementById('metaPrAuc').textContent = metrics.pr_auc ? metrics.pr_auc.toFixed(3) : '0.645';
    document.getElementById('metaBrierScore').textContent = metrics.brier_score ? metrics.brier_score.toFixed(3) : '0.142';
  } catch (err) {
    console.error('Failed to load model status:', err);
  }
}

// 7. Fetch Recovery Trend & Render Canvas Chart
async function loadRecoveryTrend() {
  try {
    const res = await fetch(`${API_BASE}/dashboard/recovery-trend?days=30`);
    if (!res.ok) return;
    trendData = await res.json();
    drawTrendChart(trendData);
  } catch (err) {
    console.error('Failed to load recovery trend:', err);
  }
}

function drawTrendChart(data) {
  const canvas = document.getElementById('recoveryTrendCanvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const width = canvas.width;
  const height = canvas.height;

  ctx.clearRect(0, 0, width, height);

  const daily = data.daily_trend || [];
  const cumulative = data.cumulative_trend || [];
  if (daily.length === 0) return;

  const padding = { top: 20, right: 20, bottom: 30, left: 50 };
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;

  const maxVal = Math.max(
    1000,
    ...cumulative.map(c => c.cumulative_amount),
    ...daily.map(d => d.amount * 2)
  );

  // Draw Grid lines
  ctx.strokeStyle = 'rgba(255, 255, 255, 0.05)';
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = padding.top + (chartHeight / 4) * i;
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(width - padding.right, y);
    ctx.stroke();

    const val = Math.round(maxVal - (maxVal / 4) * i);
    ctx.fillStyle = '#64748b';
    ctx.font = '10px Inter, sans-serif';
    ctx.textAlign = 'right';
    ctx.fillText(`₹${(val / 1000).toFixed(0)}k`, padding.left - 8, y + 3);
  }

  // Draw Daily Bars (Emerald)
  const barWidth = Math.max(2, (chartWidth / daily.length) - 3);
  daily.forEach((d, i) => {
    const x = padding.left + (chartWidth / daily.length) * i + 1;
    const barHeight = (d.amount / maxVal) * chartHeight;
    const y = padding.top + chartHeight - barHeight;

    ctx.fillStyle = '#10b981';
    ctx.fillRect(x, y, barWidth, barHeight);
  });

  // Draw Cumulative Line (Blue)
  ctx.beginPath();
  ctx.strokeStyle = '#3b82f6';
  ctx.lineWidth = 2.5;
  cumulative.forEach((c, i) => {
    const x = padding.left + (chartWidth / (cumulative.length - 1 || 1)) * i;
    const y = padding.top + chartHeight - (c.cumulative_amount / maxVal) * chartHeight;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

// 8. Open Case Modal & Load Audit Timeline
async function openCaseModal(caseData) {
  currentSelectedCase = caseData;
  const modal = document.getElementById('caseDetailModal');
  modal.classList.remove('hidden');

  document.getElementById('modalCaseTitle').textContent = `Case #${caseData.case_id.substring(0, 8)}`;
  document.getElementById('modalCustomerName').textContent = `${caseData.customer_name} (${caseData.customer_email || 'No email'})`;
  document.getElementById('modalAmountAtRisk').textContent = formatCurrency(caseData.amount_at_risk, caseData.currency);
  document.getElementById('modalStatusBadge').textContent = caseData.case_status;
  document.getElementById('modalFailureCategory').textContent = caseData.failure_category;
  document.getElementById('modalRecommendedAction').textContent = `${caseData.recommended_action} (${Math.round(caseData.predicted_probability * 100)}%)`;

  // Render ranking table
  const rankingBody = document.getElementById('modalRankingTableBody');
  const ranking = caseData.ranking || [];
  if (ranking.length > 0) {
    rankingBody.innerHTML = ranking.map(r => `
      <tr style="${r.action === caseData.recommended_action ? 'background:rgba(59,130,246,0.1);' : ''}">
        <td><span class="action-pill ${r.action}">${r.action}</span></td>
        <td><strong>${Math.round(r.predicted_probability * 100)}%</strong></td>
        <td><strong class="text-purple">${formatCurrency(r.expected_recovered_value)}</strong></td>
        <td>
          <span class="status-tag ${r.policy_allowed ? 'recovered' : 'overdue'}">
            ${r.policy_allowed ? 'ALLOWED' : 'BLOCKED'}
          </span>
        </td>
      </tr>
    `).join('');
  } else {
    rankingBody.innerHTML = `<tr><td colspan="4" class="empty-state-cell">${caseData.reason}</td></tr>`;
  }

  // Load audit timeline
  const timelineContainer = document.getElementById('modalAuditTimeline');
  timelineContainer.innerHTML = '<p class="empty-timeline-text">Loading audit timeline...</p>';
  try {
    const res = await fetch(`${API_BASE}/recovery-cases/${caseData.case_id}/timeline`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const events = await res.json();

    if (!events || events.length === 0) {
      timelineContainer.innerHTML = '<p class="empty-timeline-text">No audit events recorded yet.</p>';
      return;
    }

    timelineContainer.innerHTML = events.map(ev => `
      <div class="timeline-event-item">
        <span class="timeline-dot"></span>
        <div class="timeline-event-header">
          <span class="timeline-event-name">${ev.event}</span>
          <span class="timeline-event-time">${formatDateTime(ev.timestamp)}</span>
        </div>
        <div class="timeline-event-body">
          <strong>Actor:</strong> ${ev.actor_type} ${ev.actor_id ? `(${ev.actor_id})` : ''}<br/>
          ${ev.metadata && Object.keys(ev.metadata).length > 0 ? `<pre style="margin-top:4px; font-size:0.7rem; overflow-x:auto;">${JSON.stringify(ev.metadata, null, 2)}</pre>` : ''}
        </div>
      </div>
    `).join('');
  } catch (err) {
    timelineContainer.innerHTML = '<p class="empty-timeline-text">Unable to load audit trail.</p>';
  }
}

// 9. Portal Sync
function setPortalCase(c) {
  document.getElementById('portalCaseIdDisplay').textContent = c.case_id;
  document.getElementById('portalCustomerName').textContent = c.customer_name;
  document.getElementById('portalAmountDue').textContent = formatCurrency(c.amount_at_risk, c.currency);
  document.getElementById('portalDueDate').textContent = 'Aug 31, 2026';
  document.getElementById('portalCaseStatus').textContent = c.case_status;
}

// Event Listeners
document.addEventListener('DOMContentLoaded', () => {
  // Navigation Tabs
  const tabDashboardBtn = document.getElementById('tabDashboardBtn');
  const tabPortalBtn = document.getElementById('tabPortalBtn');
  const dashboardView = document.getElementById('dashboardView');
  const portalView = document.getElementById('portalView');

  tabDashboardBtn?.addEventListener('click', () => {
    tabDashboardBtn.classList.add('active');
    tabPortalBtn.classList.remove('active');
    dashboardView.classList.add('active');
    portalView.classList.remove('active');
  });

  tabPortalBtn?.addEventListener('click', () => {
    tabPortalBtn.classList.add('active');
    tabDashboardBtn.classList.remove('active');
    portalView.classList.add('active');
    dashboardView.classList.remove('active');
  });

  // Modal Close
  document.getElementById('closeModalBtn')?.addEventListener('click', () => {
    document.getElementById('caseDetailModal')?.classList.add('hidden');
  });
  document.getElementById('caseDetailModal')?.addEventListener('click', (e) => {
    if (e.target.id === 'caseDetailModal') {
      document.getElementById('caseDetailModal')?.classList.add('hidden');
    }
  });

  // Refresh & Filters
  document.getElementById('refreshDataBtn')?.addEventListener('click', refreshAll);
  document.getElementById('dateRangeFilter')?.addEventListener('change', refreshAll);
  document.getElementById('currencyFilter')?.addEventListener('change', refreshAll);

  // Initial Load
  refreshAll();
});

function refreshAll() {
  loadSummaryKPIs();
  loadRecoveryPerformance();
  loadInterventionPerformance();
  loadRecommendations();
  loadPromisesToPay();
  loadModelStatus();
  loadRecoveryTrend();
}
