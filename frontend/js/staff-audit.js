// Staff audit trail: who created, deactivated, deleted, reset, moved or
// reassigned what. Global for the Super Admin, team-scoped for a Manager.

async function initAuditPage(mode) {
  const isAdmin = mode === "admin";
  requireAuth(isAdmin ? ["SUPER_ADMIN"] : ["MANAGER"]);
  mountUserChip();

  const root = document.getElementById("pageContent");
  let entries = [];

  root.innerHTML = `
    <div class="page-header">
      <h2>${isAdmin ? "Audit Trail" : "Team Audit Trail"}</h2>
      <button class="btn btn-outline" id="refreshBtn">Refresh</button>
    </div>
    <p style="color:var(--muted);font-size:0.88rem;margin-top:-10px;">
      Staff account and candidate ownership changes${isAdmin ? " across every team" : " touching your team"}, newest first.
      Edits to a candidate's submitted data are on that candidate's own page.
    </p>
    <div class="filters">
      <input type="text" class="search-input" id="searchInput" placeholder="Search by person, action or detail...">
      <select id="typeFilter">
        <option value="">All actions</option>
        <option value="STAFF">Staff accounts</option>
        <option value="CANDIDATE">Candidate ownership</option>
      </select>
    </div>
    <div class="card" id="auditCard"></div>`;

  function render() {
    const q = document.getElementById("searchInput").value.trim().toLowerCase();
    const type = document.getElementById("typeFilter").value;
    const rows = entries.filter(e =>
      (!type || e.target_type === type) &&
      (!q || [e.actor_name, e.target_name, e.detail, actionLabel(e.action)]
        .some(v => (v || "").toLowerCase().includes(q))));
    renderAudit(document.getElementById("auditCard"), rows);
  }

  async function load() {
    entries = await apiFetch(isAdmin ? "/admin/audit" : "/manager/audit");
    render();
  }

  document.getElementById("searchInput").addEventListener("input", render);
  document.getElementById("typeFilter").addEventListener("change", render);
  document.getElementById("refreshBtn").onclick = load;
  await load();
}
