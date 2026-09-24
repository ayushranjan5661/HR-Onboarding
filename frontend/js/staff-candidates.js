// Candidate list for a Manager (their team) or the admins (everyone),
// with owner, stage, reassignment and invite-with-owner. Opens the shared
// candidate detail page for everything else.

async function initCandidatesPage(mode) {
  const isAdmin = mode === "admin";
  requireAuth(isAdmin ? ADMIN_ROLES : ["MANAGER"]);
  mountUserChip();

  const root = document.getElementById("pageContent");
  let candidates = [];
  let assignable = [];       // active staff the caller may make an owner
  const myId = (await apiFetch("/hr/me")).id;

  root.innerHTML = `
    <div class="page-header">
      <h2>${isAdmin ? "All Candidates" : "Team Candidates"}</h2>
      <button class="btn btn-primary" id="inviteBtn">+ Invite Candidate</button>
    </div>

    <div class="stat-grid">
      <div class="stat-card"><div class="stat-value" id="statTotal">0</div><div class="stat-label">Total Candidates</div></div>
      <div class="stat-card accent-warn"><div class="stat-value" id="statPending">0</div><div class="stat-label">Awaiting Decision</div></div>
      <div class="stat-card accent-brand"><div class="stat-value" id="statInProgress">0</div><div class="stat-label">BGV / Docs In Progress</div></div>
      <div class="stat-card accent-success"><div class="stat-value" id="statComplete">0</div><div class="stat-label">Onboarding Complete</div></div>
    </div>

    <div class="filters">
      <input type="text" class="search-input" id="searchInput" placeholder="Search by name or email...">
      ${isAdmin ? `<select id="managerFilter"></select>` : ""}
      <select id="ownerFilter"></select>
      <select id="stageFilter">
        <option value="">All stages</option>
        <option value="INVITED">Invited</option>
        <option value="CIF_SUBMITTED">CIF submitted</option>
        <option value="APPROVED_FOR_BGV">Approved for BGV</option>
        <option value="ONBOARDING_COMPLETE">Onboarding complete</option>
        <option value="REJECTED">Rejected</option>
      </select>
    </div>

    <div class="card">
      <table>
        <thead><tr><th>Name</th><th>Email</th><th>Stage</th><th>Owner</th>${isAdmin ? "<th>Manager</th>" : ""}<th>Invited On</th><th></th></tr></thead>
        <tbody id="candBody"></tbody>
      </table>
      <div class="empty-state hidden" id="emptyState">No candidates match.</div>
    </div>

    <div class="modal-backdrop hidden" id="inviteModal">
      <div class="modal">
        <h3>Invite a Candidate</h3>
        <p style="color:var(--muted);font-size:0.85rem;margin-top:-6px;">
          Creates their portal login and unlocks the CIF form for them.
        </p>
        <form id="inviteForm">
          <label>Full Name</label>
          <input type="text" id="inviteName" required>
          <label>Email</label>
          <input type="email" id="inviteEmail" required>
          <label>Candidate Type</label>
          <select id="inviteType">
            <option value="EXPERIENCED">Experienced</option>
            <option value="FRESHER">Fresher / Trainee</option>
          </select>
          <label>Assign to</label>
          <select id="inviteOwner"></select>
          <div class="muted-text" style="margin-top:4px;">The owner sees and works this candidate. You can reassign later.</div>
          <div class="modal-actions">
            <button type="button" class="btn btn-outline" id="inviteCancel">Cancel</button>
            <button type="submit" class="btn btn-primary">Send Invite</button>
          </div>
          <div class="error-msg" id="inviteError"></div>
        </form>
        <div class="credential-box hidden" id="inviteCred"></div>
      </div>
    </div>`;

  // --- data -----------------------------------------------------------------
  async function load() {
    [candidates, assignable] = await Promise.all([apiFetch("/hr/candidates"), apiFetch("/hr/assignable-staff")]);
    renderStats();
    fillFilters();
    render();
  }

  function ownerLabel(u) {
    return u.id === myId ? `${u.name} (me)` : `${u.name}${u.role === "MANAGER" ? " · Manager" : ""}`;
  }

  function fillFilters() {
    const ownerSel = document.getElementById("ownerFilter");
    const keep = ownerSel.value;
    const owners = new Map();
    candidates.forEach(c => { if (c.assigned_hr_id) owners.set(c.assigned_hr_id, c.assigned_hr_name); });
    ownerSel.innerHTML = selectOptions(
      [...owners].map(([id, name]) => ({ value: id, label: name })).sort((a, b) => a.label.localeCompare(b.label)),
      keep, "All owners");
    if (isAdmin) {
      const mSel = document.getElementById("managerFilter");
      const keepM = mSel.value;
      const mgrs = new Map();
      candidates.forEach(c => { if (c.manager_id) mgrs.set(c.manager_id, c.manager_name); });
      mSel.innerHTML = selectOptions(
        [...mgrs].map(([id, name]) => ({ value: id, label: name })).sort((a, b) => a.label.localeCompare(b.label)),
        keepM, "All teams");
    }
  }

  function renderStats() {
    document.getElementById("statTotal").textContent = candidates.length;
    document.getElementById("statPending").textContent = candidates.filter(c => c.stage === "CIF_SUBMITTED").length;
    document.getElementById("statInProgress").textContent = candidates.filter(c => c.stage === "APPROVED_FOR_BGV").length;
    document.getElementById("statComplete").textContent = candidates.filter(c => c.stage === "ONBOARDING_COMPLETE").length;
  }

  function filtered() {
    const q = document.getElementById("searchInput").value.trim().toLowerCase();
    const owner = document.getElementById("ownerFilter").value;
    const stage = document.getElementById("stageFilter").value;
    const mgr = isAdmin ? document.getElementById("managerFilter").value : "";
    return candidates.filter(c =>
      (!q || c.name.toLowerCase().includes(q) || c.email.toLowerCase().includes(q)) &&
      (!owner || String(c.assigned_hr_id) === owner) &&
      (!stage || c.stage === stage) &&
      (!mgr || String(c.manager_id) === mgr));
  }

  function ownerSelect(c) {
    const known = assignable.some(u => u.id === c.assigned_hr_id);
    const options = assignable.map(u => ({ value: u.id, label: ownerLabel(u) }));
    if (!known) {
      // Owner is deactivated, teamless, or outside what we may assign to.
      options.unshift({ value: c.assigned_hr_id || "", label: c.assigned_hr_name ? `${c.assigned_hr_name} (unavailable)` : "Unassigned" });
    }
    return `<select class="inline-select" data-assign="${c.id}" title="Reassign">${selectOptions(options, c.assigned_hr_id ?? "")}</select>`;
  }

  function render() {
    const rows = filtered();
    const body = document.getElementById("candBody");
    document.getElementById("emptyState").classList.toggle("hidden", rows.length > 0);
    body.innerHTML = rows.map(c => `
      <tr data-open="${c.id}">
        <td><div class="name-cell"><span class="row-avatar">${escapeHtml(initials(c.name))}</span>${escapeHtml(c.name)}</div></td>
        <td>${escapeHtml(c.email)}</td>
        <td>${stageBadge(c.stage)}</td>
        <td>${ownerSelect(c)}</td>
        ${isAdmin ? `<td>${c.manager_name ? escapeHtml(c.manager_name) : `<span class="pending-tag">No team</span>`}</td>` : ""}
        <td>${fmtDate(c.created_at)}</td>
        <td><div class="row-actions"><button class="btn btn-danger btn-small" data-del="${c.id}">Delete Invitation</button></div></td>
      </tr>`).join("");

    body.querySelectorAll("tr[data-open]").forEach(tr => {
      tr.onclick = () => window.location.href = `../hr-portal/candidate.html?id=${tr.dataset.open}`;
    });
    body.querySelectorAll("select[data-assign], button[data-del]").forEach(el => {
      el.addEventListener("click", e => e.stopPropagation());
    });
    body.querySelectorAll("select[data-assign]").forEach(sel => {
      sel.onchange = () => reassign(Number(sel.dataset.assign), Number(sel.value), sel);
    });
    body.querySelectorAll("button[data-del]").forEach(b => b.onclick = () => remove(Number(b.dataset.del)));
  }

  ["searchInput", "ownerFilter", "stageFilter", ...(isAdmin ? ["managerFilter"] : [])].forEach(id => {
    document.getElementById(id).addEventListener("input", render);
    document.getElementById(id).addEventListener("change", render);
  });

  // --- actions ----------------------------------------------------------------
  async function reassign(candidateId, newOwnerId, sel) {
    const c = candidates.find(x => x.id === candidateId);
    const target = assignable.find(u => u.id === newOwnerId);
    if (!c || !target || newOwnerId === c.assigned_hr_id) { render(); return; }
    const ok = await confirmDialog({
      title: `Reassign ${c.name}?`, danger: false, confirmLabel: "Reassign",
      html: `From <strong>${escapeHtml(c.assigned_hr_name || "nobody")}</strong> to <strong>${escapeHtml(target.name)}</strong>.
             ${escapeHtml(c.assigned_hr_name || "The previous owner")} will no longer see this candidate.`,
    });
    if (!ok) { render(); return; }
    try {
      await apiFetch(`/manager/candidates/${candidateId}/assign`, {
        method: "POST", body: JSON.stringify({ assigned_hr_id: newOwnerId }),
      });
      await load();
    } catch (err) {
      alert(err.message);
      render();
    }
  }

  async function remove(candidateId) {
    const c = candidates.find(x => x.id === candidateId);
    if (!c) return;
    const ok = await confirmDialog({
      title: "Delete Candidate", confirmLabel: "Delete Candidate", typed: c.name,
      html: `This permanently removes <strong>${escapeHtml(c.name)}</strong>'s login, submitted forms and uploaded documents. <strong>This cannot be undone.</strong>`,
    });
    if (!ok) return;
    try {
      await apiFetch(`/hr/candidates/${candidateId}`, { method: "DELETE" });
      await load();
    } catch (err) {
      alert(err.message);
    }
  }

  // --- invite -----------------------------------------------------------------
  document.getElementById("inviteBtn").onclick = () => {
    document.getElementById("inviteForm").reset();
    document.getElementById("inviteForm").classList.remove("hidden");
    document.getElementById("inviteCred").classList.add("hidden");
    setError("inviteError", "");
    document.getElementById("inviteOwner").innerHTML = selectOptions(
      assignable.map(u => ({ value: u.id, label: ownerLabel(u) })), myId);
    openModal("inviteModal");
  };
  document.getElementById("inviteCancel").onclick = () => closeModal("inviteModal");

  document.getElementById("inviteForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    setError("inviteError", "");
    try {
      const data = await apiFetch("/hr/candidates", {
        method: "POST",
        body: JSON.stringify({
          name: document.getElementById("inviteName").value.trim(),
          email: document.getElementById("inviteEmail").value.trim(),
          candidate_type: document.getElementById("inviteType").value,
          assigned_hr_id: Number(document.getElementById("inviteOwner").value),
        }),
      });
      document.getElementById("inviteForm").classList.add("hidden");
      renderCredentials(document.getElementById("inviteCred"), {
        title: "Candidate account created",
        sub: `Send these login details to ${data.email}.`,
        rows: [{ label: "Login ID", value: data.email }, { label: "Password", value: data.temp_password },
               { label: "Login link", value: data.login_url, link: true }],
        note: "The password is temporary and shown only once — copy it before closing.",
        onDone: () => { closeModal("inviteModal"); load(); },
      });
      load();
    } catch (err) {
      setError("inviteError", err.message);
    }
  });

  await load();
}
