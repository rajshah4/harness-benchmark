/* Incident Operations Center — browser application */
"use strict";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
const state = {
  incidents: [],
  selectedId: null,
  filters: { status: "", severity: "" },
  feedback: "",
  _detailVersion: null,
};

// ---------------------------------------------------------------------------
// DOM refs
// ---------------------------------------------------------------------------
const $ = (id) => document.getElementById(id);

const incidentList  = $("incident-list");
const emptyMsg      = $("empty-msg");
const detailPane    = $("detail-pane");
const feedbackEl    = $("feedback");
const statusFilter  = $("status-filter");
const severityFilter = $("severity-filter");
const refreshBtn    = $("refresh-btn");
const closeDetail   = $("close-detail");

const detailTitle   = $("detail-title");
const detailSev     = $("detail-sev");
const detailStatus  = $("detail-status");
const detailId      = $("detail-id");
const detailFp      = $("detail-fp");
const detailCount   = $("detail-count");
const detailEsc     = $("detail-esc");
const detailSla     = $("detail-sla");
const detailCreated = $("detail-created");
const detailUpdated = $("detail-updated");
const ownerInput    = $("owner-input");
const assignBtn     = $("assign-btn");
const ackBtn        = $("acknowledge-btn");
const resolveBtn    = $("resolve-btn");
const timeline      = $("timeline");

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function fmtTs(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString();
}

function fmtSla(ts) {
  if (!ts) return "—";
  const diff = ts - Date.now() / 1000;
  const abs  = Math.abs(diff) | 0;
  const sign = diff < 0 ? "−" : "+";
  const mins = (abs / 60 | 0);
  const secs = abs % 60;
  const rel  = mins > 0 ? `${sign}${mins}m ${secs}s` : `${sign}${secs}s`;
  return `${fmtTs(ts)} (${rel})`;
}

let _feedbackTimer = null;
function showFeedback(msg, error = false) {
  state.feedback = msg;
  feedbackEl.textContent = msg;
  feedbackEl.className   = "feedback" + (error ? " feedback-error" : " feedback-ok");
  feedbackEl.classList.add("feedback-visible");
  clearTimeout(_feedbackTimer);
  _feedbackTimer = setTimeout(() => {
    feedbackEl.classList.remove("feedback-visible");
  }, 4000);
}

function sevClass(sev) {
  return { P1: "sev-p1", P2: "sev-p2", P3: "sev-p3", P4: "sev-p4" }[sev] || "";
}

function statusClass(st) {
  return { open: "st-open", acknowledged: "st-ack", resolved: "st-resolved" }[st] || "";
}

// ---------------------------------------------------------------------------
// API calls
// ---------------------------------------------------------------------------
async function apiFetch(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const body = await res.json().catch(() => ({}));
  return { ok: res.ok, status: res.status, body };
}

// ---------------------------------------------------------------------------
// Refresh — load list and summary
// ---------------------------------------------------------------------------
async function refresh() {
  const qs = new URLSearchParams();
  if (state.filters.status)   qs.set("status",   state.filters.status);
  if (state.filters.severity) qs.set("severity", state.filters.severity);

  const [listRes, summaryRes] = await Promise.all([
    apiFetch(`/api/incidents?${qs}`),
    apiFetch("/api/summary"),
  ]);

  if (listRes.ok) {
    state.incidents = listRes.body;
    renderList();
  }
  if (summaryRes.ok) {
    renderSummary(summaryRes.body);
  }

  // Re-render detail if one is selected (data may have changed).
  if (state.selectedId) {
    const existing = state.incidents.find((i) => i.id === state.selectedId);
    if (existing) {
      // Re-fetch detail for events.
      const dr = await apiFetch(`/api/incidents/${state.selectedId}`);
      if (dr.ok) renderDetail(dr.body);
    } else {
      closeDetailPane();
    }
  }
}

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------
function renderSummary(data) {
  $("sum-total-n").textContent = data.total ?? 0;
  $("sum-open-n").textContent  = data.open  ?? 0;
  $("sum-ack-n").textContent   = data.acknowledged ?? 0;
  $("sum-res-n").textContent   = data.resolved ?? 0;
  $("sum-p1-n").textContent    = data.P1 ?? 0;
  $("sum-p2-n").textContent    = data.P2 ?? 0;
  $("sum-p3-n").textContent    = data.P3 ?? 0;
  $("sum-p4-n").textContent    = data.P4 ?? 0;
}

