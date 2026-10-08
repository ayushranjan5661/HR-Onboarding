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
        if existing:
            raise ZohoPushError(
                f"A Zoho record for {email} already exists on '{form}' that this candidate "
                "is not linked to. Resolve it in Zoho first (or link it via the CLI's "
                "--record-id) before publishing from here.")

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
