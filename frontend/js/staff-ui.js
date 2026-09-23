// Shared UI helpers for the Manager and Super Admin portals.
// Loaded after ../js/config.js and ../hr-portal/js/api.js.

function escapeHtml(str) {
  return String(str ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

function initials(name) {
  const parts = (name || "").trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "?";
  return (parts[0][0] + (parts.length > 1 ? parts[parts.length - 1][0] : "")).toUpperCase();
}

function stageBadge(stage) {
  return `<span class="badge badge-${String(stage).toLowerCase()}">${escapeHtml(String(stage).replaceAll("_", " "))}</span>`;
}
function roleBadge(role) {
  return `<span class="badge badge-role-${String(role).toLowerCase()}">${escapeHtml(roleLabel(role))}</span>`;
}
function activeBadge(isActive) {
  return isActive ? `<span class="badge badge-active">Active</span>`
                  : `<span class="badge badge-inactive">Deactivated</span>`;
}

function fmtDate(iso) { return iso ? new Date(iso).toLocaleDateString() : "—"; }
function fmtDateTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return `${d.toLocaleDateString()} ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
}

function openModal(id) { document.getElementById(id).classList.remove("hidden"); }
function closeModal(id) { document.getElementById(id).classList.add("hidden"); }
function setError(id, msg) { const el = document.getElementById(id); if (el) el.textContent = msg || ""; }

function mountUserChip() {
  const who = document.getElementById("whoami");
  const av = document.getElementById("whoamiAvatar");
  const role = document.getElementById("whoamiRole");
  if (who) who.textContent = getName();
  if (av) av.textContent = getName().charAt(0).toUpperCase();
  if (role) role.textContent = roleLabel(getRole());
}

function selectOptions(items, selected, placeholder) {
  let html = placeholder ? `<option value="">${escapeHtml(placeholder)}</option>` : "";
  for (const it of items) {
    html += `<option value="${it.value}" ${String(it.value) === String(selected) ? "selected" : ""}>${escapeHtml(it.label)}</option>`;
  }
  return html;
}

// ---------------------------------------------------------------------------
// Confirmation dialog. Resolves true when confirmed. With `typed`, the user
// has to type that exact text before the confirm button enables — used for
// anything that cannot be undone.
// ---------------------------------------------------------------------------
function confirmDialog({ title, html, confirmLabel = "Confirm", danger = true, typed = null }) {
  let modal = document.getElementById("uiConfirmModal");
  if (!modal) {
    modal = document.createElement("div");
    modal.id = "uiConfirmModal";
    modal.className = "modal-backdrop hidden";
    document.body.appendChild(modal);
  }
  return new Promise(resolve => {
    modal.innerHTML = `
      <div class="modal">
        <h3>${escapeHtml(title)}</h3>
        <div style="color:var(--muted);font-size:0.88rem;">${html}</div>
        ${typed ? `<label style="margin-top:12px;">Type <strong>${escapeHtml(typed)}</strong> to confirm</label>
                   <input type="text" id="uiConfirmInput" autocomplete="off">` : ""}
        <div class="modal-actions">
          <button type="button" class="btn btn-outline" id="uiConfirmCancel">Cancel</button>
          <button type="button" class="btn ${danger ? "btn-danger" : "btn-primary"}" id="uiConfirmOk" ${typed ? "disabled" : ""}>${escapeHtml(confirmLabel)}</button>
        </div>
      </div>`;
    modal.classList.remove("hidden");
    const done = (v) => { modal.classList.add("hidden"); resolve(v); };
    modal.querySelector("#uiConfirmCancel").onclick = () => done(false);
    modal.querySelector("#uiConfirmOk").onclick = () => done(true);
    if (typed) {
      const input = modal.querySelector("#uiConfirmInput");
      input.focus();
      input.addEventListener("input", () => {
        modal.querySelector("#uiConfirmOk").disabled = input.value !== typed;
      });
    }
  });
}

// ---------------------------------------------------------------------------
// One-time credentials box (new staff account, new candidate, password reset).
// ---------------------------------------------------------------------------
const COPY_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
  stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9"
  width="12" height="12" rx="2"></rect><path d="M5 15V5a2 2 0 0 1 2-2h10"></path></svg>`;
const TICK_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
  stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"></path></svg>`;

async function copyText(value, btn) {
  try {
    await navigator.clipboard.writeText(value);
  } catch {
    window.prompt("Copy this value:", value);
    return;
  }
  if (!btn) return;
  const original = btn.innerHTML;
  btn.classList.add("copied");
  btn.innerHTML = btn.classList.contains("cred-copy") ? TICK_ICON : "Copied!";
  setTimeout(() => { btn.classList.remove("copied"); btn.innerHTML = original; }, 1400);
}

function renderCredentials(container, { title, sub, rows, note, doneLabel = "Done", onDone }) {
  container.classList.remove("hidden");
  container.innerHTML = `
    <div class="cred-head">
      <span class="cred-check" aria-hidden="true">&#10003;</span>
      <div class="cred-head-text">
        <div class="cred-title">${escapeHtml(title)}</div>
        <div class="cred-sub">${escapeHtml(sub || "")}</div>
      </div>
    </div>
    <div class="cred-list">
      ${rows.map((r, i) => {
        const safe = escapeHtml(r.value);
        const cell = r.link
          ? `<a class="cred-value is-link" href="${safe}" target="_blank" rel="noopener noreferrer" title="${safe}">${safe}</a>`
          : `<div class="cred-value" title="${safe}">${safe}</div>`;
        return `<div class="cred-row">
          <div class="cred-label">${escapeHtml(r.label)}</div>${cell}
          <button type="button" class="cred-copy" data-i="${i}" aria-label="Copy ${escapeHtml(r.label)}" title="Copy">${COPY_ICON}</button>
        </div>`;
      }).join("")}
    </div>
    <div class="cred-note">${escapeHtml(note || "The password is shown only once — copy it before closing.")}</div>
    <div class="cred-actions">
      <button type="button" class="btn btn-outline" id="credCopyAll">Copy all details</button>
      <button type="button" class="btn btn-primary" id="credDone">${escapeHtml(doneLabel)}</button>
    </div>`;
  container.querySelectorAll(".cred-copy").forEach(btn => {
    btn.onclick = () => copyText(rows[Number(btn.dataset.i)].value, btn);
  });
  container.querySelector("#credCopyAll").onclick = (e) =>
    copyText(rows.map(r => `${r.label}: ${r.value}`).join("\n"), e.currentTarget);
  container.querySelector("#credDone").onclick = () => { container.classList.add("hidden"); onDone && onDone(); };
}

// ---------------------------------------------------------------------------
// Staff audit trail
// ---------------------------------------------------------------------------
const ACTION_LABELS = {
  STAFF_CREATED: "Account created",
  STAFF_DELETED: "Account deleted",
  STAFF_DEACTIVATED: "Account deactivated",
  STAFF_ACTIVATED: "Account reactivated",
  STAFF_RENAMED: "Account renamed",
  STAFF_PASSWORD_RESET: "Password reset",
  STAFF_MOVED: "Moved to another team",
  STAFF_MOVED_OUT: "Moved out of team",
  CANDIDATE_ASSIGNED: "Candidate assigned",
  CANDIDATE_REASSIGNED: "Candidate reassigned",
  CANDIDATE_MOVED_OUT: "Candidate moved out of team",
};
function actionLabel(a) { return ACTION_LABELS[a] || a.replaceAll("_", " ").toLowerCase(); }

function renderAudit(container, entries) {
  if (!entries.length) {
    container.innerHTML = `<div class="empty-state">Nothing on record yet.</div>`;
    return;
  }
  container.innerHTML = `<table>
    <thead><tr><th>When</th><th>Who</th><th>Action</th><th>Target</th><th>Detail</th></tr></thead>
    <tbody>${entries.map(e => `<tr>
      <td style="white-space:nowrap;">${fmtDateTime(e.created_at)}</td>
      <td><div class="name-cell"><span class="row-avatar">${escapeHtml(initials(e.actor_name))}</span>
        <div>${escapeHtml(e.actor_name || "—")}<div class="muted-text">${escapeHtml(roleLabel(e.actor_role))}</div></div></div></td>
      <td><span class="badge badge-action">${escapeHtml(actionLabel(e.action))}</span></td>
      <td>${escapeHtml(e.target_name || "—")}<div class="muted-text">${e.target_type === "CANDIDATE" ? "Candidate" : "Staff"}</div></td>
      <td style="color:var(--muted);font-size:0.85rem;">${escapeHtml(e.detail || "")}</td>
    </tr>`).join("")}</tbody></table>`;
}
