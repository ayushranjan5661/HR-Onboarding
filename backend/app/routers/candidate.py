import json
import os
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.deps import get_current_candidate
from app import edit_access
from app import date_format
from app.date_format import normalize_field
from app.form_definitions import (
    BGV_FIELDS,
    BGV_FILE_FIELDS,
    BGV_TABLE_SECTIONS,
    CIF_FIELDS,
    CIF_FILE_FIELDS,
    DECLARATION_DOCS,
    DECLARATION_EXPECTED_DATES,
    DECLARATION_FIELDS,
    DECLARATION_FILE_FIELDS,
    DECLARATION_OPTIONAL,
    DOC_FIELDS,
    DOC_FILE_FIELDS,
    DOC_REQUIRED_BY_TYPE,
    EDUCATION_COLUMNS,
    EDUCATION_SECTIONS,
    EMPLOYMENT_COLUMNS,
    ONDEMAND_ACCOUNT_TYPES,
    ONDEMAND_FIELDS,
    ONDEMAND_FILE_FIELDS,
    ONDEMAND_OPTIONAL,
    PROFILE_FIELDS,
    REF_CHECK_FIELDS,
    REF_CHECK_FILE_FIELDS,
    REF_CHECK_OPTIONAL,
    REFERENCE_COLUMNS,
)
from app.models import (
    BGVAddressHistory,
    BGVDetails,
    BGVEducationCheck,
    BGVEmploymentCheck,
    BGVGap,
    BGVReferenceCheck,
    Candidate,
    CandidateProfile,
    CandidateStage,
    CandidateType,
    CIFDetails,
    DeclarationDetails,
    DocCollectionDetails,
    Document,
    EditPermissionStatus,
    EducationDetail,
    EmploymentDetail,
    FieldEditLog,
    FieldEditPermission,
    FormDraft,
    FormDraftDocument,
    FormStatus,
    FormSubmission,
    FormType,
    OnDemandDetails,
    ReferenceCheckDetails,
    ReferenceDetail,
)
from app.agents import doc_check_queue, doc_validator
from app.agents import prefill as prefill_agent
from app.schemas import MyStatusOut
from app.utils.file_storage import (save_bytes, save_upload, snapshot_document,
                                     upload_path)

router = APIRouter(prefix="/candidate", tags=["candidate"])

_BGV_ROW_MODELS = {
    "bgv_address_history": BGVAddressHistory,
    "bgv_education_checks": BGVEducationCheck,
    "bgv_employment_checks": BGVEmploymentCheck,
    "bgv_reference_checks": BGVReferenceCheck,
    "bgv_gaps": BGVGap,
}


@router.get("/me/status", response_model=MyStatusOut)
def my_status(db: Session = Depends(get_db), current: Candidate = Depends(get_current_candidate)):
    candidate = (
        db.query(Candidate)
        .options(joinedload(Candidate.profile), joinedload(Candidate.submissions))
        .filter(Candidate.id == current.id)
        .first()
    )
    return MyStatusOut(stage=candidate.stage.value, candidate_type=candidate.candidate_type.value,
                        forms=candidate.visible_submissions, profile=candidate.profile)


@router.get("/me/cif-summary")
def my_cif_summary(db: Session = Depends(get_db), current: Candidate = Depends(get_current_candidate)):
    """The candidate's own CIF education/employment rows, so later forms can
    pre-fill them instead of asking the same questions twice."""
    education = db.query(EducationDetail).filter(EducationDetail.candidate_id == current.id).all()
    employment = db.query(EmploymentDetail).filter(EmploymentDetail.candidate_id == current.id).all()
    return {
        "education": [{c: getattr(e, c) for c in EDUCATION_COLUMNS} for e in education],
        "employment": [{c: getattr(e, c) for c in EMPLOYMENT_COLUMNS} for e in employment],
    }


@router.get("/me/submission/{form_type}")
def my_submission(form_type: str, db: Session = Depends(get_db),
                   current: Candidate = Depends(get_current_candidate)):
    """The candidate's own saved answers for one form, so they can edit and
    resubmit instead of filling everything in again."""
    if form_type not in ("CIF", "BGV", "DOCUMENT_COLLECTION", "REFERENCE_CHECK", "DECLARATION",
                         "ONDEMAND"):
        raise HTTPException(status_code=404, detail="Unknown form")

    out: dict = {"fields": {}, "tables": {}, "documents": []}

    if form_type == "CIF":
        profile = db.query(CandidateProfile).filter(
            CandidateProfile.candidate_id == current.id).first()
        cif = db.query(CIFDetails).filter(CIFDetails.candidate_id == current.id).first()
        if profile:
            out["fields"].update({f: getattr(profile, f) for f in PROFILE_FIELDS})
        if cif:
            out["fields"].update({f: getattr(cif, f) for f in CIF_FIELDS})
        for form_key, section in EDUCATION_SECTIONS.items():
            out["tables"][form_key] = [
                {c: getattr(e, c) for c in EDUCATION_COLUMNS}
                for e in db.query(EducationDetail).filter(
                    EducationDetail.candidate_id == current.id,
                    EducationDetail.section == section).all()
            ]
        out["tables"]["employment_details"] = [
            {c: getattr(e, c) for c in EMPLOYMENT_COLUMNS}
            for e in db.query(EmploymentDetail).filter(
                EmploymentDetail.candidate_id == current.id).all()
        ]
        out["tables"]["references_list"] = [
            {c: getattr(r, c) for c in REFERENCE_COLUMNS}
            for r in db.query(ReferenceDetail).filter(
                ReferenceDetail.candidate_id == current.id).all()
        ]
    elif form_type == "BGV":
        bgv = db.query(BGVDetails).filter(BGVDetails.candidate_id == current.id).first()
        if bgv:
            out["fields"] = {f: getattr(bgv, f) for f in BGV_FIELDS}
        for key, (attr, cols) in BGV_TABLE_SECTIONS.items():
            model = _BGV_ROW_MODELS[key]
            out["tables"][key] = [
                {c: getattr(r, c) for c in cols}
                for r in db.query(model).filter(model.candidate_id == current.id).all()
            ]
    elif form_type == "REFERENCE_CHECK":
        details = db.query(ReferenceCheckDetails).filter(
            ReferenceCheckDetails.candidate_id == current.id).first()
        if details:
            out["fields"] = {f: getattr(details, f) for f in REF_CHECK_FIELDS}
    elif form_type == "DECLARATION":
        details = db.query(DeclarationDetails).filter(
            DeclarationDetails.candidate_id == current.id).first()
        if details:
            out["fields"] = {f: getattr(details, f) for f in DECLARATION_FIELDS}
    elif form_type == "ONDEMAND":
        details = db.query(OnDemandDetails).filter(
            OnDemandDetails.candidate_id == current.id).first()
        if details:
            out["fields"] = {f: getattr(details, f) for f in ONDEMAND_FIELDS}
    else:
        details = db.query(DocCollectionDetails).filter(
            DocCollectionDetails.candidate_id == current.id).first()
        if details:
            out["fields"] = {f: getattr(details, f) for f in DOC_FIELDS}

    # Which uploads are already on record, so the form can say "keep or replace".
    out["documents"] = [
        {"id": d.id, "field_key": d.field_key,
         "original_filename": d.original_filename,
         "content_type": d.content_type,
         # so the portal can show a preview rather than just a filename
         "file_available": os.path.isfile(upload_path(d.stored_filename))}
        for d in db.query(Document).filter(
            Document.candidate_id == current.id,
            Document.form_type == FormType(form_type)).all()
    ]
    return out


