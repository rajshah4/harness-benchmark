/**
 * Freight Control Tower – browser dashboard
 *
 * Security: all dynamic content is inserted via textContent / createElement,
 * never innerHTML with unsanitised data.
 */
'use strict';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let token = sessionStorage.getItem('fct_token') || '';
let activeTab = 'shipments';

// ---------------------------------------------------------------------------
// DOM references
// ---------------------------------------------------------------------------
const loginPanel  = document.getElementById('login-panel');
const dashboard   = document.getElementById('dashboard');
const loginForm   = document.getElementById('login-form');
const tokenInput  = document.getElementById('token-input');
const loginFb     = document.getElementById('login-feedback');
const dashFb      = document.getElementById('dash-feedback');
const signOutBtn  = document.getElementById('sign-out-btn');

const panelMap = {
  shipments:  document.getElementById('panel-shipments'),
  exceptions: document.getElementById('panel-exceptions'),
  audit:      document.getElementById('panel-audit'),
  deliveries: document.getElementById('panel-deliveries'),
};

// ---------------------------------------------------------------------------
// Utility helpers
// ---------------------------------------------------------------------------
function setText(el, msg, cls = '') {
  el.textContent = msg;
  el.className = 'feedback' + (cls ? ' ' + cls : '');
}

function setLoading(panel) {
  panel.innerHTML = '';
  const p = document.createElement('p');
  p.className = 'loading';
  p.setAttribute('aria-live', 'polite');
  p.textContent = 'Loading…';
  panel.appendChild(p);
}

function setEmpty(panel, msg = 'No items found.') {
  panel.innerHTML = '';
  const p = document.createElement('p');
  p.className = 'empty';
  p.textContent = msg;
  panel.appendChild(p);
}

function badge(text, cls) {
  const span = document.createElement('span');
  span.className = 'badge badge-' + (cls || text);
  span.textContent = text;
  return span;
}

function ts(unix) {
  if (!unix) return '—';
  return new Date(unix * 1000).toLocaleString();
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------
async function api(method, path, body = null) {
  const opts = {
    method,
    headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' },
  };
  if (body !== null) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  const json = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(json.error || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return json;
}

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------
function showDashboard() {
  loginPanel.hidden = true;
  dashboard.hidden = false;
  loadTab(activeTab);
}

function showLogin() {
  loginPanel.hidden = false;
  dashboard.hidden = true;
}

loginForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const t = tokenInput.value.trim();
  if (!t) return;
  setText(loginFb, 'Verifying…');
  try {
    await api('GET', '/api/shipments', null);  // test token
    // ^ will throw 401 if bad
    token = t;
    sessionStorage.setItem('fct_token', token);
    setText(loginFb, '');
    showDashboard();
  } catch (err) {
    if (err.status === 401) {
      setText(loginFb, 'Invalid token – please check and try again.', 'error');
    } else {
      setText(loginFb, err.message, 'error');
    }
  }
});

signOutBtn.addEventListener('click', () => {
  token = '';
  sessionStorage.removeItem('fct_token');
  tokenInput.value = '';
  setText(loginFb, '');
  showLogin();
});

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------
document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    activeTab = btn.dataset.tab;
    // Toggle filter bars
    document.querySelectorAll('.filter-group').forEach(g => {
      g.classList.toggle('hidden', g.dataset.panel !== activeTab);
    });
    // Toggle panels
    Object.entries(panelMap).forEach(([k, p]) => {
      p.classList.toggle('hidden', k !== activeTab);
    });
    loadTab(activeTab);
  });
});

function loadTab(tab) {
  if (tab === 'shipments') loadShipments();
  else if (tab === 'exceptions') loadExceptions();
  else if (tab === 'audit') loadAudit();
  else if (tab === 'deliveries') loadDeliveries();
}

// ---------------------------------------------------------------------------
// Shipments
// ---------------------------------------------------------------------------
async function loadShipments() {
  const panel = panelMap.shipments;
  setLoading(panel);
  try {
    const status = document.getElementById('ship-status-filter').value;
    const qs = status ? `?status=${encodeURIComponent(status)}` : '';
    const ships = await api('GET', '/api/shipments' + qs);
    panel.innerHTML = '';
    if (!ships.length) { setEmpty(panel, 'No shipments found.'); return; }
    ships.forEach(s => panel.appendChild(renderShipment(s)));
    setText(dashFb, `${ships.length} shipment(s)`);
  } catch (err) {
    setEmpty(panel, 'Failed to load shipments: ' + err.message);
    setText(dashFb, err.message, 'error');
  }
}

