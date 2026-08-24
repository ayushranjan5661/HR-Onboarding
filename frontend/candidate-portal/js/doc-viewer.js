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

/** Adds a View button for the document currently attached to a file input. */
function attachViewButton(input, doc, labelPrefix) {
  if (!input || !doc || input.dataset.viewBtn) return;
  input.dataset.viewBtn = "1";
  if (doc.file_available === false) {
    const warn = document.createElement("div");
    warn.className = "file-hint";
    warn.style.color = "#b45309";
    warn.textContent = "This file is missing on the server — please upload it again.";
    input.insertAdjacentElement("afterend", warn);
    input.required = true;
    return;
  }
  const btn = makeViewButton(labelPrefix || "View uploaded file",
    () => viewStoredDoc(doc.id, doc.original_filename, doc.content_type));
  input.insertAdjacentElement("afterend", btn);
}

/**
 * Every file input gets a "Preview" button that appears once the candidate
 * picks a file, so they can confirm they attached the right document.
 */
function enableLocalPreviews(formEl) {
  formEl.querySelectorAll('input[type="file"]').forEach(input => {
    if (input.dataset.localPreview) return;
    input.dataset.localPreview = "1";
    const btn = makeViewButton("Preview selected file", () => viewLocalFile(input));
    btn.classList.add("hidden");
    input.insertAdjacentElement("afterend", btn);
    input.addEventListener("change", () => {
      btn.classList.toggle("hidden", !(input.files && input.files.length));
    });
  });
}
