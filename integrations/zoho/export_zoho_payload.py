"""
Build the Zoho People `inputData` payload for one candidate.

    python integrations/zoho/export_zoho_payload.py --candidate-id 7
    python integrations/zoho/export_zoho_payload.py --email someone@example.com

Writes integrations/zoho/out/candidate_<id>.inputData.json (gitignored).
zoho_client.py --push builds the same payload in-process; this script is
for eyeballing the payload without touching Zoho.

The mapping lives in field_map.json. Any local key mapped to "" is skipped,
so an incomplete map produces a smaller payload rather than a failed push.
Values are never printed to the terminal unless you pass --show.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime

# Good enough to reject obvious non-emails (e.g. the "NANA" placeholder HR
# sometimes types into an optional email field); not a full RFC validator.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# yyyy-mm-dd as produced by <input type=date>; reformatted to Zoho's dd-MMM-yyyy.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from app.database import SessionLocal          # noqa: E402
from app.models import (Candidate, CandidateProfile, CIFDetails,  # noqa: E402
                         EducationDetail, EmploymentDetail, ReferenceDetail)

# Local EducationDetail.section -> the _subforms key in field_map.json.
_EDU_SECTION_TO_KEY = {"UG_PG": "education_ug_pg", "12TH": "education_12th",
                        "10TH": "education_10th"}


def _load_field_map():
    with open(os.path.join(HERE, "field_map.json"), encoding="utf-8") as fh:
        return json.load(fh)


def load_map():
    return _load_field_map()["map"]


def load_value_map():
    """local key -> {local value: exact Zoho picklist option text}, for the
    handful of fields whose local option wording doesn't already match what's
    configured in Zoho. See field_map.json's _value_map_readme."""
    return _load_field_map().get("_value_map", {})


def load_date_fields():
    """Local keys whose value needs reformatting from the local yyyy-mm-dd
    into the dd-MMM-yyyy Zoho's Date fields expect."""
    return set(_load_field_map().get("_date_fields", []))


def load_email_fields():
    """Local keys mapped to Zoho Email-type fields. Zoho rejects the whole
    record if any of these carries a non-email value, so a value that doesn't
    look like an email (e.g. an HR "NANA" placeholder) is dropped rather than
    sent — see field_map.json's _email_fields."""
    return set(_load_field_map().get("_email_fields", []))


def load_subforms():
    """Tabular-section config from field_map.json: {subform key: {_zoho_section,
    map: {local col -> Zoho field}}}. See field_map.json's _subforms."""
    return _load_field_map().get("_subforms", {})


def collect_values(db, candidate):
    """Flatten profile + CIF detail columns into one local-key -> value dict."""
    values = {}
    for row in (
        db.query(CandidateProfile).filter_by(candidate_id=candidate.id).one_or_none(),
        db.query(CIFDetails).filter_by(candidate_id=candidate.id).one_or_none(),
    ):
        if row is None:
            continue
        for col in row.__table__.columns:
            if col.name in ("id", "candidate_id", "updated_at"):
                continue
            values[col.name] = getattr(row, col.name)

    # full_name already came through in the loop above (CandidateProfile.full_name);
    # fall back to the account name only if the CIF profile row is missing entirely.
    if not values.get("full_name"):
        values["full_name"] = candidate.name or ""

    return values


def _apply_value_map(key, value, value_map):
    vmap = value_map.get(key)
    if not vmap:
        return value
    text = str(value)
    if text in vmap:
        return vmap[text]
    return vmap.get("_default", value)


def _reformat_date(value):
    """yyyy-mm-dd (what <input type=date> produces) -> dd-MMM-yyyy (what Zoho's
    Date fields expect). Any other/unparseable format is passed through as-is —
    better to let Zoho reject it with a clear field error than silently drop it."""
    text = str(value).strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%d-%b-%Y")
    except ValueError:
        return text


