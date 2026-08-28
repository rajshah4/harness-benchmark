/**
 * Freight Control Tower dashboard.
 *
 * Security: all carrier-supplied values are written via .textContent to
 * prevent XSS injection.  No carrier string ever reaches .className, .href,
 * .src, or any other DOM attribute — badge() always receives a class suffix
 * produced by the hardcoded statusClass() lookup table, never a raw API value.
 *
 * Authentication: the bearer token is stored only in sessionStorage and
 * never embedded in the source.
 */

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let TOKEN = sessionStorage.getItem("ft_token") || "";
let CURRENT_TAB = "shipments";

// ---------------------------------------------------------------------------
// API helper
// ---------------------------------------------------------------------------
async function api(method, path, body) {
  const opts = {
    method,
    headers: { "Authorization": "Bearer " + TOKEN, "Content-Type": "application/json" },
  };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  const json = await res.json();
  if (!res.ok) {
    const msg = json.message || json.error || "Request failed";
    throw Object.assign(new Error(msg), { status: res.status, code: json.error });
  }
  return json;
}

// ---------------------------------------------------------------------------
// Login screen
// ---------------------------------------------------------------------------
const loginScreen = document.getElementById("login-screen");
const appEl = document.getElementById("app");
const loginForm = document.getElementById("login-form");
const loginError = document.getElementById("login-error");
const tokenInput = document.getElementById("token-input");

function showError(el, msg) {
  el.textContent = msg;
  el.hidden = false;
}
function clearError(el) { el.hidden = true; el.textContent = ""; }

loginForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  clearError(loginError);
  TOKEN = tokenInput.value.trim();
  if (!TOKEN) { showError(loginError, "Token is required"); return; }
  try {
    await api("GET", "/api/shipments");
    sessionStorage.setItem("ft_token", TOKEN);
    showApp();
  } catch (err) {
    if (err.status === 401 || err.status === 403) {
      showError(loginError, "Invalid token — check your credentials");
    } else {
      showError(loginError, "Could not connect: " + err.message);
    }
  }
});

function showApp() {
  loginScreen.hidden = true;
  appEl.hidden = false;
  switchTab("shipments");
}

function showLogin() {
  TOKEN = "";
  sessionStorage.removeItem("ft_token");
  appEl.hidden = true;
  loginScreen.hidden = false;
  tokenInput.value = "";
}

document.getElementById("sign-out-btn").addEventListener("click", showLogin);

if (TOKEN) {
  // Try auto-login with stored token
  api("GET", "/api/shipments").then(showApp).catch(() => showLogin());
}

// ---------------------------------------------------------------------------
// Tab navigation
// ---------------------------------------------------------------------------
const tabButtons = document.querySelectorAll(".nav-btn");
const tabs = document.querySelectorAll(".tab");

