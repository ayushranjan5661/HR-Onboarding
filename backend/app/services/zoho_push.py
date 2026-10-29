"""
Push one candidate into Zoho People — backs the HR portal's "Publish to Zoho
People" button (POST /hr/candidates/{id}/zoho/push).

This deliberately does not reimplement anything: it imports
integrations/zoho/zoho_client.py and export_zoho_payload.py as-is, so the
button, the CLI, and integrations/zoho/out/zoho_push.log agree on the payload
builder, the field map, the token cache, and the dedupe/audit logic. See
integrations/zoho/README.md before pointing this at a live form.

Safety, same as the CLI:
  * refuses to run at all unless ZOHO_CANDIDATE_WRITE_FORM is set — the
    read-only ZOHO_CANDIDATE_FORM is never used for a write (app/config.py);
  * first push per candidate inserts as a Zoho DRAFT; every push after that
    updates the same record (candidate.zoho_record_id), never a second insert;
  * before that first insert, a getRecords lookup on email — a hit, or a
    failed lookup, aborts rather than risk a duplicate (insertRecord has no
    dedupe of its own);
  * every request/response logged to integrations/zoho/out/zoho_push.log.
"""
import os
import sys

_INTEGRATIONS_ZOHO = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..",
                 "integrations", "zoho"))
if _INTEGRATIONS_ZOHO not in sys.path:
    sys.path.insert(0, _INTEGRATIONS_ZOHO)

import zoho_client as _zc                                                 # noqa: E402
from export_zoho_payload import (CANDIDATE_MAP_FILE, DEFAULT_MAP_FILE,   # noqa: E402
                                 build_full_payload, collect_values, load_subforms)

from sqlalchemy.orm import Session                                       # noqa: E402

from app.config import settings                                          # noqa: E402
from app.models import Candidate                                         # noqa: E402

# Zoho's stock Candidate form names the email field Email_ID; the CLI's
# --email-field lets that be overridden per-org, which this button does not
# expose — fix here if a target form uses a different name.
EMAIL_FIELD = "Email_ID"

# The two Zoho forms HR can publish to, one button each. Each has its own
# .env setting (blank = button refuses), field map, and set of Candidate
# columns tracking its record - a push to one never touches the other's record.
#   confirmation -> Confirmation_Process_Form ("Candidate Information")
#   candidate    -> Zoho's "Candidate" form
TARGETS = {
    "confirmation": {"setting": "ZOHO_CANDIDATE_WRITE_FORM",
                     "map_file": DEFAULT_MAP_FILE, "prefix": "zoho_"},
    "candidate": {"setting": "ZOHO_CANDIDATE_PROFILE_WRITE_FORM",
                  "map_file": CANDIDATE_MAP_FILE, "prefix": "zoho_cand_"},
}


class ZohoPushError(RuntimeError):
    """Message is safe to show HR as-is."""


def _network_message(exc: "_zc.ZohoUnreachable") -> str:
    """Wording for a push that never left this machine. Saying "Zoho rejected
    the push" here is actively misleading — Zoho never saw the request, and the
    fix is on the network side, not in the candidate's data."""
    return (f"Could not reach Zoho People from this server: {exc} "
            "The candidate's data is unchanged and safe — re-sync once the "
            "connection is back.")


