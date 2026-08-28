/* ── state ── */
const state = {
  filters: { status: "", severity: "", owner: "" },
  selectedId: null,
  incidents: [],
  feedback: "",
};

/* ── DOM refs ── */
const elList        = document.getElementById("incident-list");
const elDetail      = document.getElementById("incident-detail");
const elPlaceholder = document.getElementById("detail-placeholder");
const elFeedback    = document.getElementById("feedback");
const elTimeline    = document.getElementById("timeline");
const elOwnerInput  = document.getElementById("owner-input");
const elAckBtn      = document.getElementById("ack-btn");
const elResolveBtn  = document.getElementById("resolve-btn");
const elAssignBtn   = document.getElementById("assign-btn");
const statusFilter  = document.getElementById("status-filter");
const severityFilter= document.getElementById("severity-filter");
const refreshBtn    = document.getElementById("refresh-btn");

/* ── helpers ── */
function ts(epoch) {
  return new Date(epoch * 1000).toLocaleString();
}

function sevClass(s) {
  return { P1: "sev-p1", P2: "sev-p2", P3: "sev-p3", P4: "sev-p4" }[s] || "";
}

function setFeedback(msg, isError = false) {
  state.feedback = msg;
  elFeedback.textContent = msg;
  elFeedback.className = "feedback" + (isError ? " feedback-error" : msg ? " feedback-ok" : "");
}

async function apiFetch(url, opts = {}) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const body = await res.json();
  return { ok: res.ok, status: res.status, body };
}

/* ── summary ── */
async function loadSummary() {
  const { ok, body } = await apiFetch("/api/summary");
  if (!ok) return;
  const s = body;
  const bs = s.by_status || {};
  const bsev = s.by_severity || {};
  document.querySelector("#summary-total .count").textContent  = s.total;
  document.querySelector("#summary-open .count").textContent   = bs.open || 0;
  document.querySelector("#summary-acked .count").textContent  = bs.acknowledged || 0;
  document.querySelector("#summary-resolved .count").textContent = bs.resolved || 0;
  document.querySelector("#summary-p1 .count").textContent = bsev.P1 || 0;
  document.querySelector("#summary-p2 .count").textContent = bsev.P2 || 0;
  document.querySelector("#summary-p3 .count").textContent = bsev.P3 || 0;
  document.querySelector("#summary-p4 .count").textContent = bsev.P4 || 0;
}

/* ── incident list ── */
async function loadIncidents() {
  const qs = new URLSearchParams();
  if (state.filters.status)   qs.set("status",   state.filters.status);
  if (state.filters.severity) qs.set("severity", state.filters.severity);
  if (state.filters.owner)    qs.set("owner",    state.filters.owner);

  const { ok, body } = await apiFetch("/api/incidents?" + qs);
  if (!ok) { setFeedback("Failed to load incidents.", true); return; }

  state.incidents = body;
  renderList();
}

function renderList() {
  if (!state.incidents.length) {
    elList.innerHTML = '<p class="empty-msg">No incidents match the current filters.</p>';
    return;
  }
  elList.replaceChildren(...state.incidents.map(renderRow));
}

function renderRow(inc) {
  const row = document.createElement("div");
  row.className = "incident-row" + (inc.id === state.selectedId ? " selected" : "");
  row.setAttribute("data-testid", "incident-row");
  row.setAttribute("data-id", inc.id);
  row.setAttribute("role", "button");
  row.setAttribute("tabindex", "0");

  const sla = inc.sla_deadline ? `SLA ${ts(inc.sla_deadline)}` : "";
  row.innerHTML =
    `<div class="row-left">
       <span class="sev-badge ${sevClass(inc.severity)}">${inc.severity}</span>
     </div>
     <div class="row-center">
       <div class="row-title"></div>
       <div class="row-meta">
         <span class="status-pill status-${inc.status}">${inc.status}</span>
         ${inc.owner ? `<span class="owner-tag">${escHtml(inc.owner)}</span>` : ""}
         ${inc.alert_count > 1 ? `<span class="alert-count">${inc.alert_count}×</span>` : ""}
       </div>
     </div>
     <div class="row-right">
       <span class="sla-label">${escHtml(sla)}</span>
     </div>`;

  row.querySelector(".row-title").textContent = inc.title;
  row.addEventListener("click",  () => selectIncident(inc.id));
  row.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") selectIncident(inc.id); });
  return row;
}

