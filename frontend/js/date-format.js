// One date format for the whole app: DD/MM/YYYY — what the candidate types,
// what HR reads back, and what is stored.
//
// Native <input type="date"> cannot be pinned to a format: it renders in the
// viewer's browser/OS locale, so the same value showed as mm/dd/yyyy here and
// dd/mm/yyyy elsewhere. Every date field is therefore a plain text input
// carrying data-date, wired up by attachDateInputs().

// Every column that holds a date, flat or inside a repeating table. The CIF
// and BGV reuse these names, so one set covers both forms and the HR portal.
const DATE_FIELD_NAMES = new Set([
  "date_of_birth", "declaration_date", "passport_expiry", "from_date", "to_date",
]);
function isDateField(name) { return DATE_FIELD_NAMES.has(name); }

const DATE_FORMAT_LABEL = "DD/MM/YYYY";
const DATE_FORMAT_PATTERN = String.raw`\d{2}/\d{2}/\d{4}`;

// Legacy rows (and any <input type=date> draft saved before this change) hold
// yyyy-mm-dd; corrections typed as free text may use dashes or dots.
const _ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;
const _DMY_DATE = /^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$/;

// A real calendar day, not just the right shape — rejects 31/02/2024.
function isRealDate(day, month, year) {
  if (month < 1 || month > 12 || day < 1 || year < 1900 || year > 2999) return false;
  const probe = new Date(year, month - 1, day);
  return probe.getDate() === day && probe.getMonth() === month - 1
      && probe.getFullYear() === year;
}

function isValidDisplayDate(text) {
  const m = _DMY_DATE.exec(String(text || "").trim());
  return !!m && isRealDate(+m[1], +m[2], +m[3]);
}

// Anything date-shaped -> DD/MM/YYYY. A value we cannot read is handed back
// untouched: losing what someone typed is worse than showing an odd string.
function toDisplayDate(value) {
  const text = String(value === null || value === undefined ? "" : value).trim();
  if (!text) return "";
  const iso = _ISO_DATE.exec(text);
  if (iso && isRealDate(+iso[3], +iso[2], +iso[1])) return `${iso[3]}/${iso[2]}/${iso[1]}`;
  const dmy = _DMY_DATE.exec(text);
  if (dmy && isRealDate(+dmy[1], +dmy[2], +dmy[3])) {
    return `${String(+dmy[1]).padStart(2, "0")}/${String(+dmy[2]).padStart(2, "0")}/${dmy[3]}`;
  }
  return text;
}

// Slashes appear as they type, so nobody has to punctuate 8 digits by hand.
function _autoSlash(text) {
  const digits = String(text).replace(/\D/g, "").slice(0, 8);
  if (digits.length <= 2) return digits;
  if (digits.length <= 4) return `${digits.slice(0, 2)}/${digits.slice(2)}`;
  return `${digits.slice(0, 2)}/${digits.slice(2, 4)}/${digits.slice(4)}`;
}

// Report a bad date through the browser's own validation, so a malformed date
// blocks submit exactly like a missing required field does.
function _validateDateInput(el) {
  const text = el.value.trim();
  if (!text || isValidDisplayDate(text)) el.setCustomValidity("");
  else el.setCustomValidity(`Enter the date as ${DATE_FORMAT_LABEL} — e.g. 15/08/1998.`);
}