def push_candidate(db: Session, candidate: Candidate, target: str = "confirmation") -> dict:
    """Insert (first call) or update (every call after) this candidate's Zoho
    People record on the form `target` names (see TARGETS). Returns
    {"record_id", "status", "fields_pushed", "fields_unmapped"}. Raises
    ZohoPushError on anything that stops the push — the caller is expected
    to surface str(exc) to HR and move on."""
    cfg = TARGETS[target]
    form = getattr(settings, cfg["setting"])
    if not form:
        raise ZohoPushError(
            f"Zoho push is not configured: set {cfg['setting']} in .env to "
            "the exact Zoho form (formLinkName) this button should write to, once "
            "the payload has round-tripped against it via the CLI.")

    payload, tabular, _skipped, unmapped, subform_counts = build_full_payload(
        db, candidate, map_file=cfg["map_file"])
    if not payload:
        raise ZohoPushError(
            "Nothing to push — field_map.json has no mapped field with a value yet.")

    email = candidate.email or collect_values(db, candidate).get("email") or ""
    if not email:
        raise ZohoPushError("Candidate has no email on record — Zoho's duplicate check needs one.")

    # Some file-upload fields (e.g. the profile picture) are mandatory on the
    # Zoho form — attach whatever's mapped in field_map.json's "files" map so
    # the insert doesn't fail on a missing mandatory upload.
    files, _files_unmapped, files_missing = _zc.load_candidate_documents(
        candidate.id, _zc.load_file_map(cfg["map_file"]))
    # Two local documents can share one Zoho field (Candidate form: profile
    # picture and passport photo -> Photo). Upload only one; the last in key
    # order wins, matching what the dict merge in upload_files would keep.
    files = list({f[0]: f for f in files}.values())
    if files_missing:
        raise ZohoPushError(
            "Cannot push: these documents are mapped for Zoho but missing from the "
            f"server's uploads folder: {', '.join(files_missing)}.")

    try:
        token = _zc.access_token(force=True)  # fresh token on every publish click
    except _zc.ZohoUnreachable as exc:
        raise ZohoPushError(_network_message(exc)) from exc
    except _zc.ZohoError as exc:
        raise ZohoPushError(f"Could not authenticate with Zoho: {exc}") from exc

    record_id = getattr(candidate, cfg["prefix"] + "record_id")
    if not record_id:
        try:
            existing = _zc.find_by_email(token, form, email, EMAIL_FIELD)
        except _zc.ZohoUnreachable as exc:
            raise ZohoPushError(_network_message(exc)) from exc
        except _zc.ZohoError as exc:
            raise ZohoPushError(
                f"Duplicate check against Zoho failed, refusing to insert: {exc}") from exc
        if len(existing) > 1:
            raise ZohoPushError(
                f"{len(existing)} records for {email} already exist on '{form}'. "
                "Remove the duplicates in Zoho first, then publish again.")
        if existing:
            # The record Zoho already has for this email is this candidate's:
            # link it and overwrite it below (its table rows are replaced too).
            record_id = _hit_id(existing[0])
            if not record_id:
                raise ZohoPushError(f"Could not read the id of the existing Zoho record: "
                                    f"{str(existing[0])[:400]}")

    # Tabular columns in inputData always APPEND rows, so a re-sync would
    # duplicate every table. For sections the map marks with a section id,
    # note the rows already there now and delete them once the update has
    # landed — the record ends up mirroring the portal. Read failures abort
    # before anything is written.
    replace_sections = {cfg_s["_zoho_section_id"]: cfg_s.get("_zoho_section_display", "")
                        for cfg_s in load_subforms(cfg["map_file"]).values()
                        if cfg_s.get("_zoho_section_id")}
    old_rows = {}
    if record_id and replace_sections:
        try:
            existing_record = _zc.get_record(token, form, record_id)
        except _zc.ZohoUnreachable as exc:
            raise ZohoPushError(_network_message(exc)) from exc
        except _zc.ZohoError as exc:
            raise ZohoPushError(f"Could not read the existing Zoho record before updating it: {exc}") from exc
        old_rows = {sec: _zc.tabular_row_ids(existing_record, display)
                    for sec, display in replace_sections.items()}

    # The record is written as a Zoho DRAFT: a non-draft write enforces every
    # mandatory field on the form, including ones no local data answers
    # (error 7052). As a draft it lands valid and editable for HR.
    # Education/employment rows ARE sent — they ride inside inputData as
    # column arrays, which build_full_payload has already merged in.
    _zc.log("push.request", source="hr-portal", target=target, form=form, candidate_id=candidate.id,
            email=email, record_id=record_id, fields=len(payload),
            files=[f[0] for f in files], tabular_rows=subform_counts)
    try:
        response = (_zc.update_record(token, form, record_id, payload, files=files, draft=True)
                    if record_id
                    else _zc.insert_record(token, form, payload, draft=True, files=files))
    except _zc.ZohoUnreachable as exc:
        _zc.log("push.error", source="hr-portal", form=form, candidate_id=candidate.id,
                reason="unreachable", message=str(exc))
        raise ZohoPushError(_network_message(exc)) from exc
    except _zc.ZohoError as exc:
        _zc.log("push.error", source="hr-portal", form=form, candidate_id=candidate.id,
                message=str(exc))
        raise ZohoPushError(f"Zoho rejected the push: {exc}") from exc

    _zc.log("push.response", source="hr-portal", form=form, candidate_id=candidate.id,
            response=response)
    if _zc.has_errors(response):
        raise ZohoPushError(
            "Zoho reported an error — it usually names the offending field: "
            f"{str(response)[:600]}")

    new_id = record_id or _zc.record_id_from(response)
    if not new_id:
        raise ZohoPushError(f"Zoho did not return a record id: {str(response)[:600]}")

    if any(old_rows.values()):
        try:
            cleanup = _zc.delete_tabular_rows(token, form, record_id, old_rows, draft=True)
        except _zc.ZohoError as exc:
            cleanup = {"error": str(exc)}
        _zc.log("push.tabular_cleanup", source="hr-portal", target=target, form=form,
                candidate_id=candidate.id, deleted={s: len(i) for s, i in old_rows.items()},
                response=cleanup)
        if "error" in cleanup or _zc.has_errors(cleanup):
            raise ZohoPushError(
                "The record was updated, but its previous table rows could not be removed, "
                "so Education/Experience may now appear twice in Zoho. Re-sync again or "
                f"delete the extra rows in Zoho: {str(cleanup)[:400]}")

    return {
        "record_id": new_id,
        # Always a draft in Zoho for now (see the tabular note above).
        "status": "DRAFT",
        "fields_pushed": len(payload),
        "fields_unmapped": len(unmapped),
        "tabular_columns": len(tabular),
        "tabular_rows": sum(subform_counts.values()),
    }


