// Lets a candidate see any document attached to a field — one already on
// record, one being reused from an earlier form, or a file they just picked.
// The modal is created on demand so no form markup has to change.

let _viewerUrl = null;

function ensureViewer() {
  if (document.getElementById("docViewer")) return;
  const wrap = document.createElement("div");
  wrap.id = "docViewer";
  wrap.className = "modal-backdrop hidden";
  wrap.innerHTML = `
    <div class="modal" style="width:90vw;max-width:900px;">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;">
        <h3 id="docViewerTitle" style="margin:0;overflow:hidden;text-overflow:ellipsis;
            white-space:nowrap;font-size:1rem;">Document</h3>
        <div style="display:flex;gap:8px;flex:0 0 auto;">
          <button type="button" class="btn btn-outline btn-small" id="docViewerNewTab">Open in new tab</button>
          <button type="button" class="btn btn-primary btn-small" id="docViewerClose">Close</button>
        </div>
      </div>
      <div id="docViewerBody" style="margin-top:14px;background:#f9fafb;
           border:1px solid var(--border);border-radius:8px;height:68vh;display:flex;
           align-items:center;justify-content:center;overflow:auto;"></div>
    </div>`;
  document.body.appendChild(wrap);
  document.getElementById("docViewerClose").addEventListener("click", closeViewer);
  wrap.addEventListener("click", (e) => { if (e.target.id === "docViewer") closeViewer(); });
}

function releaseViewerUrl() {
  if (_viewerUrl) {
    URL.revokeObjectURL(_viewerUrl);
    _viewerUrl = null;
  }
}

function closeViewer() {
  const w = document.getElementById("docViewer");
  if (w) w.classList.add("hidden");
  const b = document.getElementById("docViewerBody");
  if (b) b.innerHTML = "";
  releaseViewerUrl();
}

function renderInViewer(url, type, title) {
  const body = document.getElementById("docViewerBody");
  document.getElementById("docViewerTitle").textContent = title || "Document";
  if ((type || "").startsWith("image/")) {
    body.innerHTML = `<img src="${url}" alt="" style="max-width:100%;max-height:100%;object-fit:contain;">`;
  } else if (type === "application/pdf") {
    body.innerHTML = `<iframe src="${url}" style="width:100%;height:100%;border:0;"></iframe>`;
  } else {
    body.innerHTML = `<div style="text-align:center;color:#6b7280;padding:20px;">
      This file type can't be shown in the browser. Use <strong>Open in new tab</strong>
      to download it.</div>`;
  }
  document.getElementById("docViewerNewTab").onclick = () => window.open(url, "_blank");
}

// A document already stored for this candidate (any of their forms).
async function viewStoredDoc(docId, filename, contentType) {
  ensureViewer();
  document.getElementById("docViewer").classList.remove("hidden");
  const body = document.getElementById("docViewerBody");
  document.getElementById("docViewerTitle").textContent = filename || "Document";
  body.innerHTML = "<span style='color:#6b7280'>Loading…</span>";
  try {
    releaseViewerUrl();
    const res = await fetch(`${API_BASE}/candidate/documents/${docId}/download`, {
      headers: { Authorization: `Bearer ${getToken()}` },
    });
    if (!res.ok) {
      const msg = res.status === 404
        ? "This file is no longer stored on the server. Please upload it again."
        : "Could not load this file.";
      body.innerHTML = `<div style="color:#b91c1c;padding:20px;text-align:center;">${msg}</div>`;
      return;
    }
    const blob = await res.blob();
    _viewerUrl = URL.createObjectURL(blob);
    renderInViewer(_viewerUrl, contentType || blob.type, filename);
  } catch (err) {
    body.innerHTML = `<div style="color:#b91c1c;padding:20px;">Could not load this file.</div>`;
  }
}

// A file the candidate has just chosen but not submitted yet.
function viewLocalFile(input) {
  if (!input.files || !input.files[0]) return;
  const file = input.files[0];
  ensureViewer();
  document.getElementById("docViewer").classList.remove("hidden");
  releaseViewerUrl();
  _viewerUrl = URL.createObjectURL(file);
  renderInViewer(_viewerUrl, file.type, `${file.name} (not yet submitted)`);
}

// --- attaching the buttons -------------------------------------------------

