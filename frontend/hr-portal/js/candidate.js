requireAuth();
document.getElementById("whoami").textContent = getName();
document.getElementById("whoamiAvatar").textContent = getName().charAt(0).toUpperCase();

const candidateId = new URLSearchParams(window.location.search).get("id");
let currentData = null;
let editTarget = null; // { form: 'PROFILE'|'CIF'|'BGV'|'DOCUMENT_COLLECTION', field }

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

// One editable field row: value stored in a relational column, edited/deleted
// through /hr/candidates/{id}/details/{form}/field. The current value is
// looked up from currentData at click time — never inlined into onclick,
// where quotes in the value would break the attribute.
function fieldRow(form, field, value) {
  const hasValue = value !== null && value !== undefined && value !== "";
  return `
    <div class="field-row">
      <div class="fname">${labelFor(field)}</div>
      <div class="fval ${hasValue ? "" : "empty"}">${hasValue ? escapeHtml(String(value)) : "Not provided"}</div>
      <div class="field-actions">
        <button class="btn btn-outline btn-small" onclick="openEditModal('${form}', '${field}')">Edit</button>
        <button class="btn btn-outline btn-small" onclick="deleteField('${form}', '${field}')">Delete</button>
      </div>
    </div>`;
}

function currentFieldValue(form, field) {
  const source = {
    PROFILE: currentData.profile,
    CIF: currentData.cif_details,
    BGV: currentData.bgv_details,
    DOCUMENT_COLLECTION: currentData.doc_details,
  }[form];
  return source ? source[field] : null;
}

function fieldRows(form, fieldList, data) {
  if (!data) return "<p style='color:#6b7280'>Not submitted yet.</p>";
  return fieldList.map(f => fieldRow(form, f, data[f])).join("");
}

// Repeating-row table (education / employment / references) with per-row delete
function rowTable(title, tableName, columns, rows) {
  const header = `<h4 style="margin:16px 0 4px;font-size:0.85rem;color:#6b7280;">${title.toUpperCase()}</h4>`;
  if (!rows || !rows.length) {
    return `${header}<div class="fval empty" style="padding:4px 0 8px;">No entries provided</div>`;
  }
  return `${header}
    <div style="overflow-x:auto;margin-bottom:8px;">
      <table>
        <thead><tr>${columns.map(c => `<th>${labelFor(c)}</th>`).join("")}<th></th></tr></thead>
        <tbody>${rows.map(r => `
          <tr style="cursor:default;">
            ${columns.map(c => `<td>${escapeHtml(String(r[c] ?? ""))}</td>`).join("")}
            <td><button class="btn btn-outline btn-small" onclick="deleteRow('${tableName}', ${r.id})">Delete</button></td>
          </tr>`).join("")}
        </tbody>
      </table>
    </div>`;
}


// The five BGV verification sections, each a repeating table with per-row delete.
function bgvTables(c) {
  const tables = c.bgv_tables || {};
  return Object.keys(BGV_TABLE_TITLES).map(key =>
    rowTable(BGV_TABLE_TITLES[key], key, BGV_TABLE_COLUMNS[key], tables[key])
  ).join("");
}

