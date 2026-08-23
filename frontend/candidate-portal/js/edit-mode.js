// Shared "edit my submitted form" support for the candidate portal.
// A form is editable until HR reviews it; these helpers reload what the
// candidate already saved so nothing has to be typed twice.

async function loadExistingSubmission(formType) {
  try {
    return await apiFetch(`/candidate/me/submission/${formType}`);
  } catch (err) {
    return null;
  }
}

// Populate plain inputs / selects / textareas / checkboxes by their name.
function fillFlatFields(formEl, fields) {
  if (!fields) return;
  Object.entries(fields).forEach(([name, value]) => {
    if (value === null || value === undefined || value === "") return;
    const el = formEl.querySelector(`[name="${name}"]`);
    if (!el || el.type === "file") return;
    if (el.type === "checkbox") {
      el.checked = true;
      el.dispatchEvent(new Event("change", { bubbles: true }));
    } else {
      el.value = value;
      el.dispatchEvent(new Event("change", { bubbles: true }));
    }
  });
}

// Show what is already on file next to each upload, and stop a previously
// satisfied "required" upload from forcing a re-upload on every edit.
function markExistingUploads(formEl, documents) {
  (documents || []).forEach(doc => {
    const input = formEl.querySelector(`input[type="file"][name="${doc.field_key}"]`);
    if (!input) return;
    input.required = false;
    if (input.dataset.existingShown) return;
    input.dataset.existingShown = "1";
    const note = document.createElement("div");
    note.className = "file-hint";
    note.style.color = "#15803d";
    note.textContent = `Already uploaded: ${doc.original_filename} — choose a file only if you want to replace it.`;
    input.insertAdjacentElement("afterend", note);
  });
}

// Turn the page into an explicit "editing" state.
function applyEditModeChrome(submitBtn, bannerHost, formTitle) {
  if (submitBtn) submitBtn.textContent = "Update " + (formTitle || "Form");
  if (bannerHost && !document.getElementById("editBanner")) {
    const banner = document.createElement("div");
    banner.id = "editBanner";
    banner.style.cssText =
      "background:#fffbeb;border:1px solid #fde68a;border-radius:8px;" +
      "padding:10px 14px;font-size:0.86rem;margin-bottom:16px;";
    banner.innerHTML =
      "<strong>Editing your submitted form.</strong> Your previous answers are loaded below. " +
      "Change what you need and press Update — this replaces what HR sees. " +
      "You can edit until HR reviews it.";
    bannerHost.prepend(banner);
  }
}

// Fill a repeating table from saved rows, using the page's own addRow().
function fillTable(tableId, rows, addRowFn) {
  if (!rows || !rows.length) return 0;
  document.querySelectorAll(`#${tableId} tbody tr`).forEach(tr => tr.remove());
  rows.forEach(row => addRowFn(tableId, row));
  return rows.length;
}
