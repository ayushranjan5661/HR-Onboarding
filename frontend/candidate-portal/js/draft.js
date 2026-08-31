// "Save Draft" support for the candidate forms.
//
// Drafts live in THIS browser's IndexedDB, keyed per candidate and form:
// nothing is sent to the server, so HR never sees a half-finished form, and
// an expired session or closed tab no longer costs the candidate their work.
// IndexedDB (unlike localStorage) can store File objects, so the documents
// the candidate attached are saved with the draft and put back into the file
// inputs on restore.

const _DRAFT_DB = "candidate_form_drafts";
const _DRAFT_STORE = "drafts";

function _draftCandidateId() {
  // The JWT payload's `sub` is the candidate id — a stable key even if the
  // display name changes. Fall back to the name if the token can't be read.
  try {
    const payload = JSON.parse(
      atob(getToken().split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    return payload.sub || getName();
  } catch (e) {
    return getName();
  }
}

function _draftKey(formType) {
  return `draft_${formType}_${_draftCandidateId()}`;
}

function _openDraftDb() {
  return new Promise((resolve, reject) => {
    let req;
    try {
      req = indexedDB.open(_DRAFT_DB, 1);
    } catch (e) {
      reject(e);
      return;
    }
    req.onupgradeneeded = () => req.result.createObjectStore(_DRAFT_STORE);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error || new Error("IndexedDB unavailable"));
  });
}

// One transaction, one operation: fn(store) must return the IDBRequest.
function _draftDbOp(mode, fn) {
  return _openDraftDb().then(db => new Promise((resolve, reject) => {
    const tx = db.transaction(_DRAFT_STORE, mode);
    const req = fn(tx.objectStore(_DRAFT_STORE));
    tx.oncomplete = () => { db.close(); resolve(req.result); };
    tx.onerror = () => { db.close(); reject(tx.error); };
    tx.onabort = () => { db.close(); reject(tx.error || new Error("aborted")); };
  }));
}

// Every named non-file field, as {name: value}. Checkboxes store their value
// when checked and "" when not, which is exactly what fillFlatFields
// (edit-mode.js) expects back when the draft is restored.
function collectFlatFields(formEl) {
  const fields = {};
  formEl.querySelectorAll("input[name], select[name], textarea[name]").forEach(el => {
    if (el.type === "file") return;
    fields[el.name] = el.type === "checkbox" ? (el.checked ? (el.value || "Yes") : "") : el.value;
  });
  return fields;
}

// The File currently attached to each named file input.
function collectDraftFiles(formEl) {
  const files = {};
  formEl.querySelectorAll('input[type="file"][name]').forEach(el => {
    if (el.files && el.files[0]) files[el.name] = el.files[0];
  });
  return files;
}

// Put drafted Files back into their inputs. The change event is dispatched so
// everything listening on the input (preview button, type check, "will reuse"
// note) reacts exactly as if the candidate had picked the file by hand.
function restoreDraftFiles(formEl, files) {
  let count = 0;
  Object.entries(files || {}).forEach(([name, file]) => {
    const input = formEl.querySelector(`input[type="file"][name="${name}"]`);
    if (!input || !(file instanceof File)) return;
    try {
      const dt = new DataTransfer();
      dt.items.add(file);
      input.files = dt.files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
      count++;
    } catch (e) { /* very old browser without DataTransfer — skip this file */ }
  });
  return count;
}

async function clearFormDraft(formType) {
  try { await _draftDbOp("readwrite", s => s.delete(_draftKey(formType))); } catch (e) {}
  // Also drop any draft saved by the earlier localStorage-based version.
  try { localStorage.removeItem(_draftKey(formType)); } catch (e) {}
}

/**
 * Hook up the Save Draft button. Call this synchronously at script start —
 * it must not wait on any network call, so the button always works even
 * while the page is still loading (or failed to load).
 * Expects #saveDraftBtn and #draftStatus on the page.
 */
function wireDraftSave({ formType, formEl, collectTables }) {
  const btn = document.getElementById("saveDraftBtn");
  const status = document.getElementById("draftStatus");
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    status.textContent = "Saving draft…";
    try {
      const draft = {
        savedAt: new Date().toISOString(),
        fields: collectFlatFields(formEl),
        tables: collectTables ? collectTables() : {},
        files: collectDraftFiles(formEl),
      };
      await _draftDbOp("readwrite", s => s.put(draft, _draftKey(formType)));
      const n = Object.keys(draft.files).length;
      status.textContent =
        `Draft saved on this device at ${new Date(draft.savedAt).toLocaleTimeString()}` +
        (n ? `, including ${n} attached file${n > 1 ? "s" : ""}.` : ".") +
        " It is not submitted yet — HR sees nothing until you press Submit.";
    } catch (e) {
      status.textContent = "Could not save the draft — this browser is blocking " +
                            "site storage (private/incognito windows often do).";
    } finally {
      btn.disabled = false;
    }
  });
}

/**
 * Offer to restore a saved draft. Call (and await) this at the END of the
 * page's load sequence, so restored values land on top of whatever the edit
 * reload / prefill seeding filled in.
 */
async function offerDraftRestore({ formType, formEl, restoreTables }) {
  let draft = null;
  try {
    draft = await _draftDbOp("readonly", s => s.get(_draftKey(formType)));
  } catch (e) { /* storage blocked — nothing to offer */ }
  if (!draft) {
    // A draft saved by the earlier localStorage-based version (no files).
    try { draft = JSON.parse(localStorage.getItem(_draftKey(formType))); } catch (e) {}
  }
  if (!draft || typeof draft !== "object") return;

  const when = draft.savedAt ? new Date(draft.savedAt).toLocaleString() : "earlier";
  const restore = await showConfirm(
    `You have an unsubmitted draft of this form, saved on this device (${when}). ` +
    "Load it into the form?",
    { title: "Draft found", okText: "Load draft", cancelText: "Not now" });
  if (!restore) return;   // draft is kept; the prompt will offer it again next visit

  fillFlatFields(formEl, draft.fields);
  if (restoreTables) restoreTables(draft.tables || {});
  const nFiles = restoreDraftFiles(formEl, draft.files);
  document.getElementById("draftStatus").textContent =
    "Draft loaded" + (nFiles ? ` with ${nFiles} attached file${nFiles > 1 ? "s" : ""}` : "") +
    ". It stays saved on this device until you submit.";
}