// Checklist: for every expected upload on this form, show whether the
// candidate submitted it — with a download button when they did.
function renderDocs(allDocs, formType) {
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
        </div>`;
    }
    if (doc) {
      return `
        <div class="field-row">
          <div class="fname">${labelFor(fieldKey)}</div>
          <div class="fval">✅ Submitted — ${escapeHtml(doc.original_filename)}</div>
          <div class="field-actions">
            <button class="btn btn-outline btn-small" onclick="viewDoc(event, ${doc.id})">View</button>
            <button class="btn btn-outline btn-small" onclick="downloadDoc(event, ${doc.id})">Download</button>
          </div>
        </div>`;
    }
    return `
      <div class="field-row">
        <div class="fname">${labelFor(fieldKey)}</div>
        <div class="fval empty">❌ Not submitted</div>
      </div>`;
  }).join("");
  return `<h4 style="margin:16px 0 4px;font-size:0.85rem;color:#6b7280;">UPLOADED DOCUMENTS</h4>${rows}`;
}


// The download endpoint needs an Authorization header, so a plain <img src>
// or <a href> can't reach it. Fetch the bytes once and render from a blob URL.
let currentPreviewUrl = null;

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

    if (type.startsWith("image/")) {
      body.innerHTML = `<img src="${currentPreviewUrl}" alt="" style="max-width:100%;max-height:100%;object-fit:contain;">`;
    } else if (type === "application/pdf") {
      body.innerHTML = `<iframe src="${currentPreviewUrl}" style="width:100%;height:100%;border:0;"></iframe>`;
    } else {
      // Office docs etc. can't render in-browser — offer the alternatives.
      body.innerHTML = `<div style="text-align:center;color:#6b7280;padding:20px;">
        This file type can't be previewed in the browser${type ? " (" + escapeHtml(type) + ")" : ""}.<br>
        Use <strong>Download</strong> or <strong>Open in new tab</strong> instead.</div>`;
    }

    document.getElementById("viewNewTabBtn").onclick = () => window.open(currentPreviewUrl, "_blank");
    document.getElementById("viewDownloadBtn").onclick = (ev) => downloadDoc(ev, docId);
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
    URL.revokeObjectURL(url);
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
    render();
  } catch (err) {
    showLoadError(err.message);
  }
}

function showLoadError(message) {
  document.getElementById("candName").textContent = "Could not load candidate";
  document.getElementById("candStage").innerHTML = "";
  document.getElementById("credentialsCard").classList.add("hidden");
  document.getElementById("decisionCard").classList.add("hidden");
  document.getElementById("cifCard").classList.add("hidden");
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
    <div class="field-row"><div class="fname">Password</div><div class="fval"><code>${c.temp_password ? escapeHtml(c.temp_password) : "—"}</code></div></div>
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

  // ---- Profile (shared identity fields) ----
  document.getElementById("profileFields").innerHTML = fieldRows("PROFILE", PROFILE_FIELDS, c.profile || {});

  // ---- AI Summary & Flags: only once the candidate has actually submitted a CIF.
  // Generated once per page visit (not on every re-render, e.g. after a field
  // edit elsewhere) so an unrelated edit doesn't trigger another LLM call.
  document.getElementById("insightsCard").classList.toggle("hidden", !c.cif_details);
  if (c.cif_details && !insightsLoaded) {
    insightsLoaded = true;
    loadInsights();
  }

  // ---- CIF: flat fields + repeating tables + uploads ----
  let cifHtml = fieldRows("CIF", CIF_FIELDS, c.cif_details);
  if (c.cif_details) {
    for (const [section, label] of Object.entries(EDUCATION_SECTION_LABELS)) {
      cifHtml += rowTable(label, "education", EDUCATION_COLUMNS, (c.education || {})[section]);
    }
    cifHtml += rowTable("Employment Details", "employment", EMPLOYMENT_COLUMNS, c.employment);
    cifHtml += rowTable("References", "references", REFERENCE_COLUMNS, c.references);
  }
  document.getElementById("cifExtraFields").innerHTML = cifHtml;
  document.getElementById("cifDocs").innerHTML = renderDocs(c.documents, "CIF");

  // ---- Follow-up forms (BGV / Document Collection) ----
  document.getElementById("followupForms").innerHTML = "";
  // Sequential order: Document Collection is reviewed first, then BGV.
  const followupConfig = {
    DOCUMENT_COLLECTION: { title: "Document Collection Form", fields: DOC_FIELDS, data: c.doc_details },
    BGV: { title: "Background Verification Form", fields: BGV_FIELDS, data: c.bgv_details },
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
    const wrap = document.createElement("div");
    wrap.className = "card section-card collapsed";
    wrap.innerHTML = `
      <div class="section-title collapsible" onclick="toggleCollapse(this)">
        <h3>${cfg.title} <span class="badge badge-${sub.status.toLowerCase()}">${sub.status.replaceAll("_"," ")}</span></h3>
        <span class="chevron">&#9660;</span>
      </div>
      ${sub.status === "PENDING" ? "<p style='color:#6b7280'>Waiting for candidate to submit.</p>" :
        fieldRows(type, cfg.fields, cfg.data)
          + (type === "BGV" ? bgvTables(c) : "")
          + renderDocs(c.documents, type)}
      ${canReview ? `
        <div class="decision-bar">
          <button class="btn btn-success btn-small" onclick="reviewSubmission(${sub.id}, 'APPROVED')">Approve</button>
          <button class="btn btn-danger btn-small" onclick="reviewSubmission(${sub.id}, 'REJECTED')">Reject</button>
        </div>` : ""}
    `;
    document.getElementById("followupForms").appendChild(wrap);
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
  if (!confirm("Issue a new login link?\n\nThe link already sent to this candidate will stop working immediately.")) return;
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

// ---- Edit / delete field values ----

function openEditModal(form, field) {
  editTarget = { form, field };
  document.getElementById("editModalTitle").textContent = `Edit: ${labelFor(field)}`;
  document.getElementById("editModalLabel").textContent = labelFor(field);
  document.getElementById("editModalValue").value = currentFieldValue(form, field) ?? "";
  document.getElementById("editModal").classList.remove("hidden");
}

document.getElementById("saveEditBtn").addEventListener("click", async () => {
  const value = document.getElementById("editModalValue").value;
  try {
    await apiFetch(`/hr/candidates/${candidateId}/details/${editTarget.form}/field`, {
      method: "PATCH",
      body: JSON.stringify({ field_name: editTarget.field, new_value: value }),
    });
    document.getElementById("editModal").classList.add("hidden");
    await load();
  } catch (err) {
    alert(err.message);
  }
});

async function deleteField(form, field) {
  if (!confirm(`Delete "${labelFor(field)}"? This cannot be undone.`)) return;
  try {
    await apiFetch(`/hr/candidates/${candidateId}/details/${form}/field/${field}`, { method: "DELETE" });
    await load();
  } catch (err) {
    alert(err.message);
  }
}

async function deleteRow(tableName, rowId) {
  if (!confirm("Delete this entry? This cannot be undone.")) return;
  try {
    await apiFetch(`/hr/rows/${tableName}/${rowId}`, { method: "DELETE" });
    await load();
  } catch (err) {
    alert(err.message);
  }
}

// ---- Collapsible section headers (CIF / Document Collection / BGV / AI Summary) ----
function toggleCollapse(headerEl) {
  headerEl.closest(".section-card").classList.toggle("collapsed");
}

// ---- AI Summary & Flags ----
let insightsLoaded = false;

function renderInsights(data) {
  const flagsHtml = data.flags.length
    ? data.flags.map(f => `
        <div class="insight-flag sev-${f.severity}">
          <span class="sev sev-${f.severity}-badge">${f.severity}</span>
          <div class="body">
            <span class="field">${escapeHtml(f.field)}</span>
            ${escapeHtml(f.issue)}
          </div>
        </div>`).join("")
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
  if (!confirm(`Mark this form as ${decision}?`)) return;
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
  if (!confirm("Mark this candidate's onboarding as complete?")) return;
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
  if (!confirm("Approve this candidate? The Document Collection form will be unlocked. BGV opens once you approve their documents.")) return;
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