function makeViewButton(label, onClick) {
  const b = document.createElement("button");
  b.type = "button";                       // never submit the form
  b.className = "btn btn-outline btn-small";
  b.style.marginTop = "4px";
  b.textContent = label;
  b.addEventListener("click", onClick);
  return b;
}

/** Adds a View button for the document currently attached to a file input.
 * Returns the button element (or undefined if none was created), so callers
 * that need to show/hide it later — e.g. once the candidate picks a new
 * file to replace it — have a direct reference. */
function attachViewButton(input, doc, labelPrefix) {
  if (!input || !doc || input.dataset.viewBtn) return undefined;
  input.dataset.viewBtn = "1";
  if (doc.file_available === false) {
    const warn = document.createElement("div");
    warn.className = "file-hint";
    warn.style.color = "#b45309";
    warn.textContent = "This file is missing on the server — please upload it again.";
    input.insertAdjacentElement("afterend", warn);
    // Re-impose "required" only if the field was mandatory to begin with;
    // an optional upload going missing must not block resubmission.
    if (input.dataset.origRequired !== undefined) {
      input.required = input.dataset.origRequired === "1";
    }
    return undefined;
  }
  const btn = makeViewButton(labelPrefix || "View uploaded file",
    () => viewStoredDoc(doc.id, doc.original_filename, doc.content_type));
  input.insertAdjacentElement("afterend", btn);
  return btn;
}

// Same whitelist the backend enforces (app/utils/file_storage.py). The OS
// file picker's "All Files" option can't be removed from the page side, so
// this is what actually stops a disallowed file from ever sitting in the
// form: the moment one is picked, it's cleared right back out again.
const _ALLOWED_UPLOAD_EXTENSIONS = new Set(
  [".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf", ".doc", ".docx"]);

function _fileExtension(filename) {
  const m = /\.[^./\\]+$/.exec(filename || "");
  return m ? m[0].toLowerCase() : "";
}

/** Clears the input and shows why if the picked file's type isn't allowed.
 * Returns true when the selection is fine (or empty) and can stand. */
function rejectDisallowedFile(input) {
  if (!input._rejectMsgEl) {
    input._rejectMsgEl = document.createElement("div");
    input._rejectMsgEl.className = "file-reject-msg";
    input.insertAdjacentElement("afterend", input._rejectMsgEl);
  }
  if (!input.files || !input.files.length) {
    input._rejectMsgEl.textContent = "";
    return true;
  }
  const file = input.files[0];
  if (_ALLOWED_UPLOAD_EXTENSIONS.has(_fileExtension(file.name))) {
    input._rejectMsgEl.textContent = "";
    return true;
  }
  input.value = "";   // clears input.files too — nothing invalid stays attached
  input._rejectMsgEl.textContent =
    `"${file.name}" isn't allowed — only images (JPG, PNG, GIF, WEBP), PDF, ` +
    "and Word documents (.doc/.docx) can be uploaded.";
  return false;
}

/**
 * Every file input gets a "Preview" button that appears once the candidate
 * picks a file, so they can confirm they attached the right document — and a
 * "Remove" button next to it, so an accidental pick can be undone without
 * having to find and re-select a different file just to overwrite it.
 */
function enableLocalPreviews(formEl) {
  formEl.querySelectorAll('input[type="file"]').forEach(input => {
    if (input.dataset.localPreview) return;
    input.dataset.localPreview = "1";

    const previewBtn = makeViewButton("Preview selected file", () => viewLocalFile(input));
    previewBtn.classList.add("hidden");
    input.insertAdjacentElement("afterend", previewBtn);

    const removeBtn = makeViewButton("Remove", () => {
      input.value = "";
      // Let every listener on this input (this one, plus the carried-document
      // reuse toggle if this field has one) react the same way a real
      // selection change would — one source of truth, not duplicated logic.
      input.dispatchEvent(new Event("change"));
    });
    removeBtn.classList.add("hidden", "btn-remove-file");
    previewBtn.insertAdjacentElement("afterend", removeBtn);

    input.addEventListener("change", () => {
      const ok = rejectDisallowedFile(input);
      const hasFile = ok && !!(input.files && input.files.length);
      previewBtn.classList.toggle("hidden", !hasFile);
      removeBtn.classList.toggle("hidden", !hasFile);
    });
  });
}