# "Documents Collection - Trainee" — the Reference Check PDF goes into its
# Reference_check file field; the record is matched on Candidate_Email_ID.
REF_EMAIL_FIELD = "Candidate_Email_ID"
REF_NAME_FIELD = "Candidate_Name"
REF_FILE_FIELD = "Reference_check"


def push_reference_check(candidate: Candidate) -> dict:
    """Upload the generated Reference Check PDF (app/services/reference_pdf.py)
    to ZOHO_REFERENCE_CHECK_WRITE_FORM. Updates the record already linked to
    this candidate, else the one Zoho has for their email, else inserts one.
    Returns {"record_id", "status", "created"}; raises ZohoPushError."""
    form = settings.ZOHO_REFERENCE_CHECK_WRITE_FORM
    if not form:
        raise ZohoPushError("Zoho push is not configured: set "
                            "ZOHO_REFERENCE_CHECK_WRITE_FORM in .env.")
    from app.services import reference_pdf
    reason = reference_pdf.not_ready_reason(candidate)
    if reason:
        raise ZohoPushError(reason)
    email = candidate.email or ""
    if not email:
        raise ZohoPushError("Candidate has no email on record — Zoho's duplicate check needs one.")

    pdf = reference_pdf.build(candidate)
    safe = "".join(ch if ch.isalnum() else "_" for ch in (candidate.name or "candidate")).strip("_")
    filename = f"Referee_Check_{safe or candidate.id}.pdf"

    try:
        token = _zc.access_token(force=True)
    except _zc.ZohoUnreachable as exc:
        raise ZohoPushError(_network_message(exc)) from exc
    except _zc.ZohoError as exc:
        raise ZohoPushError(f"Could not authenticate with Zoho: {exc}") from exc

    record_id = candidate.zoho_ref_record_id
    if not record_id:
        try:
            existing = _zc.find_by_email(token, form, email, REF_EMAIL_FIELD)
        except _zc.ZohoUnreachable as exc:
            raise ZohoPushError(_network_message(exc)) from exc
        except _zc.ZohoError as exc:
            raise ZohoPushError(
                f"Duplicate check against Zoho failed, refusing to insert: {exc}") from exc
        if len(existing) > 1:
            raise ZohoPushError(
                f"{len(existing)} records for {email} already exist on '{form}'. "
                "Remove the duplicates in Zoho first, then publish again.")
        if existing:
            # getRecords keys each hit by its record id: [{"<id>": [{fields}]}]
            hit = existing[0] if isinstance(existing[0], dict) else {}
            record_id = next((str(k) for k in hit if str(k).isdigit()), "")
            if not record_id:
                raise ZohoPushError(f"Could not read the id of the existing Zoho record: "
                                    f"{str(existing[0])[:400]}")

    # The PDF is uploaded straight from memory: no temp file, since
    # tempfile hangs on this machine (its temp dir refuses new files and
    # Python retries effectively forever).
    files = [(REF_FILE_FIELD, filename, "application/pdf", pdf)]
    # On an existing record only the PDF is touched; a new one also gets
    # the email/name it is matched on next time.
    payload = {} if record_id else {REF_EMAIL_FIELD: email, REF_NAME_FIELD: candidate.name or ""}
    _zc.log("push.request", source="hr-portal", target="reference_check", form=form,
            candidate_id=candidate.id, email=email, record_id=record_id, files=[REF_FILE_FIELD])
    try:
        response = (_zc.update_record(token, form, record_id, payload, files=files)
                    if record_id
                    else _zc.insert_record(token, form, payload, files=files))
    except _zc.ZohoUnreachable as exc:
        _zc.log("push.error", source="hr-portal", form=form, candidate_id=candidate.id,
                reason="unreachable", message=str(exc))
        raise ZohoPushError(_network_message(exc)) from exc
    except _zc.ZohoError as exc:
        _zc.log("push.error", source="hr-portal", form=form, candidate_id=candidate.id,
                message=str(exc))
        raise ZohoPushError(f"Zoho rejected the push: {exc}") from exc

    _zc.log("push.response", source="hr-portal", form=form, candidate_id=candidate.id,
            response=response)
    if _zc.has_errors(response):
        raise ZohoPushError(f"Zoho reported an error: {str(response)[:600]}")

    new_id = record_id or _zc.record_id_from(response)
    if not new_id:
        raise ZohoPushError(f"Zoho did not return a record id: {str(response)[:600]}")
    return {"record_id": new_id, "status": "SYNCED", "created": not record_id}


