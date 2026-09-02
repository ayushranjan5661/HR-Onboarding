requireAuth();
document.getElementById("whoami").textContent = getName();
document.getElementById("whoamiAvatar").textContent = getName().charAt(0).toUpperCase();

const candidateId = new URLSearchParams(window.location.search).get("id");
let currentData = null;
// What HR has opened for the candidate to correct, plus what may be opened.
// Populated by loadEditAccess(); render() reads it, so it is fetched first.
let editAccess = { submitted: {}, grantable: {}, permissions: [] };
// Whole-form edit mode, tracked per card and limited to one card at a time.
// PROFILE fields are shown inside the CIF card, so they follow CIF's mode.
const editModes = { CIF: false, DOCUMENT_COLLECTION: false, BGV: false };

const FORM_TITLES = {
  CIF: "Candidate Details (CIF)",
  DOCUMENT_COLLECTION: "Document Collection Form",
  BGV: "Background Verification Form",
};
// The CIF card is static markup; the follow-up cards are built by render().
const FORM_CARD_ID = { CIF: "cifCard", DOCUMENT_COLLECTION: "card-DOCUMENT_COLLECTION", BGV: "card-BGV" };
const FORM_DOCS_ID = { CIF: "cifDocs", DOCUMENT_COLLECTION: "docs-DOCUMENT_COLLECTION", BGV: "docs-BGV" };

// Values long enough that a single-line input is unusable.
const LONG_TEXT_FIELDS = new Set([
  "current_address", "permanent_address", "skills_technologies",
  "technical_certifications", "understanding_of_levelshift", "aspirations",
  "other_offers", "declaration_place",
]);

function badge(stage) {
  return `<span class="badge badge-${stage.toLowerCase()}">${stage.replaceAll("_", " ")}</span>`;
}

