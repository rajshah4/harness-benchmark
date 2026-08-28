/* ----------------------------------------------------------------
 * Freight Control Tower — Dashboard
 * All carrier-supplied values use .textContent to prevent XSS.
 * ---------------------------------------------------------------- */

// ── Element refs ───────────────────────────────────────────────────
const loginScreen   = document.getElementById('login-screen');
const dashboard     = document.getElementById('dashboard');
const loginForm     = document.getElementById('login-form');
const tokenInput    = document.getElementById('token-input');
const loginFeedback = document.getElementById('login-feedback');
const signOutBtn    = document.getElementById('sign-out-btn');
const refreshBtn    = document.getElementById('refresh-btn');
const boardFeedback = document.getElementById('board-feedback');
const shipmentsList = document.getElementById('shipments-list');
const exceptionsList= document.getElementById('exceptions-list');
const filterStatus  = document.getElementById('filter-status');
const filterSeverity= document.getElementById('filter-severity');
const filterAssignee= document.getElementById('filter-assignee');

const excDialog     = document.getElementById('exc-dialog');
const excDetail     = document.getElementById('exc-detail');
const excMutateForm = document.getElementById('exc-mutate-form');
const excAction     = document.getElementById('exc-action');
const assigneeGroup = document.getElementById('assignee-group');
const noteGroup     = document.getElementById('note-group');
const excAssignee   = document.getElementById('exc-assignee');
const excNote       = document.getElementById('exc-note');
const excActor      = document.getElementById('exc-actor');
const mutateFeedback= document.getElementById('mutate-feedback');
const closeDialogBtn= document.getElementById('close-dialog-btn');

// ── State ──────────────────────────────────────────────────────────
let currentToken = sessionStorage.getItem('freight_token') || '';
let currentException = null;

// ── API helper ─────────────────────────────────────────────────────
async function api(method, path, body) {
  const opts = {
    method,
    headers: { 'Authorization': 'Bearer ' + currentToken }
  };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, status: res.status, data };
}

// ── Auth ───────────────────────────────────────────────────────────
function showLogin() {
  loginScreen.hidden = false;
  dashboard.hidden   = true;
  loginFeedback.textContent = '';
}

function showDashboard() {
  loginScreen.hidden = true;
  dashboard.hidden   = false;
  refresh();
}

loginForm.addEventListener('submit', async e => {
  e.preventDefault();
  const token = tokenInput.value.trim();
  if (!token) return;
  currentToken = token;
  loginFeedback.textContent = 'Checking…';

  // Verify by fetching shipments (if 401 → bad token)
  const { ok, status } = await api('GET', '/api/shipments');
  if (ok || status !== 401) {
    sessionStorage.setItem('freight_token', token);
    loginFeedback.textContent = '';
    showDashboard();
  } else {
    loginFeedback.textContent = '✗ Invalid token — try again.';
    currentToken = '';
  }
});

signOutBtn.addEventListener('click', () => {
  sessionStorage.removeItem('freight_token');
  currentToken = '';
  showLogin();
});

// ── Filtering ──────────────────────────────────────────────────────
refreshBtn.addEventListener('click', refresh);

// ── Data loading ───────────────────────────────────────────────────
async function refresh() {
  setBoardFeedback('loading', 'Loading…');

  const statusFilter   = filterStatus.value;
  const severityFilter = filterSeverity.value;
  const assigneeFilter = filterAssignee.value.trim();

  let shipsUrl = '/api/shipments';
  if (statusFilter) shipsUrl += '?status=' + encodeURIComponent(statusFilter);

  let excsUrl = '/api/exceptions';
  const excsParams = [];
  if (severityFilter) excsParams.push('severity=' + encodeURIComponent(severityFilter));
  if (assigneeFilter) excsParams.push('assignee=' + encodeURIComponent(assigneeFilter));
  if (excsParams.length) excsUrl += '?' + excsParams.join('&');

  const [shipsRes, excsRes] = await Promise.all([
    api('GET', shipsUrl),
    api('GET', excsUrl),
  ]);

  if (!shipsRes.ok) {
    if (shipsRes.status === 401) { showLogin(); return; }
    setBoardFeedback('error', '✗ ' + (shipsRes.data.error || 'Failed to load shipments'));
    return;
  }

  const ships = shipsRes.data || [];
  const excs  = excsRes.ok ? (excsRes.data || []) : [];

  // Build exception lookup
  const excByShip = {};
  for (const ex of excs) {
    if (!excByShip[ex.shipment_id]) excByShip[ex.shipment_id] = [];
    excByShip[ex.shipment_id].push(ex);
  }

  renderShipments(ships);
  renderExceptions(excs);

  const count = ships.length;
  setBoardFeedback('', count ? `${count} shipment(s) · ${excs.length} exception(s)` : 'No shipments found.');
}

// ── Render helpers ─────────────────────────────────────────────────
function setText(el, text) {
  el.textContent = text;
}

function tag(name, cls) {
  const el = document.createElement(name);
  if (cls) el.className = cls;
  return el;
}

function chip(label, val) {
  const wrap = tag('span', 'chip chip--' + slugify(val));
  wrap.textContent = label + ': ' + val;
  return wrap;
}

function slugify(s) {
  return String(s).toLowerCase().replace(/[^a-z0-9]/g, '-');
}