function renderShipment(s) {
  const card = document.createElement('div');
  card.className = 'card';
  const title = document.createElement('span');
  title.className = 'card-title';
  title.textContent = s.reference;
  const meta = document.createElement('span');
  meta.className = 'card-meta';
  meta.textContent = `ID: ${s.id} · Location: ${s.last_location || '—'} · v${s.version} · ${ts(s.updated_at)}`;
  const actions = document.createElement('div');
  actions.className = 'card-actions';
  actions.appendChild(badge(s.status));
  card.append(title, meta, actions);
  return card;
}

document.getElementById('ship-refresh-btn').addEventListener('click', loadShipments);

// ---------------------------------------------------------------------------
// Exceptions
// ---------------------------------------------------------------------------
async function loadExceptions() {
  const panel = panelMap.exceptions;
  setLoading(panel);
  try {
    const status   = document.getElementById('exc-status-filter').value;
    const severity = document.getElementById('exc-severity-filter').value;
    const assignee = document.getElementById('exc-assignee-filter').value.trim();
    const params = new URLSearchParams();
    if (status)   params.set('status', status);
    if (severity) params.set('severity', severity);
    if (assignee) params.set('assigned_to', assignee);
    const qs = params.toString() ? '?' + params.toString() : '';
    const excs = await api('GET', '/api/exceptions' + qs);
    panel.innerHTML = '';
    if (!excs.length) { setEmpty(panel, 'No exceptions found.'); return; }
    excs.forEach(e => panel.appendChild(renderException(e)));
    setText(dashFb, `${excs.length} exception(s)`);
  } catch (err) {
    setEmpty(panel, 'Failed to load exceptions: ' + err.message);
    setText(dashFb, err.message, 'error');
  }
}

function renderException(e) {
  const card = document.createElement('div');
  card.className = 'card';
  const title = document.createElement('span');
  title.className = 'card-title';
  title.textContent = `Exception for shipment ${e.shipment_id.slice(0, 8)}…`;
  const meta = document.createElement('span');
  meta.className = 'card-meta';
  meta.textContent = `Opened: ${ts(e.opened_at)} · Assigned: ${e.assigned_to || 'Unassigned'} · v${e.version}`;
  const actions = document.createElement('div');
  actions.className = 'card-actions';
  actions.appendChild(badge(e.severity, e.severity));
  actions.appendChild(badge(e.status));
  if (e.status !== 'resolved') {
    const btn = document.createElement('button');
    btn.textContent = 'Manage';
    btn.addEventListener('click', () => openExcModal(e));
    actions.appendChild(btn);
  }
  card.append(title, meta, actions);
  return card;
}

document.getElementById('exc-refresh-btn').addEventListener('click', loadExceptions);

// ---------------------------------------------------------------------------
// Exception modal
// ---------------------------------------------------------------------------
const modal        = document.getElementById('exc-modal');
const modalExcId   = document.getElementById('modal-exc-id');
const modalVersion = document.getElementById('modal-exc-version');
const modalInfo    = document.getElementById('modal-info');
const modalFields  = document.getElementById('modal-fields');
const modalFb      = document.getElementById('modal-feedback');

function openExcModal(e) {
  modalExcId.value   = e.id;
  modalVersion.value = e.version;
  modalInfo.textContent = `Status: ${e.status} · Severity: ${e.severity} · Assigned: ${e.assigned_to || 'none'}`;
  modalFields.innerHTML = '';
  setText(modalFb, '');
  // Disable actions based on status
  document.getElementById('modal-ack-btn').disabled    = e.status !== 'open';
  document.getElementById('modal-resolve-btn').disabled = e.status === 'resolved';
  modal.showModal();
}

document.getElementById('modal-close-btn').addEventListener('click', () => modal.close());

async function doMutation(action, extra = {}) {
  setText(modalFb, 'Saving…');
  try {
    const body = {
      expected_version: parseInt(modalVersion.value, 10),
      action,
      ...extra,
    };
    const result = await api('PATCH', `/api/exceptions/${modalExcId.value}`, body);
    modalVersion.value = result.version;
    setText(modalFb, 'Saved.', 'success');
    modalInfo.textContent = `Status: ${result.status} · v${result.version}`;
    document.getElementById('modal-ack-btn').disabled    = result.status !== 'open';
    document.getElementById('modal-resolve-btn').disabled = result.status === 'resolved';
    loadExceptions();  // refresh list
  } catch (err) {
    if (err.status === 409) {
      setText(modalFb, 'Version conflict – reloading exception…', 'error');
      setTimeout(() => loadExceptions(), 800);
    } else {
      setText(modalFb, err.message, 'error');
    }
  }
}

document.getElementById('modal-ack-btn').addEventListener('click', () => doMutation('acknowledge'));
document.getElementById('modal-resolve-btn').addEventListener('click', () => doMutation('resolve'));

