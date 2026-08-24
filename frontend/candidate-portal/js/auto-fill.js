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
// the system already holds. Once the candidate picks a new file for that
// field, this "will reuse ..." messaging steps aside so only their new
// selection is shown — reappearing if they clear the selection again. It
// only comes back for real if they never save: a submit persists whichever
// file (carried or newly picked) was in effect at that moment.
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
    let viewBtn;
    if (typeof attachViewButton === "function") {
      viewBtn = attachViewButton(input, {
        id: doc.source_document_id,
        original_filename: doc.original_filename,
        content_type: doc.content_type,
        file_available: doc.available !== false,
      }, "View file being reused");
    }
    input.addEventListener("change", () => {
      const pickedNewFile = input.files && input.files.length > 0;
      note.classList.toggle("hidden", pickedNewFile);
      if (viewBtn) viewBtn.classList.toggle("hidden", pickedNewFile);
    });
    count++;
  });
  return count;
}

async function runAutoFill(formType, formEl, bannerHost) {
  const data = await loadAutoFill(formType);
  if (!data) return null;
  applyAutoFilledFields(formEl, data.fields);
  applyCarriedDocuments(formEl, data.documents);
  // Section headings and field labels are intentionally left as the
  // candidate sees them by default — not tagged with the employer name from
  // their CIF ("(OdNest)") — and the top-level "Filled in from your earlier
  // forms" banner is skipped too; the per-field "auto-filled" / "will reuse
  // ..." notes already tell the candidate exactly what happened, field by field.
  return data;
}
