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


def _flag(field: str, issue: str, severity: str, source: str) -> dict:
    return {"field": field, "issue": issue, "severity": severity, "source": source}


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
                "education", f"12th passing year ({y12}) is not after 10th ({y10}) — "
                "impossible timeline, please verify.", "high", "rule"))
        elif gap == 1:
            flags.append(_flag(
                "education", f"Only 1 year between 10th ({y10}) and 12th ({y12}) — "
                "normally 2 years. Exceptional case, worth confirming with the candidate.",
                "medium", "rule"))

    if y12 and ug_years:
        gap = ug_years[0] - y12
        if gap < 0:
            flags.append(_flag(
                "education", f"Undergraduate passing year ({ug_years[0]}) is before "
                f"12th completion ({y12}) — impossible timeline.", "high", "rule"))
        elif gap < 2:
            flags.append(_flag(
                "education", f"Undergraduate completed only {gap} year(s) after 12th "
                f"({y12} → {ug_years[0]}) — a bachelor's degree is normally 3+ years.",
                "medium", "rule"))

    grad_year = _year(ctx.get("graduation_year"))
    if grad_year and ug_years and grad_year not in ug_years:
        flags.append(_flag(
            "graduation_year",
            f"Profile graduation year ({grad_year}) doesn't match the education "
            f"record on the CIF ({', '.join(map(str, ug_years))}).", "low", "rule"))

    # -- Date of birth vs. education age plausibility ---------------------
    dob_year = _year(ctx.get("date_of_birth"))
    if dob_year and y10:
        age_at_10 = y10 - dob_year
        if not (13 <= age_at_10 <= 19):
            flags.append(_flag(
                "date_of_birth",
                f"Age at 10th completion works out to {age_at_10} (DOB year {dob_year}, "
                f"10th passed {y10}) — typical range is 14–17. Verify DOB or passing year.",
                "medium", "rule"))

    # -- Employment: internal validity + overlaps --------------------------
    parsed = []
    for e in ctx["employment"]:
        f, t = _parse_date(e["from_date"]), _parse_date(e["to_date"])
        if f and t and f > t and not _is_yes(e["currently_working"]):
            flags.append(_flag(
                "employment",
                f"{e['company_name'] or 'An employer'}: start date ({e['from_date']}) is "
                f"after end date ({e['to_date']}).", "high", "rule"))
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
                    "employment",
                    f"Overlapping employment: '{e1['company_name'] or '?'}' and "
                    f"'{e2['company_name'] or '?'}' both claim dates around "
                    f"{e1['from_date']}–{e1['to_date'] or 'present'}.", "medium", "rule"))

    cif = ctx.get("cif") or {}
    total_exp = _number(cif.get("total_experience_yrs"))
    relevant_exp = _number(cif.get("relevant_skill_exp_yrs"))
    if total_exp is not None and relevant_exp is not None and relevant_exp > total_exp:
        flags.append(_flag(
            "relevant_skill_exp_yrs",
            f"Relevant skill experience ({relevant_exp}y) exceeds total experience "
            f"({total_exp}y).", "medium", "rule"))

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
                "total_experience_yrs",
                f"Declared total experience ({total_exp}y) doesn't line up with the "
                f"employment history listed (~{summed_years}y).", "low", "rule"))

    # -- Compensation ------------------------------------------------------
    current_ctc = _number(cif.get("current_ctc_lpa"))
    expected_ctc = _number(cif.get("expected_ctc_lpa"))
    if current_ctc and expected_ctc:
        if expected_ctc < current_ctc:
            flags.append(_flag(
                "expected_ctc_lpa",
                f"Expected CTC ({expected_ctc} LPA) is lower than current CTC "
                f"({current_ctc} LPA) — confirm this is intentional.", "low", "rule"))
        elif expected_ctc > current_ctc * 3:
            flags.append(_flag(
                "expected_ctc_lpa",
                f"Expected CTC ({expected_ctc} LPA) is more than 3x current CTC "
                f"({current_ctc} LPA) — unusually large jump.", "medium", "rule"))

    # -- Declaration ---------------------------------------------------------
    if cif and _is_yes(cif.get("declaration_accepted")):
        if not cif.get("declaration_place") or not cif.get("declaration_date"):
            flags.append(_flag(
                "declaration_accepted",
                "Declaration marked accepted but place/date wasn't captured.",
                "low", "rule"))

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
  "summary": "<4-6 sentence plain-English summary of who this candidate is,
              their experience, education, and what they're asking for
              (role, CTC, notice period). Write it the way an HR reviewer
              would want it, not a restatement of every field. ALWAYS include
              the marks/CGPA/percentage for every education level present
              (10th, 12th or Diploma, UG, PG) — e.g. '10th: 85%, 12th: 80%,
              B.Tech: 75%' — this is something HR specifically checks.>",
  "flags": [
    {{"field": "<short field name this is about>",
      "issue": "<one sentence, specific, actionable>",
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
        issue = str(p.get("issue") or "").strip()[:300]
        if not issue:
            continue
        severity = str(p.get("severity") or "medium").lower()
        if severity not in _VALID_SEVERITIES:
            severity = "medium"
        field = str(p.get("field") or "general")[:60]
        valid.append(_flag(field, issue, severity, "llm"))
    return valid


_SECTION_LABEL = {"10TH": "10th", "12TH": "12th/Diploma"}


def _education_breakdown(ctx: dict) -> str:
    """One line per education entry: '10th: 85%, 12th: 80%, B.Tech: 75%'."""
    bits = []
    for e in ctx.get("education", []):
        label = _SECTION_LABEL.get(e["section"], e.get("qualification") or e["section"])
        mark = e.get("cgpa_percent")
        bits.append(f"{label}: {mark}%" if mark else label)
    return ", ".join(bits)


def _fallback_summary(ctx: dict) -> str:
    """Used only when the LLM is unavailable — a plain templated digest so
    HR always sees something instead of an error."""
    cif = ctx.get("cif") or {}
    parts = []
    if ctx.get("candidate_name"):
        parts.append(f"{ctx['candidate_name']} applied for "
                     f"{cif.get('position_applied_for') or 'an unspecified role'}.")
    if ctx.get("highest_qualification"):
        parts.append(f"Highest qualification: {ctx['highest_qualification']}"
                      + (f" from {ctx['university_name']}" if ctx.get("university_name") else "")
                      + (f" ({ctx['graduation_year']})" if ctx.get("graduation_year") else "") + ".")
    education_line = _education_breakdown(ctx)
    if education_line:
        parts.append(f"Education: {education_line}.")
    if cif.get("total_experience_yrs"):
        parts.append(f"Total experience: {cif['total_experience_yrs']} years "
                      f"({cif.get('relevant_skill_exp_yrs') or '?'} years relevant).")
    if cif.get("current_ctc_lpa") or cif.get("expected_ctc_lpa"):
        parts.append(f"CTC: {cif.get('current_ctc_lpa') or '?'} LPA current, "
                      f"{cif.get('expected_ctc_lpa') or '?'} LPA expected.")
    if cif.get("notice_period_days"):
        parts.append(f"Notice period: {cif['notice_period_days']} days.")
    if ctx.get("employment"):
        parts.append(f"{len(ctx['employment'])} employer(s) listed.")
    return " ".join(parts) or "No CIF data available to summarise yet."


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