// ---------------------------------------------------------------------------
// Incident list
// ---------------------------------------------------------------------------
function renderList() {
  incidentList.innerHTML = "";
  const items = state.incidents;
  emptyMsg.classList.toggle("hidden", items.length > 0);

  items.forEach((inc) => {
    const row = document.createElement("div");
    row.className = "incident-row" + (inc.id === state.selectedId ? " selected" : "");
    row.setAttribute("role", "listitem");
    row.setAttribute("data-testid", "incident-row");
    row.setAttribute("data-id", inc.id);
    row.tabIndex = 0;

    row.innerHTML = `
      <span class="sev-badge ${sevClass(inc.severity)}">${inc.severity}</span>
      <div class="row-body">
        <p class="row-title"></p>
        <p class="row-meta">
          <span class="status-badge ${statusClass(inc.status)}">${inc.status}</span>
          ${inc.owner ? `<span class="row-owner">@${inc.owner}</span>` : ""}
          <span class="row-count" title="Alert count">${inc.alert_count > 1 ? "×" + inc.alert_count : ""}</span>
        </p>
      </div>
    `;
    row.querySelector(".row-title").textContent = inc.title;

    row.addEventListener("click", () => selectIncident(inc.id));
    row.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") selectIncident(inc.id);
    });
    incidentList.appendChild(row);
  });
}

// ---------------------------------------------------------------------------
// Incident detail
// ---------------------------------------------------------------------------
async function selectIncident(id) {
  state.selectedId = id;
  // Highlight the selected row.
  document.querySelectorAll(".incident-row").forEach((r) => {
    r.classList.toggle("selected", r.dataset.id === id);
  });

  const res = await apiFetch(`/api/incidents/${id}`);
  if (!res.ok) {
    showFeedback(res.body.error || "Could not load incident", true);
    return;
  }
  renderDetail(res.body);
}

function renderDetail(inc) {
  state._detailVersion = inc.version;

  detailTitle.textContent    = inc.title;
  detailSev.textContent      = inc.severity;
  detailSev.className        = `sev-badge ${sevClass(inc.severity)}`;
  detailStatus.textContent   = inc.status;
  detailStatus.className     = `status-badge ${statusClass(inc.status)}`;
  detailId.textContent       = inc.id;
  detailFp.textContent       = inc.fingerprint;
  detailCount.textContent    = inc.alert_count;
  detailEsc.textContent      = inc.escalation_level;
  detailSla.textContent      = fmtSla(inc.sla_deadline);
  detailCreated.textContent  = fmtTs(inc.created_at);
  detailUpdated.textContent  = fmtTs(inc.updated_at);
  ownerInput.value           = inc.owner || "";

  // Show/hide action buttons based on status.
  const isResolved = inc.status === "resolved";
  const isOpen     = inc.status === "open";
  ackBtn.disabled     = !isOpen;
  resolveBtn.disabled = isResolved;
  assignBtn.disabled  = isResolved;

  // Timeline.
  timeline.innerHTML = "";
  (inc.events || []).slice().reverse().forEach((ev) => {
    const li = document.createElement("li");
    li.className = "timeline-item";
    const details = ev.details && Object.keys(ev.details).length
      ? `<pre class="event-details">${JSON.stringify(ev.details, null, 2)}</pre>`
      : "";
    li.innerHTML = `
      <span class="event-type">${ev.type}</span>
      <span class="event-ts">${fmtTs(ev.timestamp)}</span>
      ${details}
    `;
    timeline.appendChild(li);
  });

  detailPane.classList.remove("hidden");
}

function closeDetailPane() {
  state.selectedId = null;
  state._detailVersion = null;
  detailPane.classList.add("hidden");
  document.querySelectorAll(".incident-row").forEach((r) => r.classList.remove("selected"));
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------
async function patchIncident(patch) {
  if (!state.selectedId) return;
  const payload = { expected_version: state._detailVersion, ...patch };
  const res = await apiFetch(`/api/incidents/${state.selectedId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
  if (res.ok) {
    renderDetail(res.body);
    showFeedback("Updated successfully");
    // Refresh list in background.
    refresh();
  } else if (res.status === 409) {
    showFeedback("Conflict: someone else updated this incident. Refreshing…", true);
    await refresh();
  } else {
    showFeedback(res.body.error || "Update failed", true);
  }
}

assignBtn.addEventListener("click", () => {
  patchIncident({ owner: ownerInput.value.trim() });
});
ownerInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") patchIncident({ owner: ownerInput.value.trim() });
});

ackBtn.addEventListener("click", () => patchIncident({ status: "acknowledged" }));
resolveBtn.addEventListener("click", () => patchIncident({ status: "resolved" }));

// ---------------------------------------------------------------------------
// Filter events
// ---------------------------------------------------------------------------
statusFilter.addEventListener("change", () => {
  state.filters.status = statusFilter.value;
  refresh();
});
severityFilter.addEventListener("change", () => {
  state.filters.severity = severityFilter.value;
  refresh();
});

refreshBtn.addEventListener("click", () => refresh());
closeDetail.addEventListener("click", () => closeDetailPane());

// ---------------------------------------------------------------------------
// window.incidentOps contract
// ---------------------------------------------------------------------------
window.incidentOps = {
  refresh,
  selectIncident,
  getState() {
    return {
      filters:    { ...state.filters },
      selectedId: state.selectedId,
      incidents:  state.incidents.map((i) => ({ ...i })),
      feedback:   state.feedback,
    };
  },
};

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
refresh();

