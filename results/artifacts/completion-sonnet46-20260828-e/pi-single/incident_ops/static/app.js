/* Incident Operations Center – browser application */
'use strict';

// ── State ──────────────────────────────────────────────────────────────────
const state = {
  incidents: [],
  selectedId: null,
  filters: { status: '', severity: '', owner: '' },
  feedbackMessage: '',
};

// ── DOM refs ───────────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const listBody       = $('incident-list-body');
const detailEl       = $('incident-detail');
const emptyState     = $('empty-state');
const feedbackEl     = $('feedback');
const ownerInput     = $('owner-input');
const ackBtn         = $('ack-btn');
const resolveBtn     = $('resolve-btn');
const assignBtn      = $('assign-btn');
const timelineList   = $('timeline-list');
const statusFilter   = $('status-filter');
const severityFilter = $('severity-filter');
const ownerFilter    = $('owner-filter');

// ── Helpers ────────────────────────────────────────────────────────────────
function fmt(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString();
}

function fmtRelative(ts) {
  if (!ts) return '—';
  const diff = Math.floor(Date.now() / 1000 - ts);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function setFeedback(msg, type = '') {
  state.feedbackMessage = msg;
  feedbackEl.textContent = msg;
  feedbackEl.className = 'feedback-bar ' + type;
}

function clearFeedback() {
  state.feedbackMessage = '';
  feedbackEl.textContent = '';
  feedbackEl.className = 'feedback-bar';
}

// ── Render summary ─────────────────────────────────────────────────────────
function renderSummary(incidents) {
  let open = 0, ack = 0, resolved = 0;
  const bySev = { P1: 0, P2: 0, P3: 0, P4: 0 };
  for (const inc of incidents) {
    if (inc.status === 'open') open++;
    else if (inc.status === 'acknowledged') ack++;
    else if (inc.status === 'resolved') resolved++;
    if (bySev[inc.severity] !== undefined) bySev[inc.severity]++;
  }
  $('sum-total').textContent = incidents.length;
  $('sum-open').textContent = open;
  $('sum-ack').textContent = ack;
  $('sum-resolved').textContent = resolved;

  const sevCounts = $('severity-counts');
  sevCounts.innerHTML = '';
  for (const [sev, count] of Object.entries(bySev)) {
    if (count === 0) continue;
    const pill = document.createElement('span');
    pill.className = `sev-pill ${sev}`;
    pill.textContent = `${sev}: ${count}`;
    sevCounts.appendChild(pill);
  }
}

// ── Render list ────────────────────────────────────────────────────────────
function renderList(incidents) {
  if (incidents.length === 0) {
    listBody.innerHTML = '<p style="color:var(--text-muted);font-size:0.8rem;padding:0.5rem 0">No incidents match filters</p>';
    return;
  }
  listBody.replaceChildren(...incidents.map((inc) => {
    const row = document.createElement('div');
    row.className = 'incident-row' + (inc.id === state.selectedId ? ' selected' : '');
    row.dataset.testid = 'incident-row';
    row.dataset.id = inc.id;
    row.innerHTML = `
      <div class="incident-row-top">
        <span class="sev-badge ${inc.severity}">${inc.severity}</span>
        <span class="incident-row-title"></span>
        <span class="status-chip ${inc.status}">${inc.status}</span>
      </div>
      <div class="incident-row-meta">
        <span>${inc.owner ? '👤 ' + inc.owner : 'Unassigned'}</span>
        <span>${fmtRelative(inc.updated_at)}</span>
        ${inc.alert_count > 1 ? `<span>🔔 ${inc.alert_count}</span>` : ''}
      </div>`;
    row.querySelector('.incident-row-title').textContent = inc.title;
    row.addEventListener('click', () => selectIncident(inc.id));
    return row;
  }));
}

// ── Apply filters ──────────────────────────────────────────────────────────
function filteredIncidents() {
  return state.incidents.filter((inc) => {
    if (state.filters.status && inc.status !== state.filters.status) return false;
    if (state.filters.severity && inc.severity !== state.filters.severity) return false;
    if (state.filters.owner && !(inc.owner || '').toLowerCase().includes(state.filters.owner.toLowerCase())) return false;
    return true;
  });
}

// ── Render detail ──────────────────────────────────────────────────────────
async function renderDetail(incidentId) {
  if (!incidentId) {
    detailEl.hidden = true;
    emptyState.hidden = false;
    return;
  }
  let inc;
  try {
    const resp = await fetch(`/api/incidents/${incidentId}`);
    if (!resp.ok) {
      setFeedback('Could not load incident', 'error');
      return;
    }
    inc = await resp.json();
  } catch {
    setFeedback('Network error loading incident', 'error');
    return;
  }

  emptyState.hidden = true;
  detailEl.hidden = false;

  $('detail-sev').className = `sev-badge ${inc.severity}`;
  $('detail-sev').textContent = inc.severity;
  $('detail-title').textContent = inc.title;
  $('detail-status').className = `status-chip ${inc.status}`;
  $('detail-status').textContent = inc.status;
  $('detail-id').textContent = inc.id;
  $('detail-version').textContent = inc.version ?? '—';
  $('detail-alerts').textContent = inc.alert_count ?? 1;
  $('detail-created').textContent = fmt(inc.created_at);
  $('detail-updated').textContent = fmt(inc.updated_at);
  $('detail-sla').textContent = fmt(inc.sla_deadline);
  ownerInput.value = inc.owner ?? '';

  // Actions visibility
  const resolved = inc.status === 'resolved';
  ackBtn.disabled = inc.status !== 'open';
  resolveBtn.disabled = resolved;
  assignBtn.disabled = resolved;
  ownerInput.disabled = resolved;

  // Store current version on buttons for OCC
  ackBtn.dataset.version = inc.version;
  resolveBtn.dataset.version = inc.version;
  assignBtn.dataset.version = inc.version;
  detailEl.dataset.incidentId = inc.id;

  // Timeline
  const events = inc.events || [];
  timelineList.replaceChildren(...events.map((ev) => {
    const li = document.createElement('li');
    li.className = 'timeline-item';
    const detailStr = Object.keys(ev.details || {}).length
      ? JSON.stringify(ev.details)
      : '';
    li.innerHTML = `
      <span class="timeline-dot"></span>
      <span class="timeline-type">${ev.type}</span>
      <span class="timeline-time">${fmt(ev.timestamp)}</span>
      ${detailStr ? `<div class="timeline-details">${escapeHtml(detailStr)}</div>` : ''}
    `;
    return li;
  }));
}

function escapeHtml(str) {
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// ── Actions ────────────────────────────────────────────────────────────────
async function patchIncident(incidentId, version, payload) {
  try {
    const resp = await fetch(`/api/incidents/${incidentId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ expected_version: version, ...payload }),
    });
    const data = await resp.json();
    if (resp.status === 409) {
      setFeedback('⚠ Version conflict – someone else made a change. Refreshing…', 'conflict');
      await refresh();
      await renderDetail(incidentId);
      return null;
    }
    if (!resp.ok) {
      setFeedback('Error: ' + (data.error || resp.statusText), 'error');
      return null;
    }
    return data;
  } catch (err) {
    setFeedback('Network error: ' + err.message, 'error');
    return null;
  }
}

ackBtn.addEventListener('click', async () => {
  const id = detailEl.dataset.incidentId;
  const ver = parseInt(ackBtn.dataset.version, 10);
  clearFeedback();
  const result = await patchIncident(id, ver, { status: 'acknowledged' });
  if (result) {
    setFeedback('Incident acknowledged', 'success');
    await refresh();
    await renderDetail(id);
  }
});

resolveBtn.addEventListener('click', async () => {
  const id = detailEl.dataset.incidentId;
  const ver = parseInt(resolveBtn.dataset.version, 10);
  clearFeedback();
  const result = await patchIncident(id, ver, { status: 'resolved' });
  if (result) {
    setFeedback('Incident resolved', 'success');
    await refresh();
    await renderDetail(id);
  }
});

assignBtn.addEventListener('click', async () => {
  const id = detailEl.dataset.incidentId;
  const ver = parseInt(assignBtn.dataset.version, 10);
  clearFeedback();
  const result = await patchIncident(id, ver, { owner: ownerInput.value.trim() });
  if (result) {
    setFeedback('Owner updated', 'success');
    await refresh();
    await renderDetail(id);
  }
});

// ── Filters ────────────────────────────────────────────────────────────────
statusFilter.addEventListener('change', () => {
  state.filters.status = statusFilter.value;
  const filtered = filteredIncidents();
  renderList(filtered);
});

severityFilter.addEventListener('change', () => {
  state.filters.severity = severityFilter.value;
  const filtered = filteredIncidents();
  renderList(filtered);
});

ownerFilter.addEventListener('input', () => {
  state.filters.owner = ownerFilter.value;
  const filtered = filteredIncidents();
  renderList(filtered);
});

// ── Public API ─────────────────────────────────────────────────────────────
async function refresh() {
  const params = new URLSearchParams();
  if (state.filters.status) params.set('status', state.filters.status);
  if (state.filters.severity) params.set('severity', state.filters.severity);
  const url = '/api/incidents' + (params.toString() ? '?' + params : '');
  try {
    const resp = await fetch(url);
    if (!resp.ok) return;
    state.incidents = await resp.json();
  } catch {
    return;
  }
  renderSummary(state.incidents);
  renderList(filteredIncidents());
}

async function selectIncident(id) {
  state.selectedId = id;
  clearFeedback();
  // Re-render list to update selection highlight
  renderList(filteredIncidents());
  await renderDetail(id);
}

function getState() {
  return {
    filters: { ...state.filters },
    selectedId: state.selectedId,
    incidents: state.incidents.slice(),
    feedbackMessage: state.feedbackMessage,
  };
}

// Expose global contract
window.incidentOps = { refresh, selectIncident, getState };

$('refresh-btn').addEventListener('click', () => refresh());

// ── Init ───────────────────────────────────────────────────────────────────
refresh();