tabButtons.forEach(btn => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

function switchTab(name) {
  CURRENT_TAB = name;
  tabButtons.forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  tabs.forEach(t => { t.hidden = t.id !== "tab-" + name; });
  if (name === "shipments") loadShipments();
  else if (name === "exceptions") loadExceptions();
  else if (name === "audit") loadAudit();
  else if (name === "deliveries") loadDeliveries();
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function setStatus(el, msg, isError) {
  el.textContent = msg;
  el.className = "status-msg" + (isError ? " error" : "");
}

function fmtTime(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString();
}

function badge(text, cls) {
  const span = document.createElement("span");
  span.className = "badge badge-" + cls;
  span.textContent = text;
  return span;
}

// All class suffixes passed to badge() must originate from this table.
// No carrier-supplied string ever reaches .className or any DOM attribute.
function statusClass(s) {
  return {
    // exception / shipment status
    open: "danger", acknowledged: "warn", resolved: "ok",
    delayed: "danger", delivered: "ok", cancelled: "muted",
    in_transit: "info", created: "muted",
    // delivery status
    pending: "info", claimed: "warn", failed: "warn", dead: "danger",
    // severity levels
    P1: "danger", P2: "warn", P3: "info",
  }[s] || "muted";
}

// ---------------------------------------------------------------------------
// Shipments tab
// ---------------------------------------------------------------------------
const shipmentsStatus = document.getElementById("shipments-status");
const shipmentsList = document.getElementById("shipments-list");
const shipmentStatusFilter = document.getElementById("shipment-status-filter");

document.getElementById("refresh-shipments").addEventListener("click", loadShipments);
shipmentStatusFilter.addEventListener("change", loadShipments);

async function loadShipments() {
  setStatus(shipmentsStatus, "Loading…");
  const status = shipmentStatusFilter.value;
  const qs = status ? "?status=" + encodeURIComponent(status) : "";
  try {
    const items = await api("GET", "/api/shipments" + qs);
    shipmentsList.replaceChildren();
    if (!items.length) { setStatus(shipmentsStatus, "No shipments found."); return; }
    setStatus(shipmentsStatus, items.length + " shipment(s)");
    items.forEach(s => shipmentsList.append(renderShipment(s)));
  } catch (err) {
    handleError(shipmentsStatus, err);
  }
}

function renderShipment(s) {
  const card = document.createElement("article");
  card.className = "card";

  const h3 = document.createElement("h3");
  h3.textContent = s.reference;

  const meta = document.createElement("p");
  meta.className = "card-meta";
  meta.append(badge(s.status, statusClass(s.status)));
  if (s.last_location) {
    const loc = document.createElement("span");
    loc.textContent = " · " + s.last_location;
    meta.append(loc);
  }

  const detail = document.createElement("p");
  detail.className = "card-detail";
  detail.textContent = "ID: " + s.id;

  if (s.active_exception_id) {
    const exc = document.createElement("p");
    exc.className = "card-detail warn";
    exc.textContent = "⚠ Active exception";
    card.append(h3, meta, detail, exc);
  } else {
    card.append(h3, meta, detail);
  }
  return card;
}

// ---------------------------------------------------------------------------
// Exceptions tab
// ---------------------------------------------------------------------------
const exceptionsStatus = document.getElementById("exceptions-status");
const exceptionsList = document.getElementById("exceptions-list");
const excStatusFilter = document.getElementById("exc-status-filter");
const excSeverityFilter = document.getElementById("exc-severity-filter");
const excAssigneeFilter = document.getElementById("exc-assignee-filter");

document.getElementById("refresh-exceptions").addEventListener("click", loadExceptions);
excStatusFilter.addEventListener("change", loadExceptions);
excSeverityFilter.addEventListener("change", loadExceptions);

async function loadExceptions() {
  setStatus(exceptionsStatus, "Loading…");
  const params = new URLSearchParams();
  if (excStatusFilter.value) params.set("status", excStatusFilter.value);
  if (excSeverityFilter.value) params.set("severity", excSeverityFilter.value);
  if (excAssigneeFilter.value) params.set("assignee", excAssigneeFilter.value);
  const qs = params.toString() ? "?" + params.toString() : "";
  try {
    const items = await api("GET", "/api/exceptions" + qs);
    exceptionsList.replaceChildren();
    if (!items.length) { setStatus(exceptionsStatus, "No exceptions found."); return; }
    setStatus(exceptionsStatus, items.length + " exception(s)");
    items.forEach(ex => exceptionsList.append(renderException(ex)));
  } catch (err) {
    handleError(exceptionsStatus, err);
  }
}

function renderException(ex) {
  const card = document.createElement("article");
  card.className = "card exception-card";

  const header = document.createElement("div");
  header.className = "card-header";

  const title = document.createElement("h3");
  title.textContent = "Exception";
  // Both cls arguments come from statusClass() — no carrier value reaches .className
  const sevBadge = badge(ex.severity, statusClass(ex.severity));
  const stBadge = badge(ex.status, statusClass(ex.status));
  title.append(" ", sevBadge, " ", stBadge);

  const shipRef = document.createElement("p");
  shipRef.className = "card-detail";
  shipRef.textContent = "Shipment: " + ex.shipment_id;

  const assignee = document.createElement("p");
  assignee.className = "card-detail";
  assignee.textContent = ex.assignee ? "Assignee: " + ex.assignee : "Unassigned";

  const opened = document.createElement("p");
  opened.className = "card-detail";
  opened.textContent = "Opened: " + fmtTime(ex.opened_at);

  header.append(title, shipRef, assignee, opened);

  // Notes
  if (ex.notes && ex.notes.length) {
    const notesEl = document.createElement("ul");
    notesEl.className = "notes-list";
    ex.notes.forEach(n => {
      const li = document.createElement("li");
      // Use textContent throughout — carrier values are not trusted as HTML
      const meta = document.createElement("span");
      meta.className = "note-meta";
      meta.textContent = n.actor + " · " + fmtTime(n.created_at);
      const txt = document.createElement("span");
      txt.textContent = n.note;
      li.append(meta, " ", txt);
      notesEl.append(li);
    });
    header.append(notesEl);
  }

  // Action buttons (only for non-resolved exceptions)
  if (ex.status !== "resolved") {
    const actions = document.createElement("div");
    actions.className = "card-actions";

    if (ex.status === "open") {
      const ackBtn = document.createElement("button");
      ackBtn.className = "btn-secondary";
      ackBtn.textContent = "Acknowledge";
      ackBtn.addEventListener("click", () => openMutateDialog(ex, "acknowledge"));
      actions.append(ackBtn);
    }

    const assignBtn = document.createElement("button");
    assignBtn.className = "btn-secondary";
    assignBtn.textContent = "Assign";
    assignBtn.addEventListener("click", () => openMutateDialog(ex, "assign"));

    const noteBtn = document.createElement("button");
    noteBtn.className = "btn-secondary";
    noteBtn.textContent = "Add note";
    noteBtn.addEventListener("click", () => openMutateDialog(ex, "add_note"));

    const resolveBtn = document.createElement("button");
    resolveBtn.className = "btn-danger";
    resolveBtn.textContent = "Resolve";
    resolveBtn.addEventListener("click", () => openMutateDialog(ex, "resolve"));

    actions.append(assignBtn, noteBtn, resolveBtn);
    header.append(actions);
  }

  card.append(header);
  return card;
}

// Mutation dialog
const mutateDialog = document.getElementById("mutate-dialog");
const mutateForm = document.getElementById("mutate-form");
const mutateExcId = document.getElementById("mutate-exc-id");
const mutateExcVersion = document.getElementById("mutate-exc-version");
const mutateFields = document.getElementById("mutate-fields");
const mutateError = document.getElementById("mutate-error");
const mutateDialogTitle = document.getElementById("mutate-dialog-title");

document.getElementById("mutate-cancel").addEventListener("click", () => {
  mutateDialog.close();
});

function openMutateDialog(ex, action) {
  mutateExcId.value = ex.id;
  mutateExcVersion.value = ex.version;
  mutateFields.replaceChildren();
  clearError(mutateError);

  mutateDialogTitle.textContent = {
    acknowledge: "Acknowledge exception",
    assign: "Assign exception",
    add_note: "Add note",
    resolve: "Resolve exception",
  }[action] || "Update exception";

  // Hidden action field
  const hiddenAction = document.createElement("input");
  hiddenAction.type = "hidden";
  hiddenAction.name = "action";
  hiddenAction.value = action;
  mutateFields.append(hiddenAction);

  if (action === "assign") {
    const lbl = document.createElement("label");
    lbl.textContent = "Assignee";
    const inp = document.createElement("input");
    inp.type = "text";
    inp.name = "assignee";
    inp.required = true;
    inp.value = ex.assignee || "";
    lbl.append(inp);
    mutateFields.append(lbl);
  } else if (action === "add_note") {
    const lbl = document.createElement("label");
    lbl.textContent = "Note";
    const inp = document.createElement("textarea");
    inp.name = "note";
    inp.required = true;
    inp.rows = 3;
    lbl.append(inp);
    mutateFields.append(lbl);
  } else if (action === "resolve") {
    const p = document.createElement("p");
    p.textContent = "Mark this exception as resolved?";
    mutateFields.append(p);
  } else if (action === "acknowledge") {
    const p = document.createElement("p");
    p.textContent = "Acknowledge this exception?";
    mutateFields.append(p);
  }

  mutateDialog.showModal();
}

mutateForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  clearError(mutateError);

  const excId = mutateExcId.value;
  const version = parseInt(mutateExcVersion.value, 10);
  const fd = new FormData(mutateForm);
  const action = fd.get("action");
  const body = { expected_version: version, action, actor: "operator" };
  if (fd.get("assignee")) body.assignee = fd.get("assignee");
  if (fd.get("note")) body.note = fd.get("note");

  try {
    await api("POST", "/api/exceptions/" + encodeURIComponent(excId) + "/mutate", body);
    mutateDialog.close();
    loadExceptions();
  } catch (err) {
    if (err.code === "version_conflict") {
      showError(mutateError, "Version conflict — reload and try again");
    } else if (err.status === 401 || err.status === 403) {
      showError(mutateError, "Not authorised: " + err.message);
    } else {
      showError(mutateError, err.message);
    }
  }
});

