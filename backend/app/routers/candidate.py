import json
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.deps import get_current_candidate
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
    EducationDetail,
    EmploymentDetail,
    FormStatus,
    FormSubmission,
    FormType,
    ReferenceDetail,
)
from app.schemas import MyStatusOut
from app.utils.file_storage import save_upload, upload_path

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
        {"field_key": d.field_key, "original_filename": d.original_filename}
        for d in db.query(Document).filter(
            Document.candidate_id == current.id,
            Document.form_type == FormType(form_type)).all()
    ]
    return out


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


def _save_files(db, form, current, form_type: str, file_fields: list[str]):
    """Only fields with a newly chosen file are touched — on an edit, anything
    the candidate leaves blank keeps the file already on record."""
    for field_key in file_fields:
        upload = form.get(field_key)
        if upload is None or not hasattr(upload, "filename") or not upload.filename:
            continue
        # Replacing a file: drop the previous one so we don't accumulate
        # duplicate rows or orphaned files on disk.
        for old in db.query(Document).filter(
                Document.candidate_id == current.id,
                Document.form_type == FormType(form_type),
                Document.field_key == field_key).all():
            try:
                os.remove(upload_path(old.stored_filename))
            except OSError:
                pass
            db.delete(old)
        original, stored = save_upload(upload, current.id, form_type, field_key)
        db.add(Document(candidate_id=current.id, form_type=FormType(form_type), field_key=field_key,
                         original_filename=original, stored_filename=stored,
                         content_type=upload.content_type))


@router.post("/forms/cif")
async def submit_cif(request: Request, db: Session = Depends(get_db),
                      current: Candidate = Depends(get_current_candidate)):
    # Editable until HR acts on it: INVITED (first submission) or
    # CIF_SUBMITTED (resubmitting corrections before review).
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

    _save_files(db, form, current, "CIF", CIF_FILE_FIELDS)

    submission = db.query(FormSubmission).filter(FormSubmission.candidate_id == current.id,
                                                   FormSubmission.form_type == FormType.CIF).first()
    if not submission:
        submission = FormSubmission(candidate_id=current.id, form_type=FormType.CIF)
        db.add(submission)
    submission.status = FormStatus.SUBMITTED
    submission.submitted_at = datetime.now(timezone.utc)

    current.stage = CandidateStage.CIF_SUBMITTED   # unchanged on a resubmit
    db.commit()
    return {"detail": "CIF submitted. HR will review your details shortly."}


@router.post("/forms/{form_type}/submit")
async def submit_followup_form(form_type: str, request: Request, db: Session = Depends(get_db),
                                current: Candidate = Depends(get_current_candidate)):
    if form_type not in ("BGV", "DOCUMENT_COLLECTION"):
        raise HTTPException(status_code=404, detail="Unknown form")
    if current.stage != CandidateStage.APPROVED_FOR_BGV:
        raise HTTPException(status_code=400, detail="This form is not unlocked for you yet")

    submission = db.query(FormSubmission).filter(
        FormSubmission.candidate_id == current.id, FormSubmission.form_type == FormType(form_type)
    ).first()
    if not submission or submission.status not in (FormStatus.PENDING, FormStatus.SUBMITTED):
        raise HTTPException(status_code=400,
                             detail="This form has already been reviewed and can no longer be edited.")

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
        _save_files(db, form, current, "BGV", BGV_FILE_FIELDS)
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
        missing = [k for k in required
                    if k not in already
                    and not (hasattr(form.get(k), "filename") and form.get(k).filename)]
        if missing:
            raise HTTPException(status_code=400,
                                 detail="Missing required documents: " + ", ".join(missing))
        _save_files(db, form, current, "DOCUMENT_COLLECTION", DOC_FILE_FIELDS)

    submission.status = FormStatus.SUBMITTED
    submission.submitted_at = datetime.now(timezone.utc)
    db.commit()
    return {"detail": "Form submitted. HR will review it shortly."}


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
