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

# "Added By" (AddedBy) is never sent: Zoho fills it with the account whose
# OAuth token made the call and rejects any written value (7075, read only).
# Pushes run on the pushing HR's own token (`actor`, see zoho_oauth.py), and
# the HR also goes into settings.ZOHO_PUSHED_BY_FIELD, an editable lookup
# into P_Employee written with their employee record id — the field the
# record's ownership and access rules rely on.
# Pushed By, like every other field, is only written when the form shows it
# to the pushing HR (see _form_fields), so it starts filling as soon as a Zoho
# admin adds it, without a restart.


class ZohoPushError(RuntimeError):
    """Message is safe to show HR as-is."""


def _network_message(exc: "_zc.ZohoUnreachable") -> str:
    """Wording for a push that never left this machine. Saying "Zoho rejected
    the push" here is actively misleading — Zoho never saw the request, and the
    fix is on the network side, not in the candidate's data."""
    return (f"Could not reach Zoho People from this server: {exc} "
            "The candidate's data is unchanged and safe — re-sync once the "
            "connection is back.")


_PERMISSION_HELP = (" Ask a Zoho People admin to grant your Zoho role this permission on the "
                    "form (Settings > Permissions / Data Sharing). The candidate's data is "
                    "unchanged.")


def _error_message(response, form: str, record_id: str | None,
                   prefix: str = "Zoho reported an error") -> str:
    """HR-facing text for an error response. Permission errors come from the
    pushing HR's own Zoho role, not from bad data, so they say so."""
    errors = (response.get("response") or {}).get("errors") if isinstance(response, dict) else None
    code = str(errors.get("code")) if isinstance(errors, dict) else ""
    message = str(errors.get("message")) if isinstance(errors, dict) else ""
    if code == "7040":
        # Another account added the record and this role only sees its own.
        return (f"Your Zoho account is not allowed to view Zoho record {record_id or ''} on "
                f"'{form}' — it was added by another Zoho user, and your Zoho role only sees "
                "records you added. Ask a Zoho People admin to give your role View/Edit access "
                "to all records on this form (Settings > Data Sharing / Permissions), or have "
                "the HR who added it publish. The candidate's data is unchanged.")
    if code == "7038":
        return f"Your Zoho account is not allowed to add records to '{form}'." + _PERMISSION_HELP
    if "permission denied" in message.lower():
        return f"Zoho denied your account on '{form}': {message}." + _PERMISSION_HELP
    return f"{prefix}: {str(response)[:600]}"


def _form_fields(token, form: str) -> set[str]:
    """API names of the fields `form` shows to the token's account. Zoho
    hides fields the account's role has no permission on, and a write that
    names one fails outright (7013 "Field name ... is invalid")."""
    try:
        components = _zc.api("GET", f"forms/{form}/components", token=token)
    except _zc.ZohoUnreachable as exc:
        raise ZohoPushError(_network_message(exc)) from exc
    except _zc.ZohoError as exc:
        raise ZohoPushError(f"Could not read the fields of Zoho form '{form}': {exc}") from exc

    fields = set()

    def walk(node):
        if isinstance(node, dict):
            label = node.get("labelname") or node.get("labelName")
            if label:
                fields.add(label)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(components)
    if not fields:
        raise ZohoPushError(f"Your Zoho account cannot see any fields of '{form}'."
                            + _PERMISSION_HELP)
    return fields


def _drop_hidden(fields: set[str], payload: dict, files: list | None = None) -> list[str]:
    """Remove the payload keys and uploads `fields` (see _form_fields) does
    not include, so one hidden field doesn't sink the whole push. Returns
    what was left out, for HR to see."""
    hidden = sorted(k for k in payload if k not in fields)
    for key in hidden:
        del payload[key]
    if files is not None:
        hidden += sorted(f[0] for f in files if f[0] not in fields)
        files[:] = [f for f in files if f[0] in fields]
    if hidden and not payload and not files:
        raise ZohoPushError("Nothing to push: your Zoho role cannot see any of the fields "
                            f"this form writes ({', '.join(hidden)})." + _PERMISSION_HELP)
    return hidden


def _delete_denied(response) -> bool:
    """True when Zoho refused a table-row delete for lack of permission
    (7338 "You are not allowed to delete record in ... tabular section")."""
    body = response.get("response", response) if isinstance(response, dict) else {}
    errors = body.get("errors") if isinstance(body, dict) else None
    if isinstance(errors, dict):
        return str(errors.get("code")) == "7338" or "not allowed to delete" in str(errors).lower()
    return "not allowed to delete" in str(response).lower()


def _hidden_note(hidden: list[str]) -> str:
    return (f" Not sent, because your Zoho role cannot see these fields: {', '.join(hidden)}."
            " A Zoho People admin can grant your role access to them, then re-sync."
            if hidden else "")


