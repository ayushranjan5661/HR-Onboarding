// Applies the cross-form mapping agent's results to a form.
// Anything filled in for the candidate is visibly marked with where it came
// from, and stays editable — the agent assists, it never decides.

const SOURCE_NAMES = {
  CIF: "your CIF",
  DOCUMENT_COLLECTION: "your Document Collection form",
  BGV: "your BGV form",
};

async function loadAutoFill(formType) {
  try {
    return await apiFetch(`/candidate/me/prefill/${formType}`);
  } catch (err) {
    return null;   // agent unavailable -> form still works, just unassisted
  }
}

function autoFillBadge(text) {
  const el = document.createElement("div");
  el.className = "auto-filled-note";
  el.textContent = text;
  return el;
}

// Fill text fields the agent matched, unless the candidate already typed there.
function applyAutoFilledFields(formEl, fields) {
  let count = 0;
  Object.entries(fields || {}).forEach(([name, info]) => {
    const el = formEl.querySelector(`[name="${name}"]`);
    if (!el || el.type === "file" || el.type === "checkbox") return;
    if (el.value) return;
    el.value = info.value;
    el.classList.add("auto-filled");
    const host = el.closest("div") || el.parentElement;
    if (host && !host.querySelector(".auto-filled-note")) {
      host.appendChild(autoFilledNoteFor(info));
    }
    count++;
  });
  return count;
}

function autoFilledNoteFor(info) {
  return autoFillBadge(`Filled from ${SOURCE_NAMES[info.source_form] || info.source_form}`
                        + " — edit if this is wrong");
}

// Show uploads that will be reused, so the candidate isn't asked for a file
// the system already holds.
function applyCarriedDocuments(formEl, documents) {
  let count = 0;
  (documents || []).forEach(doc => {
    const input = formEl.querySelector(`input[type="file"][name="${doc.target_field}"]`);
    if (!input) return;
    input.required = false;            // already satisfied by the earlier upload
    if (input.dataset.carriedShown) return;
    input.dataset.carriedShown = "1";
    const note = autoFillBadge(
      `Will reuse "${doc.original_filename}" from ${SOURCE_NAMES[doc.source_form] || doc.source_form}`
      + " — choose a file only to replace it."
    );
    note.classList.add("carried-doc");
    input.insertAdjacentElement("afterend", note);
    // The reused file is one of the candidate's own uploads, so they can view it.
    if (typeof attachViewButton === "function") {
      attachViewButton(input, {
        id: doc.source_document_id,
        original_filename: doc.original_filename,
        content_type: doc.content_type,
        file_available: doc.available !== false,
      }, "View file being reused");
    }
    count++;
  });
  return count;
}

// Name the Document Collection's company sections after the real employers.
function applyCompanyLabels(labels) {
  Object.entries(labels || {}).forEach(([prefix, company]) => {
    document.querySelectorAll(`.doc-row[data-field^="${prefix}_"]`).forEach(row => {
      const lbl = row.querySelector("label");
      if (!lbl || lbl.dataset.companyNamed) return;
      lbl.dataset.companyNamed = "1";
      const tag = document.createElement("span");
      tag.className = "company-tag";
      tag.textContent = ` (${company})`;
      lbl.appendChild(tag);
    });
    // also tag the section heading
    document.querySelectorAll(".doc-group").forEach(group => {
      const first = group.querySelector(`.doc-row[data-field^="${prefix}_"]`);
      const h = group.querySelector("h3");
      if (first && h && !h.dataset.companyNamed) {
        h.dataset.companyNamed = "1";
        h.textContent = `${h.textContent} — ${company}`;
      }
    });
  });
}

// One summary line so the candidate knows the agent did something.
function showAutoFillSummary(host, filled, carried) {
  if (!host || (!filled && !carried)) return;
  const parts = [];
  if (filled) parts.push(`${filled} field${filled > 1 ? "s" : ""} filled in`);
  if (carried) parts.push(`${carried} document${carried > 1 ? "s" : ""} reused`);
  const box = document.createElement("div");
  box.id = "autoFillSummary";
  box.style.cssText = "background:#ecfdf5;border:1px solid #a7f3d0;border-radius:8px;" +
                       "padding:10px 14px;font-size:0.86rem;margin-bottom:16px;";
  box.innerHTML = `<strong>Filled in from your earlier forms:</strong> ${parts.join(" and ")}. ` +
                   "Everything is editable — please check it before submitting.";
  host.prepend(box);
}

async function runAutoFill(formType, formEl, bannerHost) {
  const data = await loadAutoFill(formType);
  if (!data) return null;
  const filled = applyAutoFilledFields(formEl, data.fields);
  const carried = applyCarriedDocuments(formEl, data.documents);
  applyCompanyLabels(data.company_labels);
  showAutoFillSummary(bannerHost, filled, carried);
  return data;
}