// Escapes quotes too, so values are safe inside attributes (title=, href=), not just text.
function escapeHtml(str) {
  return String(str ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

// Replaces native confirm() with an in-page modal — native dialogs are
// positioned by the browser/OS, not the page, so they can't be centered or
// styled and end up landing wherever the browser feels like. Returns a
// Promise<boolean>, so call sites just `if (!await showConfirm(...)) return;`
// in place of the old `if (!confirm(...)) return;`.
function showConfirm(message, { title = "Please confirm", confirmText = "Confirm", danger = false } = {}) {
  const modal = document.getElementById("confirmModal");
  const okBtn = document.getElementById("confirmOkBtn");
  const cancelBtn = document.getElementById("confirmCancelBtn");
  document.getElementById("confirmTitle").textContent = title;
  document.getElementById("confirmMessage").textContent = message;
  okBtn.textContent = confirmText;
  okBtn.className = `btn ${danger ? "btn-danger" : "btn-primary"}`;
  modal.classList.remove("hidden");

  return new Promise((resolve) => {
    function done(result) {
      modal.classList.add("hidden");
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      resolve(result);
    }
    function onOk() { done(true); }
    function onCancel() { done(false); }
    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
  });
}

// One field row: value stored in a relational column. Every form is edited as
// a whole (one Edit button in the card header) rather than field by field, so
// a row is either read-only text or — with `opts.editing` — an input.
function fieldRow(form, field, value, opts = {}) {
  const { editing = false } = opts;
  const hasValue = value !== null && value !== undefined && value !== "";
  if (editing) {
    const v = escapeHtml(String(value ?? ""));
    const attrs = `class="edit-input" data-edit-form="${form}" data-edit-field="${field}" data-orig="${v}"`;
    return `
      <div class="field-row">
        <div class="fname">${labelFor(field)}</div>
        <div class="fval">${LONG_TEXT_FIELDS.has(field)
          ? `<textarea rows="2" ${attrs}>${v}</textarea>`
          : `<input type="text" ${attrs} value="${v}">`}</div>
      </div>`;
  }
  return `
    <div class="field-row">
      <div class="fname">${labelFor(field)}</div>
      <div class="fval ${hasValue ? "" : "empty"}">${hasValue ? escapeHtml(String(value)) : "Not provided"}</div>
    </div>`;
}

function fieldRows(form, fieldList, data, opts = {}) {
  if (!fieldList.length) return "";   // e.g. Document Collection — uploads only
  if (!data) return "<p style='color:#6b7280'>Not submitted yet.</p>";
  return fieldList.map(f => fieldRow(form, f, data[f], opts)).join("");
}

// Repeating-row table (education / employment / references). In edit mode every
// cell becomes an input; the per-row Delete button is shown only when
// `opts.showDelete` is set, so the read-only CIF view stays free of buttons.
function rowTable(title, tableName, columns, rows, opts = {}) {
  const { editing = false, showDelete = false } = opts;
  const header = `<h4 style="margin:16px 0 4px;font-size:0.85rem;color:#6b7280;">${title.toUpperCase()}</h4>`;
  if (!rows || !rows.length) {
    return `${header}<div class="fval empty" style="padding:4px 0 8px;">No entries provided</div>`;
  }
  const cell = (r, c) => {
    const v = escapeHtml(String(r[c] ?? ""));
    return editing
      ? `<input type="text" class="edit-input" data-row-table="${tableName}" data-row-id="${r.id}"
          data-row-col="${c}" data-orig="${v}" value="${v}">`
      : v;
  };
  const hasActionCol = editing || showDelete;
  return `${header}
    <div style="overflow-x:auto;margin-bottom:8px;">
      <table>
        <thead><tr>${columns.map(c => `<th>${labelFor(c)}</th>`).join("")}${hasActionCol ? "<th></th>" : ""}</tr></thead>
        <tbody>${rows.map(r => `
          <tr style="cursor:default;">
            ${columns.map(c => `<td>${cell(r, c)}</td>`).join("")}
            ${hasActionCol ? `<td>${showDelete
              ? `<button class="btn btn-outline btn-small" onclick="deleteRow('${tableName}', ${r.id})">Delete</button>`
              : ""}</td>` : ""}
          </tr>`).join("")}
        </tbody>
      </table>
    </div>`;
}


// The five BGV verification sections, each a repeating table with per-row delete.
function bgvTables(c, opts = {}) {
  const tables = c.bgv_tables || {};
  return Object.keys(BGV_TABLE_TITLES).map(key =>
    rowTable(BGV_TABLE_TITLES[key], key, BGV_TABLE_COLUMNS[key], tables[key], opts)
  ).join("");
}

// Checklist: for every expected upload on this form, show whether the
// candidate submitted it — with a download button when they did. In edit mode
// each row also gets a file picker (replace / attach) and a Remove button;
// files upload immediately, since a <input type=file> selection can't be held
// across the re-render that Save triggers.
function docActions(formType, fieldKey, doc, editing, fileAvailable = true) {
  const buttons = [];
  if (doc && fileAvailable) {
    buttons.push(`<button class="btn btn-outline btn-small" onclick="viewDoc(event, ${doc.id})">View</button>`);
    // Download is a read-mode action — edit mode is for changing the file, not
    // taking a copy of it, so the row stays down to View / Replace / Remove.
    if (!editing) {
      buttons.push(`<button class="btn btn-outline btn-small" onclick="downloadDoc(event, ${doc.id})">Download</button>`);
    }
  }
  if (editing) {
    const inputId = `docUpload_${formType}_${fieldKey}`;
    buttons.push(`
      <input type="file" id="${inputId}" class="hidden"
        accept="image/jpeg,image/png,image/gif,image/webp,application/pdf,.doc,.docx"
        onchange="uploadDoc('${formType}', '${fieldKey}', this)">
      <button class="btn btn-outline btn-small"
        onclick="document.getElementById('${inputId}').click()">${doc ? "Replace" : "Attach"}</button>`);
    if (doc) {
      buttons.push(`<button class="btn btn-outline btn-small" onclick="removeDoc('${formType}', ${doc.id})">Remove</button>`);
    }
  }
  return buttons.length ? `<div class="field-actions">${buttons.join("")}</div>` : "";
}

function renderDocs(allDocs, formType, opts = {}) {
  const { editing = false } = opts;
  const expected = FORM_FILE_FIELDS[formType] || [];
  if (!expected.length) return "";
  const docs = allDocs.filter(d => d.form_type === formType);
  const rows = expected.map(fieldKey => {
    const doc = docs.find(d => d.field_key === fieldKey);
    if (doc && doc.file_available === false) {
      // Recorded as submitted, but the file is gone from the server.
      return `
        <div class="field-row">
          <div class="fname">${labelFor(fieldKey)}</div>
          <div class="fval" style="color:#b45309;">⚠️ File missing on server — ${escapeHtml(doc.original_filename)}
            <div style="color:#6b7280;font-size:0.8rem;">Ask the candidate to upload this again.</div>
          </div>
          ${docActions(formType, fieldKey, doc, editing, false)}
        </div>`;
    }
    if (doc) {
      return `
        <div class="field-row">
          <div class="fname">${labelFor(fieldKey)}</div>
          <div class="fval">✅ Submitted — ${escapeHtml(doc.original_filename)}</div>
          ${docActions(formType, fieldKey, doc, editing)}
        </div>`;
    }
    return `
      <div class="field-row">
        <div class="fname">${labelFor(fieldKey)}</div>
        <div class="fval empty">❌ Not submitted</div>
        ${docActions(formType, fieldKey, null, editing)}
      </div>`;
  }).join("");
  return `<h4 style="margin:16px 0 4px;font-size:0.85rem;color:#6b7280;">UPLOADED DOCUMENTS</h4>${rows}`;
}


// The download endpoint needs an Authorization header, so a plain <img src>
// or <a href> can't reach it. Fetch the bytes once and render from a blob URL.
let currentPreviewUrl = null;

// Images and PDFs render inline; anything else can only be opened or saved.
function renderPreview(body, type, url) {
  if (type.startsWith("image/")) {
    body.innerHTML = `<img src="${url}" alt="" style="max-width:100%;max-height:100%;object-fit:contain;">`;
  } else if (type === "application/pdf") {
    body.innerHTML = `<iframe src="${url}" style="width:100%;height:100%;border:0;"></iframe>`;
  } else {
    body.innerHTML = `<div style="text-align:center;color:#6b7280;padding:20px;">
      This file type can't be previewed in the browser${type ? " (" + escapeHtml(type) + ")" : ""}.<br>
      Use <strong>Open in new tab</strong> instead.</div>`;
  }
}

async function fetchDocBlob(docId) {
  const res = await fetch(`${API_BASE}/hr/documents/${docId}/download`, {
    headers: { Authorization: `Bearer ${getToken()}` },
  });
  if (!res.ok) throw new Error("Could not load this file");
  return res.blob();
}

function releasePreview() {
  if (currentPreviewUrl) {
    URL.revokeObjectURL(currentPreviewUrl);
    currentPreviewUrl = null;
  }
}

function closeViewModal() {
  document.getElementById("viewModal").classList.add("hidden");
  document.getElementById("viewBody").innerHTML = "";
  releasePreview();
}

// Clicking the dark backdrop (but not the dialog) closes the preview.
document.getElementById("viewModal").addEventListener("click", (e) => {
  if (e.target.id === "viewModal") closeViewModal();
});

async function viewDoc(e, docId) {
  e.preventDefault();
  const doc = currentData.documents.find(d => d.id === docId);
  const body = document.getElementById("viewBody");
  document.getElementById("viewTitle").textContent =
    `${labelFor(doc ? doc.field_key : "")} — ${doc ? doc.original_filename : ""}`;
  document.getElementById("viewModal").classList.remove("hidden");
  body.innerHTML = "<span style='color:#6b7280'>Loading…</span>";

  try {
    releasePreview();
    const blob = await fetchDocBlob(docId);
    const type = (doc && doc.content_type) || blob.type || "";
    currentPreviewUrl = URL.createObjectURL(blob);

    renderPreview(body, type, currentPreviewUrl);

    document.getElementById("viewNewTabBtn").onclick = () => window.open(currentPreviewUrl, "_blank");
    // Same rule as the row buttons: no download offered while the form the
    // document belongs to is being edited.
    const dlBtn = document.getElementById("viewDownloadBtn");
    dlBtn.classList.toggle("hidden", !!(doc && editModes[doc.form_type]));
    dlBtn.onclick = (ev) => downloadDoc(ev, docId);
  } catch (err) {
    body.innerHTML = `<div style="color:#b91c1c;padding:20px;">${escapeHtml(err.message)}</div>`;
  }
}

async function downloadDoc(e, docId) {
  e.preventDefault();
  const doc = currentData.documents.find(d => d.id === docId);
  try {
    const blob = await fetchDocBlob(docId);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = doc ? doc.original_filename : `document-${docId}`; a.click();
    // Revoking synchronously can abort the save on large files — the browser
    // may still be reading from the blob URL when click() returns.
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  } catch (err) {
    alert(err.message);
  }
}

async function load() {
  // Surface failures on the page. Without this the heading sits on
  // "Loading..." forever and the cause is invisible.
  if (!candidateId) {
    showLoadError("No candidate selected — this page needs an ?id= in the URL.");
    return;
  }
  try {
    currentData = await apiFetch(`/hr/candidates/${candidateId}`);
  } catch (err) {
    showLoadError(err.message);
    return;
  }
  // Which cards get an "Allow Candidate Edit" button comes from edit-access,
  // so it has to be in hand before the first render. A failure here must not
  // blank out the candidate record — just draw it without the grant controls.
  try {
    editAccess = await apiFetch(`/hr/candidates/${candidateId}/edit-access`);
  } catch (err) { /* leave editAccess as-is */ }
  render();
  loadAudit().catch(() => {});
}

function showLoadError(message) {
  document.getElementById("candName").textContent = "Could not load candidate";
  document.getElementById("candStage").innerHTML = "";
  document.getElementById("credentialsCard").classList.add("hidden");
  document.getElementById("decisionCard").classList.add("hidden");
  document.getElementById("cifCard").classList.add("hidden");
  document.getElementById("auditCard").classList.add("hidden");
  document.getElementById("openAccessBanner").innerHTML = "";
  document.getElementById("followupForms").innerHTML = "";
  const notice = document.getElementById("rejectedNotice");
  notice.classList.remove("hidden");
  notice.innerHTML = `<strong style="color:#b91c1c;">${escapeHtml(message)}</strong>
    <div style="margin-top:8px;color:#6b7280;font-size:0.88rem;">
      The candidate may have been deleted. <a href="dashboard.html">Back to all candidates</a>
    </div>`;
}

function render() {
  const c = currentData;
  document.getElementById("candName").textContent = `${c.name} — ${c.email}`;
  const typeLabel = c.candidate_type === "FRESHER" ? "Fresher / Trainee" : "Experienced";
  document.getElementById("candStage").innerHTML =
    `<span class="badge badge-pending" style="margin-right:6px;">${typeLabel}</span>` + badge(c.stage);

  // Login credentials issued to this candidate (HR-owned; candidate cannot change them)
  document.getElementById("credentialsBody").innerHTML = `
    <div class="field-row"><div class="fname">Login ID</div><div class="fval"><code>${escapeHtml(c.email)}</code></div></div>
    <div class="field-row"><div class="fname">Password</div>
      <div class="fval" id="pwdCell">${c.temp_password
        ? `<code>${escapeHtml(c.temp_password)}</code>`
        : `<span style="color:#6b7280;">Not on record for this candidate.</span>`}</div></div>
    <div class="field-row"><div class="fname">Login Link</div>
      <div class="fval"><a class="link-truncate" href="${escapeHtml(c.login_url || "")}"
        target="_blank" rel="noopener noreferrer"
        title="${escapeHtml(c.login_url || "")}">${escapeHtml(c.login_url || "—")}</a></div>
      <div class="field-actions">
        <button class="btn btn-outline btn-small" onclick="copyLoginLink(this)">Copy Link</button>
        <button class="btn btn-outline btn-small" onclick="regenerateLink()">New Link</button>
      </div>
    </div>
    <div style="color:#6b7280;font-size:0.82rem;padding-top:8px;">
      The login link signs the candidate straight into their form — treat it like a password.
      "New Link" issues a fresh one and immediately kills the old one.
    </div>`;

  // Decision card only when awaiting the final-interview decision
  document.getElementById("decisionCard").classList.toggle("hidden", c.stage !== "CIF_SUBMITTED");

  const notice = document.getElementById("rejectedNotice");
  if (c.stage === "REJECTED") {
    notice.classList.remove("hidden");
    notice.innerHTML = `<strong style="color:#b91c1c;">Application Rejected.</strong>
      ${c.rejection_reason ? `<div style="margin-top:6px;color:#6b7280;">Reason: ${escapeHtml(c.rejection_reason)}</div>` : ""}`;
  } else {
    notice.classList.add("hidden");
  }

  // ---- Profile (shared identity fields; shown inside the CIF card) ----
  // No per-field buttons here — the CIF card has one Edit button for the whole form.
  const cifOpts = { editing: editModes.CIF };
  document.getElementById("profileFields").innerHTML = fieldRows("PROFILE", PROFILE_FIELDS, c.profile || {}, cifOpts);
  document.getElementById("actions-CIF").innerHTML = formEditControls("CIF");
  renderOpenAccess();

  // ---- AI Summary & Flags: only once the candidate has actually submitted a CIF.
  // Generated once per page visit (not on every re-render, e.g. after a field
  // edit elsewhere) so an unrelated edit doesn't trigger another LLM call.
  document.getElementById("insightsCard").classList.toggle("hidden", !c.cif_details);
  if (c.cif_details && !insightsLoaded) {
    insightsLoaded = true;
    loadInsights();
  }

  // ---- CIF: flat fields + repeating tables + uploads ----
  const cifRowOpts = { editing: editModes.CIF, showDelete: editModes.CIF };
  let cifHtml = fieldRows("CIF", CIF_FIELDS, c.cif_details, cifOpts);
  if (c.cif_details) {
    for (const [section, label] of Object.entries(EDUCATION_SECTION_LABELS)) {
      cifHtml += rowTable(label, "education", EDUCATION_COLUMNS, (c.education || {})[section], cifRowOpts);
    }
    cifHtml += rowTable("Employment Details", "employment", EMPLOYMENT_COLUMNS, c.employment, cifRowOpts);
    cifHtml += rowTable("References", "references", REFERENCE_COLUMNS, c.references, cifRowOpts);
  }
  document.getElementById("cifExtraFields").innerHTML = cifHtml;
  document.getElementById("cifDocs").innerHTML = renderDocs(c.documents, "CIF", cifOpts);

  // ---- Follow-up forms (BGV / Document Collection) ----
  document.getElementById("followupForms").innerHTML = "";
  // Sequential order: Document Collection is reviewed first, then BGV.
  const followupConfig = {
    DOCUMENT_COLLECTION: { title: FORM_TITLES.DOCUMENT_COLLECTION, fields: DOC_FIELDS, data: c.doc_details },
    BGV: { title: FORM_TITLES.BGV, fields: BGV_FIELDS, data: c.bgv_details },
  };
  Object.entries(followupConfig).forEach(([type, cfg]) => {
    const sub = c.submissions.find(s => s.form_type === type);
    if (!sub) return;
    if (sub.status === "LOCKED") {
      const wrap = document.createElement("div");
      wrap.className = "card section-card collapsed";
      wrap.style.opacity = "0.7";
      wrap.innerHTML = `<div class="section-title collapsible" onclick="toggleCollapse(this)">
          <h3>${cfg.title} <span class="badge badge-locked">LOCKED</span></h3>
          <span class="chevron">&#9660;</span></div>
        <p style="color:#6b7280;">Unlocks for the candidate once you approve their
        Document Collection form above.</p>`;
      document.getElementById("followupForms").appendChild(wrap);
      return;
    }
    const canReview = sub.status === "SUBMITTED" || sub.status === "UNDER_REVIEW";
    const submitted = sub.status !== "PENDING";
    const editing = editModes[type];
    const wrap = document.createElement("div");
    wrap.id = FORM_CARD_ID[type];
    wrap.className = "card section-card collapsed";
    wrap.innerHTML = `
      <div class="section-title collapsible" onclick="toggleCollapse(this)">
        <h3>${cfg.title} <span class="badge badge-${sub.status.toLowerCase()}">${sub.status.replaceAll("_"," ")}</span></h3>
        <div class="section-title-actions" onclick="event.stopPropagation()">
          ${submitted ? formEditControls(type) : ""}
        </div>
        <span class="chevron">&#9660;</span>
      </div>
      ${!submitted ? "<p style='color:#6b7280'>Waiting for candidate to submit.</p>" :
        fieldRows(type, cfg.fields, cfg.data, { editing })
          + (type === "BGV" ? bgvTables(c, { editing, showDelete: editing }) : "")
          + `<div id="${FORM_DOCS_ID[type]}">${renderDocs(c.documents, type, { editing })}</div>`}
      ${canReview && !editing ? `
        <div class="decision-bar">
          <button class="btn btn-success btn-small" onclick="reviewSubmission(${sub.id}, 'APPROVED')">Approve</button>
          <button class="btn btn-danger btn-small" onclick="reviewSubmission(${sub.id}, 'REJECTED')">Reject</button>
        </div>` : ""}
    `;
    document.getElementById("followupForms").appendChild(wrap);
  });

  // A card under edit must stay open: render() rebuilds the follow-up cards
  // collapsed, and collapsing the form someone is typing in would hide it.
  Object.keys(editModes).forEach(form => {
    if (!editModes[form]) return;
    const card = document.getElementById(FORM_CARD_ID[form]);
    if (card) card.classList.remove("collapsed");
  });

  // ---- Mark onboarding complete once both follow-ups are approved ----
  if (c.stage === "APPROVED_FOR_BGV") {
    const bgv = c.submissions.find(s => s.form_type === "BGV");
    const doc = c.submissions.find(s => s.form_type === "DOCUMENT_COLLECTION");
    if (bgv && doc && bgv.status === "APPROVED" && doc.status === "APPROVED") {
      const wrap = document.createElement("div");
      wrap.className = "card section-card";
      wrap.innerHTML = `<div class="section-title"><h3>Onboarding</h3></div>
        <p style="color:#6b7280;">Both BGV and Document Collection are approved.</p>
        <button class="btn btn-success" onclick="markComplete()">Mark Onboarding Complete</button>`;
      document.getElementById("followupForms").appendChild(wrap);
    }
  }
}

async function regenerateLink() {
  if (!currentData) return;
  if (!await showConfirm("The link already sent to this candidate will stop working immediately.",
      { title: "Issue a new login link?", confirmText: "Issue New Link" })) return;
  try {
    await apiFetch(`/hr/candidates/${candidateId}/regenerate-link`, { method: "POST" });
    await load();
    alert("New link issued. Copy it and send it to the candidate — the old one no longer works.");
  } catch (err) {
    alert(err.message);
  }
}

// Copies the login link only — that's the one thing HR sends the candidate.
async function copyLoginLink(btn) {
  if (!currentData) return;
  const original = btn.textContent;
  try {
    await navigator.clipboard.writeText(currentData.login_url);
    btn.textContent = "Copied!";
  } catch {
    // Clipboard API needs a secure context; fall back to a selectable prompt.
    window.prompt("Copy the login link:", currentData.login_url);
    return;
  }
  setTimeout(() => { btn.textContent = original; }, 1500);
}

// ---- Whole-form edit ----
// Each form (CIF, Document Collection, BGV) is reviewed as one document, so
// instead of Edit/Delete on every row its card carries a single Edit button
// that turns every value — flat fields, repeating-table cells, attached
// documents — into something editable. Save then PATCHes only what actually
// changed, keeping the field_edit_log meaningful.

function formEditControls(form) {
  if (editModes[form]) {
    return `<button class="btn btn-primary btn-small" onclick="saveFormEdits('${form}', this)">Save Changes</button>
            <button class="btn btn-outline btn-small" onclick="cancelFormEdit('${form}')">Cancel</button>`;
  }
  // Every form freezes for the candidate on submission, so each one needs a
  // way to hand a single value back to them.
  const grant = (editAccess.submitted || {})[form]
    ? `<button class="btn btn-outline btn-small" onclick="openGrantModal('${form}')">Allow Candidate Edit</button>`
    : "";
  return `${grant}<button class="btn btn-outline btn-small" onclick="startFormEdit('${form}')">Edit</button>`;
}

// What the inputs inside one card currently differ from, keyed the way each
// PATCH endpoint wants it. Elements carry their loaded value in data-orig, so
// an untouched field is never sent.
function collectEdits(form) {
  const card = document.getElementById(FORM_CARD_ID[form]);
  const fieldEdits = [];
  const rowEdits = new Map();
  if (!card) return { fieldEdits, rowEdits };

  card.querySelectorAll("[data-edit-field]").forEach(el => {
    if (el.value === el.dataset.orig) return;
    fieldEdits.push({ form: el.dataset.editForm, field: el.dataset.editField, value: el.value });
  });

  // One PATCH per repeating-table row, carrying just that row's changed columns.
  card.querySelectorAll("[data-row-col]").forEach(el => {
    if (el.value === el.dataset.orig) return;
    const key = `${el.dataset.rowTable}:${el.dataset.rowId}`;
    if (!rowEdits.has(key)) {
      rowEdits.set(key, { table: el.dataset.rowTable, id: el.dataset.rowId, values: {} });
    }
    rowEdits.get(key).values[el.dataset.rowCol] = el.value === "" ? null : el.value;
  });

  return { fieldEdits, rowEdits };
}

async function startFormEdit(form) {
  // Only one card is editable at a time — render() redraws every card from
  // currentData, so leaving a second one open would silently drop its typing.
  const other = Object.keys(editModes).find(f => f !== form && editModes[f]);
  if (other) {
    const { fieldEdits, rowEdits } = collectEdits(other);
    if ((fieldEdits.length || rowEdits.size) &&
        !await showConfirm(`Your unsaved changes to the ${FORM_TITLES[other]} will be lost.`,
          { title: "Discard unsaved changes?", confirmText: "Discard", danger: true })) return;
  }
  Object.keys(editModes).forEach(f => { editModes[f] = f === form; });
  render();   // render() re-opens whichever card is under edit
}

function cancelFormEdit(form) {
  editModes[form] = false;
  render();
  const card = document.getElementById(FORM_CARD_ID[form]);
  if (card) card.classList.remove("collapsed");
}

async function saveFormEdits(form, btn) {
  const { fieldEdits, rowEdits } = collectEdits(form);
  if (!fieldEdits.length && !rowEdits.size) {
    cancelFormEdit(form);   // nothing changed — just leave edit mode
    return;
  }

  // Every change to submitted data is audited, and an audit entry without a
  // reason is close to useless — so the reason is collected before saving.
  const count = fieldEdits.length + rowEdits.size;
  const reason = await askReason({
    title: "Why are you changing this?",
    subtitle: `${count} change(s) to ${FORM_TITLES[form]}. This is stored in the `
              + `candidate's Change History next to the old and new values.`,
  });
  if (reason === null) return;   // cancelled — stay in edit mode, nothing lost

  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Saving…";
  try {
    // One request for the whole save: it applies in a single transaction, so a
    // failure changes nothing, and the audit trail shows it as one entry.
    await apiFetch(`/hr/candidates/${candidateId}/changes`, {
      method: "POST",
      body: JSON.stringify({
        fields: fieldEdits.map(e => ({
          form: e.form, field_name: e.field, new_value: e.value === "" ? null : e.value })),
        rows: [...rowEdits.values()].map(r => ({
          table: r.table, row_id: Number(r.id), values: r.values })),
        reason,
      }),
    });
    editModes[form] = false;
    await load();   // render() restores the read-only view and the Edit button
  } catch (err) {
    alert(err.message);
    btn.disabled = false;
    btn.textContent = original;
  }
}


// Modal prompt for the mandatory reason on any HR action that touches the
// audit trail. Resolves the text, or null if HR backed out — a cancel must
// leave their typing on the page untouched.
function askReason({ title = "Reason for this change", subtitle = "",
                      okText = "Save Changes", danger = false,
                      placeholder = "Why is this being changed?" } = {}) {
  const modal = document.getElementById("reasonModal");
  const text = document.getElementById("reasonText");
  const error = document.getElementById("reasonError");
  const okBtn = document.getElementById("reasonOkBtn");
  const cancelBtn = document.getElementById("reasonCancelBtn");

  document.getElementById("reasonTitle").textContent = title;
  document.getElementById("reasonSubtitle").textContent = subtitle;
  text.value = "";
  text.placeholder = placeholder;
  error.textContent = "";
  okBtn.textContent = okText;
  okBtn.className = `btn ${danger ? "btn-danger" : "btn-primary"}`;
  modal.classList.remove("hidden");
  text.focus();

  return new Promise((resolve) => {
    function done(result) {
      modal.classList.add("hidden");
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      resolve(result);
    }
    function onOk() {
      const value = text.value.trim();
      if (value.length < 5) {
        error.textContent = "Please write at least a few words.";
        return;
      }
      done(value);
    }
    function onCancel() { done(null); }
    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
  });
}


// Uploads happen the moment a file is chosen — a file input's selection can't
// survive the re-render that Save triggers. Only that form's document list is
// redrawn afterwards, so text edits typed elsewhere in the card aren't lost.
async function refreshFormDocs(form) {
  const fresh = await apiFetch(`/hr/candidates/${candidateId}`);
  currentData.documents = fresh.documents;
  const container = document.getElementById(FORM_DOCS_ID[form]);
  if (container) {
    container.innerHTML = renderDocs(currentData.documents, form, { editing: editModes[form] });
  }
}

async function uploadDoc(formType, fieldKey, input) {
  const file = input.files && input.files[0];
  if (!file) return;
  input.value = "";   // let the same file be re-picked if the upload fails

  // Replacing a candidate's document is a change to submitted data like any
  // other, so it is explained and audited the same way.
  const existing = currentData.documents.find(
    d => d.form_type === formType && d.field_key === fieldKey);
  const reason = await askReason({
    title: existing ? "Replace this document?" : "Attach this document?",
    subtitle: `${labelFor(fieldKey)} — ${file.name}. `
              + (existing ? "The file it replaces is kept, so both versions stay openable "
                            + "from the Change History. " : "")
              + "Say why you are doing this.",
    placeholder: "e.g. Candidate emailed a clearer scan.",
    okText: existing ? "Replace" : "Attach",
  });
  if (reason === null) return;

  const body = new FormData();
  body.append("file", file);
  body.append("reason", reason);
  try {
    await apiFetch(`/hr/candidates/${candidateId}/documents/${formType}/${fieldKey}`,
                    { method: "POST", body });
    await refreshFormDocs(formType);
    await loadAudit();
  } catch (err) {
    alert(err.message);
  }
}

async function removeDoc(formType, docId) {
  const doc = currentData.documents.find(d => d.id === docId);
  const reason = await askReason({
    title: `Remove "${doc ? doc.original_filename : "this document"}"?`,
    subtitle: "The document goes back to \"Not submitted\". The file itself is kept, so it "
              + "stays openable from the Change History. Say why you are removing it.",
    placeholder: "e.g. Wrong document uploaded for this field.",
    okText: "Remove",
    danger: true,
  });
  if (reason === null) return;
  try {
    await apiFetch(`/hr/documents/${docId}?reason=${encodeURIComponent(reason)}`,
                    { method: "DELETE" });
    await refreshFormDocs(formType);
    await loadAudit();
  } catch (err) {
    alert(err.message);
  }
}


// ---- Delete one entry from a repeating section (edit mode only) ----

async function deleteRow(tableName, rowId) {
  if (!await showConfirm("This cannot be undone, and the form is reloaded — any other "
      + "unsaved changes in it are discarded.",
      { title: "Delete this entry?", confirmText: "Delete", danger: true })) return;
  try {
    await apiFetch(`/hr/rows/${tableName}/${rowId}`, { method: "DELETE" });
    await load();
  } catch (err) {
    alert(err.message);
  }
}

// ---- Field-level edit access ----
// Every submitted form is read-only to the candidate. When something turns
// out to be wrong, HR opens that one value — a field, an upload, or a single
// cell of one repeating entry — rather than the whole form; the candidate
// then has to say why they are changing it, and the change is audited.

function fmtWhen(value) {
  if (!value) return "";
  const d = new Date(value);
  return isNaN(d) ? String(value) : d.toLocaleString();
}

// Identifies one grantable value, and matches the key the checkbox carries.
function accessKey(item) {
  return [item.form_type, item.field_kind, item.field_name,
          item.row_table || "", item.row_id || ""].join("|");
}

// "Employment #2 — Acme Ltd → Reason for Leaving", or just the field label.
function accessLabel(item) {
  const field = labelFor(item.field_name);
  if (item.field_kind === "ROW_FIELD") return `${item.row_label} → ${field}`;
  if (item.field_kind === "DOCUMENT") return `${field} (document)`;
  return field;
}

async function loadEditAccess() {
  editAccess = await apiFetch(`/hr/candidates/${candidateId}/edit-access`);
  render();   // the grant buttons and the open-access banner both come from it
}

function renderOpenAccess() {
  const host = document.getElementById("openAccessBanner");
  if (!host) return;
  const open = (editAccess.permissions || []).filter(p => p.status === "ACTIVE");
  if (!open.length) { host.innerHTML = ""; return; }
  host.innerHTML = `
    <div class="access-banner">
      <div class="access-banner-head">
        <span>Open for the candidate to edit — everything else stays locked.</span>
        ${open.length > 1
          ? `<button class="btn btn-outline btn-small" onclick="revokeAccess()">Revoke all</button>`
          : ""}
      </div>
      ${open.map(p => `
        <div class="access-row">
          <div class="access-text">
            <span class="access-field">${escapeHtml(accessLabel(p))}</span>
            <span class="access-meta">${escapeHtml(FORM_TITLES[p.form] || p.form)}
              &nbsp;·&nbsp; currently: ${p.current_value
                ? escapeHtml(String(p.current_value)) : "<em>not provided</em>"}
              &nbsp;·&nbsp; opened ${escapeHtml(fmtWhen(p.granted_at))}${p.granted_by
                ? " by " + escapeHtml(p.granted_by) : ""}</span>
            ${p.hr_note ? `<span class="access-meta">Note: ${escapeHtml(p.hr_note)}</span>` : ""}
          </div>
          <button class="btn btn-outline btn-small" onclick="revokeAccess(${p.id})">Revoke</button>
        </div>`).join("")}
    </div>`;
}

let grantModalForm = "CIF";

function openGrantModal(form) {
  grantModalForm = form;
  const openKeys = new Set((editAccess.permissions || [])
    .filter(p => p.status === "ACTIVE").map(accessKey));
  const items = (editAccess.grantable || {})[form] || [];

  document.getElementById("grantModalTitle").textContent =
    `Allow the candidate to edit specific values — ${FORM_TITLES[form]}`;
  document.getElementById("grantList").innerHTML = items.length
    ? items.map(item => {
        const key = accessKey(item);
        const isOpen = openKeys.has(key);
        const label = accessLabel(item);
        return `
          <label class="grant-option" data-search="${escapeHtml(label.toLowerCase())} ${escapeHtml(item.field_name)}">
            <input type="checkbox" value="${escapeHtml(key)}" ${isOpen ? "disabled" : ""}>
            <span class="grant-label">${escapeHtml(label)}</span>
            ${isOpen ? `<span class="grant-open-tag">already open</span>` : ""}
          </label>`;
      }).join("")
    : `<p style="color:var(--muted);margin:8px 4px;">This form has no editable values on record.</p>`;

  document.getElementById("grantSearch").value = "";
  document.getElementById("grantNote").value = "";
  document.getElementById("grantError").textContent = "";
  document.getElementById("grantModal").classList.remove("hidden");
  document.getElementById("grantSearch").focus();
}

function closeGrantModal() {
  document.getElementById("grantModal").classList.add("hidden");
}

document.getElementById("grantSearch").addEventListener("input", (e) => {
  const q = e.target.value.trim().toLowerCase();
  document.querySelectorAll("#grantList .grant-option").forEach(el => {
    el.classList.toggle("hidden", !!q && !el.dataset.search.includes(q));
  });
});

document.getElementById("grantModal").addEventListener("click", (e) => {
  if (e.target.id === "grantModal") closeGrantModal();
});

async function submitGrants(btn) {
  const checked = Array.from(
    document.querySelectorAll("#grantList input[type=checkbox]:checked"));
  const error = document.getElementById("grantError");
  if (!checked.length) {
    error.textContent = "Tick at least one value.";
    return;
  }
  const grants = checked.map(cb => {
    const [form_type, field_kind, field_name, row_table, row_id] = cb.value.split("|");
    return { form_type, field_kind, field_name,
             row_table: row_table || null, row_id: row_id ? Number(row_id) : null };
  });

  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Opening…";
  try {
    const res = await apiFetch(`/hr/candidates/${candidateId}/edit-access`, {
      method: "POST",
      body: JSON.stringify({ grants, hr_note: document.getElementById("grantNote").value }),
    });
    closeGrantModal();
    await loadEditAccess();
    alert(res.detail);
  } catch (err) {
    error.textContent = err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

// Called with a permission id for one field, or with nothing to close every
// field still open. Either way it is one audited action.
async function revokeAccess(permissionId) {
  const open = (editAccess.permissions || []).filter(p => p.status === "ACTIVE");
  const targets = permissionId ? open.filter(p => p.id === permissionId) : open;
  if (!targets.length) return;

  const what = targets.length === 1
    ? accessLabel(targets[0])
    : `all ${targets.length} fields currently open`;
  const reason = await askReason({
    title: targets.length === 1 ? "Withdraw edit access?" : "Withdraw all edit access?",
    subtitle: `The candidate will no longer be able to change ${what}. This is `
              + `recorded in the Change History, so say why you are closing it again.`,
    placeholder: "e.g. Sent to the wrong candidate / no longer needs correcting.",
    okText: targets.length === 1 ? "Revoke" : `Revoke all ${targets.length}`,
    danger: true,
  });
  if (reason === null) return;
  try {
    await apiFetch(`/hr/candidates/${candidateId}/edit-access/revoke`, {
      method: "POST",
      body: JSON.stringify({ permission_ids: targets.map(p => p.id), reason }),
    });
    await loadEditAccess();
    await loadAudit();   // a revoke is itself an audit entry
  } catch (err) {
    alert(err.message);
  }
}


// ---- Change history (audit trail) ----
// One row per change to submitted data: the value before, the value after,
// the reason given, when it happened, and who made it.

async function loadAudit() {
  const body = document.getElementById("auditBody");
  body.innerHTML = `<p style="color:#6b7280;">Loading…</p>`;
  try {
    renderAudit(await apiFetch(`/hr/candidates/${candidateId}/audit`));
  } catch (err) {
    body.innerHTML = `<p style="color:var(--danger);">Could not load the change history: ${escapeHtml(err.message)}</p>`;
  }
}

// Field names from repeating sections arrive as "education.course_college".
function auditFieldLabel(name) {
  if (!name) return "";
  const dot = name.indexOf(".");
  if (dot === -1) return labelFor(name);
  const table = name.slice(0, dot);
  return `${labelFor(name.slice(dot + 1))} (${ROW_TABLE_TITLES[table] || table.replaceAll("_", " ")})`;
}

function auditValue(value) {
  return (value === null || value === undefined || value === "")
    ? `<span class="audit-empty">empty</span>`
    : escapeHtml(String(value));
}

// Everything saved in one action shares a change_set_id, so it reads as one
// entry. Rows predating change sets have none — each of those stands alone.
function groupChangeSets(entries) {
  const groups = [];
  const byId = new Map();
  entries.forEach(e => {
    const key = e.change_set_id || `single-${e.id}`;
    let group = byId.get(key);
    if (!group) {
      group = { key, changes: [], actor_role: e.actor_role, actor_name: e.actor_name,
                edited_at: e.edited_at };
      byId.set(key, group);
      groups.push(group);
    }
    group.changes.push(e);
  });
  return groups;
}

const AUDIT_DAY = { weekday: "short", day: "numeric", month: "long", year: "numeric" };

function auditDayLabel(value) {
  const d = new Date(value);
  if (isNaN(d)) return "";
  const today = new Date();
  const days = Math.round((new Date(today.getFullYear(), today.getMonth(), today.getDate())
                            - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 86400000);
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  return d.toLocaleDateString(undefined, AUDIT_DAY);
}

function auditTimeLabel(value) {
  const d = new Date(value);
  return isNaN(d) ? String(value || "")
                   : d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

// A replaced file is kept on disk, so both sides of a document change can be
// opened straight from the trail.
function fileButton(file, label) {
  if (!file) return "";
  if (!file.available) {
    return `<span class="audit-file is-gone" title="${escapeHtml(file.filename)}">${label}: missing on server</span>`;
  }
  return `<button class="audit-file" title="${escapeHtml(file.filename)}"
    onclick="viewSnapshot(event, ${file.id}, '${escapeHtml(label).replaceAll("'", "&#39;")}')"
    >${label}</button>`;
}

function auditFileBar(e) {
  if (!e.old_file && !e.new_file) return "";
  return `<div class="audit-files">
    ${fileButton(e.old_file, "Previous file")}
    ${fileButton(e.new_file, "New file")}
  </div>`;
}

// One line per change inside a set.
function auditChangeLine(e, showReason, inRevokeSet = false) {
  const body = e.action === "REVOKE"
    // In a set of revokes the heading already says it — repeating the sentence
    // on every line would drown out which fields were closed.
    ? (inRevokeSet ? ""
        : `<span class="audit-access">Edit access withdrawn — the value itself was not changed.</span>`)
    : `<span class="audit-old">${auditValue(e.old_value)}</span>
       <span class="audit-arrow" aria-hidden="true">&rarr;</span>
       <span class="audit-new">${auditValue(e.new_value)}</span>`;
  return `
    <div class="audit-change">
      <div class="audit-change-name">${escapeHtml(auditFieldLabel(e.field_name))}${
        e.action === "DELETE" ? ` <span class="audit-action">cleared</span>` : ""}</div>
      ${body ? `<div class="audit-diff">${body}</div>` : ""}
      ${auditFileBar(e)}
      ${showReason && e.reason
        ? `<div class="audit-reason"><span>Reason</span> ${escapeHtml(e.reason)}</div>`
        : ""}
    </div>`;
}

function renderAudit(entries) {
  const groups = groupChangeSets(entries);
  document.getElementById("auditCount").textContent = groups.length;
  const body = document.getElementById("auditBody");
  if (!groups.length) {
    body.innerHTML = `<div class="audit-empty-state">
      <strong>No changes yet.</strong>
      <span>Every edit to submitted data — by you or by the candidate — is recorded here.</span>
    </div>`;
    return;
  }

  let lastDay = null;
  body.innerHTML = `<div class="audit-timeline">` + groups.map(g => {
    const reasons = [...new Set(g.changes.map(c => c.reason || ""))];
    // One reason for the whole save is stated once; differing ones sit with
    // the change they belong to.
    const sharedReason = reasons.length === 1 ? reasons[0] : null;
    const forms = [...new Set(g.changes.map(c => c.form_type).filter(Boolean))];
    const isCandidate = g.actor_role === "CANDIDATE";
    const allRevokes = g.changes.every(c => c.action === "REVOKE");
    const heading = g.changes.length === 1
      ? auditFieldLabel(g.changes[0].field_name)
      : allRevokes
        ? `Edit access withdrawn — ${g.changes.length} fields`
        : `${g.changes.length} changes`;
    const actor = g.actor_name || (isCandidate ? "the candidate" : "HR");

    const day = auditDayLabel(g.edited_at);
    const separator = day && day !== lastDay
      ? `<div class="audit-day">${escapeHtml(day)}</div>` : "";
    lastDay = day || lastDay;

    return separator + `
      <div class="audit-entry${allRevokes ? " is-revoke" : ""}${isCandidate ? " is-candidate" : ""}">
        <span class="audit-dot" aria-hidden="true"></span>
        <div class="audit-head">
          <span class="audit-field">${escapeHtml(heading)}</span>
          <span class="audit-when" title="${escapeHtml(fmtWhen(g.edited_at))}">
            ${escapeHtml(auditTimeLabel(g.edited_at))}</span>
        </div>
        <div class="audit-byline">
          <span class="audit-avatar">${escapeHtml((actor[0] || "?").toUpperCase())}</span>
          <strong>${escapeHtml(actor)}</strong>
          <span class="audit-role ${isCandidate ? "is-candidate" : "is-hr"}">${
            isCandidate ? "Candidate" : "HR"}</span>
          ${forms.length
            ? `<span class="audit-forms">${escapeHtml(
                 forms.map(f => f.replaceAll("_", " ")).join(" · "))}</span>`
            : ""}
        </div>
        <div class="audit-changes${g.changes.length > 1 ? " is-set" : ""}">
          ${g.changes.map(c => auditChangeLine(c, sharedReason === null,
                                                allRevokes && g.changes.length > 1)).join("")}
        </div>
        ${sharedReason
          ? `<div class="audit-reason"><span>Reason</span> ${escapeHtml(sharedReason)}</div>`
          : sharedReason === ""
            ? `<div class="audit-reason is-missing"><span>Reason</span> not recorded</div>`
            : ""}
      </div>`;
  }).join("") + `</div>`;
}

// Opens an archived file in the same preview modal the document rows use.
async function viewSnapshot(e, snapshotId, label = "Document version") {
  e.preventDefault();
  const body = document.getElementById("viewBody");
  // The button's tooltip carries the filename; put it in the title too so the
  // viewer says which of the two versions is on screen.
  const filename = e.currentTarget ? e.currentTarget.title : "";
  document.getElementById("viewTitle").textContent =
    filename ? `${label} — ${filename}` : label;
  document.getElementById("viewModal").classList.remove("hidden");
  body.innerHTML = "<span style='color:#6b7280'>Loading…</span>";
  document.getElementById("viewDownloadBtn").classList.add("hidden");

  try {
    releasePreview();
    const res = await fetch(`${API_BASE}/hr/document-snapshots/${snapshotId}/download`, {
      headers: { Authorization: `Bearer ${getToken()}` },
    });
    if (!res.ok) {
      const data = await res.json().catch(() => null);
      throw new Error((data && data.detail) || "Could not load this file");
    }
    const blob = await res.blob();
    currentPreviewUrl = URL.createObjectURL(blob);
    renderPreview(body, blob.type, currentPreviewUrl);
    document.getElementById("viewNewTabBtn").onclick = () => window.open(currentPreviewUrl, "_blank");
  } catch (err) {
    body.innerHTML = `<div style="color:#b91c1c;padding:20px;">${escapeHtml(err.message)}</div>`;
  }
}


// ---- Collapsible section headers (CIF / Document Collection / BGV / AI Summary) ----
function toggleCollapse(headerEl) {
  headerEl.closest(".section-card").classList.toggle("collapsed");
}

// ---- AI Summary & Flags ----
let insightsLoaded = false;

// Flag keys are field names ("date_of_birth") or whole sections ("education").
// labelFor covers the real CIF fields; the rest get title-cased so the chip
// never shows a raw DATE_OF_BIRTH-style identifier.
const FLAG_SECTION_LABELS = {
  education: "Education", employment: "Employment", general: "General",
  graduation_year: "Graduation Year",
};

function flagFieldLabel(key) {
  if (!key) return "";
  if (FLAG_SECTION_LABELS[key]) return FLAG_SECTION_LABELS[key];
  const label = labelFor(key);
  return label === key
    ? key.replaceAll("_", " ").replace(/\w/g, ch => ch.toUpperCase())
    : label;
}

function renderInsights(data) {
  // Flags lead with a plain-English headline; the numbers behind it and the
  // suggested action sit underneath. `issue` is the older single-string shape.
  const flagsHtml = data.flags.length
    ? data.flags.map(f => {
        const heading = f.title || f.issue || "";
        const detail = f.title ? (f.detail || "") : "";
        return `
        <div class="insight-flag sev-${f.severity}">
          <span class="sev sev-${f.severity}-badge">${escapeHtml(f.severity)}</span>
          <div class="body">
            <span class="flag-title">${escapeHtml(heading)}</span>
            ${detail ? `<span class="flag-detail">${escapeHtml(detail)}</span>` : ""}
            <span class="field">${escapeHtml(flagFieldLabel(f.field))}</span>
          </div>
        </div>`;
      }).join("")
    : `<p class="insight-empty">No anomalies flagged.</p>`;

  const modelNote = data.generated_by === "llm"
    ? `Generated by ${escapeHtml(data.model || "AI")}.`
    : "AI model unavailable — showing a basic summary and rule-based checks only.";

  document.getElementById("insightsBody").innerHTML = `
    <p class="insight-summary">${escapeHtml(data.summary)}</p>
    <div class="insight-flags">${flagsHtml}</div>
    <div class="insight-meta">${modelNote}</div>`;
}

async function loadInsights() {
  document.getElementById("insightsBody").innerHTML = `<p class="insight-empty">Reading the CIF and checking for inconsistencies…</p>`;
  try {
    const data = await apiFetch(`/hr/candidates/${candidateId}/insights`);
    renderInsights(data);
  } catch (err) {
    document.getElementById("insightsBody").innerHTML =
      `<p class="insight-empty" style="color:var(--danger);">Could not generate summary: ${escapeHtml(err.message)}</p>`;
  }
}

// ---- Decisions ----

async function reviewSubmission(submissionId, decision) {
  if (!await showConfirm(`Mark this form as ${decision}?`, { confirmText: "Confirm" })) return;
  try {
    await apiFetch(`/hr/submissions/${submissionId}/review`, {
      method: "POST",
      body: JSON.stringify({ decision }),
    });
    await load();
  } catch (err) {
    alert(err.message);
  }
}

async function markComplete() {
  if (!await showConfirm("Mark this candidate's onboarding as complete?", { confirmText: "Mark Complete" })) return;
  try {
    await apiFetch(`/hr/candidates/${candidateId}/mark-complete`, { method: "POST" });
    await load();
  } catch (err) {
    alert(err.message);
  }
}

// Deleting an invitation is done from the dashboard list, not here.

document.getElementById("approveBtn").addEventListener("click", async () => {
  if (!currentData) return;  // page never loaded — don't act on a stale id
  if (!await showConfirm("The Document Collection form will be unlocked. BGV opens once you approve their documents.",
      { title: "Approve this candidate?", confirmText: "Approve" })) return;
  try {
    await apiFetch(`/hr/candidates/${candidateId}/approve`, { method: "POST", body: JSON.stringify({}) });
    await load();
  } catch (err) {
    alert(err.message);
  }
});

document.getElementById("rejectBtn").addEventListener("click", () => {
  if (!currentData) return;  // page never loaded — don't act on a stale id
  document.getElementById("rejectModal").classList.remove("hidden");
});

document.getElementById("confirmRejectBtn").addEventListener("click", async () => {
  try {
    await apiFetch(`/hr/candidates/${candidateId}/reject`, {
      method: "POST",
      body: JSON.stringify({ reason: document.getElementById("rejectReason").value }),
    });
    document.getElementById("rejectModal").classList.add("hidden");
    await load();
  } catch (err) {
    alert(err.message);
  }
});

load();
