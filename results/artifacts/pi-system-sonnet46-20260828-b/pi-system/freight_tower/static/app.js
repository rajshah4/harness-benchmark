/* Freight Control Tower – dashboard application
 *
 * Security: all dynamic content is inserted via textContent / createElement.
 * Untrusted carrier-supplied values never reach the DOM via unsafe injection.
 */
'use strict';

// ─── State ───────────────────────────────────────────────────────────────────
let TOKEN = sessionStorage.getItem('fct_token') || '';
let CURRENT_TAB = 'shipments';

// ─── API helpers ─────────────────────────────────────────────────────────────

async function api(method, path, body) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json' },
  };
  if (TOKEN) opts.headers['Authorization'] = `Bearer ${TOKEN}`;
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch('/api/v1' + path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.message || res.statusText), { status: res.status, data });
  return data;
}

function qs(el, sel) { return el.querySelector(sel); }
function qsa(el, sel) { return [...el.querySelectorAll(sel)]; }

// ─── Status bar ──────────────────────────────────────────────────────────────

let _statusTimer;
function showStatus(msg, type = 'info', duration = 4000) {
  const bar = document.getElementById('status-bar');
  bar.textContent = msg;
  bar.className = `status-bar ${type}`;
  bar.hidden = false;
  clearTimeout(_statusTimer);
  if (duration > 0) _statusTimer = setTimeout(() => { bar.hidden = true; }, duration);
}

// ─── Auth ────────────────────────────────────────────────────────────────────

function isAuthenticated() { return Boolean(TOKEN); }

function signIn(token) {
  TOKEN = token;
  sessionStorage.setItem('fct_token', token);
  document.getElementById('login-screen').hidden = true;
  document.getElementById('dashboard').hidden = false;
  loadCurrentTab();
}

function signOut() {
  TOKEN = '';
  sessionStorage.removeItem('fct_token');
  document.getElementById('dashboard').hidden = true;
  document.getElementById('login-screen').hidden = false;
}

// ─── Tab navigation ──────────────────────────────────────────────────────────

