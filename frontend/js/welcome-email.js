// Welcome-email draft sent to a candidate once their account (Candidate ID)
// is created. There is no SMTP on the backend, so HR reviews the draft here,
// then copies it or opens it in their own mail app.

function _fmtExpiry(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  if (isNaN(d)) return null;
  return d.toLocaleString("en-IN", {
    day: "2-digit", month: "short", year: "numeric",
    hour: "2-digit", minute: "2-digit", hour12: true, timeZone: "Asia/Kolkata",
  }).replace(/\b(am|pm)\b/, (m) => m.toUpperCase()) + " IST";
}

const EMAIL_TEMPLATES = {
  CIF: {
    label: "Candidate Information Form (CIF)",
    subject: "LevelShift - Complete Your Candidate Information Form (CIF)",
    // Mirrors HR's approved CIF mail.
    body: (data, name) => [
        `Dear ${name},`,
        ``,
        `Greetings from LevelShift!`,
        ``,
        `Congratulations on successfully clearing Level 1 discussion.`,
        ``,
        `As the next step, please complete the Candidate Information Form (CIF) on our candidate portal. The information provided will help us proceed with the next stages of the selection process.`,
        ``,
        `Please use the link below to access the form:`,
        ``,
        `${data.login_url || "-"}`,
        ``,
        `While filling out the form, please make sure all information is accurate and matches your official documents.`,
        ``,
        `Please complete and submit the form at the earliest.`,
        ``,
        `If you face any issues, please reply to this email and our team will assist you.`,
        ``,
        `Regards,`,
        `HR Team`,
        `LevelShift`,
    ],
  },
  DOCUMENT_COLLECTION: {
    label: "Document Collection Form",
    subject: "LevelShift Onboarding - Upload your documents (Document Collection Form)",
    // Mirrors HR's approved Document Collection mail.
    body: (data, name) => {
      const expiry = _fmtExpiry(data.login_link_expires_at);
      return [
        `Dear ${name},`,
        ``,
        `Greetings from LevelShift!`,
        ``,
        `Congratulations on clearing all the discussions! As the next step in your onboarding, please upload your documents through the Document Collection Form on our onboarding portal.`,
        ``,
        `Please use the link below to access the form:`,
        ``,
        `${data.login_url || "-"}`,
        ``,
        ...(expiry ? [`This link expires on ${expiry}.`, ``] : []),
        `Please keep clear and readable copies of the following documents ready:`,
        ``,
        `Personal Documents`,
        `- Passport-size photograph`,
        `- PAN Card`,
        `- Aadhaar Card`,
        `- Passport, if available`,
        `- Address proof and ID proof`,
        ``,
        `Educational Documents`,
        `- 10th Marksheet`,
        `- 12th Marksheet / Diploma`,
        `- UG marksheets and certificate`,
        `- PG marksheets and certificate, if applicable`,
        `- Other educational certificates or certifications`,
        ...(data.candidate_type === "FRESHER" ? [] : [
          ``,
          `Employment Documents (current and each previous company)`,
          `- Offer letter`,
          `- Role change letter, if any`,
          `- Experience letter`,
          `- Relieving letter (not required for your current company if you are still serving)`,
          `- Last 3 months pay slips`,
        ]),
        ``,
        `Please make sure that all documents are complete, clear, and readable, and that your name is consistent across the documents.`,
        ``,
        `Please upload and submit all the required documents at the earliest.`,
        ``,
        `If you face any issues, please reply to this email and our team will assist you.`,
        ``,
        `Regards,`,
        `HR Team`,
        `LevelShift`,
      ];
    },
  },
};

// data: { name, email, candidate_type, temp_password, login_url, login_page_url, login_link_expires_at }
function buildWelcomeEmail(data, template = "CIF") {
  const t = EMAIL_TEMPLATES[template] || EMAIL_TEMPLATES.CIF;
  const name = (data.name || "").trim() || "Candidate";
  const lines = t.body(data, name);
  return { to: data.email, subject: t.subject, body: lines.join("\n") };
}

