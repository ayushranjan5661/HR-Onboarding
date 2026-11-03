// "Connect Zoho" in the sidebar footer of the staff pages that publish to
// Zoho. Each staff user links their own Zoho account; pushes then run on
// their token and are assigned to them (backend app/services/zoho_oauth.py).
// Loaded after ../js/config.js and hr-portal/js/api.js.

const zohoConn = { status: null };

function zohoEsc(s) {
  return String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;");
}

// The page path relative to the portal root, e.g. "hr-portal/candidate.html?id=4",
// so Zoho's redirect lands back where the user was.
function zohoReturnPath() {
  const parts = location.pathname.split("/").filter(Boolean).slice(-2);
  const params = new URLSearchParams(location.search);
  params.delete("zoho");
  params.delete("zoho_error");
  const q = params.toString();
  return parts.join("/") + (q ? `?${q}` : "");
}

async function startZohoConnect() {
  try {
    const { url } = await apiFetch("/hr/zoho/connect", {
      method: "POST", body: JSON.stringify({ return_to: zohoReturnPath() }),
    });
    window.location.href = url;
  } catch (err) {
    zohoNotify(err.message, true);
  }
}

async function disconnectZoho() {
  const ok = typeof showConfirm === "function"
    ? await showConfirm("Publishing to Zoho will stop working for you until you connect again.",
                        { title: "Disconnect Zoho?", confirmText: "Disconnect", danger: true })
    : confirm("Disconnect Zoho? Publishing will stop working for you until you connect again.");
  if (!ok) return;
  try {
    zohoConn.status = await apiFetch("/hr/zoho/disconnect", { method: "POST" });
    renderZohoConnect();
  } catch (err) {
    zohoNotify(err.message, true);
  }
}

// Called by a page when a push comes back 428 (not connected).
async function promptZohoConnect(message) {
  const ok = typeof showConfirm === "function"
    ? await showConfirm(message, { title: "Connect Zoho", confirmText: "Connect Zoho" })
    : confirm(message);
  if (ok) startZohoConnect();
}

function zohoNotify(message, danger) {
  if (typeof showAlert === "function") {
    showAlert(message, { title: danger ? "Zoho connection failed" : "Zoho connected", danger });
  } else {
    alert(message);
  }
}

function renderZohoConnect() {
  const box = document.getElementById("zohoConnectBox");
  const s = zohoConn.status;
  if (!box) return;
  if (!s || s.mode !== "user") { box.innerHTML = ""; return; }
  box.innerHTML = s.connected
    ? `<div class="zoho-card" title="Zoho: ${zohoEsc(s.zoho_email)}">
         <div class="zoho-card-head">
           <span class="zoho-status"><span class="zoho-dot"></span><span>Zoho connected</span></span>
           <button type="button" class="zoho-disconnect" onclick="disconnectZoho()">Disconnect</button>
         </div>
         <div class="zoho-email">${zohoEsc(s.zoho_email)}</div>
         ${s.zoho_employee_id ? `<div class="zoho-meta">Employee ID ${zohoEsc(s.zoho_employee_id)}</div>` : ""}
       </div>`
    : `<button class="logout-btn" onclick="startZohoConnect()"
               title="${s.configured ? "Link your Zoho account so your pushes are made as you" : "Not set up on the server yet"}"
               ${s.configured ? "" : "disabled"}>Connect Zoho</button>`;
}

async function mountZohoConnect() {
  const footer = document.querySelector(".sidebar-footer");
  if (!footer) return;
  if (!document.getElementById("zohoConnectBox")) {
    const box = document.createElement("div");
    box.id = "zohoConnectBox";
    const chip = footer.querySelector(".user-chip");
    chip ? chip.after(box) : footer.prepend(box);
  }

  // Outcome of the Zoho redirect, then drop it from the address bar.
  const params = new URLSearchParams(location.search);
  if (params.has("zoho") || params.has("zoho_error")) {
    const error = params.get("zoho_error");
    history.replaceState(null, "", location.pathname
      + (zohoReturnPath().includes("?") ? "?" + zohoReturnPath().split("?")[1] : ""));
    zohoNotify(error || "Your Zoho account is connected. Records you publish are now created "
                        + "under your Zoho login and assigned to you.", !!error);
  }

  try {
    zohoConn.status = await apiFetch("/hr/zoho/connection");
  } catch (err) {
    zohoConn.status = null;
  }
  renderZohoConnect();
}

document.addEventListener("DOMContentLoaded", mountZohoConnect);
