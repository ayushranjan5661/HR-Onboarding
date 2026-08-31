// "Save Draft" support for the candidate forms.
//
// Drafts are stored on the SERVER, against the candidate's account, so work
// saved in one browser is offered again in any other browser or device they
// log in from. HR never sees a draft: it lives in its own tables and is
// deleted the moment the form is actually submitted.
//
// Files attached to a draft are uploaded with it. A browser cannot put a file
// back into a file input for security reasons, so on restore the page shows
// "Saved in your draft: <name>" with a View button instead — and the server
// promotes that file into the real submission when the candidate submits.

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

async function saveFormDraft(formType, formEl, tables) {
  const fd = new FormData();
  fd.set("fields", JSON.stringify(collectFlatFields(formEl)));
  fd.set("tables", JSON.stringify(tables || {}));
  let fileCount = 0;
  formEl.querySelectorAll('input[type="file"][name]').forEach(el => {
    if (el.files && el.files[0]) { fd.set(el.name, el.files[0]); fileCount++; }
  });
  const res = await apiFetch(`/candidate/me/draft/${formType}`, { method: "POST", body: fd });
  return { ...res, uploadedNow: fileCount };
}

async function loadFormDraft(formType) {
  try {
    const data = await apiFetch(`/candidate/me/draft/${formType}`);
    return data && data.exists ? data : null;
  } catch (err) {
    return null;   // draft unavailable -> the form still works normally
  }
}

async function clearFormDraft(formType) {
  try {
    await apiFetch(`/candidate/me/draft/${formType}`, { method: "DELETE" });
  } catch (err) { /* already gone, or cleared server-side on submit */ }
}

/**
 * Hook up the Save Draft button. Call this synchronously at script start —
 * it must not wait on any network call, so the button always works even
 * while the page is still loading. Expects #saveDraftBtn and #draftStatus.
 */
function wireDraftSave({ formType, formEl, collectTables }) {
  const btn = document.getElementById("saveDraftBtn");
  const status = document.getElementById("draftStatus");
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    status.style.color = "";
    status.textContent = "Saving draft…";
    try {
      const res = await saveFormDraft(formType, formEl, collectTables ? collectTables() : {});
      const n = res.document_count || 0;
      status.textContent =
        `Draft saved to your account at ${new Date().toLocaleTimeString()}` +
        (n ? `, including ${n} file${n > 1 ? "s" : ""}.` : ".") +
        " You can continue on any device — it is not submitted until you press Submit.";
    } catch (err) {
      status.style.color = "#b91c1c";
      status.textContent = "Could not save the draft: " + err.message;
    } finally {
      btn.disabled = false;
    }
  });
}

// Show each file held in the draft next to its input, with a View button, and
// lift "required" — the server already has this file and will use it.
function _showDraftDocuments(formEl, documents) {
  let shown = 0;
  (documents || []).forEach(doc => {
    const input = formEl.querySelector(`input[type="file"][name="${doc.field_key}"]`);
    if (!input) return;
    if (doc.file_available !== false) input.required = false;
    if (input.dataset.draftDocShown) return;
    input.dataset.draftDocShown = "1";

    const note = document.createElement("div");
    note.className = "file-hint";
    note.style.color = "#15803d";
    note.textContent = doc.file_available === false
      ? `Saved in your draft: ${doc.original_filename} — but the file is missing on the server, please attach it again.`
      : `Saved in your draft: ${doc.original_filename} — choose a file only if you want to replace it.`;
    input.insertAdjacentElement("afterend", note);

    if (doc.file_available !== false && typeof makeViewButton === "function") {
      const btn = makeViewButton("View draft file", () => viewDraftDoc(doc));
      input.insertAdjacentElement("afterend", btn);
      input.addEventListener("change", () => {
        const picked = input.files && input.files.length > 0;
        note.classList.toggle("hidden", picked);
        btn.classList.toggle("hidden", picked);
      });
    }
    shown++;
  });
  return shown;
}

// Opens a drafted file in the shared document viewer (doc-viewer.js).
async function viewDraftDoc(doc) {
  ensureViewer();
  document.getElementById("docViewer").classList.remove("hidden");
  const body = document.getElementById("docViewerBody");
  document.getElementById("docViewerTitle").textContent = doc.original_filename || "Document";
  body.innerHTML = "<span style='color:#6b7280'>Loading…</span>";
  try {
    releaseViewerUrl();
    const res = await fetch(`${API_BASE}/candidate/draft-documents/${doc.id}/download`, {
      headers: { Authorization: `Bearer ${getToken()}` },
    });
    if (!res.ok) {
      body.innerHTML = `<div style="color:#b91c1c;padding:20px;text-align:center;">
        Could not load this file.</div>`;
      return;
    }
    const blob = await res.blob();
    renderInViewer(URL.createObjectURL(blob), doc.content_type || blob.type, doc.original_filename);
  } catch (err) {
    body.innerHTML = `<div style="color:#b91c1c;padding:20px;">Could not load this file.</div>`;
  }
}

/**
 * Offer to restore a saved draft. Call (and await) this at the END of the
 * page's load sequence, so restored values land on top of whatever the edit
 * reload / prefill seeding filled in.
 */
async function offerDraftRestore({ formType, formEl, restoreTables }) {
  const draft = await loadFormDraft(formType);
  if (!draft) return;

  const when = draft.saved_at ? new Date(draft.saved_at).toLocaleString() : "earlier";
  const nDocs = (draft.documents || []).length;
  const restore = await showConfirm(
    `You have an unsubmitted draft of this form, saved to your account (${when})` +
    (nDocs ? ` with ${nDocs} attached file${nDocs > 1 ? "s" : ""}` : "") +
    ". Load it into the form?",
    { title: "Draft found", okText: "Load draft", cancelText: "Not now" });
  if (!restore) return;   // draft is kept; it will be offered again next visit

  fillFlatFields(formEl, draft.fields);
  if (restoreTables) restoreTables(draft.tables || {});
  const shown = _showDraftDocuments(formEl, draft.documents);
  document.getElementById("draftStatus").textContent =
    "Draft loaded" + (shown ? ` with ${shown} saved file${shown > 1 ? "s" : ""}` : "") +
    ". It stays on your account until you submit.";
}