def build_payload(local, mapping, value_map=None, date_fields=None, email_fields=None):
    """local values + field map -> (payload, skipped, unmapped).

    Shared with zoho_client.py so the CLI push and this exporter (and, in the
    HR portal, app/services/zoho_push.py) can never disagree about what gets
    sent. value_map/date_fields/email_fields default to empty for callers that
    only pass mapping (e.g. older scripts) — pass the load_*() helpers to get
    the picklist, date-format, and email-validation corrections applied.

    A value dropped for failing email validation is reported in `skipped` with
    an "(invalid email)" suffix, so a dry run shows why it wasn't sent.
    """
    value_map = value_map or {}
    date_fields = date_fields or set()
    email_fields = email_fields or set()
    payload, skipped, unmapped = {}, [], []
    for key, zoho_field in mapping.items():
        value = local.get(key)
        if not zoho_field:
            unmapped.append(key)
        elif value in (None, ""):
            skipped.append(key)
        elif key in email_fields and not _EMAIL_RE.match(str(value).strip()):
            # Zoho rejects the ENTIRE record if an Email field carries a
            # non-email value, so drop just this field rather than lose the
            # whole push to one bad placeholder (e.g. "NANA").
            skipped.append("%s (invalid email)" % key)
        else:
            if key in date_fields:
                value = _reformat_date(value)
            value = _apply_value_map(key, value, value_map)
            payload[zoho_field] = str(value)
    return payload, skipped, unmapped


def collect_subform_rows(db, candidate):
    """Local repeating-section rows keyed by the field_map.json _subforms key.

    {"education_ug_pg": [{col: val, ...}], "education_12th": [...],
     "education_10th": [...], "work_experience": [...], "references": [...]}.
    Ordered by row id so the Zoho table mirrors what the candidate entered.
    """
    rows = {}
    for e in (db.query(EducationDetail).filter_by(candidate_id=candidate.id)
              .order_by(EducationDetail.id).all()):
        key = _EDU_SECTION_TO_KEY.get(e.section)
        if not key:
            continue
        rows.setdefault(key, []).append({
            "qualification": e.qualification, "course_college": e.course_college,
            "cgpa_percent": e.cgpa_percent, "year_of_passing": e.year_of_passing,
            "has_marksheet": e.has_marksheet, "gaps": e.gaps})
    for m in (db.query(EmploymentDetail).filter_by(candidate_id=candidate.id)
              .order_by(EmploymentDetail.id).all()):
        rows.setdefault("work_experience", []).append({
            "company_name": m.company_name, "position_held": m.position_held,
            "from_date": m.from_date, "to_date": m.to_date,
            "currently_working": m.currently_working,
            "reason_for_leaving": m.reason_for_leaving, "offer_letter": m.offer_letter,
            "relieving_letter_status": m.relieving_letter_status,
            "experience_certificate": m.experience_certificate, "gaps": m.gaps})
    for r in (db.query(ReferenceDetail).filter_by(candidate_id=candidate.id)
              .order_by(ReferenceDetail.id).all()):
        rows.setdefault("references", []).append({
            "employee_name": r.employee_name, "email_id": r.email_id,
            "technology": r.technology, "experience": r.experience,
            "contact_number": r.contact_number})
    return rows


