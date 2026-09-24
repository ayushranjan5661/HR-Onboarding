// Shared session + API helper for every staff portal (HR, Manager, Super
// Admin, Master Admin).
// API_BASE comes from ../js/config.js, which every page loads first.
//
// One staff session, whatever the role. The role decides which portal the
// user lands in and which navigation they see; the server decides what they
// may actually do, so nothing here is a security boundary.
const TOKEN_KEY = "staff_token";
const NAME_KEY = "staff_name";
const ROLE_KEY = "staff_role";

// Sessions opened before the hierarchy existed used hr_* keys. Carry one
// over once so nobody is logged out by the upgrade.
(function migrateLegacySession() {
  const old = sessionStorage.getItem("hr_token");
  if (old && !sessionStorage.getItem(TOKEN_KEY)) {
    sessionStorage.setItem(TOKEN_KEY, old);
    sessionStorage.setItem(NAME_KEY, sessionStorage.getItem("hr_name") || "");
    // Role unknown: requireAuth asks the server before trusting any page.
    sessionStorage.removeItem(ROLE_KEY);
  }
  sessionStorage.removeItem("hr_token");
  sessionStorage.removeItem("hr_name");
})();

function saveSession(token, name, role) {
  sessionStorage.setItem(TOKEN_KEY, token);
  sessionStorage.setItem(NAME_KEY, name || "");
  sessionStorage.setItem(ROLE_KEY, role || "HR");
}

function getToken() { return sessionStorage.getItem(TOKEN_KEY); }
function getName() { return sessionStorage.getItem(NAME_KEY) || "Staff"; }
function getRole() { return sessionStorage.getItem(ROLE_KEY) || "HR"; }

function clearSession() {
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(NAME_KEY);
  sessionStorage.removeItem(ROLE_KEY);
}

// Where each role lands after login. Every portal folder (hr-portal/,
// manager/, super-admin/) sits one level under frontend/, so these relative
// paths resolve the same from any of them.
// The Master Admin (developer account) uses the Super Admin portal; the
// pages show it more (Super Admins, every password) based on the role.
const ROLE_HOME = {
  MASTER_ADMIN: "../super-admin/users.html",
  SUPER_ADMIN: "../super-admin/users.html",
  MANAGER: "../manager/candidates.html",
  HR: "../hr-portal/dashboard.html",
};
const ROLE_CANDIDATES = {
  MASTER_ADMIN: "../super-admin/candidates.html",
  SUPER_ADMIN: "../super-admin/candidates.html",
  MANAGER: "../manager/candidates.html",
  HR: "../hr-portal/dashboard.html",
};
const ROLE_LABEL = { MASTER_ADMIN: "Master Admin", SUPER_ADMIN: "Super Admin", MANAGER: "Manager", HR: "HR Executive" };
// Roles allowed into the super-admin/ pages.
const ADMIN_ROLES = ["MASTER_ADMIN", "SUPER_ADMIN"];

function roleHome(role) { return ROLE_HOME[role || getRole()] || ROLE_HOME.HR; }

// Send the user to another page, unless that page is this one: a role this
// build does not know (or a stale cached script) would otherwise bounce the
// page to itself forever. In that case the session is dropped and they log
// in again, which fetches the current scripts.
function bounceTo(target) {
  const dest = new URL(target, location.href);
  if (dest.pathname === location.pathname) {
    clearSession();
    window.location.href = "../index.html";
    return;
  }
  window.location.href = target;
}
function isMasterAdmin() { return getRole() === "MASTER_ADMIN"; }
function roleCandidatesPage(role) { return ROLE_CANDIDATES[role || getRole()] || ROLE_CANDIDATES.HR; }
function roleLabel(role) { return ROLE_LABEL[role] || role || ""; }

// Guard a page: no session -> login; wrong role for this page -> that role's
// own home. Pass nothing to allow every staff role (the candidate detail
// page, which all three roles use).
function requireAuth(allowedRoles) {
  if (!getToken()) { window.location.href = "../index.html"; return; }
  const stored = sessionStorage.getItem(ROLE_KEY);
  if (!stored) {
    // Session predates roles (or storage was tampered with): the server
    // says who this really is, then the page is re-entered with that role.
    fetch(`${API_BASE}/auth/staff/me`, { headers: { Authorization: `Bearer ${getToken()}` } })
      .then(r => r.ok ? r.json() : Promise.reject())
      .then(me => {
        saveSession(getToken(), me.name, me.role);
        if (me.must_reset_password) { window.location.href = "../change-password.html?forced=1"; return; }
        if (!ROLE_HOME[me.role]) { clearSession(); window.location.href = "../index.html"; return; }
        if (allowedRoles && !allowedRoles.includes(me.role)) bounceTo(roleHome(me.role));
        else window.location.reload();
      })
      .catch(() => { clearSession(); window.location.href = "../index.html"; });
    return;
  }
  if (!ROLE_HOME[stored]) {
    // A role this script does not know: never guess a home for it.
    clearSession();
    window.location.href = "../index.html";
    return;
  }
  if (allowedRoles && !allowedRoles.includes(stored)) {
    bounceTo(roleHome(stored));
  }
}

// FastAPI sends `detail` as a string for our own errors, but as an array of
// objects for 422 validation failures — which would print [object Object].
function errorMessage(data, status) {
  const d = data && data.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d) && d.length) {
    return d.map(e => e.msg || JSON.stringify(e)).join("; ");
  }
  return `Request failed (${status})`;
}

async function apiFetch(path, options = {}) {
  const headers = options.headers || {};
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (!(options.body instanceof FormData) && options.body) {
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(`${API_BASE}${path}`, { ...options, headers });
  if (res.status === 401) {
    // Only reads may bounce to the login page; a write surfaces the error so
    // whatever the user was in the middle of (notes, edits) isn't wiped by a redirect.
    const method = (options.method || "GET").toUpperCase();
    if (method === "GET") {
      clearSession();
      window.location.href = "../index.html";
      return new Promise(() => {});   // page is navigating; never settle
    }
    throw new Error("Your session has expired, so this was not saved. " +
                     "Please log in again from the login page.");
  }
  const contentType = res.headers.get("content-type") || "";
  const data = contentType.includes("application/json") ? await res.json() : null;
  if (res.status === 403 && data && data.detail === "PASSWORD_RESET_REQUIRED") {
    // First login with a system-generated password: nothing else works
    // until the user has chosen their own.
    window.location.href = "../change-password.html";
    return new Promise(() => {});
  }
  if (!res.ok) {
    throw new Error(errorMessage(data, res.status));
  }
  return data;
}

async function staffLogout() {
  try { await apiFetch("/auth/staff/logout", { method: "POST" }); } catch (e) { /* ignore */ }
  clearSession();
  window.location.href = "../index.html";
}
// Old name still used by the HR portal pages.
const hrLogout = staffLogout;

// The candidate detail page is shared by all roles but its links point at
// the HR dashboard; send Managers and the Super Admin back to their own
// candidate list instead.
function applyRoleNav() {
  const target = roleCandidatesPage();
  if (target === ROLE_CANDIDATES.HR) return;
  document.querySelectorAll('a[href="dashboard.html"]').forEach(a => { a.href = target; });
}