// Idempotent: safe to call again after new table rows are added.
function attachDateInputs(root) {
  const scope = root || document;
  scope.querySelectorAll("input[data-date]").forEach(el => {
    el.value = toDisplayDate(el.value);
    // Re-check on every call, not only the first: a value set in code (a
    // restored draft, an auto-fill, the browser's own form-fill) fires no
    // input or blur event, so this is the only place it gets validated.
    _validateDateInput(el);
    if (el.dataset.dateWired) return;
    el.dataset.dateWired = "1";
    el.type = "text";
    el.placeholder = DATE_FORMAT_LABEL;
    el.setAttribute("inputmode", "numeric");
    el.setAttribute("maxlength", "10");
    el.setAttribute("autocomplete", "off");
    el.setAttribute("pattern", DATE_FORMAT_PATTERN);
    el.setAttribute("title", `Date in ${DATE_FORMAT_LABEL} format`);
    el.addEventListener("input", () => {
      // A whole date arriving at once (a paste, or the browser's autofill) is
      // already punctuated — re-slashing its digits would mangle it, turning
      // 2/5/2004 into 25/20/04. Normalise those, and only auto-slash what is
      // genuinely being typed digit by digit.
      const whole = toDisplayDate(el.value);
      if (whole !== el.value && isValidDisplayDate(whole)) el.value = whole;
      // Editing mid-string would fight the caret; only reformat plain typing.
      else if (el.selectionStart === el.value.length) el.value = _autoSlash(el.value);
      _validateDateInput(el);
    });
    el.addEventListener("blur", () => {
      el.value = toDisplayDate(el.value);
      _validateDateInput(el);
    });
    // Browser autofill sets the value without an input event, but does fire
    // change — the only signal that some fills give us.
    el.addEventListener("change", () => {
      el.value = toDisplayDate(el.value);
      _validateDateInput(el);
    });
    _guardFormSubmit(el.form);
  });
}

// Last line of defence: re-validate every date in a form as it is submitted.
// The listeners above can be bypassed — a value written in code fires nothing,
// and Enter submits without blurring the field — so a stale "valid" verdict
// could otherwise let 09/22/2026 through. Capture phase, so this runs before
// the page's own submit handler builds the FormData.
function _guardFormSubmit(form) {
  if (!form || form.dataset.dateGuarded) return;
  form.dataset.dateGuarded = "1";
  form.addEventListener("submit", event => {
    let firstBad = null;
    form.querySelectorAll("input[data-date]").forEach(el => {
      el.value = toDisplayDate(el.value);
      _validateDateInput(el);
      if (!firstBad && !el.checkValidity()) firstBad = el;
    });
    if (firstBad) {
      event.preventDefault();
      event.stopImmediatePropagation();
      firstBad.reportValidity();
      firstBad.focus();
    }
  }, true);
}

document.addEventListener("DOMContentLoaded", () => attachDateInputs());

// --- Employment tenure -----------------------------------------------------
// How long a job ran, worked out from the dates the candidate entered so
// nobody has to subtract two dates by hand. Mirrors _month_span/
// _humanize_months in backend/app/agents/insights.py — keep the two in step.

function _toDate(value) {
  const text = toDisplayDate(value);
  const m = /^(\d{2})\/(\d{2})\/(\d{4})$/.exec(text);
  return m ? new Date(+m[3], +m[2] - 1, +m[1]) : null;
}

// Whole months between two form dates, counting a part-month as a month. An
// open-ended row ("currently working here") runs to today. Returns null when
// the dates are missing or run backwards.
function monthSpan(fromValue, toValue, currentlyWorking) {
  const start = _toDate(fromValue);
  if (!start) return null;
  let end = _toDate(toValue);
  if (!end) {
    if (String(currentlyWorking || "").trim().toLowerCase() !== "yes") return null;
    end = new Date();
  }
  if (end < start) return null;
  let months = (end.getFullYear() - start.getFullYear()) * 12
             + (end.getMonth() - start.getMonth());
  if (end.getDate() >= start.getDate()) months += 1;
  return Math.max(months, 0);
}

// Months as HR would say it: "7 months", "1 yr 3 mos", "2 yrs".
function humanizeMonths(months) {
  if (months === null || months === undefined) return "";
  if (months < 12) return `${months} month${months === 1 ? "" : "s"}`;
  const years = Math.floor(months / 12);
  const rest = months % 12;
  const label = `${years} yr${years === 1 ? "" : "s"}`;
  return rest ? `${label} ${rest} mo${rest === 1 ? "" : "s"}` : label;
}