document.getElementById('modal-assign-btn').addEventListener('click', () => {
  modalFields.innerHTML = '';
  const lbl = document.createElement('label');
  lbl.textContent = 'Assignee';
  const inp = document.createElement('input');
  inp.id = 'modal-assignee';
  inp.placeholder = 'Username or email';
  lbl.appendChild(inp);
  modalFields.appendChild(lbl);
  inp.focus();
  // Replace button action
  const btn = document.getElementById('modal-assign-btn');
  btn.textContent = 'Submit assign';
  const orig = btn.onclick;
  btn.onclick = async () => {
    const assignee = inp.value.trim();
    if (!assignee) { setText(modalFb, 'Assignee is required.', 'error'); return; }
    await doMutation('assign', { assignee });
    btn.textContent = 'Assign';
    btn.onclick = orig;
    modalFields.innerHTML = '';
  };
});

document.getElementById('modal-note-btn').addEventListener('click', () => {
  modalFields.innerHTML = '';
  const lbl = document.createElement('label');
  lbl.textContent = 'Note';
  const inp = document.createElement('input');
  inp.id = 'modal-note';
  inp.placeholder = 'Your note…';
  lbl.appendChild(inp);
  modalFields.appendChild(lbl);
  inp.focus();
  const btn = document.getElementById('modal-note-btn');
  btn.textContent = 'Submit note';
  const orig = btn.onclick;
  btn.onclick = async () => {
    const note = inp.value.trim();
    if (!note) { setText(modalFb, 'Note text is required.', 'error'); return; }
    await doMutation('note', { note });
    btn.textContent = 'Add note';
    btn.onclick = orig;
    modalFields.innerHTML = '';
  };
});

// ---------------------------------------------------------------------------
// Audit log
// ---------------------------------------------------------------------------
async function loadAudit() {
  const panel = panelMap.audit;
  setLoading(panel);
  try {
    const rtype = document.getElementById('audit-type-filter').value.trim();
    const params = new URLSearchParams();
    if (rtype) params.set('resource_type', rtype);
    const qs = params.toString() ? '?' + params.toString() : '';
    const entries = await api('GET', '/api/audit' + qs);
    panel.innerHTML = '';
    if (!entries.length) { setEmpty(panel, 'No audit entries.'); return; }
    const table = document.createElement('table');
    const thead = document.createElement('thead');
    thead.innerHTML = '';
    const headerRow = document.createElement('tr');
    ['Time', 'Actor', 'Action', 'Resource type', 'Resource id'].forEach(h => {
      const th = document.createElement('th');
      th.textContent = h;
      headerRow.appendChild(th);
    });
    thead.appendChild(headerRow);
    table.appendChild(thead);
    const tbody = document.createElement('tbody');
    entries.forEach(e => {
      const tr = document.createElement('tr');
      [ts(e.created_at), e.actor, e.action, e.resource_type, e.resource_id].forEach(v => {
        const td = document.createElement('td');
        td.textContent = v;
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    panel.appendChild(table);
  } catch (err) {
    setEmpty(panel, 'Failed to load audit log: ' + err.message);
  }
}

document.getElementById('audit-refresh-btn').addEventListener('click', loadAudit);

// ---------------------------------------------------------------------------
// Deliveries
// ---------------------------------------------------------------------------
async function loadDeliveries() {
  const panel = panelMap.deliveries;
  setLoading(panel);
  try {
    const status = document.getElementById('del-status-filter').value;
    const qs = status ? `?status=${encodeURIComponent(status)}` : '';
    const deliveries = await api('GET', '/api/deliveries' + qs);
    panel.innerHTML = '';
    if (!deliveries.length) { setEmpty(panel, 'No deliveries found.'); return; }
    deliveries.forEach(d => panel.appendChild(renderDelivery(d)));
  } catch (err) {
    setEmpty(panel, 'Failed to load deliveries: ' + err.message);
  }
}

function renderDelivery(d) {
  const card = document.createElement('div');
  card.className = 'card';
  const title = document.createElement('span');
  title.className = 'card-title';
  title.textContent = d.idempotency_key;
  const meta = document.createElement('span');
  meta.className = 'card-meta';
  meta.textContent = `Attempts: ${d.attempts} · Created: ${ts(d.created_at)}`;
  if (d.last_error) {
    const err = document.createElement('span');
    err.style.cssText = 'color:var(--c-danger);font-size:.8rem;';
    err.textContent = 'Last error: ' + d.last_error;
    meta.appendChild(document.createElement('br'));
    meta.appendChild(err);
  }
  const actions = document.createElement('div');
  actions.className = 'card-actions';
  actions.appendChild(badge(d.status, d.status === 'delivered' ? 'delivered-d' : d.status));
  card.append(title, meta, actions);
  return card;
}

document.getElementById('del-refresh-btn').addEventListener('click', loadDeliveries);

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
if (token) {
  // Validate stored token before showing dashboard
  api('GET', '/api/shipments').then(() => showDashboard()).catch(() => {
    token = '';
    sessionStorage.removeItem('fct_token');
    showLogin();
  });
} else {
  showLogin();
}