def _add_pushed_by(fields: set[str], payload: dict, employee_record_id: str) -> str:
    """Put the pushing HR's P_Employee record id into the Pushed By field when
    `fields` has one. Returns "linked", "no_field" or "no_employee_id" (admin
    mode only: an HR without an Employee ID)."""
    field = settings.ZOHO_PUSHED_BY_FIELD
    if not field or field not in fields:
        return "no_field"
    if not employee_record_id:
        return "no_employee_id"
    payload[field] = employee_record_id
    return "linked"


def read_added_by(token, form: str, record_id: str) -> str | None:
    """Zoho's Added By on a record, read back after a push to confirm it names
    the HR whose token wrote it. Best effort: None if it cannot be read."""
    try:
        record = _zc.get_record(token, form, record_id)
    except _zc.ZohoError:
        return None
    for key, value in record.items():
        if key.replace("_", "").lower() == "addedby" and value:
            return str(value)[:255]
    return None


def push_candidate(db: Session, candidate: Candidate, target: str = "confirmation",
                   *, actor) -> dict:
    """Insert (first call) or update (every call after) this candidate's Zoho
    People record on the form `target` names (see TARGETS). `actor` is the
    pushing HR's zoho_oauth.ZohoActor. Returns
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

    token = actor.token   # the pushing HR's own token (app/services/zoho_oauth.py)

    fields = _form_fields(token, form)
    hidden = _drop_hidden(fields, payload, files)
    pushed_by_status = _add_pushed_by(fields, payload, actor.employee_record_id)

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
    # the rows already there are deleted BEFORE the update, so the record ends
    # up mirroring the portal. A role that may not delete table rows (7338)
    # makes the delete fail with nothing changed; those tables are then left
    # as they are and not re-sent, rather than duplicated. Read failures abort
    # before anything is written.
    subforms = load_subforms(cfg["map_file"])
    replace_sections = {cfg_s["_zoho_section_id"]: cfg_s
                        for cfg_s in subforms.values() if cfg_s.get("_zoho_section_id")}
    old_rows = {}
    if record_id and replace_sections:
        try:
            existing_record = _zc.get_record(token, form, record_id)
        except _zc.ZohoUnreachable as exc:
            raise ZohoPushError(_network_message(exc)) from exc
        except _zc.ZohoError as exc:
            raise ZohoPushError(f"Could not read the existing Zoho record before updating it: {exc}") from exc
        old_rows = {sec: _zc.tabular_row_ids(existing_record, cfg_s.get("_zoho_section_display", ""))
                    for sec, cfg_s in replace_sections.items()}
        old_rows = {sec: ids for sec, ids in old_rows.items() if ids}

    kept_tables = []
    if old_rows:
        try:
            cleanup = _zc.delete_tabular_rows(token, form, record_id, old_rows, draft=True)
        except _zc.ZohoUnreachable as exc:
            raise ZohoPushError(_network_message(exc)) from exc
        except _zc.ZohoError as exc:
            cleanup = {"error": str(exc)}
        _zc.log("push.tabular_cleanup", source="hr-portal", target=target, form=form,
                candidate_id=candidate.id, deleted={s: len(i) for s, i in old_rows.items()},
                response=cleanup)
        if "error" in cleanup or _zc.has_errors(cleanup):
            if not _delete_denied(cleanup):
                raise ZohoPushError("Could not remove the record's previous table rows, so "
                                    "nothing was updated. Re-sync again: "
                                    f"{str(cleanup)[:400]}")
            for sec in old_rows:
                cfg_s = replace_sections[sec]
                kept_tables.append(cfg_s.get("_zoho_section_display") or cfg_s.get("_zoho_section", sec))
                for column in (cfg_s.get("map") or {}).values():
                    payload.pop(column, None)

    # The record is written as a Zoho DRAFT: a non-draft write enforces every
    # mandatory field on the form, including ones no local data answers
    # (error 7052). As a draft it lands valid and editable for HR.
    # Education/employment rows ARE sent — they ride inside inputData as
    # column arrays, which build_full_payload has already merged in.
    _zc.log("push.request", source="hr-portal", target=target, form=form, candidate_id=candidate.id,
            email=email, record_id=record_id, fields=len(payload),
            files=[f[0] for f in files], tabular_rows=subform_counts)
    # Old table rows already deleted above are only restored by a successful update.
    rows_gone = (" Its previous Education/Experience rows were already removed; re-sync "
                 "to put them back." if old_rows and not kept_tables else "")
    try:
        response = (_zc.update_record(token, form, record_id, payload, files=files, draft=True)
                    if record_id
                    else _zc.insert_record(token, form, payload, draft=True, files=files))
    except _zc.ZohoUnreachable as exc:
        _zc.log("push.error", source="hr-portal", form=form, candidate_id=candidate.id,
                reason="unreachable", message=str(exc))
        raise ZohoPushError(_network_message(exc) + rows_gone) from exc
    except _zc.ZohoError as exc:
        _zc.log("push.error", source="hr-portal", form=form, candidate_id=candidate.id,
                message=str(exc))
        raise ZohoPushError(f"Zoho rejected the push: {exc}{rows_gone}") from exc

    _zc.log("push.response", source="hr-portal", form=form, candidate_id=candidate.id,
            response=response)
    if _zc.has_errors(response):
        raise ZohoPushError(_error_message(
            response, form, record_id,
            "Zoho reported an error — it usually names the offending field") + rows_gone)

    new_id = record_id or _zc.record_id_from(response)
    if not new_id:
        raise ZohoPushError(f"Zoho did not return a record id: {str(response)[:600]}")

    return {
        "record_id": new_id,
        "form": form,
        # Always a draft in Zoho for now (see the tabular note above).
        "status": "DRAFT",
        "fields_pushed": len(payload),
        "fields_unmapped": len(unmapped),
        "tabular_columns": len(tabular),
        "tabular_rows": sum(subform_counts.values()),
        "pushed_by": pushed_by_status,
        "hidden_fields": hidden,
        "kept_tables": kept_tables,
    }


# "Documents Collection - Trainee" — the Reference Check PDF goes into its
# Reference_check file field; the record is matched on Candidate_Email_ID.
REF_EMAIL_FIELD = "Candidate_Email_ID"
REF_NAME_FIELD = "Candidate_Name"
REF_FILE_FIELD = "Reference_check"


def push_reference_check(candidate: Candidate, *, actor) -> dict:
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
    # The CIF full name, not the (often first-name-only) name HR typed in.
    name = ((candidate.profile.full_name if candidate.profile else None)
            or candidate.name or "").strip()
    safe = "".join(ch if ch.isalnum() else "_" for ch in (name or "candidate")).strip("_")
    filename = f"Referee_Check_{safe or candidate.id}.pdf"

    token = actor.token   # the pushing HR's own token (app/services/zoho_oauth.py)

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
    # The name is always (re)written so records created with a short name
    # get corrected; a new record also gets the email it is matched on.
    payload = {REF_NAME_FIELD: name} if record_id else {REF_EMAIL_FIELD: email, REF_NAME_FIELD: name}
    fields = _form_fields(token, form)
    hidden = _drop_hidden(fields, payload, files)
    pushed_by_status = _add_pushed_by(fields, payload, actor.employee_record_id)
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
        raise ZohoPushError(_error_message(response, form, record_id))

    new_id = record_id or _zc.record_id_from(response)
    if not new_id:
        raise ZohoPushError(f"Zoho did not return a record id: {str(response)[:600]}")
    return {"record_id": new_id, "form": form, "status": "SYNCED", "created": not record_id,
            "pushed_by": pushed_by_status, "hidden_fields": hidden}


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


def push_declaration(candidate: Candidate, *, actor) -> dict:
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

    token = actor.token   # the pushing HR's own token (app/services/zoho_oauth.py)

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
    fields = _form_fields(token, form)
    hidden = _drop_hidden(fields, payload)
    pushed_by_status = _add_pushed_by(fields, payload, actor.employee_record_id)

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
        raise ZohoPushError(_error_message(response, form, record_id))

    new_id = record_id or _zc.record_id_from(response)
    if not new_id:
        raise ZohoPushError(f"Zoho did not return a record id: {str(response)[:600]}")
    return {"record_id": new_id, "form": form, "status": "SYNCED", "created": not record_id,
            "employee_linked": bool(employee_id), "pushed_by": pushed_by_status,
            "hidden_fields": hidden}


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


def push_ondemand(candidate: Candidate, *, actor) -> dict:
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

    token = actor.token   # the pushing HR's own token (app/services/zoho_oauth.py)

    fields = _form_fields(token, form)
    hidden = _drop_hidden(fields, payload, files)
    pushed_by_status = _add_pushed_by(fields, payload, actor.employee_record_id)

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
        raise ZohoPushError(_error_message(
            response, form, record_id,
            "Zoho reported an error — it usually names the offending field"))

    new_id = record_id or _zc.record_id_from(response)
    if not new_id:
        raise ZohoPushError(f"Zoho did not return a record id: {str(response)[:600]}")
    return {"record_id": new_id, "form": form, "status": "DRAFT", "created": not record_id,
            "pushed_by": pushed_by_status, "hidden_fields": hidden}