@router.get("/me/prefill/{form_type}")
def my_prefill(form_type: str, db: Session = Depends(get_db),
                current: Candidate = Depends(get_current_candidate)):
    """What this form can inherit from the candidate's earlier submissions —
    matched by the cross-form mapping agent, including uploads that can be
    carried over rather than re-uploaded."""
    if form_type not in ("DOCUMENT_COLLECTION", "REFERENCE_CHECK", "DECLARATION", "BGV"):
        return {"fields": {}, "documents": [], "company_labels": {}}
    return prefill_agent.build_prefill(db, current.id, form_type)


# ---------------------------------------------------------------------------
# Drafts: unsubmitted work, saved server-side so it follows the candidate to
# any device or browser. HR never reads these tables.
# ---------------------------------------------------------------------------

_DRAFT_FILE_FIELDS = {
    "CIF": CIF_FILE_FIELDS,
    "BGV": BGV_FILE_FIELDS,
    "DOCUMENT_COLLECTION": DOC_FILE_FIELDS,
    "REFERENCE_CHECK": REF_CHECK_FILE_FIELDS,
    "DECLARATION": DECLARATION_FILE_FIELDS,
    "ONDEMAND": ONDEMAND_FILE_FIELDS,
}


def _draft_form_type(form_type: str) -> FormType:
    if form_type not in _DRAFT_FILE_FIELDS:
        raise HTTPException(status_code=404, detail="Unknown form")
    return FormType(form_type)


@router.get("/me/draft/{form_type}")
def get_draft(form_type: str, db: Session = Depends(get_db),
               current: Candidate = Depends(get_current_candidate)):
    """The candidate's saved draft for one form, if any."""
    ft = _draft_form_type(form_type)
    draft = db.query(FormDraft).filter(FormDraft.candidate_id == current.id,
                                        FormDraft.form_type == ft).first()
    docs = db.query(FormDraftDocument).filter(
        FormDraftDocument.candidate_id == current.id,
        FormDraftDocument.form_type == ft).all()
    if not draft and not docs:
        return {"exists": False}

    try:
        payload = json.loads(draft.payload) if draft else {}
    except ValueError:
        payload = {}
    return {
        "exists": True,
        "saved_at": draft.updated_at if draft else None,
        "fields": payload.get("fields") or {},
        "tables": payload.get("tables") or {},
        "documents": [
            {"id": d.id, "field_key": d.field_key,
             "original_filename": d.original_filename,
             "content_type": d.content_type,
             "file_available": os.path.isfile(upload_path(d.stored_filename))}
            for d in docs
        ],
    }


@router.post("/me/draft/{form_type}")
async def save_draft(form_type: str, request: Request, db: Session = Depends(get_db),
                      current: Candidate = Depends(get_current_candidate)):
    """Save (or overwrite) the draft for one form. Accepts the same multipart
    shape as a real submit, so the page can reuse its own FormData: `fields`
    and `tables` as JSON strings, plus any attached files."""
    ft = _draft_form_type(form_type)
    form = await request.form()
    # The rest is blocking (database, OCR check of attached files) — off the
    # event loop, so one upload doesn't stall every other request.
    return await run_in_threadpool(_save_draft, ft, form, db, current)


def _save_draft(ft: FormType, form, db: Session, current: Candidate):
    def _json_or_empty(key):
        try:
            value = json.loads(form.get(key) or "{}")
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    payload = json.dumps({"fields": _json_or_empty("fields"), "tables": _json_or_empty("tables")})

    draft = db.query(FormDraft).filter(FormDraft.candidate_id == current.id,
                                        FormDraft.form_type == ft).first()
    if not draft:
        draft = FormDraft(candidate_id=current.id, form_type=ft, payload=payload)
        db.add(draft)
    else:
        draft.payload = payload
        draft.updated_at = datetime.now(timezone.utc)

    # Attached files are stored the same way real uploads are (validated,
    # neutral filename), just in the draft table.
    replaced_paths: list[str] = []
    written_paths: list[str] = []
    try:
        for field_key in _DRAFT_FILE_FIELDS[ft.value]:
            upload = form.get(field_key)
            if upload is None or not hasattr(upload, "filename") or not upload.filename:
                continue
            original, stored = save_upload(upload, current.id, f"DRAFT_{ft.value}", field_key)
            written_paths.append(upload_path(stored))
            verdict = _check_document(current, stored, upload, field_key)
            for old in db.query(FormDraftDocument).filter(
                    FormDraftDocument.candidate_id == current.id,
                    FormDraftDocument.form_type == ft,
                    FormDraftDocument.field_key == field_key).all():
                replaced_paths.append(upload_path(old.stored_filename))
                db.delete(old)
            db.flush()   # release the (candidate, form, field) unique slot
            row = FormDraftDocument(candidate_id=current.id, form_type=ft, field_key=field_key,
                                    original_filename=original, stored_filename=stored,
                                    content_type=upload.content_type)
            _record_verdict(db, row, verdict)
            db.add(row)
    except Exception:
        db.rollback()
        _remove_files(written_paths)
        raise

    db.commit()
    doc_check_queue.flush_pending(db)
    _remove_files(replaced_paths)

    saved_docs = db.query(FormDraftDocument).filter(
        FormDraftDocument.candidate_id == current.id,
        FormDraftDocument.form_type == ft).count()
    return {"detail": "Draft saved", "saved_at": draft.updated_at, "document_count": saved_docs}


@router.delete("/me/draft/{form_type}")
def delete_draft(form_type: str, db: Session = Depends(get_db),
                  current: Candidate = Depends(get_current_candidate)):
    ft = _draft_form_type(form_type)
    paths = _discard_draft(db, current.id, ft)
    db.commit()
    _remove_files(paths)   # files go only once the rows are really gone
    return {"detail": "Draft discarded"}


