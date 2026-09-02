import json
import os
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.deps import get_current_candidate
from app import edit_access
from app.form_definitions import (
    BGV_FIELDS,
    BGV_FILE_FIELDS,
    BGV_TABLE_SECTIONS,
    CIF_FIELDS,
    CIF_FILE_FIELDS,
    DOC_FIELDS,
    DOC_FILE_FIELDS,
    DOC_REQUIRED_BY_TYPE,
    EDUCATION_COLUMNS,
    EDUCATION_SECTIONS,
    EMPLOYMENT_COLUMNS,
    PROFILE_FIELDS,
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
    CIFDetails,
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
    ReferenceDetail,
)
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
                        forms=candidate.submissions, profile=candidate.profile)


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
    if form_type not in ("CIF", "BGV", "DOCUMENT_COLLECTION"):
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
    if form_type not in ("DOCUMENT_COLLECTION", "BGV"):
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
        for field_key in _DRAFT_FILE_FIELDS[form_type]:
            upload = form.get(field_key)
            if upload is None or not hasattr(upload, "filename") or not upload.filename:
                continue
            original, stored = save_upload(upload, current.id, f"DRAFT_{form_type}", field_key)
            written_paths.append(upload_path(stored))
            for old in db.query(FormDraftDocument).filter(
                    FormDraftDocument.candidate_id == current.id,
                    FormDraftDocument.form_type == ft,
                    FormDraftDocument.field_key == field_key).all():
                replaced_paths.append(upload_path(old.stored_filename))
                db.delete(old)
            db.flush()   # release the (candidate, form, field) unique slot
            db.add(FormDraftDocument(candidate_id=current.id, form_type=ft, field_key=field_key,
                                      original_filename=original, stored_filename=stored,
                                      content_type=upload.content_type))
    except Exception:
        db.rollback()
        _remove_files(written_paths)
        raise

    db.commit()
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
        db.add(Document(candidate_id=candidate_id, form_type=ft, field_key=draft_doc.field_key,
                         original_filename=draft_doc.original_filename,
                         stored_filename=stored, content_type=draft_doc.content_type))
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
    """Copy each present form value onto the matching model column."""
    for name in field_names:
        if name in form:
            setattr(obj, name, form.get(name))


def _rows_from_json(form, key: str, columns: list[str]) -> list[dict]:
    """A repeating table arrives as a JSON string; return sanitized row dicts."""
    try:
        rows = json.loads(form.get(key, "[]"))
    except (TypeError, ValueError):
        return []
    if not isinstance(rows, list):
        return []
    return [{c: str(row.get(c, "") or "") for c in columns} for row in rows if isinstance(row, dict)]


def _remove_files(paths: list[str]) -> None:
    for path in paths:
        try:
            os.remove(path)
        except OSError:
            pass


def _save_files(db, form, current, form_type: str, file_fields: list[str]) -> list[str]:
    """Only fields with a newly chosen file are touched — on an edit, anything
    the candidate leaves blank keeps the file already on record.

    Returns the disk paths of replaced files. They are deleted by the caller
    AFTER the transaction commits: deleting them here would destroy the old
    upload for good if a later field failed validation and rolled the request
    back. If anything raises mid-way, files already written for this request
    are removed so the failed attempt leaves no orphans."""
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
            # Replacing a file: drop the previous rows so we don't accumulate
            # duplicates; the old files on disk go only after commit.
            for old in db.query(Document).filter(
                    Document.candidate_id == current.id,
                    Document.form_type == FormType(form_type),
                    Document.field_key == field_key).all():
                replaced_paths.append(upload_path(old.stored_filename))
                db.delete(old)
            db.add(Document(candidate_id=current.id, form_type=FormType(form_type), field_key=field_key,
                             original_filename=original, stored_filename=stored,
                             content_type=upload.content_type))
    except Exception:
        _remove_files(written_paths)
        raise
    return replaced_paths


@router.post("/forms/cif")
async def submit_cif(request: Request, db: Session = Depends(get_db),
                      current: Candidate = Depends(get_current_candidate)):
    # The CIF is a one-shot form: once submitted it is frozen, and the only
    # way a value changes afterwards is a field HR explicitly opens (see
    # /me/edit-access). That keeps the record HR reviewed from moving under
    # them, and makes every later change auditable.
    submission = db.query(FormSubmission).filter(FormSubmission.candidate_id == current.id,
                                                   FormSubmission.form_type == FormType.CIF).first()
    if submission and submission.status != FormStatus.PENDING:
        raise HTTPException(
            status_code=400,
            detail="Your CIF has already been submitted and can no longer be edited. "
                    "If something needs correcting, ask HR to open that field for you.")
    if current.stage not in (CandidateStage.INVITED, CandidateStage.CIF_SUBMITTED):
        raise HTTPException(status_code=400,
                             detail="Your CIF has already been reviewed and can no longer be edited.")

    form = await request.form()

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

    # Repeating tables -> one DB row per entry (replace any previous rows)
    db.query(EducationDetail).filter(EducationDetail.candidate_id == current.id).delete()
    for form_key, section in EDUCATION_SECTIONS.items():
        for row in _rows_from_json(form, form_key, EDUCATION_COLUMNS):
            db.add(EducationDetail(candidate_id=current.id, section=section, **row))

    db.query(EmploymentDetail).filter(EmploymentDetail.candidate_id == current.id).delete()
    for row in _rows_from_json(form, "employment_details", EMPLOYMENT_COLUMNS):
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
    _remove_files(replaced)   # replaced uploads go only once the new state is safe
    return {"detail": "CIF submitted. HR will review your details shortly."}


@router.post("/forms/{form_type}/submit")
async def submit_followup_form(form_type: str, request: Request, db: Session = Depends(get_db),
                                current: Candidate = Depends(get_current_candidate)):
    if form_type not in ("BGV", "DOCUMENT_COLLECTION"):
        raise HTTPException(status_code=404, detail="Unknown form")
    if current.stage != CandidateStage.APPROVED_FOR_BGV:
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

    form = await request.form()

    if form_type == "BGV":
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
    provided = {k for k in (BGV_FILE_FIELDS if form_type == "BGV" else DOC_FILE_FIELDS)
                 if hasattr(form.get(k), "filename") and form.get(k).filename}
    carried = prefill_agent.carry_documents(db, current.id, form_type, provided)

    submission.status = FormStatus.SUBMITTED
    submission.submitted_at = datetime.now(timezone.utc)
    replaced += _discard_draft(db, current.id, FormType(form_type))
    db.commit()
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
            }
            for p in permissions
        ]
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
    try:
        for change in changes:
            if not isinstance(change, dict):
                raise HTTPException(status_code=400, detail="Malformed change.")
            perm = _active_permission(db, current.id, change.get("permission_id"))
            if perm.field_kind == "DOCUMENT":
                written_paths += _apply_granted_document(
                    db, current, perm, form.get(f"file_{perm.id}"), reason, change_set_id)
            else:
                _apply_granted_field(db, current, perm, change.get("new_value"),
                                      reason, change_set_id)

            perm.status = EditPermissionStatus.USED.value
            perm.resolved_at = datetime.now(timezone.utc)
            applied += 1
        db.commit()
    except Exception:
        db.rollback()
        _remove_files(written_paths)   # a failed attempt leaves no orphans
        raise

    return {"detail": f"{applied} change(s) saved and recorded in the audit trail.",
            "changed": applied}


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
