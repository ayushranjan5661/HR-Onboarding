"""
Candidate insight agent — CIF summariser + anomaly flagger.

A submitted CIF spans the shared profile plus ~25 flat fields, education,
employment and references — too much for an HR reviewer to safely eyeball
for consistency. This agent gives HR two things on demand (they click a
button on the candidate page; nothing here runs automatically):

  * summary — a short, plain-English digest of what the candidate said,
              organised the way a reviewer thinks: background, experience,
              compensation ask, notice.
  * flags   — things worth a second look before approving. E.g. a candidate
              who passed 10th in 2019 and 12th in 2020 (one year apart,
              where two is normal) — an "exceptional case" worth confirming
              before it becomes a bigger problem in background verification.

Flags come from two independent sources, both landing in the same list:

  1. RULE-BASED — deterministic date/number arithmetic (education
     chronology, employment overlaps, experience vs. declared years, CTC
     jumps). These never hallucinate: a rule fires on real data or it
     doesn't, and they run even if the LLM is unavailable.
  2. LLM        — Azure OpenAI reads the narrative parts of the submission
     (aspirations, reasons for leaving, certifications) and flags anything
     inconsistent that arithmetic can't catch. Every LLM flag is validated
     before HR sees it — malformed or empty entries are dropped, same
     discipline as the field-mapping agent in this package.

PII minimisation: identity numbers (Aadhaar, PAN, bank account/IFSC), the
candidate's full address and raw contact numbers are never sent to the LLM
— summarising and sanity-checking a submission doesn't need them, and
keeping them out is one less thing to worry about if the prompt or response
ever gets logged. Date-of-birth plausibility is checked in code instead of
being handed to the model.

Not cached: unlike the field-mapping agent (which caches because form
*schemas* are static), a candidate's *data* changes every time HR edits a
field or the candidate resubmits, so a fresh read is the correct default.
HR triggers this by clicking a button, not on every page load, so the
per-click LLM cost is acceptable.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Candidate,
    CandidateProfile,
    CIFDetails,
    EducationDetail,
    EmploymentDetail,
    ReferenceDetail,
)

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
_VALID_SEVERITIES = set(_SEVERITY_ORDER)
MAX_LLM_FLAGS = 8


def _flag(field: str, title: str, detail: str, severity: str, source: str) -> dict:
    """One anomaly, split so the UI can lead with a scannable headline.

    `title` is the plain-English conclusion ("Date of birth is after the 10th
    passing year"); `detail` carries the numbers it came from and what to do
    about it. `issue` keeps the old single-string shape for any consumer that
    still reads it.
    """
    detail = detail.strip()
    return {
        "field": field,
        "title": title.strip(),
        "detail": detail,
        "issue": f"{title.strip()} {detail}".strip(),
        "severity": severity,
        "source": source,
    }


def _year(value) -> int | None:
    """First 4-digit year found in a free-text field (e.g. "2020", "May 2020")."""
    if not value:
        return None
    m = re.search(r"(19|20)\d{2}", str(value))
    return int(m.group(0)) if m else None


def _number(value) -> float | None:
    if value in (None, ""):
        return None
    m = re.search(r"-?\d+(\.\d+)?", str(value).replace(",", ""))
    return float(m.group(0)) if m else None


def _parse_date(value) -> date | None:
    """Best-effort date parse. Form inputs are usually YYYY-MM-DD; fall back
    to year-only so coarse comparisons still work on messier input."""
    if not value:
        return None
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    y = _year(s)
    return date(y, 1, 1) if y else None


def _is_yes(value) -> bool:
    return str(value or "").strip().lower() in ("yes", "true", "y")


# ---------------------------------------------------------------------------
# Gather the submission into one plain structure both the rules and the LLM
# work from, so the two never disagree about what the candidate actually said.
# ---------------------------------------------------------------------------
def build_context(db: Session, candidate_id: int) -> dict:
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    profile = db.query(CandidateProfile).filter(
        CandidateProfile.candidate_id == candidate_id).first()
    cif = db.query(CIFDetails).filter(CIFDetails.candidate_id == candidate_id).first()
    education = (db.query(EducationDetail)
                   .filter(EducationDetail.candidate_id == candidate_id).all())
    employment = (db.query(EmploymentDetail)
                    .filter(EmploymentDetail.candidate_id == candidate_id)
                    .order_by(EmploymentDetail.id).all())
    references = (db.query(ReferenceDetail)
                    .filter(ReferenceDetail.candidate_id == candidate_id).all())

    return {
        "candidate_name": candidate.name if candidate else None,
        "date_of_birth": profile.date_of_birth if profile else None,
        "highest_qualification": profile.highest_qualification if profile else None,
        "university_name": profile.university_name if profile else None,
        "graduation_year": profile.graduation_year if profile else None,
        "cif": {c: getattr(cif, c) for c in (
            "position_applied_for", "skills_technologies", "marital_status",
            "any_backlogs", "worked_in_levelshift_before", "total_experience_yrs",
            "relevant_skill_exp_yrs", "current_ctc_lpa", "expected_ctc_lpa",
            "additional_allowance", "variable_comp", "notice_period_days",
            "other_offers", "technical_certifications", "understanding_of_levelshift",
            "aspirations", "declaration_accepted", "declaration_place", "declaration_date",
        )} if cif else None,
        "education": [
            {"section": e.section, "qualification": e.qualification,
             "course_college": e.course_college, "cgpa_percent": e.cgpa_percent,
             "year_of_passing": e.year_of_passing, "gaps": e.gaps}
            for e in education
        ],
        "employment": [
            {"company_name": e.company_name, "position_held": e.position_held,
             "from_date": e.from_date, "to_date": e.to_date,
             "currently_working": e.currently_working,
             "reason_for_leaving": e.reason_for_leaving, "gaps": e.gaps}
            for e in employment
        ],
        "reference_count": len(references),
    }


# ---------------------------------------------------------------------------
# 1. Rule-based flags — deterministic, always run
# ---------------------------------------------------------------------------
def rule_based_flags(ctx: dict) -> list[dict]:
    flags: list[dict] = []
    edu = ctx["education"]

    def year_of(section: str) -> int | None:
        rows = [e for e in edu if e["section"] == section]
        years = [_year(r["year_of_passing"]) for r in rows]
        years = [y for y in years if y]
        return min(years) if years else None

    y10, y12 = year_of("10TH"), year_of("12TH")
    ug_years = sorted(y for y in (_year(e["year_of_passing"]) for e in edu
                                    if e["section"] == "UG_PG") if y)

    # -- Education chronology --------------------------------------------
    if y10 and y12:
        gap = y12 - y10
        if gap <= 0:
            flags.append(_flag(
                "education", "12th was passed before or in the same year as 10th",
                f"The form says 10th in {y10} and 12th in {y12}. That order is not "
                f"possible, so one of the two years is wrong. Ask the candidate to "
                f"confirm both passing years.", "high", "rule"))
        elif gap == 1:
            flags.append(_flag(
                "education", "Only 1 year between 10th and 12th",
                f"10th in {y10}, 12th in {y12}. This is normally a 2-year gap, so it "
                f"is either a genuine exception or a typo in one year. Worth a quick "
                f"confirmation.", "medium", "rule"))

    if y12 and ug_years:
        gap = ug_years[0] - y12
        if gap < 0:
            flags.append(_flag(
                "education", "Degree was completed before 12th",
                f"The form says 12th in {y12} and the degree in {ug_years[0]}. A degree "
                f"cannot finish before 12th, so one of the two years is wrong.",
                "high", "rule"))
        elif gap < 2:
            flags.append(_flag(
                "education",
                f"Degree finished only {gap} year{'' if gap == 1 else 's'} after 12th",
                f"12th in {y12}, degree in {ug_years[0]}. A bachelor's degree normally "
                f"takes at least 3 years, so check whether a passing year is mistyped.",
                "medium", "rule"))

    grad_year = _year(ctx.get("graduation_year"))
    if grad_year and ug_years and grad_year not in ug_years:
        flags.append(_flag(
            "graduation_year", "Graduation year does not match the education table",
            f"The profile says {grad_year}, but the CIF education rows say "
            f"{', '.join(map(str, ug_years))}. Confirm which one is correct.",
            "low", "rule"))

    # -- Date of birth vs. education age plausibility ---------------------
    dob_year = _year(ctx.get("date_of_birth"))
    if dob_year and y10:
        age_at_10 = y10 - dob_year
        # A non-positive age isn't "unusual", it's impossible — the birth year is
        # after the exam. Saying "age is -7" makes the reviewer do the arithmetic
        # to work out what actually went wrong, so name the real problem instead.
        if age_at_10 <= 0:
            flags.append(_flag(
                "date_of_birth", "Date of birth is later than the 10th passing year",
                f"The form says born in {dob_year} but 10th passed in {y10} — the "
                f"candidate would not have been born yet. One of the two years is "
                f"mistyped; the birth year is the more likely culprit.",
                "high", "rule"))
        elif age_at_10 < 13:
            flags.append(_flag(
                "date_of_birth", f"Candidate would have been only {age_at_10} at 10th",
                f"Born {dob_year}, 10th passed {y10}. Students are normally 14–17 when "
                f"they pass 10th, so check the birth year and the passing year.",
                "medium", "rule"))
        elif age_at_10 > 19:
            flags.append(_flag(
                "date_of_birth", f"Candidate would have been {age_at_10} at 10th",
                f"Born {dob_year}, 10th passed {y10}. Students are normally 14–17 when "
                f"they pass 10th. This can be genuine (a break in schooling), but it is "
                f"worth confirming.", "medium", "rule"))

    # -- Employment: internal validity + overlaps --------------------------
    parsed = []
    for e in ctx["employment"]:
        f, t = _parse_date(e["from_date"]), _parse_date(e["to_date"])
        if f and t and f > t and not _is_yes(e["currently_working"]):
            flags.append(_flag(
                "employment", "Job start date is after its end date",
                f"{e['company_name'] or 'An employer'} is listed as {e['from_date']} to "
                f"{e['to_date']}, which runs backwards. Ask for the correct dates.",
                "high", "rule"))
        parsed.append((e, f, t))

    for i in range(len(parsed)):
        e1, f1, t1 = parsed[i]
        if not f1:
            continue
        end1 = t1 or date.today()
        for j in range(i + 1, len(parsed)):
            e2, f2, t2 = parsed[j]
            if not f2:
                continue
            end2 = t2 or date.today()
            if f1 <= end2 and f2 <= end1:
                flags.append(_flag(
                    "employment", "Two jobs overlap in time",
                    f"{e1['company_name'] or 'One employer'} and "
                    f"{e2['company_name'] or 'another'} both cover "
                    f"{e1['from_date']} to {e1['to_date'] or 'present'}. Check whether "
                    f"this was dual employment or a wrong date.", "medium", "rule"))

    cif = ctx.get("cif") or {}
    total_exp = _number(cif.get("total_experience_yrs"))
    relevant_exp = _number(cif.get("relevant_skill_exp_yrs"))
    if total_exp is not None and relevant_exp is not None and relevant_exp > total_exp:
        flags.append(_flag(
            "relevant_skill_exp_yrs", "Relevant experience is more than total experience",
            f"Relevant is {relevant_exp} years but total is {total_exp} years. Relevant "
            f"experience is part of the total, so it cannot be the larger number.",
            "medium", "rule"))

    if total_exp is not None and parsed:
        years_seen = set()
        total_months = 0
        for e, f, t in parsed:
            if not f:
                continue
            end = t or date.today()
            if end < f:
                continue
            total_months += (end.year - f.year) * 12 + (end.month - f.month)
        summed_years = round(total_months / 12, 1)
        if summed_years and abs(summed_years - total_exp) > 1.5:
            flags.append(_flag(
                "total_experience_yrs", "Stated experience does not match the jobs listed",
                f"The candidate declared {total_exp} years, but the employment dates on "
                f"the form add up to about {summed_years} years — a gap of roughly "
                f"{abs(round(total_exp - summed_years, 1))} years. A missing job entry is "
                f"the usual cause.", "low", "rule"))

    # -- Compensation ------------------------------------------------------
    current_ctc = _number(cif.get("current_ctc_lpa"))
    expected_ctc = _number(cif.get("expected_ctc_lpa"))
    if current_ctc and expected_ctc:
        if expected_ctc < current_ctc:
            flags.append(_flag(
                "expected_ctc_lpa", "Asking for less than they currently earn",
                f"Expected {expected_ctc} LPA against a current {current_ctc} LPA. This is "
                f"unusual — confirm it is deliberate and not a swapped entry.",
                "low", "rule"))
        elif expected_ctc > current_ctc * 3:
            multiple = round(expected_ctc / current_ctc, 1)
            flags.append(_flag(
                "expected_ctc_lpa", f"Expected CTC is {multiple}x their current CTC",
                f"They earn {current_ctc} LPA and are asking for {expected_ctc} LPA — a "
                f"jump of {round(expected_ctc - current_ctc, 1)} LPA. Worth checking early, "
                f"since it may be outside the budget for the role.", "medium", "rule"))

    # -- Declaration ---------------------------------------------------------
    if cif and _is_yes(cif.get("declaration_accepted")):
        if not cif.get("declaration_place") or not cif.get("declaration_date"):
            flags.append(_flag(
                "declaration_accepted", "Declaration accepted but place/date is missing",
                "The candidate ticked the declaration without filling in the place and "
                "date. These are needed for the signed record.", "low", "rule"))

    return flags


# ---------------------------------------------------------------------------
# 2. LLM pass — narrative summary + anything arithmetic can't catch
# ---------------------------------------------------------------------------
_PROMPT = """You are helping an HR reviewer at an Indian IT company quickly review a
candidate's onboarding form (CIF). Below is the submission, minus identity
documents and contact details (those aren't relevant here).

{context}

Return ONLY a JSON object with exactly two keys:
{{
  "summary": "<Plain-English summary of who this candidate is, their
              education, experience, and what they're asking for (role, CTC,
              notice period). Write it the way an HR reviewer would want it,
              not a restatement of every field. Structure it as short lines
              separated by \\n (NOT one run-on paragraph) — roughly: one line
              on who/role, one on education, one on experience, one on
              certifications (if any), one on compensation/notice.
              ALWAYS include the marks/CGPA/percentage for every education
              level present (10th, 12th or Diploma, UG, PG) exactly as given
              — do not add a '%' if the value already has one or already has
              a unit like 'CGPA'. If technical certifications are listed and
              they look relevant/strong for the applied role, call that out
              positively — it's a genuine plus worth an HR reviewer noticing,
              not just another field to restate.>",
  "flags": [
    {{"field": "<short field name this is about>",
      "title": "<the problem in plain English, under 60 characters, no numbers
                 unless essential — this is the headline the reviewer scans>",
      "detail": "<1-2 sentences: the values it came from, and what the reviewer
                  should do about it. Never make the reviewer do arithmetic to
                  understand the problem — state the conclusion, then the numbers.>",
      "severity": "low|medium|high"}}
  ]
}}

Only flag things in the free-text/narrative content that a simple date or
number check would NOT catch — e.g. a reason-for-leaving that contradicts
"currently working", aspirations that don't match the role applied for,
notice period inconsistent with other answers, vague or evasive answers.
Do NOT invent education/employment date-math flags — that is handled
separately. An empty flags array is a valid answer if nothing stands out.
No prose, no markdown fences. JSON object only."""


def _llm_review(ctx: dict) -> dict | None:
    endpoint = (settings.AZURE_OPENAI_ENDPOINT or "").rstrip("/")
    key = settings.AZURE_OPENAI_API_KEY
    deployment = settings.AZURE_OPENAI_DEPLOYMENT
    if not (endpoint and key and deployment):
        return None

    # PII minimisation: only narrative/decision-relevant fields reach the model.
    safe_ctx = {
        "highest_qualification": ctx.get("highest_qualification"),
        "university_name": ctx.get("university_name"),
        "graduation_year": ctx.get("graduation_year"),
        "cif": ctx.get("cif"),
        "education": ctx.get("education"),
        "employment": ctx.get("employment"),
        "reference_count": ctx.get("reference_count"),
    }
    prompt = _PROMPT.format(context=json.dumps(safe_ctx, indent=2, default=str))
    url = (f"{endpoint}/openai/deployments/{deployment}/chat/completions"
           f"?api-version={settings.AZURE_OPENAI_API_VERSION}")
    body = json.dumps({
        "messages": [
            {"role": "system", "content": "You return only valid JSON objects."},
            {"role": "user", "content": prompt},
        ],
        "max_completion_tokens": 1200,
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=settings.AI_INSIGHTS_TIMEOUT) as resp:
            payload = json.loads(resp.read())
        text = payload["choices"][0]["message"]["content"].strip()
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError,
            json.JSONDecodeError, TimeoutError, OSError) as exc:
        print(f"[insights] LLM unavailable ({type(exc).__name__}); using rule-only fallback")
        return None

    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            result = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return result if isinstance(result, dict) else None


def _validate_llm_flags(raw: list) -> list[dict]:
    if not isinstance(raw, list):
        return []
    valid = []
    for p in raw[:MAX_LLM_FLAGS]:
        if not isinstance(p, dict):
            continue
        title = str(p.get("title") or "").strip()[:120]
        detail = str(p.get("detail") or "").strip()[:300]
        # Older/looser responses may still send a single "issue" string. Take it
        # as the detail and let the field name carry the headline.
        if not (title or detail):
            detail = str(p.get("issue") or "").strip()[:300]
        if not (title or detail):
            continue
        severity = str(p.get("severity") or "medium").lower()
        if severity not in _VALID_SEVERITIES:
            severity = "medium"
        field = str(p.get("field") or "general")[:60]
        valid.append(_flag(field, title, detail, severity, "llm"))
    return valid


_SECTION_LABEL = {"10TH": "10th", "12TH": "12th/Diploma"}


def _format_mark(mark) -> str:
    """Candidates sometimes type '85', sometimes '85%', sometimes '8.5 CGPA'
    — never assume it's a bare number. Only append '%' when the value is
    plain digits; anything that already carries its own unit is left as-is
    so it never doubles up ('85%%' or '8.5 CGPA%')."""
    m = str(mark or "").strip()
    if not m:
        return ""
    return m if ("%" in m or any(c.isalpha() for c in m)) else f"{m}%"


def _education_breakdown(ctx: dict) -> str:
    """One line per education entry: '10th: 85%, 12th: 80%, B.Tech: 75%'."""
    bits = []
    for e in ctx.get("education", []):
        label = _SECTION_LABEL.get(e["section"], e.get("qualification") or e["section"])
        mark = _format_mark(e.get("cgpa_percent"))
        bits.append(f"{label}: {mark}" if mark else label)
    return ", ".join(bits)


def _fallback_summary(ctx: dict) -> str:
    """Used only when the LLM is unavailable — a plain templated digest so
    HR always sees something instead of an error. Built as short lines
    (joined with newlines, not one run-on paragraph) grouped the way an HR
    reviewer actually scans a CIF: who/role, education, experience, ask."""
    cif = ctx.get("cif") or {}
    lines = []

    if ctx.get("candidate_name"):
        lines.append(f"{ctx['candidate_name']} applied for "
                      f"{cif.get('position_applied_for') or 'an unspecified role'}.")

    education_line = _education_breakdown(ctx)
    edu_bit = f"Education: {education_line}." if education_line else ""
    if ctx.get("highest_qualification"):
        edu_bit = (f"Highest qualification: {ctx['highest_qualification']}"
                   + (f" from {ctx['university_name']}" if ctx.get("university_name") else "")
                   + (f" ({ctx['graduation_year']})" if ctx.get("graduation_year") else "")
                   + ". " + edu_bit)
    if edu_bit:
        lines.append(edu_bit.strip())

    exp_bit = []
    if cif.get("total_experience_yrs"):
        exp_bit.append(f"Total experience: {cif['total_experience_yrs']} years "
                        f"({cif.get('relevant_skill_exp_yrs') or '?'} years relevant)")
    if ctx.get("employment"):
        exp_bit.append(f"{len(ctx['employment'])} employer(s) listed")
    if exp_bit:
        lines.append(". ".join(exp_bit) + ".")

    certs = str(cif.get("technical_certifications") or "").strip()
    if certs:
        lines.append(f"Certifications: {certs}.")

    ask_bit = []
    if cif.get("current_ctc_lpa") or cif.get("expected_ctc_lpa"):
        ask_bit.append(f"CTC: {cif.get('current_ctc_lpa') or '?'} LPA current, "
                        f"{cif.get('expected_ctc_lpa') or '?'} LPA expected")
    if cif.get("notice_period_days"):
        ask_bit.append(f"notice period {cif['notice_period_days']} days")
    if ask_bit:
        lines.append(", ".join(ask_bit) + ".")

    return "\n".join(lines) or "No CIF data available to summarise yet."


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def generate(db: Session, candidate_id: int) -> dict:
    ctx = build_context(db, candidate_id)
    rule_flags = rule_based_flags(ctx)

    llm_result = _llm_review(ctx) if settings.AI_INSIGHTS_ENABLED else None
    if llm_result:
        summary = str(llm_result.get("summary") or "").strip() or _fallback_summary(ctx)
        llm_flags = _validate_llm_flags(llm_result.get("flags", []))
        generated_by = "llm"
    else:
        summary = _fallback_summary(ctx)
        llm_flags = []
        generated_by = "rule-only"

    flags = sorted(rule_flags + llm_flags, key=lambda f: _SEVERITY_ORDER.get(f["severity"], 1))
    return {
        "summary": summary,
        "flags": flags,
        "generated_by": generated_by,
        "model": settings.AZURE_OPENAI_DEPLOYMENT if generated_by == "llm" else None,
    }