function switchTab(name) {
  CURRENT_TAB = name;
  qsa(document, '.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  qsa(document, '.tab-content').forEach(s => s.hidden = s.id !== `tab-${name}`);
  loadCurrentTab();
}

function loadCurrentTab() {
  if (CURRENT_TAB === 'shipments') loadShipments();
  else if (CURRENT_TAB === 'exceptions') loadExceptions();
  else if (CURRENT_TAB === 'audit') loadAudit();
  else if (CURRENT_TAB === 'deliveries') loadDeliveries();
}

// ─── Generic table utilities ─────────────────────────────────────────────────

function badge(value) {
  const span = document.createElement('span');
  span.className = `badge badge-${value}`;
  span.textContent = value;
  return span;
}

function text(value) {
  const span = document.createElement('span');
  span.textContent = value ?? '—';
  return span;
}

function td(...children) {
  const cell = document.createElement('td');
  children.forEach(c => typeof c === 'string' ? cell.append(document.createTextNode(c)) : cell.appendChild(c));
  return cell;
}

function showTable(loadingId, emptyId, tableId, tbodyId, rows, buildRow) {
  document.getElementById(loadingId).hidden = true;
  document.getElementById(emptyId).hidden = rows.length > 0;
  document.getElementById(tableId).hidden = rows.length === 0;
  const tbody = document.getElementById(tbodyId);
  tbody.replaceChildren(...rows.map(buildRow));
}

function setLoading(loadingId, emptyId, tableId) {
  document.getElementById(loadingId).hidden = false;
  document.getElementById(emptyId).hidden = true;
  document.getElementById(tableId).hidden = true;
}

function fmtTime(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString();
}

// ─── Shipments ───────────────────────────────────────────────────────────────

async function loadShipments() {
  setLoading('ship-loading', 'ship-empty', 'ship-table');
  const params = new URLSearchParams();
  const status = document.getElementById('ship-status-filter').value;
  const ref = document.getElementById('ship-ref-filter').value.trim();
  if (status) params.set('status', status);
  if (ref) params.set('reference', ref);
  try {
    const ships = await api('GET', `/shipments?${params}`);
    showTable('ship-loading', 'ship-empty', 'ship-table', 'ship-tbody', ships, buildShipRow);
  } catch (e) {
    document.getElementById('ship-loading').hidden = true;
    showStatus(`Error loading shipments: ${e.message}`, 'error');
    if (e.status === 401) signOut();
  }
}

function buildShipRow(ship) {
  const tr = document.createElement('tr');
  const actBtn = document.createElement('button');
  actBtn.className = 'btn btn-sm';
  actBtn.textContent = 'Events';
  actBtn.onclick = () => showIngestEventModal(ship);
  tr.append(
    td(text(ship.reference)),
    td(badge(ship.status)),
    td(text(ship.last_location)),
    td(String(ship.version)),
    td(fmtTime(ship.created_at)),
  );
  return tr;
}

async function createShipment(reference) {
  return api('POST', '/shipments', { reference });
}

// ─── Exceptions ──────────────────────────────────────────────────────────────

async function loadExceptions() {
  setLoading('exc-loading', 'exc-empty', 'exc-table');
  const params = new URLSearchParams();
  const status = document.getElementById('exc-status-filter').value;
  const severity = document.getElementById('exc-severity-filter').value;
  const assignee = document.getElementById('exc-assignee-filter').value.trim();
  if (status) params.set('status', status);
  if (severity) params.set('severity', severity);
  if (assignee) params.set('assignee', assignee);
  try {
    const excs = await api('GET', `/exceptions?${params}`);
    showTable('exc-loading', 'exc-empty', 'exc-table', 'exc-tbody', excs, buildExcRow);
  } catch (e) {
    document.getElementById('exc-loading').hidden = true;
    showStatus(`Error loading exceptions: ${e.message}`, 'error');
    if (e.status === 401) signOut();
  }
}

function buildExcRow(exc) {
  const tr = document.createElement('tr');
  const ackBtn = document.createElement('button');
  ackBtn.className = 'btn btn-sm';
  ackBtn.textContent = 'Ack';
  ackBtn.disabled = exc.status !== 'open';
  ackBtn.onclick = () => mutateException(exc, 'acknowledge');

  const resolveBtn = document.createElement('button');
  resolveBtn.className = 'btn btn-sm btn-success';
  resolveBtn.textContent = 'Resolve';
  resolveBtn.disabled = exc.status === 'resolved';
  resolveBtn.onclick = () => mutateException(exc, 'resolve');

  const noteBtn = document.createElement('button');
  noteBtn.className = 'btn btn-sm btn-ghost';
  noteBtn.textContent = 'Note';
  noteBtn.onclick = () => showNoteModal(exc);

  const actionsCell = document.createElement('td');
  actionsCell.style.whiteSpace = 'nowrap';
  [ackBtn, resolveBtn, noteBtn].forEach(b => actionsCell.appendChild(b));

  tr.append(
    td(text(exc.shipment_id)),
    td(badge(exc.severity)),
    td(badge(exc.status)),
    td(text(exc.assignee)),
    td(fmtTime(exc.opened_at)),
    actionsCell,
  );
  return tr;
}

async function mutateException(exc, action, extraValues = {}) {
  try {
    await api('PATCH', `/exceptions/${exc.id}`, {
      expected_version: exc.version,
      action,
      ...extraValues,
    });
    showStatus(`Exception ${action}d successfully.`, 'success');
    loadExceptions();
  } catch (e) {
    if (e.status === 409) {
      showStatus('Version conflict – reloading…', 'error');
      loadExceptions();
    } else {
      showStatus(`Error: ${e.message}`, 'error');
    }
  }
}

function showNoteModal(exc) {
  const textarea = el('textarea', { id: 'note-text', rows: '4', placeholder: 'Describe the action taken…' });
  const errP = el('p', { class: 'modal-error', id: 'note-error' });
  const cancelBtn = el('button', { class: 'btn btn-ghost' }, 'Cancel');
  cancelBtn.onclick = closeModal;
  const submitBtn = el('button', { class: 'btn btn-primary', id: 'note-submit' }, 'Add Note');
  const actions = el('div', { class: 'modal-actions' });
  actions.append(cancelBtn, submitBtn);
  const lbl = el('label', {});
  lbl.append(document.createTextNode('Note'), textarea);
  openModal('Add Note', [lbl, errP, actions]);

  submitBtn.onclick = async () => {
    const note = textarea.value.trim();
    if (!note) { errP.textContent = 'Note is required.'; return; }
    try {
      await api('PATCH', `/exceptions/${exc.id}`, {
        expected_version: exc.version,
        action: 'note',
        note,
      });
      closeModal();
      showStatus('Note added.', 'success');
      loadExceptions();
    } catch (e) {
      errP.textContent = e.message;
    }
  };
}

// ─── Ingest event modal ───────────────────────────────────────────────────────

function showIngestEventModal(ship) {
  const evIdInput    = el('input',  { id: 'ev-id', type: 'text', placeholder: 'unique-event-id-001', required: '' });
  const evTypeSelect = el('select', { id: 'ev-type' });
  [['picked_up','Picked Up'],['in_transit','In Transit'],['delayed','Delayed'],
   ['delivered','Delivered'],['cancelled','Cancelled']].forEach(([v, t]) => {
    const opt = el('option', { value: v }, t);
    evTypeSelect.appendChild(opt);
  });
  const evTimeInput  = el('input',  { id: 'ev-time', type: 'number', step: 'any' });
  evTimeInput.value  = String(Date.now() / 1000);
  const evLocInput   = el('input',  { id: 'ev-location', type: 'text', placeholder: 'Chicago, IL' });
  const evDetInput   = el('input',  { id: 'ev-details',  type: 'text', placeholder: 'Weather delay' });
  const errP         = el('p', { class: 'modal-error', id: 'ev-error' });
  const cancelBtn    = el('button', { class: 'btn btn-ghost' }, 'Cancel');
  cancelBtn.onclick  = closeModal;
  const submitBtn    = el('button', { class: 'btn btn-primary', id: 'ev-submit' }, 'Ingest');
  const actions      = el('div', { class: 'modal-actions' });
  actions.append(cancelBtn, submitBtn);

  const lblId   = labelWrap('Event ID', evIdInput);
  const lblType = labelWrap('Event Type', evTypeSelect);
  const lblTime = labelWrap('Event Time (unix timestamp)', evTimeInput);
  const lblLoc  = labelWrap('Location (optional)', evLocInput);
  const lblDet  = labelWrap('Details (optional)', evDetInput);

  openModal('Ingest Carrier Event', [lblId, lblType, lblTime, lblLoc, lblDet, errP, actions]);

  submitBtn.onclick = async () => {
    errP.textContent = '';
    const event_id   = evIdInput.value.trim();
    const event_type = evTypeSelect.value;
    const event_time = parseFloat(evTimeInput.value);
    const location   = evLocInput.value.trim() || null;
    const details    = evDetInput.value.trim() || null;
    if (!event_id) { errP.textContent = 'Event ID is required.'; return; }
    try {
      await api('POST', `/shipments/${ship.id}/events`, { event_id, event_type, event_time, location, details });
      closeModal();
      showStatus('Event ingested.', 'success');
      loadShipments();
    } catch (e) {
      errP.textContent = e.message;
    }
  };
}

// ─── Create shipment modal ────────────────────────────────────────────────────

function showCreateShipModal() {
  const refInput  = el('input', { id: 'ship-ref', type: 'text', placeholder: 'SHIP-2026-001', required: '' });
  const errP      = el('p', { class: 'modal-error', id: 'ship-error' });
  const cancelBtn = el('button', { class: 'btn btn-ghost' }, 'Cancel');
  cancelBtn.onclick = closeModal;
  const submitBtn = el('button', { class: 'btn btn-primary', id: 'ship-submit' }, 'Create');
  const actions   = el('div', { class: 'modal-actions' });
  actions.append(cancelBtn, submitBtn);
  openModal('New Shipment', [labelWrap('Reference', refInput), errP, actions]);

  submitBtn.onclick = async () => {
    errP.textContent = '';
    const reference = refInput.value.trim();
    if (!reference) { errP.textContent = 'Reference is required.'; return; }
    try {
      await createShipment(reference);
      closeModal();
      showStatus('Shipment created.', 'success');
      loadShipments();
    } catch (e) {
      errP.textContent = e.message;
    }
  };
}

// ─── Audit ───────────────────────────────────────────────────────────────────

async function loadAudit() {
  setLoading('audit-loading', 'audit-empty', 'audit-table');
  const params = new URLSearchParams();
  const et = document.getElementById('audit-entity-type').value.trim();
  const eid = document.getElementById('audit-entity-id').value.trim();
  if (et) params.set('entity_type', et);
  if (eid) params.set('entity_id', eid);
  try {
    const entries = await api('GET', `/audit?${params}`);
    showTable('audit-loading', 'audit-empty', 'audit-table', 'audit-tbody', entries, buildAuditRow);
  } catch (e) {
    document.getElementById('audit-loading').hidden = true;
    showStatus(`Error loading audit: ${e.message}`, 'error');
  }
}

function buildAuditRow(entry) {
  const tr = document.createElement('tr');
  // entity cell: combine entity_type + entity_id via textContent only
  const entityCell = document.createElement('td');
  const typeSpan = document.createElement('span');
  typeSpan.textContent = entry.entity_type;
  const sep = document.createTextNode(':');
  const idSpan = document.createElement('span');
  idSpan.textContent = entry.entity_id;
  entityCell.append(typeSpan, sep, idSpan);
  tr.append(
    td(fmtTime(entry.created_at)),
    td(text(entry.actor)),
    td(text(entry.action)),
    entityCell,
  );
  return tr;
}

// ─── Deliveries ───────────────────────────────────────────────────────────────

async function loadDeliveries() {
  setLoading('del-loading', 'del-empty', 'del-table');
  const params = new URLSearchParams();
  const status = document.getElementById('del-status-filter').value;
  if (status) params.set('status', status);
  try {
    const dels = await api('GET', `/deliveries?${params}`);
    showTable('del-loading', 'del-empty', 'del-table', 'del-tbody', dels, buildDelRow);
  } catch (e) {
    document.getElementById('del-loading').hidden = true;
    showStatus(`Error loading deliveries: ${e.message}`, 'error');
  }
}

function buildDelRow(del) {
  const tr = document.createElement('tr');
  const actionsCell = document.createElement('td');
  if (del.status === 'dead') {
    const btn = document.createElement('button');
    btn.className = 'btn btn-sm';
    btn.textContent = 'Replay';
    btn.onclick = async () => {
      try {
        await api('POST', `/deliveries/${del.id}/replay`, { now: Date.now() / 1000 });
        showStatus('Delivery replayed.', 'success');
        loadDeliveries();
      } catch (e) {
        showStatus(`Replay failed: ${e.message}`, 'error');
      }
    };
    actionsCell.appendChild(btn);
  }
  tr.append(
    td(text(del.event_type)),
    td(badge(del.status)),
    td(String(del.attempts)),
    td(fmtTime(del.next_attempt_at)),
    td(text(del.last_error)),
    actionsCell,
  );
  return tr;
}

// ─── DOM helpers (all content set via textContent / createElement) ────────────

/**
 * Create an element with optional attributes and a single text child.
 * attrs values are set via setAttribute (safe for all attribute names).
 * textContent, if provided, is set via .textContent (safe DOM assignment).
 */
function el(tag, attrs = {}, textContent = undefined) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    node.setAttribute(k, v);
  }
  if (textContent !== undefined) node.textContent = textContent;
  return node;
}

