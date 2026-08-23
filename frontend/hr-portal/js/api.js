// Shared API helper for the HR portal.
// API_BASE comes from ../js/config.js, which every page loads first.
const TOKEN_KEY = "hr_token";
const NAME_KEY = "hr_name";

function saveSession(token, name) {
  sessionStorage.setItem(TOKEN_KEY, token);
  sessionStorage.setItem(NAME_KEY, name);
}

function getToken() { return sessionStorage.getItem(TOKEN_KEY); }
function getName() { return sessionStorage.getItem(NAME_KEY) || "HR"; }

function clearSession() {
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(NAME_KEY);
}

function requireAuth() {
  if (!getToken()) window.location.href = "../index.html";
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
    clearSession();
    window.location.href = "../index.html";
    throw new Error("Session expired");
  }
  const contentType = res.headers.get("content-type") || "";
  const data = contentType.includes("application/json") ? await res.json() : null;
  if (!res.ok) {
    throw new Error(errorMessage(data, res.status));
  }
  return data;
}

async function hrLogout() {
  try { await apiFetch("/auth/hr/logout", { method: "POST" }); } catch (e) { /* ignore */ }
  clearSession();
  window.location.href = "../index.html";
}