function escHtml(s) {
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

/* ── incident detail ── */
async function selectIncident(id) {
  state.selectedId = id;
  setFeedback("");

  // Highlight in list
  document.querySelectorAll(".incident-row").forEach(r => {
    r.classList.toggle("selected", r.dataset.id === id);
  });

  const { ok, body } = await apiFetch(`/api/incidents/${id}`);
  if (!ok) { setFeedback("Failed to load incident details.", true); return; }

  renderDetail(body);
}

function renderDetail(inc) {
  elDetail.classList.remove("hidden");
  elPlaceholder.classList.add("hidden");

  document.getElementById("detail-severity").textContent = inc.severity;
  document.getElementById("detail-severity").className   = `sev-badge ${sevClass(inc.severity)}`;
  document.getElementById("detail-status").textContent   = inc.status;
  document.getElementById("detail-status").className     = `status-badge status-${inc.status}`;
  document.getElementById("detail-title").textContent    = inc.title;
  document.getElementById("detail-meta").innerHTML =
    `<span>Owner: <strong>${inc.owner ? escHtml(inc.owner) : "—"}</strong></span>` +
    `<span>Alerts: <strong>${inc.alert_count}</strong></span>` +
    `<span>Escalation: <strong>${inc.escalation_level}</strong></span>` +
    `<span>Version: <strong>${inc.version}</strong></span>` +
    `<span>Created: <strong>${ts(inc.created_at)}</strong></span>` +
    `<span>SLA: <strong>${inc.sla_deadline ? ts(inc.sla_deadline) : "—"}</strong></span>`;

  elOwnerInput.value = inc.owner || "";

  // Show/hide action buttons based on valid transitions
  const isResolved = inc.status === "resolved";
  const isOpen     = inc.status === "open";
  const isAcked    = inc.status === "acknowledged";
  elAckBtn.disabled     = !(isOpen);
  elResolveBtn.disabled = isResolved;
  elAckBtn.style.display     = isAcked || isResolved ? "none" : "";
  elResolveBtn.style.display = isResolved ? "none" : "";

  // Store version for optimistic locking
  elDetail.dataset.version = inc.version;
  elDetail.dataset.id      = inc.id;

  // Render timeline
  renderTimeline(inc.events || []);
}

function renderTimeline(events) {
  if (!events.length) {
    elTimeline.innerHTML = '<li class="timeline-empty">No events recorded.</li>';
    return;
  }
  elTimeline.replaceChildren(
    ...[...events].reverse().map(ev => {
      const li = document.createElement("li");
      li.className = "timeline-item";
      const det = ev.details ? JSON.stringify(ev.details, null, 0) : "";
      li.innerHTML =
        `<span class="ev-type">${escHtml(ev.type)}</span>` +
        `<span class="ev-ts">${ts(ev.timestamp)}</span>` +
        (det && det !== "{}" ? `<pre class="ev-det">${escHtml(det)}</pre>` : "");
      return li;
    })
  );
}

/* ── actions ── */
async function doUpdate(patch) {
  const id      = elDetail.dataset.id;
  const version = parseInt(elDetail.dataset.version, 10);
  if (!id) return;

  const { ok, status, body } = await apiFetch(`/api/incidents/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ expected_version: version, ...patch }),
  });

  if (status === 409) {
    setFeedback("Conflict: another change was made. Refreshing…", true);
    await refresh();
    return;
  }
  if (!ok) {
    setFeedback(`Error: ${body.error || "Unknown error"}`, true);
    return;
  }

  setFeedback("Updated successfully.");
  // Re-fetch detail with events
  const detail = await apiFetch(`/api/incidents/${id}`);
  if (detail.ok) renderDetail(detail.body);
  await loadSummary();
  await loadIncidents();
}

elAssignBtn.addEventListener("click", () => {
  doUpdate({ owner: elOwnerInput.value.trim() });
});

elAckBtn.addEventListener("click", () => {
  doUpdate({ status: "acknowledged" });
});

elResolveBtn.addEventListener("click", () => {
  doUpdate({ status: "resolved" });
});

/* ── filters ── */
statusFilter.addEventListener("change", () => {
  state.filters.status = statusFilter.value;
  loadIncidents();
});

severityFilter.addEventListener("change", () => {
  state.filters.severity = severityFilter.value;
  loadIncidents();
});

refreshBtn.addEventListener("click", () => refresh());

/* ── public contract ── */
async function refresh() {
  await Promise.all([loadSummary(), loadIncidents()]);
  if (state.selectedId) {
    const { ok, body } = await apiFetch(`/api/incidents/${state.selectedId}`);
    if (ok) renderDetail(body);
  }
}

window.incidentOps = {
  refresh,
  selectIncident,
  getState() {
    return {
      filters: { ...state.filters },
      selectedId: state.selectedId,
      incidents: [...state.incidents],
      feedback: state.feedback,
    };
  },
};

/* ── boot ── */
refresh();