/** Wrap a form control in a <label> with a safe text prefix. */
function labelWrap(labelText, control) {
  const lbl = document.createElement('label');
  lbl.appendChild(document.createTextNode(labelText));
  lbl.appendChild(control);
  return lbl;
}

// ─── Modal ────────────────────────────────────────────────────────────────────

/**
 * Open the modal.  title is a plain string (set via textContent).
 * nodes is an array of DOM nodes built entirely with createElement/textContent.
 * Modal content is built entirely from DOM nodes; no unsafe injection is used.
 */
function openModal(title, nodes) {
  document.getElementById('modal-title').textContent = title;
  const body = document.getElementById('modal-body');
  body.replaceChildren(...nodes);
  document.getElementById('modal-backdrop').hidden = false;
}

function closeModal() {
  document.getElementById('modal-backdrop').hidden = true;
  document.getElementById('modal-body').replaceChildren();
}

// ─── Bootstrap ────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  // Login
  document.getElementById('login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const token = document.getElementById('token-input').value.trim();
    const errEl = document.getElementById('login-error');
    errEl.hidden = true;
    try {
      // Quick validation: try listing shipments
      const savedToken = TOKEN;
      TOKEN = token;
      await api('GET', '/shipments');
      signIn(token);
    } catch (err) {
      TOKEN = '';
      errEl.textContent = err.status === 401 ? 'Invalid token – check your credentials.' : `Error: ${err.message}`;
      errEl.hidden = false;
    }
  });

  // Sign-out
  document.getElementById('signout-btn').addEventListener('click', signOut);

  // Tab switching
  qsa(document, '.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  // Create shipment button
  document.getElementById('create-ship-btn').addEventListener('click', showCreateShipModal);

  // Filter buttons
  document.getElementById('ship-filter-btn').addEventListener('click', loadShipments);
  document.getElementById('exc-filter-btn').addEventListener('click', loadExceptions);
  document.getElementById('audit-filter-btn').addEventListener('click', loadAudit);
  document.getElementById('del-filter-btn').addEventListener('click', loadDeliveries);

  // Modal close
  document.getElementById('modal-close').addEventListener('click', closeModal);
  document.getElementById('modal-backdrop').addEventListener('click', (e) => {
    if (e.target === document.getElementById('modal-backdrop')) closeModal();
  });

  // Restore session
  if (isAuthenticated()) {
    document.getElementById('login-screen').hidden = true;
    document.getElementById('dashboard').hidden = false;
    loadCurrentTab();
  }
});