def build_subforms(local_rows, subforms_config, value_map=None, email_fields=None):
    """Local rows + _subforms config -> {Zoho column API name: [row1, row2, ...]}.

    Tabular sections are written by putting their COLUMNS flat inside
    `inputData`, each value a JSON array with one element per row. There is no
    `tabularData` parameter, no section id and no section name anywhere in the
    request — the section a column belongs to is implied by the column itself.
    Zoho names this shape itself: send a tabular column as a bare string and it
    answers 7022 "Input is expected in JSONArray for tabular field '...'".

    The columns are returned separately from the flat fields only so callers
    can count rows for logging; build_full_payload merges them straight into
    the payload.

    Row alignment is positional, so a column is padded with "" for any row that
    left it blank — dropping the blank instead would silently shift every later
    value up a row. A column that is empty in every row is left out entirely
    rather than sent as a list of blanks. Sections with no rows contribute
    nothing. yyyy-mm-dd values are reformatted to Zoho's dd-MMM-yyyy, and
    email-shaped columns are validated the same way the flat fields are, so
    one bad cell cannot sink the whole record.
    """
    value_map = value_map or {}
    email_fields = email_fields or set()
    out = {}
    for key, cfg in subforms_config.items():
        colmap = cfg.get("map", {})
        rows = local_rows.get(key, [])
        if not colmap or not rows:
            continue
        for local_col, zoho_field in colmap.items():
            if not zoho_field:
                continue
            column = []
            for entry in rows:
                value = entry.get(local_col)
                if value in (None, ""):
                    column.append("")
                    continue
                text = str(value).strip()
                if local_col in email_fields and not _EMAIL_RE.match(text):
                    column.append("")
                    continue
                if _DATE_RE.match(text):
                    text = _reformat_date(text)
                column.append(str(_apply_value_map(local_col, text, value_map)))
            if any(column):
                out[zoho_field] = column
    return out


def build_full_payload(db, candidate):
    """Everything one candidate sends to Zoho, in the single `inputData` dict
    the API wants: flat fields as plain strings, tabular columns as arrays with
    one element per row. The single source of truth shared by the CLI, this
    exporter, and the HR-portal service, so all three send exactly the same
    thing.

    Returns (payload, tabular, skipped, unmapped, subform_counts):
      payload  - the whole inputData dict, tabular columns already merged in
      tabular  - just the tabular columns, kept separate for logging
      subform_counts - {local section key: row count} for what was emitted.
    """
    local = collect_values(db, candidate)
    payload, skipped, unmapped = build_payload(
        local, load_map(), value_map=load_value_map(), date_fields=load_date_fields(),
        email_fields=load_email_fields())
    local_rows = collect_subform_rows(db, candidate)
    tabular = build_subforms(local_rows, load_subforms(),
                             value_map=load_value_map(), email_fields={"email_id"})
    # Tabular columns ride inside inputData alongside the flat fields — see
    # build_subforms for why there is no separate tabularData parameter.
    payload.update(tabular)
    subform_counts = {key: len(rows) for key, rows in local_rows.items() if rows}
    return payload, tabular, skipped, unmapped, subform_counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate-id", type=int)
    ap.add_argument("--email")
    ap.add_argument("--show", action="store_true", help="print the payload instead of only the field names")
    args = ap.parse_args()

    if not args.candidate_id and not args.email:
        ap.error("pass --candidate-id or --email")

    db = SessionLocal()
    try:
        q = db.query(Candidate)
        candidate = (
            q.filter_by(id=args.candidate_id).one_or_none()
            if args.candidate_id
            else q.filter_by(email=args.email).one_or_none()
        )
        if candidate is None:
            sys.exit("candidate not found")

        payload, tabular, skipped, unmapped, subform_counts = build_full_payload(db, candidate)

        out_dir = os.path.join(HERE, "out")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "candidate_%d.inputData.json" % candidate.id)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump({"inputData": payload}, fh,
                      ensure_ascii=False, separators=(",", ":"))

        print("wrote %s" % out_path)
        flat_only = sorted(k for k in payload if k not in tabular)
        print("  %d flat fields in payload: %s" % (len(flat_only), ", ".join(flat_only)))
        print("  %d tabular column(s) across %d section(s): %s" % (
            len(tabular), len(subform_counts),
            ", ".join("%s=%d rows" % (s, n) for s, n in subform_counts.items()) or "-"))
        print("  %d empty in DB, omitted: %s" % (len(skipped), ", ".join(skipped) or "-"))
        print("  %d unmapped in field_map.json: %s" % (len(unmapped), ", ".join(unmapped) or "-"))
        if args.show:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
    finally:
        db.close()


if __name__ == "__main__":
    main()
