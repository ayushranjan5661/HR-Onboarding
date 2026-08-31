// Replacement for native alert(). Browser dialogs are positioned by the
// browser chrome, not the page, so they land at the top of the window and
// can't be styled — see the CIF submit confirmation. This renders a centered
// modal using the portal's own .modal styles instead.
//
// Returns a Promise that settles when the candidate acknowledges, so call
// sites that navigate afterwards must await it:
//   await showAlert("Submitted."); window.location.href = "home.html";
// Without the await the redirect fires immediately and the dialog is never
// seen — the same reason the old blocking alert() worked by accident.

const DIALOG_ID = "appDialog";

const DIALOG_ICONS = {
  success: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"' +
           ' stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>',
  info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"' +
        ' stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/>' +
        '<path d="M12 11v5M12 7.5v.01"/></svg>',
};

function dialogEl() {
  let el = document.getElementById(DIALOG_ID);
  if (el) return el;

  el = document.createElement("div");
  el.id = DIALOG_ID;
  el.className = "modal-backdrop hidden";
  el.innerHTML = `
    <div class="modal dialog-card" role="alertdialog" aria-modal="true"
         aria-labelledby="dialogTitle" aria-describedby="dialogMessage">
      <div class="dialog-icon" id="dialogIcon" aria-hidden="true"></div>
      <h3 id="dialogTitle"></h3>
      <p class="dialog-message" id="dialogMessage"></p>
      <div class="modal-actions">
        <button type="button" class="btn btn-outline hidden" id="dialogCancelBtn">Cancel</button>
        <button type="button" class="btn btn-primary" id="dialogOkBtn">OK</button>
      </div>
    </div>`;
  document.body.appendChild(el);
  return el;
}

function showAlert(message, { title = "", okText = "OK", tone = "success" } = {}) {
  const el = dialogEl();
  const okBtn = el.querySelector("#dialogOkBtn");
  el.querySelector("#dialogCancelBtn").classList.add("hidden");
  const icon = el.querySelector("#dialogIcon");

  icon.className = `dialog-icon tone-${tone}`;
  icon.innerHTML = DIALOG_ICONS[tone] || DIALOG_ICONS.info;

  const heading = el.querySelector("#dialogTitle");
  heading.textContent = title || (tone === "success" ? "Submitted" : "Heads up");
  el.querySelector("#dialogMessage").textContent = message;
  okBtn.textContent = okText;
  el.classList.remove("hidden");

  // Returning focus to whatever was focused before would fight the redirect
  // most call sites do next, so we only move focus in.
  okBtn.focus();

  return new Promise((resolve) => {
    function done() {
      el.classList.add("hidden");
      okBtn.removeEventListener("click", done);
      document.removeEventListener("keydown", onKey);
      resolve();
    }
    function onKey(e) {
      if (e.key === "Escape" || e.key === "Enter") { e.preventDefault(); done(); }
    }
    okBtn.addEventListener("click", done);
    document.addEventListener("keydown", onKey);
  });
}

// Two-choice variant of showAlert. Resolves true for OK, false for Cancel
// (Escape also cancels; Enter confirms).
function showConfirm(message, { title = "Please confirm", okText = "OK",
                                 cancelText = "Cancel", tone = "info" } = {}) {
  const el = dialogEl();
  const okBtn = el.querySelector("#dialogOkBtn");
  const cancelBtn = el.querySelector("#dialogCancelBtn");
  const icon = el.querySelector("#dialogIcon");

  icon.className = `dialog-icon tone-${tone}`;
  icon.innerHTML = DIALOG_ICONS[tone] || DIALOG_ICONS.info;
  el.querySelector("#dialogTitle").textContent = title;
  el.querySelector("#dialogMessage").textContent = message;
  okBtn.textContent = okText;
  cancelBtn.textContent = cancelText;
  cancelBtn.classList.remove("hidden");
  el.classList.remove("hidden");
  okBtn.focus();

  return new Promise((resolve) => {
    function done(answer) {
      el.classList.add("hidden");
      cancelBtn.classList.add("hidden");
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      document.removeEventListener("keydown", onKey);
      resolve(answer);
    }
    function onOk() { done(true); }
    function onCancel() { done(false); }
    function onKey(e) {
      if (e.key === "Escape") { e.preventDefault(); done(false); }
      if (e.key === "Enter") { e.preventDefault(); done(true); }
    }
    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
    document.addEventListener("keydown", onKey);
  });
}
