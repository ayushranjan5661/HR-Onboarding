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
  }) + " IST";
}

// Login block shared by every template.
function _loginLines(data) {
  const expiry = _fmtExpiry(data.login_link_expires_at);
  const loginPage = data.login_page_url
    || (data.login_url ? data.login_url.split("?")[0] + "?role=candidate" : "");
  return [
    `Your login details:`,
    ``,
    `  Candidate ID (Login ID): ${data.email}`,
    `  Password: ${data.temp_password || "(will be shared separately)"}`,
    ``,
    `Option 1 - One-click login link (opens your form directly):`,
    `  ${data.login_url || "-"}`,
    expiry
      ? `  This link expires on ${expiry}. After that, please use Option 2 or ask us for a new link.`
      : `  Please use Option 2 if this link does not work.`,
    ``,
    `Option 2 - Login page (sign in with your Candidate ID and password):`,
    `  ${loginPage || "-"}`,
    `  Select "Candidate" on the login page, then enter the details above.`,
  ];
}

const _SIGN_OFF = [
  `Do not share this link or your password with anyone - they give direct access to your application.`,
  ``,
  `If you face any issues, reply to this email and we will help you.`,
  ``,
  `Regards,`,
  `HR Team`,
  `LevelShift`,
];

const EMAIL_TEMPLATES = {
  CIF: {
    label: "Candidate Information Form (CIF)",
    subject: "LevelShift Onboarding - Fill your Candidate Information Form (CIF)",
    intro: () => [
      `Greetings from LevelShift!`,
      ``,
      `As the next step in your onboarding, please fill in the Candidate Information Form (CIF) on our onboarding portal. The form captures your personal, education, employment and identity details, which we need to proceed with your application.`,
    ],
    outro: () => [
      `While filling the Candidate Information Form, please:`,
      `  - Keep your Aadhaar, PAN, and education/employment details handy.`,
      `  - Make sure your name and date of birth match your official ID documents.`,
      `  - Review every section carefully before submitting.`,
      ``,
      `Please complete and submit the form at the earliest.`,
    ],
  },
  DOCUMENT_COLLECTION: {
    label: "Document Collection Form",
    subject: "LevelShift Onboarding - Upload your documents (Document Collection Form)",
    intro: () => [
      `Greetings from LevelShift!`,
      ``,
      `Congratulations on clearing the interview process! As the next step in your onboarding, please upload your documents through the Document Collection Form on our onboarding portal.`,
    ],
    outro: (data) => [
      `Please keep scanned copies (PDF, image or Word, clearly readable) of the following ready:`,
      ``,
      `Personal:`,
      `  - Passport size photo`,
      `  - PAN card`,
      `  - Aadhaar card`,
      `  - Passport copy (if available)`,
      `  - Address proof and ID proof`,
      ``,
      `Education (certificates and marksheets of all semesters and degrees):`,
      `  - 10th marksheet`,
      `  - 12th marksheet / Diploma`,
      `  - UG consolidated marksheet and certificate`,
      `  - PG consolidated marksheet and certificate (if applicable)`,
      `  - Any other educational marksheets/certificates and additional certifications`,
      ...(data.candidate_type === "FRESHER" ? [] : [
        ``,
        `Employment (current company and each previous company):`,
        `  - Offer letter`,
        `  - Role change letter (if any)`,
        `  - Experience letter`,
        `  - Relieving letter (not required for your current company if you are still serving)`,
        `  - Last 3 months pay slips`,
      ]),
      ``,
      `Make sure each document is complete, legible and that your name matches across all documents. Please upload all documents and submit the form at the earliest.`,
    ],
  },
};

// data: { name, email, candidate_type, temp_password, login_url, login_page_url, login_link_expires_at }
function buildWelcomeEmail(data, template = "CIF") {
  const t = EMAIL_TEMPLATES[template] || EMAIL_TEMPLATES.CIF;
  const name = (data.name || "").trim() || "Candidate";
  const lines = [
    `Dear ${name},`, ``,
    ...t.intro(data), ``,
    ..._loginLines(data), ``,
    ...t.outro(data), ``,
    ..._SIGN_OFF,
  ];
  return { to: data.email, subject: t.subject, body: lines.join("\n") };
}

// Renders an editable draft (subject + body) with Copy / Open-in-mail actions.
// opts.templates: which templates to offer (picker hidden when only one);
// opts.template: the one selected initially.
function renderWelcomeEmailDraft(container, data, opts = {}) {
  const templates = opts.templates || ["CIF"];
  let current = opts.template || templates[0];
  const mail = buildWelcomeEmail(data, current);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
    (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  const inputStyle = "width:100%;box-sizing:border-box;padding:8px 10px;border:1px solid #d1d5db;" +
    "border-radius:8px;font:inherit;font-size:0.88rem;";
  container.innerHTML = `
    <div style="display:grid;gap:10px;">
      ${templates.length > 1 ? `
      <label style="font-size:0.8rem;font-weight:600;color:#374151;">Email for
        <select class="we-template" style="${inputStyle}margin-top:4px;background:#fff;">
          ${templates.map(k => `<option value="${k}"${k === current ? " selected" : ""}>${esc(EMAIL_TEMPLATES[k].label)}</option>`).join("")}
        </select>
      </label>` : ""}
      <div style="font-size:0.85rem;color:#6b7280;">To: <strong style="color:#111827;">${esc(mail.to)}</strong></div>
      <label style="font-size:0.8rem;font-weight:600;color:#374151;">Subject
        <input type="text" class="we-subject" style="${inputStyle}margin-top:4px;" value="${esc(mail.subject)}">
      </label>
      <label style="font-size:0.8rem;font-weight:600;color:#374151;">Message
        <textarea class="we-body" rows="16" style="${inputStyle}margin-top:4px;resize:vertical;
          font-family:ui-monospace,Consolas,monospace;line-height:1.45;">${esc(mail.body)}</textarea>
      </label>
      <div style="display:flex;gap:8px;flex-wrap:wrap;">
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
  });

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