// ---------------------------------------------------------------------------
// Audit tab
// ---------------------------------------------------------------------------
const auditStatus = document.getElementById("audit-status");
const auditBody = document.getElementById("audit-body");
const auditTypeFilter = document.getElementById("audit-type-filter");

document.getElementById("refresh-audit").addEventListener("click", loadAudit);
auditTypeFilter.addEventListener("change", loadAudit);

async function loadAudit() {
  setStatus(auditStatus, "Loading…");
  const qs = auditTypeFilter.value
    ? "?entity_type=" + encodeURIComponent(auditTypeFilter.value)
    : "";
  try {
    const items = await api("GET", "/api/audit" + qs);
    auditBody.replaceChildren();
    if (!items.length) { setStatus(auditStatus, "No audit entries found."); return; }
    setStatus(auditStatus, items.length + " entries");
    items.forEach(a => {
      const tr = document.createElement("tr");
      [fmtTime(a.created_at), a.actor, a.action, a.entity_type, a.entity_id]
        .forEach(val => {
          const td = document.createElement("td");
          td.textContent = val;  // textContent is XSS-safe; API values never reach attributes
          tr.append(td);
        });
      auditBody.append(tr);
    });
  } catch (err) {
    handleError(auditStatus, err);
  }
}

// ---------------------------------------------------------------------------
// Deliveries tab
// ---------------------------------------------------------------------------
const deliveriesStatus = document.getElementById("deliveries-status");
const deliveriesBody = document.getElementById("deliveries-body");
const delStatusFilter = document.getElementById("del-status-filter");

