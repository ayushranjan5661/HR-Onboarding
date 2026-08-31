// Shared API helper for the candidate portal.
// API_BASE comes from ../js/config.js, which every page loads first.
const TOKEN_KEY = "candidate_token";
const NAME_KEY = "candidate_name";

function saveSession(token, name) {
  sessionStorage.setItem(TOKEN_KEY, token);
  sessionStorage.setItem(NAME_KEY, name);
}

function getToken() { return sessionStorage.getItem(TOKEN_KEY); }
function getName() { return sessionStorage.getItem(NAME_KEY) || "Candidate"; }

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
    // Only reads may bounce to the login page. A submit must never hard-
    // navigate away — that would silently destroy everything typed into the
    // form. Surface the error instead so the page (and the data) survives.
    const method = (options.method || "GET").toUpperCase();
    if (method === "GET") {
      clearSession();
      window.location.href = "../index.html";
      return new Promise(() => {});   // page is navigating; never settle
    }
    throw new Error("Your session has expired, so this was not saved. " +
                     "Your answers are still on this page — copy anything important, " +
                     "then log in again from the login page.");
  }
  const contentType = res.headers.get("content-type") || "";
  const data = contentType.includes("application/json") ? await res.json() : null;
  if (!res.ok) {
    throw new Error(errorMessage(data, res.status));
  }
  return data;
}

async function candidateLogout() {
  try { await apiFetch("/auth/candidate/logout", { method: "POST" }); } catch (e) { /* ignore */ }
  clearSession();
  window.location.href = "../index.html";
}