// Renders an editable draft (subject + body) with Copy / Open-in-mail actions.
// opts.templates: which templates to offer (picker hidden when only one);
// opts.template: the one selected initially.
function renderWelcomeEmailDraft(container, data, opts = {}) {
  const templates = opts.templates || ["CIF"];
  let current = opts.template || templates[0];
  // The candidate page re-renders after every action. Keep what HR has typed
  // into the draft rather than resetting it to the template, as long as it is
  // still the same recipient and that template is still on offer.
  const prev = container.dataset.weEdited === "1" && {
    to: container.dataset.weTo, template: container.dataset.weTemplate,
    subject: container.querySelector(".we-subject")?.value,
    body: container.querySelector(".we-body")?.value,
  };
  const keep = prev && prev.subject != null && templates.includes(prev.template)
    && prev.to === String(buildWelcomeEmail(data, prev.template).to);
  if (keep) current = prev.template;
  const mail = buildWelcomeEmail(data, current);
  if (keep) { mail.subject = prev.subject; mail.body = prev.body; }
  container.dataset.weTo = String(mail.to);
  container.dataset.weTemplate = current;
  container.dataset.weEdited = keep ? "1" : "";
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
    (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  const inputStyle = "width:100%;box-sizing:border-box;padding:8px 10px;border:1px solid #d1d5db;" +
    "border-radius:8px;font:inherit;font-size:0.88rem;";
  container.innerHTML = `
    <div style="display:grid;grid-template-columns:minmax(0,1fr);gap:10px;">
      ${templates.length > 1 ? `
      <label style="font-size:0.8rem;font-weight:600;color:#374151;">Email for
        <select class="we-template" style="${inputStyle}margin-top:4px;background:#fff;">
          ${templates.map(k => `<option value="${k}"${k === current ? " selected" : ""}>${esc(EMAIL_TEMPLATES[k].label)}</option>`).join("")}
        </select>
      </label>` : ""}
      <div style="font-size:0.85rem;color:#6b7280;overflow-wrap:anywhere;">To: <strong style="color:#111827;">${esc(mail.to)}</strong></div>
      <label style="font-size:0.8rem;font-weight:600;color:#374151;">Subject
        <input type="text" class="we-subject" style="${inputStyle}margin-top:4px;" value="${esc(mail.subject)}">
      </label>
      <label style="font-size:0.8rem;font-weight:600;color:#374151;">Message
        <textarea class="we-body" rows="16" style="${inputStyle}margin-top:4px;resize:vertical;
          font-family:ui-monospace,Consolas,monospace;line-height:1.45;">${esc(mail.body)}</textarea>
      </label>      <div style="display:flex;gap:8px;flex-wrap:wrap;">
        <button type="button" class="btn btn-outline btn-small we-copy">Copy Email</button>
        <button type="button" class="btn btn-primary btn-small we-open">Open in Mail App</button>
      </div>
    </div>`;

  const subj = container.querySelector(".we-subject");
  const body = container.querySelector(".we-body");
  const picker = container.querySelector(".we-template");
  if (picker) picker.addEventListener("change", () => {
    current = picker.value;
    const m = buildWelcomeEmail(data, current);
    subj.value = m.subject;
    body.value = m.body;
    container.dataset.weTemplate = current;
    container.dataset.weEdited = "";
  });
  const markEdited = () => { container.dataset.weEdited = "1"; };
  subj.addEventListener("input", markEdited);
  body.addEventListener("input", markEdited);

  container.querySelector(".we-copy").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const text = `To: ${mail.to}\nSubject: ${subj.value}\n\n${body.value}`;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      window.prompt("Copy the email:", text);
      return;
    }
    const original = btn.textContent;
    btn.textContent = "Copied!";
    setTimeout(() => { btn.textContent = original; }, 1500);
  });

  container.querySelector(".we-open").addEventListener("click", () => {
    window.location.href = `mailto:${encodeURIComponent(mail.to)}`
      + `?subject=${encodeURIComponent(subj.value)}`
      + `&body=${encodeURIComponent(body.value.replace(/\n/g, "\r\n"))}`;
  });
}