document.getElementById("refresh-deliveries").addEventListener("click", loadDeliveries);
delStatusFilter.addEventListener("change", loadDeliveries);

async function loadDeliveries() {
  setStatus(deliveriesStatus, "Loading…");
  const qs = delStatusFilter.value
    ? "?status=" + encodeURIComponent(delStatusFilter.value)
    : "";
  try {
    const items = await api("GET", "/api/deliveries" + qs);
    deliveriesBody.replaceChildren();
    if (!items.length) { setStatus(deliveriesStatus, "No deliveries found."); return; }
    setStatus(deliveriesStatus, items.length + " delivery records");
    items.forEach(d => {
      const tr = document.createElement("tr");
      [d.id.slice(0, 8) + "…", d.event_type, d.status,
       d.attempts + "/" + d.max_attempts, d.last_error || "—"]
        .forEach(val => {
          const td = document.createElement("td");
          td.textContent = val;
          tr.append(td);
        });
      deliveriesBody.append(tr);
    });
  } catch (err) {
    handleError(deliveriesStatus, err);
  }
}

// ---------------------------------------------------------------------------
// Error handling
// ---------------------------------------------------------------------------
function handleError(statusEl, err) {
  if (err.status === 401) {
    setStatus(statusEl, "Session expired — please sign in again.", true);
    setTimeout(showLogin, 1500);
  } else if (err.status === 403) {
    setStatus(statusEl, "Not authorised: " + err.message, true);
  } else {
    setStatus(statusEl, "Error: " + err.message, true);
  }
}