function renderShipments(ships) {
  shipmentsList.replaceChildren();
  if (!ships.length) {
    const empty = tag('p', 'empty');
    setText(empty, 'No shipments match your filters.');
    shipmentsList.append(empty);
    return;
  }
  for (const s of ships) {
    const card = tag('article', 'card card--' + slugify(s.status));
    const title = tag('h3');
    setText(title, s.reference);

    const meta = tag('div', 'card-meta');
    meta.append(chip('Status', s.status));
    if (s.last_location) {
      const loc = tag('span', 'loc');
      setText(loc, '📍 ' + s.last_location);
      meta.append(loc);
    }
    if (s.active_exception_id) {
      const flag = tag('span', 'chip chip--delayed');
      setText(flag, '⚠ Exception open');
      meta.append(flag);
    }
    card.append(title, meta);
    shipmentsList.append(card);
  }
}

function renderExceptions(excs) {
  exceptionsList.replaceChildren();
  if (!excs.length) {
    const empty = tag('p', 'empty');
    setText(empty, 'No exceptions match your filters.');
    exceptionsList.append(empty);
    return;
  }
  for (const ex of excs) {
    const card = tag('article', 'card card--exc card--exc-' + slugify(ex.status));
    const title = tag('h3');
    setText(title, ex.shipment_id.slice(0, 8) + '…');

    const meta = tag('div', 'card-meta');
    meta.append(chip('Status', ex.status));
    meta.append(chip('Severity', ex.severity));
    if (ex.assignee) {
      const aTag = tag('span', 'chip');
      setText(aTag, '👤 ' + ex.assignee);
      meta.append(aTag);
    }

    // Notes summary
    if (ex.notes && ex.notes.length) {
      const noteLine = tag('p', 'note-count');
      setText(noteLine, ex.notes.length + ' note(s)');
      card.append(title, meta, noteLine);
    } else {
      card.append(title, meta);
    }

    // Open detail dialog on click (for non-resolved exceptions)
    if (ex.status !== 'resolved') {
      card.setAttribute('role', 'button');
      card.setAttribute('tabindex', '0');
      const open = () => openExcDialog(ex);
      card.addEventListener('click', open);
      card.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') open(); });
    }

    exceptionsList.append(card);
  }
}

// ── Exception detail dialog ────────────────────────────────────────
function openExcDialog(ex) {
  currentException = ex;
  mutateFeedback.textContent = '';

  // Build detail view
  excDetail.replaceChildren();
  const dl = tag('dl', 'exc-detail-list');
  const addRow = (label, val) => {
    const dt = tag('dt'); setText(dt, label);
    const dd = tag('dd'); setText(dd, val || '—');
    dl.append(dt, dd);
  };
  addRow('ID', ex.id);
  addRow('Shipment', ex.shipment_id);
  addRow('Status', ex.status);
  addRow('Severity', ex.severity);
  addRow('Assignee', ex.assignee);
  addRow('Opened', new Date(ex.opened_at * 1000).toLocaleString());
  if (ex.resolved_at) addRow('Resolved', new Date(ex.resolved_at * 1000).toLocaleString());

  if (ex.notes && ex.notes.length) {
    const notesHeader = tag('h3');
    setText(notesHeader, 'Notes');
    excDetail.append(dl, notesHeader);
    for (const n of ex.notes) {
      const np = tag('p', 'note');
      setText(np, `[${n.actor}] ${n.note}`);
      excDetail.append(np);
    }
  } else {
    excDetail.append(dl);
  }

  excAction.value = 'acknowledge';
  updateActionFields();
  excDialog.showModal();
}

excAction.addEventListener('change', updateActionFields);

function updateActionFields() {
  const a = excAction.value;
  assigneeGroup.hidden = a !== 'assign';
  noteGroup.hidden = a !== 'add_note';
}

closeDialogBtn.addEventListener('click', () => excDialog.close());

excMutateForm.addEventListener('submit', async e => {
  e.preventDefault();
  if (!currentException) return;

  const action = excAction.value;
  const actor  = excActor.value.trim() || 'operator';
  const body   = {
    action,
    actor,
    expected_version: currentException.version,
  };
  if (action === 'assign') body.assignee = excAssignee.value.trim();
  if (action === 'add_note') body.note = excNote.value.trim();

  mutateFeedback.textContent = 'Saving…';

  const { ok, status, data } = await api(
    'POST',
    `/api/exceptions/${currentException.id}/mutate`,
    body
  );

  if (ok) {
    mutateFeedback.textContent = '✓ Done.';
    currentException = data;
    setTimeout(() => {
      excDialog.close();
      refresh();
    }, 600);
  } else if (status === 409 && data.type === 'version_conflict') {
    mutateFeedback.textContent = '✗ Conflict: someone else updated this exception. Refreshing…';
    setTimeout(() => { excDialog.close(); refresh(); }, 1200);
  } else if (status === 403) {
    mutateFeedback.textContent = '✗ Authorisation error: your role cannot perform this action.';
  } else {
    mutateFeedback.textContent = '✗ ' + (data.error || 'Unexpected error');
  }
});

// ── Feedback helper ────────────────────────────────────────────────
function setBoardFeedback(type, msg) {
  boardFeedback.className = 'feedback' + (type ? ' feedback--' + type : '');
  boardFeedback.textContent = msg;
}

// ── Boot ───────────────────────────────────────────────────────────
if (currentToken) {
  showDashboard();
} else {
  showLogin();
}
