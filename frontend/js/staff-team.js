// Staff accounts page, shared by the admins (mode "admin": Managers and HR
// Executives across all teams; the Master Admin also sees the Super Admins
// and every account's current password) and a Manager (mode "manager":
// their own HR Executives). The HTML shell provides the sidebar and an
// empty #pageContent.

async function initTeamPage(mode) {
  const isAdmin = mode === "admin";
  requireAuth(isAdmin ? ADMIN_ROLES : ["MANAGER"]);
  mountUserChip();
  const isMaster = isMasterAdmin();

  const BASE = isAdmin ? "/admin/staff" : "/manager/team";
  const root = document.getElementById("pageContent");
  const loginLink = new URL("../index.html", location.href).href;
  const passwordHeader = isMaster ? "Password" : "First password";
  let staff = [];
  let myId = null;

  root.innerHTML = `
    <div class="page-header">
      <h2>${isAdmin ? "Staff Accounts" : "My Team"}</h2>
      <button class="btn btn-primary" id="addBtn">+ ${isAdmin ? "Add Staff" : "Add HR Executive"}</button>
    </div>
    ${isMaster ? `
    <div class="card section-card" id="adminsCard">
      <div class="section-title"><h3>Super Admins</h3></div>
      <table><thead><tr><th>Name</th><th>Email</th><th>Status</th><th>Owns</th><th>${passwordHeader}</th><th></th></tr></thead>
      <tbody id="adminsBody"></tbody></table>
      <div class="empty-state hidden" id="adminsEmpty">No Super Admins yet.</div>
    </div>` : ""}
    ${isAdmin ? `
    <div class="card section-card ${isMaster ? "section-gap" : ""}" id="managersCard">
      <div class="section-title"><h3>Managers</h3></div>
      <table><thead><tr><th>Name</th><th>Email</th><th>Status</th><th>Team</th><th>Owns</th><th>${passwordHeader}</th><th></th></tr></thead>
      <tbody id="managersBody"></tbody></table>
      <div class="empty-state hidden" id="managersEmpty">No Managers yet.</div>
    </div>` : ""}
    <div class="card section-card ${isAdmin ? "section-gap" : ""}">
      <div class="section-title"><h3>HR Executives</h3></div>
      <table><thead><tr><th>Name</th><th>Email</th><th>Status</th>${isAdmin ? "<th>Manager</th>" : ""}<th>Owns</th><th>${passwordHeader}</th><th></th></tr></thead>
      <tbody id="hrBody"></tbody></table>
      <div class="empty-state hidden" id="hrEmpty">No HR Executives yet.</div>
    </div>

    <div class="modal-backdrop hidden" id="createModal">
      <div class="modal">
        <h3 id="createTitle">${isAdmin ? "Add a staff account" : "Add an HR Executive"}</h3>
        <p style="color:var(--muted);font-size:0.85rem;margin-top:-6px;">
          A password is generated and shown once. They must replace it on first login.
        </p>
        <form id="createForm">
          <label>Full name</label>
          <input type="text" id="cName" required>
          <label>Email</label>
          <input type="email" id="cEmail" required>
          ${isAdmin ? `
          <label>Role</label>
          <select id="cRole">
            <option value="HR">HR Executive</option>
            <option value="MANAGER">Manager</option>
            ${isMaster ? `<option value="SUPER_ADMIN">Super Admin</option>` : ""}
          </select>
          <div id="cManagerWrap">
            <label>Reports to</label>
            <select id="cManager"></select>
            <div class="muted-text" style="margin-top:4px;">An HR Executive without a Manager is visible only to you until moved into a team.</div>
          </div>` : ""}
          <div class="modal-actions">
            <button type="button" class="btn btn-outline" id="createCancel">Cancel</button>
            <button type="submit" class="btn btn-primary">Create account</button>
          </div>
          <div class="error-msg" id="createError"></div>
        </form>
        <div class="credential-box hidden" id="createCred"></div>
      </div>
    </div>

    <div class="modal-backdrop hidden" id="credModal">
      <div class="modal"><div class="credential-box" id="credBox"></div></div>
    </div>`;

  // --- data -----------------------------------------------------------------
  async function load() {
    if (myId === null) myId = (await apiFetch("/hr/me")).id;
    staff = await apiFetch(BASE);
    render();
  }

  function superAdmins() { return staff.filter(s => s.role === "SUPER_ADMIN"); }
  function managers() { return staff.filter(s => s.role === "MANAGER"); }
  function hrs() { return staff.filter(s => s.role === "HR"); }

  function passwordCell(s) {
    if (isMaster) {
      // The Master Admin sees the password in force. Accounts whose password
      // was last set before it was recorded show nothing until a reset.
      if (s.current_password) {
        return `<span class="temp-pass">${escapeHtml(s.current_password)}</span>
                <button type="button" class="cred-copy" title="Copy" data-copy="${escapeHtml(s.current_password)}">${COPY_ICON}</button>
                ${s.must_reset_password ? `<div class="pending-tag">Not yet changed</div>` : ""}`;
      }
      return `<span class="muted-text" title="Set before passwords were recorded. Reset it to see it.">Not recorded</span>`;
    }
    if (!s.must_reset_password) return `<span class="muted-text">Set by user</span>`;
    if (!s.temp_password) return `<span class="pending-tag">Change pending</span>`;
    return `<span class="temp-pass">${escapeHtml(s.temp_password)}</span>
            <button type="button" class="cred-copy" title="Copy" data-copy="${escapeHtml(s.temp_password)}">${COPY_ICON}</button>
            <div class="pending-tag">Not yet changed</div>`;
  }

  // Whether the caller may act on this row at all. Only the Master Admin
  // touches a Super Admin; nobody acts on their own row here.
  function canManage(s) {
    if (s.id === myId || s.role === "MASTER_ADMIN") return false;
    if (s.role === "SUPER_ADMIN") return isMaster;
    return true;
  }

  function actionButtons(s) {
    if (!canManage(s)) return `<span class="muted-text">${s.id === myId ? "You" : "—"}</span>`;
    const blocked = s.candidate_count > 0 || s.team_size > 0;
    const why = s.candidate_count > 0 ? `Still owns ${s.candidate_count} candidate(s). Reassign first.`
              : s.team_size > 0 ? `Still leads ${s.team_size} HR Executive(s). Move them first.` : "";
    return `<div class="row-actions">
      <button class="btn btn-outline btn-small" data-act="reset" data-id="${s.id}">Reset password</button>
      <button class="btn btn-outline btn-small" data-act="toggle" data-id="${s.id}">${s.is_active ? "Deactivate" : "Reactivate"}</button>
      ${isAdmin && s.role === "HR" ? `<button class="btn btn-outline btn-small" data-act="move" data-id="${s.id}">Move</button>` : ""}
      <button class="btn btn-danger btn-small" data-act="delete" data-id="${s.id}" ${blocked ? `disabled title="${escapeHtml(why)}"` : ""}>Delete</button>
    </div>`;
  }

  function row(s) {
    const managerCell = isAdmin && s.role === "HR"
      ? `<td>${s.manager_name ? escapeHtml(s.manager_name) : `<span class="pending-tag">No team</span>`}</td>` : "";
    const teamCell = s.role === "MANAGER" ? `<td>${s.team_size} HR</td>` : "";
    return `<tr class="${s.is_active ? "" : "inactive-row"}">
      <td><div class="name-cell"><span class="row-avatar">${escapeHtml(initials(s.name))}</span>${escapeHtml(s.name)}</div></td>
      <td>${escapeHtml(s.email)}</td>
      <td>${activeBadge(s.is_active)}</td>
      ${teamCell}${managerCell}
      <td>${s.candidate_count} candidate${s.candidate_count === 1 ? "" : "s"}</td>
      <td>${passwordCell(s)}</td>
      <td>${actionButtons(s)}</td>
    </tr>`;
  }

  function render() {
    if (isMaster) {
      const a = superAdmins();
      document.getElementById("adminsBody").innerHTML = a.map(row).join("");
      document.getElementById("adminsEmpty").classList.toggle("hidden", a.length > 0);
    }
    if (isAdmin) {
      const m = managers();
      document.getElementById("managersBody").innerHTML = m.map(row).join("");
      document.getElementById("managersEmpty").classList.toggle("hidden", m.length > 0);
    }
    const h = hrs();
    document.getElementById("hrBody").innerHTML = h.map(row).join("");
    document.getElementById("hrEmpty").classList.toggle("hidden", h.length > 0);

    root.querySelectorAll("[data-copy]").forEach(b => b.onclick = () => copyText(b.dataset.copy, b));
    root.querySelectorAll("[data-act]").forEach(b => b.onclick = () => act(b.dataset.act, Number(b.dataset.id)));
  }

  // --- actions ----------------------------------------------------------------
  function byId(id) { return staff.find(s => s.id === id); }

  function showCredentials(opts) {
    openModal("credModal");
    renderCredentials(document.getElementById("credBox"), { ...opts, onDone: () => { closeModal("credModal"); load(); } });
  }

  async function act(kind, id) {
    const s = byId(id);
    if (!s) return;
    try {
      if (kind === "reset") {
        const ok = await confirmDialog({
          title: `Reset ${s.name}'s password?`, danger: false, confirmLabel: "Reset password",
          html: `Their current password stops working immediately. A new one is generated and shown to you once; they must change it on next login.`,
        });
        if (!ok) return;
        const data = await apiFetch(`${BASE}/${id}/reset-password`, { method: "POST" });
        showCredentials({
          title: "Password reset", sub: `Send these login details to ${s.email}.`,
          rows: [{ label: "Login ID", value: s.email }, { label: "Password", value: data.temp_password },
                 { label: "Login link", value: loginLink, link: true }],
        });
      } else if (kind === "toggle") {
        const deactivating = s.is_active;
        const ok = await confirmDialog({
          title: `${deactivating ? "Deactivate" : "Reactivate"} ${s.name}?`, danger: deactivating,
          confirmLabel: deactivating ? "Deactivate" : "Reactivate",
          html: deactivating
            ? `They can no longer log in. Their ${s.candidate_count} candidate(s) stay with them and remain visible to their Manager, who can reassign them.`
            : `They can log in again with their existing password.`,
        });
        if (!ok) return;
        await apiFetch(`${BASE}/${id}`, { method: "PATCH", body: JSON.stringify({ is_active: !deactivating }) });
        await load();
      } else if (kind === "move") {
        const options = managers().filter(m => m.is_active)
          .map(m => `<option value="${m.id}" ${m.id === s.manager_id ? "selected" : ""}>${escapeHtml(m.name)}</option>`).join("");
        const ok = await confirmDialog({
          title: `Move ${s.name} to another team`, danger: false, confirmLabel: "Move",
          html: `<label>New Manager</label><select id="moveSelect" class="inline-select" style="max-width:100%;width:100%;">
                   <option value="0" ${!s.manager_id ? "selected" : ""}>No team (visible only to Super Admin)</option>${options}</select>
                 <div class="muted-text" style="margin-top:8px;">Their ${s.candidate_count} candidate(s) move with them.</div>`,
        });
        if (!ok) return;
        const manager_id = Number(document.getElementById("moveSelect").value);
        await apiFetch(`${BASE}/${id}`, { method: "PATCH", body: JSON.stringify({ manager_id }) });
        await load();
      } else if (kind === "delete") {
        const ok = await confirmDialog({
          title: `Delete ${s.name}?`, confirmLabel: "Delete account", typed: s.name,
          html: `This permanently removes their login. Actions they took stay on record without their name attached. <strong>This cannot be undone.</strong> To keep the history intact, deactivate instead.`,
        });
        if (!ok) return;
        await apiFetch(`${BASE}/${id}`, { method: "DELETE" });
        await load();
      }
    } catch (err) {
      alert(err.message);
    }
  }

  // --- create -----------------------------------------------------------------
  document.getElementById("addBtn").onclick = () => {
    document.getElementById("createForm").reset();
    document.getElementById("createForm").classList.remove("hidden");
    document.getElementById("createCred").classList.add("hidden");
    setError("createError", "");
    if (isAdmin) {
      document.getElementById("cManager").innerHTML = selectOptions(
        managers().filter(m => m.is_active).map(m => ({ value: m.id, label: m.name })), "", "No team yet");
      document.getElementById("cManagerWrap").style.display = "";
    }
    openModal("createModal");
  };
  document.getElementById("createCancel").onclick = () => { closeModal("createModal"); load(); };
  if (isAdmin) {
    document.getElementById("cRole").onchange = (e) => {
      document.getElementById("cManagerWrap").style.display = e.target.value === "HR" ? "" : "none";
    };
  }

  document.getElementById("createForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    setError("createError", "");
    const body = {
      name: document.getElementById("cName").value.trim(),
      email: document.getElementById("cEmail").value.trim(),
      role: isAdmin ? document.getElementById("cRole").value : "HR",
    };
    if (isAdmin && body.role === "HR") {
      const m = document.getElementById("cManager").value;
      body.manager_id = m ? Number(m) : null;
    }
    if (body.role === "SUPER_ADMIN") {
      const ok = await confirmDialog({
        title: "Create a Super Admin?", danger: false, confirmLabel: "Create Super Admin",
        html: `A Super Admin manages every Manager, HR Executive and candidate. Only you can deactivate, delete or reset this account later.`,
      });
      if (!ok) return;
    }
    try {
      const data = await apiFetch(BASE, { method: "POST", body: JSON.stringify(body) });
      document.getElementById("createForm").classList.add("hidden");
      renderCredentials(document.getElementById("createCred"), {
        title: `${roleLabel(data.staff.role)} account created`,
        sub: `Send these login details to ${data.staff.email}.`,
        rows: [{ label: "Login ID", value: data.staff.email }, { label: "Password", value: data.temp_password },
               { label: "Login link", value: loginLink, link: true }],
        note: "This password works once: they are asked to choose their own on first login. You can still see it here until they do.",
        onDone: () => { closeModal("createModal"); load(); },
      });
      load();
    } catch (err) {
      setError("createError", err.message);
    }
  });

  await load();
}
