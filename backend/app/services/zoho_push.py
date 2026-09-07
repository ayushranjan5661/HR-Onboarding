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
from export_zoho_payload import build_full_payload, collect_values       # noqa: E402

from sqlalchemy.orm import Session                                       # noqa: E402

from app.config import settings                                          # noqa: E402
from app.models import Candidate                                         # noqa: E402

# Zoho's stock Candidate form names the email field Email_ID; the CLI's
# --email-field lets that be overridden per-org, which this button does not
# expose — fix here if a target form uses a different name.
EMAIL_FIELD = "Email_ID"


class ZohoPushError(RuntimeError):
    """Message is safe to show HR as-is."""


def _network_message(exc: "_zc.ZohoUnreachable") -> str:
    """Wording for a push that never left this machine. Saying "Zoho rejected
    the push" here is actively misleading — Zoho never saw the request, and the
    fix is on the network side, not in the candidate's data."""
    return (f"Could not reach Zoho People from this server: {exc} "
            "The candidate's data is unchanged and safe — re-sync once the "
            "connection is back.")


def push_candidate(db: Session, candidate: Candidate) -> dict:
    """Insert (first call) or update (every call after) this candidate's Zoho
    People record. Returns {"record_id", "status", "fields_pushed",
    "fields_unmapped"}. Raises ZohoPushError on anything that stops the push —
    the caller is expected to surface str(exc) to HR and move on."""
    form = settings.ZOHO_CANDIDATE_WRITE_FORM
    if not form:
        raise ZohoPushError(
            "Zoho push is not configured: set ZOHO_CANDIDATE_WRITE_FORM in .env to "
            "the exact Zoho form (formLinkName) this button should write to, once "
            "the payload has round-tripped against it via the CLI.")

    payload, _tabular, _skipped, unmapped, _subform_counts = build_full_payload(db, candidate)
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
        candidate.id, _zc.load_file_map())
    if files_missing:
        raise ZohoPushError(
            "Cannot push: these documents are mapped for Zoho but missing from the "
            f"server's uploads folder: {', '.join(files_missing)}.")

    try:
        token = _zc.access_token()
    except _zc.ZohoUnreachable as exc:
        raise ZohoPushError(_network_message(exc)) from exc
    except _zc.ZohoError as exc:
        raise ZohoPushError(f"Could not authenticate with Zoho: {exc}") from exc

    record_id = candidate.zoho_record_id
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

    # The record is written as a Zoho DRAFT, with flat fields + files only.
    # Tabular sections (education/employment) are deliberately NOT sent yet:
    # Zoho's tabularData write envelope is still unresolved, and a non-draft
    # write would fail on those mandatory tables (error 7052). As a draft the
    # record lands valid and editable, and HR completes the tables in Zoho
    # when reviewing. `tabular` is built (for the CLI/experiments) but not sent.
    _zc.log("push.request", source="hr-portal", form=form, candidate_id=candidate.id,
            email=email, record_id=record_id, fields=len(payload),
            files=[f[0] for f in files], tabular_deferred=len(_subform_counts))
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

    return {
        "record_id": new_id,
        # Always a draft in Zoho for now (see the tabular note above).
        "status": "DRAFT",
        "fields_pushed": len(payload),
        "fields_unmapped": len(unmapped),
        "tabular_deferred": len(_subform_counts),
    }