# "New Joiner Documents Track" (form API name Referral_Bonus). Each document
# has a status picklist and a submission date. Its upload fields
# are left empty: the Declaration attaches no documents.
# Employee Name (Referrer_Name) is a lookup into P_Employee: it takes the
# employee's record id, never a name (a name fails with 7016).
DECL_NAME_FIELD = "Referrer_Name"
EMPLOYEE_FORM = "P_Employee"
EMPLOYEE_EMAIL_FIELDS = ("EmailID", "Other_Email")    # official, personal
DECL_COMMENT_FIELD = "comments"     # names the candidate while Employee Name is empty
# Declaration key prefix -> (status field, date field)
DECL_DOCS = {
    "semester_marksheets": ("individual_semester_mark_stateme",
                            "individual_semester_mark_statement_submission_date"),
    "consolidated_provisional": ("consolidated_mark_statement_and_provisional_certificate",
                                 "consolidated_mark_provisional_certificate_submission_date"),
    "degree_certificate": ("degree_certific", "degree_certificate_submission_date"),
    "pan_card": ("pan_card", "pan_card_submission_date"),
    "relieving_experience": ("relieving_letter", "relieving_letter_submission_date"),
}


def _zoho_date(value) -> str:
    """DD/MM/YYYY (the portal's format) -> Zoho's dd-MMM-yyyy; blank stays blank."""
    from datetime import datetime
    text = str(value or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%d-%b-%Y")
        except ValueError:
            continue
    return text


def declaration_payload(candidate: Candidate) -> dict:
    """Zoho inputData for the Declaration. A ticked document is "Required"
    with its expected date; an unticked one is not sent, so Zoho shows
    "-Select-". The picklist has only Required/Submitted and ignores "", so a
    re-push cannot clear an old status. The relieving letter applies to
    Experienced hires only, and either Declaration can mark it pending."""
    d = candidate.declaration_details
    exp = candidate.exp_declaration_details
    payload = {}
    for key, (status_f, date_f) in DECL_DOCS.items():
        pending = (getattr(d, f"{key}_pending") or "") == "Yes"
        if key == "relieving_experience":
            pending = pending or (exp is not None and exp.relieving_experience_pending == "Yes")
        if pending:
            payload[status_f] = "Required"
            date = _zoho_date(getattr(d, f"{key}_expected_date"))
            if date:
                payload[date_f] = date
    return payload


def _hit_id(hit) -> str:
    """getRecords keys each hit by its record id: {"<id>": [{fields}]}."""
    return next((str(k) for k in (hit if isinstance(hit, dict) else {}) if str(k).isdigit()), "")


def _candidate_emails(candidate: Candidate) -> list[str]:
    """Every email the candidate is known by, official one first."""
    cif = candidate.cif_details
    emails = [cif.hr_candidate_email if cif else None, candidate.email,
              candidate.profile.email if candidate.profile else None]
    return list(dict.fromkeys(e.strip() for e in emails if e and e.strip()))


def _employee_record_id(token, candidate: Candidate) -> str:
    """The candidate's Zoho employee record id, found by email, or "" when
    they are not a Zoho employee yet. A lookup failure aborts the push."""
    emails = _candidate_emails(candidate)
    if not emails:
        raise ZohoPushError("Candidate has no email on record to find their Zoho employee by.")
    for email in emails:
        for field in EMPLOYEE_EMAIL_FIELDS:
            try:
                hits = _zc.find_by_email(token, EMPLOYEE_FORM, email, field)
            except _zc.ZohoUnreachable as exc:
                raise ZohoPushError(_network_message(exc)) from exc
            except _zc.ZohoError as exc:
                raise ZohoPushError(f"Could not look up the Zoho employee: {exc}") from exc
            if len(hits) > 1:
                raise ZohoPushError(f"{len(hits)} Zoho employees have the email {email}; "
                                    "fix the duplicates in Zoho, then publish again.")
            if hits and _hit_id(hits[0]):
                return _hit_id(hits[0])
    return ""


def push_declaration(candidate: Candidate) -> dict:
    """Write the Declaration into ZOHO_DECLARATION_WRITE_FORM: one status /
    expected date per document, no uploads. The first push inserts; later pushes update the linked record.
    Returns {"record_id", "status", "created"}; raises ZohoPushError."""
    form = settings.ZOHO_DECLARATION_WRITE_FORM
    if not form:
        raise ZohoPushError("Zoho push is not configured: set "
                            "ZOHO_DECLARATION_WRITE_FORM in .env.")
    from app.services import declaration_pdf
    reason = declaration_pdf.not_ready_reason(candidate)
    if reason:
        raise ZohoPushError(reason)

    payload = declaration_payload(candidate)

    try:
        token = _zc.access_token(force=True)
    except _zc.ZohoUnreachable as exc:
        raise ZohoPushError(_network_message(exc)) from exc
    except _zc.ZohoError as exc:
        raise ZohoPushError(f"Could not authenticate with Zoho: {exc}") from exc

    # New joiners are often not Zoho employees yet. Employee Name is optional
    # on the form, so the record is written without it and named in Comments;
    # a re-sync once the employee exists fills Employee Name in.
    employee_id = _employee_record_id(token, candidate)
    if employee_id:
        payload[DECL_NAME_FIELD] = employee_id
    else:
        emails = _candidate_emails(candidate)
        payload[DECL_COMMENT_FIELD] = (
            f"Candidate: {candidate.name or ''}{f' ({emails[0]})' if emails else ''}. "
            "Not yet a Zoho employee; Employee Name is set on re-sync once they are.")

    # A tracker record already on this employee is theirs, so it is updated
    # rather than duplicated; more than one needs sorting out in Zoho first.
    record_id = candidate.zoho_decl_record_id
    if not record_id and employee_id:
        try:
            existing = _zc.find_by_email(token, form, employee_id, DECL_NAME_FIELD)
        except _zc.ZohoUnreachable as exc:
            raise ZohoPushError(_network_message(exc)) from exc
        except _zc.ZohoError as exc:
            raise ZohoPushError(
                f"Duplicate check against Zoho failed, refusing to insert: {exc}") from exc
        if len(existing) > 1:
            raise ZohoPushError(
                f"{len(existing)} records for this employee already exist on '{form}'. "
                "Remove the duplicates in Zoho first, then publish again.")
        if existing:
            record_id = _hit_id(existing[0])
            if not record_id:
                raise ZohoPushError(f"Could not read the id of the existing Zoho record: "
                                    f"{str(existing[0])[:400]}")

    _zc.log("push.request", source="hr-portal", target="declaration", form=form,
            candidate_id=candidate.id, record_id=record_id, fields=len(payload))
    try:
        response = (_zc.update_record(token, form, record_id, payload)
                    if record_id
                    else _zc.insert_record(token, form, payload))
    except _zc.ZohoUnreachable as exc:
        _zc.log("push.error", source="hr-portal", form=form, candidate_id=candidate.id,
                reason="unreachable", message=str(exc))
        raise ZohoPushError(_network_message(exc)) from exc
    except _zc.ZohoError as exc:
        _zc.log("push.error", source="hr-portal", form=form, candidate_id=candidate.id,
                message=str(exc))
        raise ZohoPushError(f"Zoho rejected the push: {exc}") from exc

    _zc.log("push.response", source="hr-portal", form=form, candidate_id=candidate.id,
            response=response)
    if _zc.has_errors(response):
        raise ZohoPushError(f"Zoho reported an error: {str(response)[:600]}")

    new_id = record_id or _zc.record_id_from(response)
    if not new_id:
        raise ZohoPushError(f"Zoho did not return a record id: {str(response)[:600]}")
    return {"record_id": new_id, "status": "SYNCED", "created": not record_id,
            "employee_linked": bool(employee_id)}


# "Consultant Tracker" (form API name consultant_tracker). The On-Demand form
# answers only a few of its fields; the rest (department, manager, agreement,
# SOW/NDA/MSA...) are mandatory there, so the record is written as a DRAFT
# for HR to complete in Zoho. Matched on Consultant_Email.
OD_EMAIL_FIELD = "Consultant_Email"
OD_CONSULTANT_TYPE = "On - Demand Consultant"
OD_COMMENT_FIELD = "Comments_Notes"      # answers the form has no field for
# On-Demand document key -> Zoho upload field
OD_FILES = {"aadhar_card": "Aadhar_Card", "pan_card": "Pan_Card",
            "passport_size_photo": "Passport_Size_Photo"}


def ondemand_payload(candidate: Candidate) -> dict:
    """Zoho inputData for the On-Demand Candidate form."""
    od = candidate.ondemand_details
    first, _, last = (od.full_name or candidate.name or "").strip().partition(" ")
    payload = {
        "First_Name": first,
        "Last_Name": last.strip() or first,     # mandatory; single names repeat
        OD_EMAIL_FIELD: od.email or "",
        "Personal_Email_ID": od.email or "",
        "Contact_Number": "".join(ch for ch in od.contact_number or "" if ch.isdigit()),
        "Consultant_Type": OD_CONSULTANT_TYPE,
        "Name_as_in_the_Bank_account": od.bank_account_name or "",
        "Account_Number": od.bank_account_number or "",
        "Bank_Name": od.bank_name or "",
        "IFSC_Code": od.bank_ifsc_code or "",
    }
    account_type = (od.bank_account_type or "").strip().lower()
    if account_type.startswith("sav"):
        payload["Account_Type"] = "Savings"
    elif account_type.startswith("cur"):
        payload["Account_Type"] = "Current"
    notes = [f"{label}: {value}" for label, value in (
        ("Alternate Email", od.alternate_email), ("Alternate Contact Number", od.alternate_number),
        ("LinkedIn", od.linkedin_link), ("Date of Birth", od.date_of_birth),
        ("Bank Branch", od.bank_branch)) if value]
    if notes:
        payload[OD_COMMENT_FIELD] = "\n".join(notes)
    return {k: v for k, v in payload.items() if v}


def push_ondemand(candidate: Candidate) -> dict:
    """Write the On-Demand Candidate form, with its Aadhaar / PAN / photo
    uploads, into ZOHO_ONDEMAND_WRITE_FORM as a draft. Updates the record
    already linked to this candidate, else the one Zoho has for their email,
    else inserts one. Returns {"record_id", "status", "created"}; raises
    ZohoPushError."""
    form = settings.ZOHO_ONDEMAND_WRITE_FORM
    if not form:
        raise ZohoPushError("Zoho push is not configured: set "
                            "ZOHO_ONDEMAND_WRITE_FORM in .env.")
    from app.models import FormType
    from app.services import ondemand_pdf
    from app.utils.file_storage import upload_path
    reason = ondemand_pdf.not_ready_reason(candidate)
    if reason:
        raise ZohoPushError(reason)
    payload = ondemand_payload(candidate)
    email = payload.get(OD_EMAIL_FIELD) or candidate.email or ""
    if not email:
        raise ZohoPushError("Candidate has no email on record — Zoho's duplicate check needs one.")
    payload[OD_EMAIL_FIELD] = email

    latest = {}
    for doc in sorted(candidate.documents, key=lambda d: d.id or 0):
        if doc.form_type == FormType.ONDEMAND and doc.field_key in OD_FILES:
            latest[doc.field_key] = doc
    files, missing = [], []
    for key, doc in latest.items():
        path = upload_path(doc.stored_filename)
        if not os.path.exists(path):
            missing.append(key)
            continue
        files.append((OD_FILES[key], doc.original_filename,
                      doc.content_type or "application/octet-stream", path))
    if missing:
        raise ZohoPushError("Cannot push: these documents are missing from the "
                            f"server's uploads folder: {', '.join(missing)}.")

    try:
        token = _zc.access_token(force=True)
    except _zc.ZohoUnreachable as exc:
        raise ZohoPushError(_network_message(exc)) from exc
    except _zc.ZohoError as exc:
        raise ZohoPushError(f"Could not authenticate with Zoho: {exc}") from exc

    record_id = candidate.zoho_od_record_id
    if not record_id:
        try:
            existing = _zc.find_by_email(token, form, email, OD_EMAIL_FIELD)
        except _zc.ZohoUnreachable as exc:
            raise ZohoPushError(_network_message(exc)) from exc
        except _zc.ZohoError as exc:
            raise ZohoPushError(
                f"Duplicate check against Zoho failed, refusing to insert: {exc}") from exc
        if len(existing) > 1:
            raise ZohoPushError(
                f"{len(existing)} records for {email} already exist on '{form}'. "
                "Remove the duplicates in Zoho first, then publish again.")
        if existing:
            record_id = _hit_id(existing[0])
            if not record_id:
                raise ZohoPushError(f"Could not read the id of the existing Zoho record: "
                                    f"{str(existing[0])[:400]}")

    _zc.log("push.request", source="hr-portal", target="ondemand", form=form,
            candidate_id=candidate.id, email=email, record_id=record_id, fields=len(payload),
            files=[f[0] for f in files])
    try:
        response = (_zc.update_record(token, form, record_id, payload, files=files, draft=True)
                    if record_id
                    else _zc.insert_record(token, form, payload, draft=True, files=files))
    except _zc.ZohoUnreachable as exc:
        _zc.log("push.error", source="hr-portal", form=form, candidate_id=candidate.id,
                reason="unreachable", message=str(exc))
        raise ZohoPushError(_network_message(exc)) from exc
    except _zc.ZohoError as exc:
        _zc.log("push.error", source="hr-portal", form=form, candidate_id=candidate.id,
                message=str(exc))
        raise ZohoPushError(f"Zoho rejected the push: {exc}") from exc

    _zc.log("push.response", source="hr-portal", form=form, candidate_id=candidate.id,
            response=response)
    if _zc.has_errors(response):
        raise ZohoPushError(
            "Zoho reported an error — it usually names the offending field: "
            f"{str(response)[:600]}")

    new_id = record_id or _zc.record_id_from(response)
    if not new_id:
        raise ZohoPushError(f"Zoho did not return a record id: {str(response)[:600]}")
    return {"record_id": new_id, "status": "DRAFT", "created": not record_id}