@router.get("/draft-documents/{draft_doc_id}/download")
def download_draft_document(draft_doc_id: int, db: Session = Depends(get_db),
                             current: Candidate = Depends(get_current_candidate)):
    """Let the candidate view a file they attached to a draft."""
    doc = db.query(FormDraftDocument).filter(
        FormDraftDocument.id == draft_doc_id,
        FormDraftDocument.candidate_id == current.id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    path = upload_path(doc.stored_filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404,
                             detail=f"'{doc.original_filename}' is no longer stored on the server. "
                                     "Please attach it again.")
    return FileResponse(path, filename=doc.original_filename,
                         media_type=doc.content_type or "application/octet-stream")


def _draft_document_fields(db, candidate_id: int, ft: FormType) -> set[str]:
    return {d.field_key for d in db.query(FormDraftDocument).filter(
        FormDraftDocument.candidate_id == candidate_id,
        FormDraftDocument.form_type == ft).all()}


def _promote_draft_documents(db, candidate_id: int, ft: FormType, provided: set[str]) -> list[str]:
    """On submit, turn drafted files into real Documents for every field the
    candidate did not upload again in this request. The file is copied, so
    discarding the draft afterwards cannot remove the submitted evidence."""
    promoted = []
    existing = {d.field_key for d in db.query(Document).filter(
        Document.candidate_id == candidate_id, Document.form_type == ft).all()}
    for draft_doc in db.query(FormDraftDocument).filter(
            FormDraftDocument.candidate_id == candidate_id,
            FormDraftDocument.form_type == ft).all():
        if draft_doc.field_key in provided or draft_doc.field_key in existing:
            continue
        try:
            with open(upload_path(draft_doc.stored_filename), "rb") as fh:
                data = fh.read()
        except OSError:
            continue   # drafted file vanished; nothing to promote
        stored = save_bytes(data, candidate_id, ft.value, draft_doc.field_key)
        doc = Document(candidate_id=candidate_id, form_type=ft, field_key=draft_doc.field_key,
                       original_filename=draft_doc.original_filename,
                       stored_filename=stored, content_type=draft_doc.content_type)
        doc_validator.copy_between(draft_doc, doc)   # same bytes: the draft's verdict stands
        if draft_doc.ai_checked_at is None and doc_check_queue.deferred():
            doc_check_queue.mark(db, doc)   # draft's check still queued; check the copy too
        db.add(doc)
        promoted.append(draft_doc.field_key)
    return promoted


def _discard_draft(db, candidate_id: int, ft: FormType) -> list[str]:
    """Delete a draft and its files. Returns paths to unlink after commit."""
    paths = []
    for doc in db.query(FormDraftDocument).filter(
            FormDraftDocument.candidate_id == candidate_id,
            FormDraftDocument.form_type == ft).all():
        paths.append(upload_path(doc.stored_filename))
        db.delete(doc)
    db.query(FormDraft).filter(FormDraft.candidate_id == candidate_id,
                                FormDraft.form_type == ft).delete()
    return paths


def _apply_fields(obj, form, field_names: list[str]):
    """Copy each present form value onto the matching model column.

    Date columns are normalised to DD/MM/YYYY on the way in — the forms send
    free text, so this is where the app's one date format is enforced."""
    for name in field_names:
        if name in form:
            setattr(obj, name, normalize_field(name, form.get(name)))


def _reject_invalid_dates(*groups) -> None:
    """Refuse a submission carrying a date we cannot read.

    Dates arrive as free text, so a US-shaped "09/22/2026" (month 22) would
    otherwise be stored verbatim and only surface much later — in a Zoho push
    or during BGV. Fail here instead, naming the fields, so the candidate can
    fix it while the form is still in front of them.
    """
    bad: list[str] = []
    for group in groups:
        for values in (group if isinstance(group, list) else [group]):
            bad.extend(date_format.invalid_labels(values))
    if bad:
        unique = list(dict.fromkeys(bad))
        raise HTTPException(status_code=400, detail=(
            f"Enter {', '.join(unique)} as DD/MM/YYYY, e.g. 15/08/1998. "
            f"Check the day and month are the right way round."))


def _rows_from_json(form, key: str, columns: list[str]) -> list[dict]:
    """A repeating table arrives as a JSON string; return sanitized row dicts."""
    try:
        rows = json.loads(form.get(key, "[]"))
    except (TypeError, ValueError):
        return []
    if not isinstance(rows, list):
        return []
    return [{c: normalize_field(c, str(row.get(c, "") or "")) for c in columns}
            for row in rows if isinstance(row, dict)]


def _remove_files(paths: list[str]) -> None:
    for path in paths:
        try:
            os.remove(path)
        except OSError:
            pass


def _check_document(current: Candidate, stored: str, upload,
                    field_key: str) -> doc_validator.Verdict | None:
    """Run the document-validation agent on a file that has just been written.

    In "block" mode a confident mismatch is refused here, as a 400 the page
    shows under the field. The caller's cleanup removes the written file — the
    check runs before any row for it exists, so there is nothing else to undo.

    Otherwise the verdict is only for HR, so the candidate is not made to wait
    for OCR: this returns None and `_record_verdict` queues the row to be
    checked after commit (app/agents/doc_check_queue.py).
    """
    if doc_check_queue.deferred():
        return None
    verdict = doc_validator.validate_path(
        upload_path(stored), upload.content_type, upload.filename or "", field_key,
        doc_validator.profile_dict(current.profile))
    if verdict.blocked:
        label = field_key.replace("_", " ")
        raise HTTPException(status_code=400, detail=f"{label}: {verdict.note}")
    return verdict


def _record_verdict(db, row, verdict: doc_validator.Verdict | None) -> None:
    if verdict is None:
        doc_check_queue.mark(db, row)    # checked in the background after commit
    else:
        doc_validator.apply_to(row, verdict)


def _save_files(db, form, current, form_type: str, file_fields: list[str]) -> list[str]:
    """Only fields with a newly chosen file are touched — on an edit, anything
    the candidate leaves blank keeps the file already on record.

    Returns the disk paths of replaced files. They are deleted by the caller
    AFTER the transaction commits: deleting them here would destroy the old
    upload for good if a later field failed validation and rolled the request
    back. If anything raises mid-way, files already written for this request
    are removed so the failed attempt leaves no orphans.

    Each new file goes through the document check; its verdict is stored on
    the row for HR. The candidate is never shown it."""
    replaced_paths: list[str] = []
    written_paths: list[str] = []
    try:
        for field_key in file_fields:
            upload = form.get(field_key)
            if upload is None or not hasattr(upload, "filename") or not upload.filename:
                continue
            # Validate and store the new file before touching the old one.
            original, stored = save_upload(upload, current.id, form_type, field_key)
            written_paths.append(upload_path(stored))
            verdict = _check_document(current, stored, upload, field_key)
            # Replacing a file: drop the previous rows so we don't accumulate
            # duplicates; the old files on disk go only after commit.
            for old in db.query(Document).filter(
                    Document.candidate_id == current.id,
                    Document.form_type == FormType(form_type),
                    Document.field_key == field_key).all():
                replaced_paths.append(upload_path(old.stored_filename))
                db.delete(old)
            doc = Document(candidate_id=current.id, form_type=FormType(form_type), field_key=field_key,
                           original_filename=original, stored_filename=stored,
                           content_type=upload.content_type)
            _record_verdict(db, doc, verdict)
            db.add(doc)
    except Exception:
        _remove_files(written_paths)
        raise
    return replaced_paths


@router.post("/forms/cif")
async def submit_cif(request: Request, db: Session = Depends(get_db),
                      current: Candidate = Depends(get_current_candidate)):
    form = await request.form()
    # Blocking from here on (database, OCR, prefill mapping) — run it off the
    # event loop so one submit doesn't stall every other request.
    return await run_in_threadpool(_submit_cif, form, db, current)


def _submit_cif(form, db: Session, current: Candidate):
    if current.candidate_type == CandidateType.ONDEMAND:
        raise HTTPException(status_code=404, detail="Unknown form")
    # The CIF is a one-shot form: once submitted it is frozen, and the only
    # way a value changes afterwards is a field HR explicitly opens (see
    # /me/edit-access). That keeps the record HR reviewed from moving under
    # them, and makes every later change auditable.
    submission = db.query(FormSubmission).filter(FormSubmission.candidate_id == current.id,
                                                   FormSubmission.form_type == FormType.CIF).first()
    # HR can withdraw a form they have sent (see hr.unsend_form), which puts it
    # back to LOCKED. That is not "already submitted", so it gets its own reply.
    if submission and submission.status == FormStatus.LOCKED:
        raise HTTPException(status_code=400,
                             detail="This form is not open for you right now.")
    if submission and submission.status != FormStatus.PENDING:
        raise HTTPException(
            status_code=400,
            detail="Your CIF has already been submitted and can no longer be edited. "
                    "If something needs correcting, ask HR to open that field for you.")
    if current.stage not in (CandidateStage.INVITED, CandidateStage.CIF_SUBMITTED):
        raise HTTPException(status_code=400,
                             detail="Your CIF has already been reviewed and can no longer be edited.")

    # Nothing is written until every date on the form reads as DD/MM/YYYY.
    employment_rows = _rows_from_json(form, "employment_details", EMPLOYMENT_COLUMNS)
    _reject_invalid_dates(
        {f: form.get(f) for f in PROFILE_FIELDS + CIF_FIELDS if f in form},
        employment_rows,
    )

    # Shared profile fields
    profile = db.query(CandidateProfile).filter(CandidateProfile.candidate_id == current.id).first()
    if not profile:
        profile = CandidateProfile(candidate_id=current.id)
        db.add(profile)
    _apply_fields(profile, form, PROFILE_FIELDS)

    # CIF-only flat fields
    cif = db.query(CIFDetails).filter(CIFDetails.candidate_id == current.id).first()
    if not cif:
        cif = CIFDetails(candidate_id=current.id)
        db.add(cif)
    _apply_fields(cif, form, CIF_FIELDS)
    # The ex-employee follow-ups only mean anything alongside a "Yes"; drop
    # them otherwise so a changed answer can't leave a stale Employee ID.
    if cif.worked_in_levelshift_before != "Yes":
        cif.levelshift_employee_id = None
        cif.levelshift_experience_yrs = None

    # Repeating tables -> one DB row per entry (replace any previous rows)
    db.query(EducationDetail).filter(EducationDetail.candidate_id == current.id).delete()
    for form_key, section in EDUCATION_SECTIONS.items():
        for row in _rows_from_json(form, form_key, EDUCATION_COLUMNS):
            db.add(EducationDetail(candidate_id=current.id, section=section, **row))

    db.query(EmploymentDetail).filter(EmploymentDetail.candidate_id == current.id).delete()
    for row in employment_rows:
        db.add(EmploymentDetail(candidate_id=current.id, **row))

    db.query(ReferenceDetail).filter(ReferenceDetail.candidate_id == current.id).delete()
    for row in _rows_from_json(form, "references_list", REFERENCE_COLUMNS):
        db.add(ReferenceDetail(candidate_id=current.id, **row))

    replaced = _save_files(db, form, current, "CIF", CIF_FILE_FIELDS)
    # Files attached to the draft count as this submission's uploads unless
    # the candidate picked a new file for that field just now.
    provided_cif = {k for k in CIF_FILE_FIELDS
                     if hasattr(form.get(k), "filename") and form.get(k).filename}
    _promote_draft_documents(db, current.id, FormType.CIF, provided_cif)
    replaced += _discard_draft(db, current.id, FormType.CIF)

    if not submission:
        submission = FormSubmission(candidate_id=current.id, form_type=FormType.CIF)
        db.add(submission)
    submission.status = FormStatus.SUBMITTED
    submission.submitted_at = datetime.now(timezone.utc)

    current.stage = CandidateStage.CIF_SUBMITTED   # unchanged on a resubmit
    db.commit()
    doc_check_queue.flush_pending(db)
    _remove_files(replaced)   # replaced uploads go only once the new state is safe
    return {"detail": "CIF submitted. HR will review your details shortly."}


@router.post("/forms/{form_type}/submit")
async def submit_followup_form(form_type: str, request: Request, db: Session = Depends(get_db),
                                current: Candidate = Depends(get_current_candidate)):
    if form_type == "DECLARATION":
        # Filled only through its own link (routers/declaration.py).
        raise HTTPException(status_code=403,
                            detail="Fill the Declaration form using the link HR sent you.")
    form = await request.form()
    # Blocking from here on (database, OCR, prefill mapping) — off the event loop.
    if form_type == "ONDEMAND":
        return await run_in_threadpool(_submit_ondemand, form, db, current)
    return await run_in_threadpool(_submit_followup_form, form_type, form, db, current)


# Profile columns the On-Demand form also fills, so documents checks, Zoho and
# the HR header read the same name / contact / bank details as for anyone else.
_ONDEMAND_PROFILE_FIELDS = ["full_name", "email", "contact_number", "alternate_number",
                            "date_of_birth", "bank_account_number", "bank_ifsc_code"]


def _submit_ondemand(form, db: Session, current: Candidate):
    """The On-Demand Candidate form: the first and only form an On-Demand
    Consultant fills. Like the CIF it is frozen once submitted, and HR decides
    on it with Approve / Reject."""
    if current.candidate_type != CandidateType.ONDEMAND:
        raise HTTPException(status_code=404, detail="Unknown form")
    submission = db.query(FormSubmission).filter(FormSubmission.candidate_id == current.id,
                                                   FormSubmission.form_type == FormType.ONDEMAND).first()
    if not submission or submission.status == FormStatus.LOCKED:
        raise HTTPException(status_code=400, detail="This form is not open for you right now.")
    if submission.status != FormStatus.PENDING:
        raise HTTPException(
            status_code=400,
            detail="Your form has already been submitted and can no longer be edited. "
                    "If something needs correcting, ask HR to open that field for you.")
    if current.stage not in (CandidateStage.INVITED, CandidateStage.CIF_SUBMITTED):
        raise HTTPException(status_code=400,
                             detail="Your form has already been reviewed and can no longer be edited.")

    values = {f: form.get(f) for f in ONDEMAND_FIELDS if f in form}
    _reject_invalid_dates(values)
    missing = [f for f in ONDEMAND_FIELDS
               if f not in ONDEMAND_OPTIONAL and not str(form.get(f) or "").strip()]
    if missing:
        raise HTTPException(status_code=400,
                             detail="Please fill in every field: " + ", ".join(missing))
    if form.get("bank_account_type") not in ONDEMAND_ACCOUNT_TYPES:
        raise HTTPException(status_code=400, detail="Choose Savings or Current account.")

    already = {d.field_key for d in db.query(Document).filter(
        Document.candidate_id == current.id, Document.form_type == FormType.ONDEMAND).all()}
    already |= _draft_document_fields(db, current.id, FormType.ONDEMAND)
    provided = {k for k in ONDEMAND_FILE_FIELDS
                if hasattr(form.get(k), "filename") and form.get(k).filename}
    missing_docs = [k for k in ONDEMAND_FILE_FIELDS if k not in already and k not in provided]
    if missing_docs:
        raise HTTPException(status_code=400,
                             detail="Missing required documents: " + ", ".join(missing_docs))

    details = db.query(OnDemandDetails).filter(OnDemandDetails.candidate_id == current.id).first()
    if not details:
        details = OnDemandDetails(candidate_id=current.id)
        db.add(details)
    _apply_fields(details, form, ONDEMAND_FIELDS)

    profile = db.query(CandidateProfile).filter(CandidateProfile.candidate_id == current.id).first()
    if not profile:
        profile = CandidateProfile(candidate_id=current.id)
        db.add(profile)
    _apply_fields(profile, form, _ONDEMAND_PROFILE_FIELDS)

    replaced = _save_files(db, form, current, "ONDEMAND", ONDEMAND_FILE_FIELDS)
    _promote_draft_documents(db, current.id, FormType.ONDEMAND, provided)
    replaced += _discard_draft(db, current.id, FormType.ONDEMAND)

    submission.status = FormStatus.SUBMITTED
    submission.submitted_at = datetime.now(timezone.utc)
    current.stage = CandidateStage.CIF_SUBMITTED   # awaiting HR's Approve / Reject
    db.commit()
    doc_check_queue.flush_pending(db)
    _remove_files(replaced)
    return {"detail": "Form submitted. HR will review your details shortly."}


def _submit_followup_form(form_type: str, form, db: Session, current: Candidate):
    if form_type not in ("BGV", "DOCUMENT_COLLECTION", "REFERENCE_CHECK", "DECLARATION"):
        raise HTTPException(status_code=404, detail="Unknown form")
    if form_type == "REFERENCE_CHECK" and not current.uses_reference_check:
        raise HTTPException(status_code=404, detail="Unknown form")
    # HR may still send a follow-up form after marking onboarding complete
    # (e.g. a Reference Check added later); the PENDING check below is the gate.
    if current.stage not in (CandidateStage.APPROVED_FOR_BGV, CandidateStage.ONBOARDING_COMPLETE):
        raise HTTPException(status_code=400, detail="This form is not unlocked for you yet")

    # Like the CIF, a follow-up form is frozen once submitted: the record HR
    # reviews must not move under them. Corrections afterwards go through a
    # value HR opens (see /me/edit-access), which costs a reason and is audited.
    submission = db.query(FormSubmission).filter(
        FormSubmission.candidate_id == current.id, FormSubmission.form_type == FormType(form_type)
    ).first()
    if not submission or submission.status == FormStatus.LOCKED:
        raise HTTPException(status_code=400, detail="This form is not unlocked for you yet")
    if submission.status != FormStatus.PENDING:
        raise HTTPException(
            status_code=400,
            detail="You have already submitted this form and it can no longer be edited. "
                    "If something needs correcting, ask HR to open that field for you.")

    if form_type == "BGV":
        _reject_invalid_dates(
            {f: form.get(f) for f in BGV_FIELDS if f in form},
            *[_rows_from_json(form, key, columns)
              for key, (_, columns) in BGV_TABLE_SECTIONS.items()],
        )
        details = db.query(BGVDetails).filter(BGVDetails.candidate_id == current.id).first()
        if not details:
            details = BGVDetails(candidate_id=current.id)
            db.add(details)
        _apply_fields(details, form, BGV_FIELDS)
        # Repeating verification sections -> one row each, replacing any previous.
        for form_key, (_, columns) in BGV_TABLE_SECTIONS.items():
            model = _BGV_ROW_MODELS[form_key]
            db.query(model).filter(model.candidate_id == current.id).delete()
            for row in _rows_from_json(form, form_key, columns):
                db.add(model(candidate_id=current.id, **row))
        replaced = _save_files(db, form, current, "BGV", BGV_FILE_FIELDS)
        provided_bgv = {k for k in BGV_FILE_FIELDS
                         if hasattr(form.get(k), "filename") and form.get(k).filename}
        _promote_draft_documents(db, current.id, FormType.BGV, provided_bgv)
    elif form_type == "REFERENCE_CHECK":
        _reject_invalid_dates({f: form.get(f) for f in REF_CHECK_FIELDS if f in form})
        missing = [f for f in REF_CHECK_FIELDS
                   if f not in REF_CHECK_OPTIONAL and not str(form.get(f) or "").strip()]
        if missing:
            raise HTTPException(status_code=400,
                                 detail="Please fill in every field: " + ", ".join(missing))
        details = db.query(ReferenceCheckDetails).filter(
            ReferenceCheckDetails.candidate_id == current.id).first()
        if not details:
            details = ReferenceCheckDetails(candidate_id=current.id)
            db.add(details)
        if details.declaration_accepted != "Yes" and form.get("declaration_accepted") != "Yes":
            raise HTTPException(status_code=400, detail="Please accept the declaration.")
        provided_ref = {k for k in REF_CHECK_FILE_FIELDS
                         if hasattr(form.get(k), "filename") and form.get(k).filename}
        _apply_fields(details, form, REF_CHECK_FIELDS)
        replaced = _save_files(db, form, current, "REFERENCE_CHECK", REF_CHECK_FILE_FIELDS)
        _promote_draft_documents(db, current.id, FormType.REFERENCE_CHECK, provided_ref)
    elif form_type == "DECLARATION":
        _reject_invalid_dates({f: form.get(f) for f in DECLARATION_FIELDS if f in form})
        missing = [f for f in DECLARATION_FIELDS
                   if f not in DECLARATION_OPTIONAL and not str(form.get(f) or "").strip()]
        if missing:
            raise HTTPException(status_code=400,
                                 detail="Please fill in every field: " + ", ".join(missing))
        if not any(form.get(key) == "Yes" for key, _ in DECLARATION_DOCS):
            raise HTTPException(status_code=400,
                                 detail="Tick at least one document you still need to submit.")
        if (form.get("semester_marksheets_pending") == "Yes"
                and not str(form.get("pending_semesters") or "").strip()):
            raise HTTPException(status_code=400,
                                 detail="Say which semesters' mark statements are pending.")
        no_date = [label for key, label in DECLARATION_DOCS
                   if form.get(key) == "Yes"
                   and not str(form.get(DECLARATION_EXPECTED_DATES[key]) or "").strip()]
        if no_date:
            raise HTTPException(status_code=400,
                                 detail="Enter the expected date to submit: " + ", ".join(no_date))
        if form.get("declaration_accepted") != "Yes":
            raise HTTPException(status_code=400, detail="Please accept the declaration.")
        details = db.query(DeclarationDetails).filter(
            DeclarationDetails.candidate_id == current.id).first()
        if not details:
            details = DeclarationDetails(candidate_id=current.id)
            db.add(details)
        # Unticked checkboxes are absent from the form; store them as blank
        # rather than keeping a tick from an earlier save.
        for key, _ in DECLARATION_DOCS:
            setattr(details, key, "Yes" if form.get(key) == "Yes" else None)
        _apply_fields(details, form, [f for f in DECLARATION_FIELDS
                                      if f not in dict(DECLARATION_DOCS)])
        if details.semester_marksheets_pending != "Yes":
            details.pending_semesters = None
        for key, date_key in DECLARATION_EXPECTED_DATES.items():
            if getattr(details, key) != "Yes":
                setattr(details, date_key, None)
        provided_decl = {k for k in DECLARATION_FILE_FIELDS
                          if hasattr(form.get(k), "filename") and form.get(k).filename}
        # The signature is required, but the CIF signature carries over when
        # no new one is given (see field_mapper.CURATED).
        has_signature = (
            provided_decl
            or db.query(Document).filter(Document.candidate_id == current.id,
                                         Document.form_type == FormType.DECLARATION,
                                         Document.field_key == "declaration_signature").first()
            or "declaration_signature" in _draft_document_fields(db, current.id, FormType.DECLARATION)
            or any(d["target_field"] == "declaration_signature" and d.get("available", True)
                   for d in prefill_agent.build_prefill(db, current.id, "DECLARATION")["documents"]))
        if not has_signature:
            raise HTTPException(status_code=400, detail="Please upload your signature.")
        replaced = _save_files(db, form, current, "DECLARATION", DECLARATION_FILE_FIELDS)
        _promote_draft_documents(db, current.id, FormType.DECLARATION, provided_decl)
    else:
        details = db.query(DocCollectionDetails).filter(DocCollectionDetails.candidate_id == current.id).first()
        if not details:
            details = DocCollectionDetails(candidate_id=current.id)
            db.add(details)
        _apply_fields(details, form, DOC_FIELDS)
        # Mandatory documents depend on whether this is an experienced hire
        # or a fresher — reject early rather than storing a partial set.
        required = DOC_REQUIRED_BY_TYPE.get(current.candidate_type.value, [])
        # On an edit, a mandatory document is already satisfied if it is on
        # record — the candidate should not have to re-pick every file.
        already = {d.field_key for d in db.query(Document).filter(
            Document.candidate_id == current.id,
            Document.form_type == FormType.DOCUMENT_COLLECTION).all()}
        # A file attached to the draft counts too — it is promoted below.
        already |= _draft_document_fields(db, current.id, FormType.DOCUMENT_COLLECTION)
        # A mandatory upload is also satisfied if it can be carried over from
        # an earlier form (e.g. the CIF profile picture is the passport photo) —
        # but only if the source file actually still exists on disk.
        carryable = {d["target_field"] for d in prefill_agent.build_prefill(
            db, current.id, "DOCUMENT_COLLECTION")["documents"]
            if d.get("available", True)}
        missing = [k for k in required
                    if k not in already and k not in carryable
                    and not (hasattr(form.get(k), "filename") and form.get(k).filename)]
        if missing:
            raise HTTPException(status_code=400,
                                 detail="Missing required documents: " + ", ".join(missing))
        replaced = _save_files(db, form, current, "DOCUMENT_COLLECTION", DOC_FILE_FIELDS)
        provided_doc = {k for k in DOC_FILE_FIELDS
                         if hasattr(form.get(k), "filename") and form.get(k).filename}
        _promote_draft_documents(db, current.id, FormType.DOCUMENT_COLLECTION, provided_doc)

    # Anything the candidate already gave us on an earlier form and did not
    # re-upload here is copied across automatically.
    file_fields = {"BGV": BGV_FILE_FIELDS, "REFERENCE_CHECK": REF_CHECK_FILE_FIELDS,
                   "DECLARATION": DECLARATION_FILE_FIELDS}.get(form_type, DOC_FILE_FIELDS)
    provided = {k for k in file_fields
                 if hasattr(form.get(k), "filename") and form.get(k).filename}
    carried = prefill_agent.carry_documents(db, current.id, form_type, provided)

    submission.status = FormStatus.SUBMITTED
    submission.submitted_at = datetime.now(timezone.utc)
    replaced += _discard_draft(db, current.id, FormType(form_type))
    db.commit()
    doc_check_queue.flush_pending(db)
    _remove_files(replaced)   # replaced uploads go only once the new state is safe
    detail = "Form submitted. HR will review it shortly."
    if carried:
        detail += f" ({len(carried)} document(s) carried over from your earlier forms.)"
    return {"detail": detail, "carried_documents": carried}


# ---------------------------------------------------------------------------
# Correcting a submitted form, one field at a time
#
# A submitted CIF is read-only. HR can open a single field for the candidate;
# changing it then costs them a written reason, and the before/after, the
# reason, the timestamp and who made the change all go to the audit log.
# ---------------------------------------------------------------------------

@router.get("/me/edit-access")
def my_edit_access(db: Session = Depends(get_db),
                    current: Candidate = Depends(get_current_candidate)):
    """The fields HR has opened for this candidate to correct."""
    permissions = edit_access.active_permissions(db, current.id)
    return {
        "permissions": [
            {
                "id": p.id,
                "form": edit_access.owning_form(p.form_type),
                "form_type": p.form_type,
                "field_kind": p.field_kind,
                "field_name": p.field_name,
                "row_table": p.row_table,
                "row_label": edit_access.row_label_for(db, current.id, p),
                "hr_note": p.hr_note,
                "granted_at": p.granted_at,
                "current_value": edit_access.current_value(db, current.id, p),
                **(_section_payload(db, current.id, p) if p.field_kind == "SECTION" else {}),
            }
            for p in permissions
        ]
    }


def _section_payload(db: Session, candidate_id: int, perm: FieldEditPermission) -> dict:
    """A reopened section's columns and entries, for the page to edit as a table."""
    columns = edit_access.ROW_TABLES[perm.row_table][1]
    return {
        "columns": columns,
        "rows": [{"id": r.id, **{c: getattr(r, c) for c in columns}}
                 for r in edit_access.section_rows(db, candidate_id, perm.row_table,
                                                    perm.field_name)],
    }


@router.post("/me/edit-access/apply")
async def apply_granted_edits(request: Request, db: Session = Depends(get_db),
                               current: Candidate = Depends(get_current_candidate)):
    """Apply every correction the candidate filled in, as one audited action.

    Multipart: `changes` is a JSON list of `{permission_id, new_value?}`, a
    document's replacement file rides along as `file_<permission_id>`, and
    `reason` explains the submission as a whole. HR opens these fields
    together, so the candidate is asked once rather than field by field — but
    asked they are: an unexplained change to reviewed data is what this flow
    exists to prevent.

    All or nothing: if one entry is invalid the whole submission is rejected
    and nothing changes, so the candidate never ends up half-corrected.
    """
    form = await request.form()
    # Blocking from here on (database, OCR of replacement files) — off the event loop.
    return await run_in_threadpool(_apply_granted_edits, form, db, current)


def _apply_granted_edits(form, db: Session, current: Candidate):
    try:
        changes = json.loads(form.get("changes") or "[]")
    except (TypeError, ValueError):
        changes = None
    if not isinstance(changes, list) or not changes:
        raise HTTPException(status_code=400, detail="Nothing to submit.")

    reason = str(form.get("reason") or "").strip()
    if len(reason) < 5:
        raise HTTPException(status_code=400,
                             detail="Please give a reason for these changes "
                                    "(at least 5 characters).")

    change_set_id = uuid4().hex
    written_paths: list[str] = []    # this request's files, removed if it fails
    applied = 0
    confirmed = 0
    try:
        for change in changes:
            if not isinstance(change, dict):
                raise HTTPException(status_code=400, detail="Malformed change.")
            perm = _active_permission(db, current.id, change.get("permission_id"))
            if change.get("unchanged"):
                # Checked and already right: close it, and say so on record.
                db.add(FieldEditLog(
                    candidate_id=current.id, form_type=edit_access.owning_form(perm.form_type),
                    field_name=edit_access.audit_field_name(perm),
                    old_value=None, new_value=None, action="CONFIRM", reason=reason,
                    actor_role="CANDIDATE", edited_by_candidate_id=current.id,
                    permission_id=perm.id, change_set_id=change_set_id))
            elif perm.field_kind == "DOCUMENT":
                written_paths += _apply_granted_document(
                    db, current, perm, form.get(f"file_{perm.id}"), reason, change_set_id)
            elif perm.field_kind == "SECTION":
                _apply_granted_section(db, current, perm, change.get("rows"),
                                        reason, change_set_id)
            else:
                _apply_granted_field(db, current, perm, change.get("new_value"),
                                      reason, change_set_id)

            perm.status = EditPermissionStatus.USED.value
            perm.resolved_at = datetime.now(timezone.utc)
            if change.get("unchanged"):
                confirmed += 1
            else:
                applied += 1
        db.commit()
        doc_check_queue.flush_pending(db)
    except Exception:
        db.rollback()
        _remove_files(written_paths)   # a failed attempt leaves no orphans
        raise

    detail = f"{applied} change(s) saved" if applied else "No values changed"
    if confirmed:
        detail += f", {confirmed} confirmed as already correct"
    return {"detail": detail + " — recorded in the audit trail.",
            "changed": applied, "confirmed": confirmed}


def _active_permission(db: Session, candidate_id: int, permission_id) -> FieldEditPermission:
    """The grant being used, if it is this candidate's and still open."""
    perm = db.query(FieldEditPermission).filter(
        FieldEditPermission.id == permission_id,
        FieldEditPermission.candidate_id == candidate_id).first()
    if not perm:
        raise HTTPException(status_code=404, detail="This edit request no longer exists")
    if perm.status != EditPermissionStatus.ACTIVE.value:
        raise HTTPException(
            status_code=400,
            detail=("You have already submitted this change." if perm.status == "USED"
                     else "HR has withdrawn access to this field."))
    # Belt and braces: the grant itself was validated, but the allowed set can
    # shrink between granting and using it.
    edit_access.validate_target(db, candidate_id, perm.form_type, perm.field_kind,
                                 perm.field_name, perm.row_table, perm.row_id)
    return perm


def _apply_granted_field(db: Session, current: Candidate, perm: FieldEditPermission,
                          raw_value, reason: str, change_set_id: str) -> None:
    """Apply a granted change to a detail-table column or to one cell of a
    repeating entry — the two differ only in which row is fetched."""
    if perm.field_kind == "ROW_FIELD":
        row = edit_access.table_row(db, perm.row_table, current.id, perm.row_id)
        if not row:
            raise HTTPException(status_code=400, detail="That entry no longer exists")
    else:
        row = edit_access.detail_row(db, perm.form_type, current.id)
        if not row:
            raise HTTPException(status_code=400, detail="No submitted data for this form yet")

    new_value = None if raw_value is None else str(raw_value).strip()
    if new_value:
        _reject_invalid_dates({perm.field_name: new_value})
        new_value = normalize_field(perm.field_name, new_value)
    old_value = getattr(row, perm.field_name)
    if (old_value or "") == (new_value or ""):
        raise HTTPException(
            status_code=400,
            detail=f"'{perm.field_name}' is the same as the value already on record.")

    setattr(row, perm.field_name, new_value or None)
    db.add(FieldEditLog(
        candidate_id=current.id, form_type=edit_access.owning_form(perm.form_type),
        field_name=edit_access.audit_field_name(perm),
        old_value=old_value, new_value=new_value or None, action="EDIT", reason=reason,
        actor_role="CANDIDATE", edited_by_candidate_id=current.id, permission_id=perm.id,
        change_set_id=change_set_id))


def _apply_granted_section(db: Session, current: Candidate, perm: FieldEditPermission,
                            raw_rows, reason: str, change_set_id: str) -> None:
    """Rewrite one reopened section: entries with an `id` are edited, entries
    without one are added, and entries left out are removed. Every changed
    cell is its own audit line, so HR sees exactly what moved."""
    if not isinstance(raw_rows, list) or not all(isinstance(r, dict) for r in raw_rows):
        raise HTTPException(status_code=400, detail="Malformed change.")
    model, columns, _, _, _ = edit_access.ROW_TABLES[perm.row_table]
    title = edit_access.section_title(perm.row_table, perm.field_name)
    existing = {r.id: r for r in edit_access.section_rows(
        db, current.id, perm.row_table, perm.field_name)}

    entries = []
    for raw in raw_rows:
        values = {c: normalize_field(c, str(raw.get(c) or "").strip()) or None for c in columns}
        if any(values.values()):          # a blank row is just an unused one
            entries.append((raw.get("id"), values))
    if not entries:
        raise HTTPException(status_code=400, detail=f"Keep at least one entry in {title}.")
    for _, values in entries:
        _reject_invalid_dates({c: v for c, v in values.items() if v})

    def log(column, old, new, action):
        db.add(FieldEditLog(
            candidate_id=current.id, form_type=edit_access.owning_form(perm.form_type),
            field_name=f"{perm.row_table}.{column}", old_value=old, new_value=new,
            action=action, reason=reason, actor_role="CANDIDATE",
            edited_by_candidate_id=current.id, permission_id=perm.id,
            change_set_id=change_set_id))

    changed = False
    kept = set()
    for row_id, values in entries:
        if row_id is None:
            db.add(model(candidate_id=current.id, section=perm.field_name, **values))
            for c, v in values.items():
                if v:
                    log(c, None, v, "ADD")
            changed = True
            continue
        row = existing.get(row_id)
        if not row or row_id in kept:
            raise HTTPException(status_code=400,
                                 detail="An entry changed while you were editing. Reload the page.")
        kept.add(row_id)
        for c, v in values.items():
            old = getattr(row, c)
            if (old or "") != (v or ""):
                setattr(row, c, v)
                log(c, old, v, "EDIT")
                changed = True
    for row_id, row in existing.items():
        if row_id not in kept:
            for c in columns:
                if getattr(row, c):
                    log(c, getattr(row, c), None, "DELETE")
            # A cell grant on a removed entry has nothing left to point at.
            for stale in db.query(FieldEditPermission).filter(
                    FieldEditPermission.candidate_id == current.id,
                    FieldEditPermission.row_table == perm.row_table,
                    FieldEditPermission.row_id == row_id,
                    FieldEditPermission.status == EditPermissionStatus.ACTIVE.value):
                stale.status = EditPermissionStatus.USED.value
                stale.resolved_at = datetime.now(timezone.utc)
            db.delete(row)
            changed = True
    if not changed:
        raise HTTPException(status_code=400, detail=f"Nothing in {title} has changed.")


def _apply_granted_document(db: Session, current: Candidate, perm: FieldEditPermission,
                             upload, reason: str, change_set_id: str) -> list[str]:
    """Replace one uploaded file. Returns the files written by this request,
    so a later failure can clean them up. Nothing is deleted: the replaced
    file stays on disk behind its snapshot."""
    if upload is None or not hasattr(upload, "filename") or not upload.filename:
        raise HTTPException(status_code=400,
                             detail=f"Choose the replacement file for '{perm.field_name}'.")

    ft = FormType(perm.form_type)
    # Write and validate the new file before touching the old rows. The file
    # being replaced is kept on disk and pinned by a snapshot, so HR can still
    # open what the document used to contain.
    original, stored = save_upload(upload, current.id, perm.form_type, perm.field_name)
    written = [upload_path(stored)]
    try:
        verdict = _check_document(current, stored, upload, perm.field_name)
    except HTTPException:
        _remove_files(written)   # refused before any row exists; nothing else to undo
        raise

    replaced = db.query(Document).filter(Document.candidate_id == current.id,
                                          Document.form_type == ft,
                                          Document.field_key == perm.field_name).all()
    old_snapshot = snapshot_document(db, replaced[0]) if replaced else None
    old_name = replaced[0].original_filename if replaced else None
    for doc in replaced:
        db.delete(doc)
    db.flush()

    new_doc = Document(candidate_id=current.id, form_type=ft, field_key=perm.field_name,
                        original_filename=original, stored_filename=stored,
                        content_type=upload.content_type)
    _record_verdict(db, new_doc, verdict)
    db.add(new_doc)
    db.flush()
    new_snapshot = snapshot_document(db, new_doc)

    db.add(FieldEditLog(
        candidate_id=current.id, form_type=edit_access.owning_form(perm.form_type),
        field_name=perm.field_name, old_value=old_name, new_value=original,
        action="EDIT", reason=reason, actor_role="CANDIDATE",
        edited_by_candidate_id=current.id, permission_id=perm.id,
        change_set_id=change_set_id,
        old_file_id=old_snapshot.id if old_snapshot else None,
        new_file_id=new_snapshot.id))
    return written


@router.get("/documents/{document_id}/download")
def download_own_document(document_id: int, db: Session = Depends(get_db),
                           current: Candidate = Depends(get_current_candidate)):
    doc = db.query(Document).filter(Document.id == document_id, Document.candidate_id == current.id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    path = upload_path(doc.stored_filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404,
                             detail=f"'{doc.original_filename}' is no longer stored on the server. "
                                     "Please upload it again.")
    return FileResponse(path, filename=doc.original_filename,
                         media_type=doc.content_type or "application/octet-stream")
