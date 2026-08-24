import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.database import get_db
from app.deps import get_current_hr
from app.form_definitions import (BGV_FIELDS, BGV_TABLE_SECTIONS, CIF_FIELDS,
                                    DOC_FIELDS, PROFILE_FIELDS)
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
    DocCollectionDetails,
    Document,
    EducationDetail,
    EmploymentDetail,
    FieldEditLog,
    FormStatus,
    FormSubmission,
    FormType,
    HRUser,
    ReferenceDetail,
)
from app.schemas import (
    CandidateDetailOut,
    DocumentOut,
    CandidateListItem,
    DecisionRequest,
    FieldEditRequest,
    InviteCandidateRequest,
    InviteCandidateResponse,
    ReviewSubmissionRequest,
)
from app.security import generate_invite_token, generate_temp_password, hash_password
from app.utils.file_storage import upload_path

router = APIRouter(prefix="/hr", tags=["hr"])


@router.get("/me")
def me(current: HRUser = Depends(get_current_hr)):
    return {"id": current.id, "name": current.name, "email": current.email}


def _candidate_login_url(candidate: Candidate) -> str:
    """One-click link: the token signs the candidate in and drops them into
    whichever form is pending, so the same link works at every stage.
    Falls back to a pre-filled login page for candidates issued before
    one-click links existed."""
    if candidate.invite_token:
        return (f"{settings.PORTAL_BASE_URL}/index.html"
                f"?token={candidate.invite_token}&next=form")
    return (f"{settings.PORTAL_BASE_URL}/index.html"
            f"?role=candidate&email={quote(candidate.email)}&next=form")


def _issue_invite_token(candidate: Candidate) -> None:
    """(Re)issue the candidate's one-click link token. Replacing it instantly
    invalidates any link previously sent out."""
    candidate.invite_token = generate_invite_token()
    candidate.invite_token_expires_at = (
        datetime.now(timezone.utc) + timedelta(days=settings.INVITE_LINK_EXPIRY_DAYS)
    )


def _document_out(doc: Document) -> DocumentOut:
    """Flag documents whose file is no longer on disk so the review checklist
    doesn't claim a document is present when it cannot be opened."""
    return DocumentOut(
        id=doc.id, form_type=doc.form_type.value, field_key=doc.field_key,
        original_filename=doc.original_filename, content_type=doc.content_type,
        file_available=os.path.isfile(upload_path(doc.stored_filename)),
        uploaded_at=doc.uploaded_at,
    )


def _row_dict(obj, columns: list[str], include_id: bool = True) -> dict:
    data = {c: getattr(obj, c) for c in columns}
    if include_id:
        data["id"] = obj.id
    return data


# ---------------------------------------------------------------------------
# Inviting candidates -> creates their portal login and assigns the CIF form
# ---------------------------------------------------------------------------

@router.post("/candidates", response_model=InviteCandidateResponse)
def invite_candidate(payload: InviteCandidateRequest, db: Session = Depends(get_db),
                      current: HRUser = Depends(get_current_hr)):
    if db.query(Candidate).filter(Candidate.email == payload.email).first():
        raise HTTPException(status_code=400, detail="A candidate with this email already exists")

    temp_password = generate_temp_password()
    candidate = Candidate(
        name=payload.name,
        email=payload.email,
        password_hash=hash_password(temp_password),
        temp_password=temp_password,
        must_reset_password=False,
        stage=CandidateStage.INVITED,
        candidate_type=(CandidateType.FRESHER if payload.candidate_type == "FRESHER"
                         else CandidateType.EXPERIENCED),
        created_by_hr_id=current.id,
    )
    _issue_invite_token(candidate)
    db.add(candidate)
    db.flush()

    db.add(CandidateProfile(candidate_id=candidate.id, full_name=payload.name, email=payload.email))
    db.add(FormSubmission(candidate_id=candidate.id, form_type=FormType.CIF, status=FormStatus.PENDING))
    db.commit()

    # Demo note: credentials are handed back to HR to send manually.
    # Wire up SMTP in .env and send here for a live flow.
    return InviteCandidateResponse(candidate_id=candidate.id, email=candidate.email,
                                    temp_password=temp_password,
                                    login_url=_candidate_login_url(candidate))


@router.get("/field-mappings")
def field_mappings(refresh: bool = False, current: HRUser = Depends(get_current_hr)):
    """What the cross-form mapping agent decided, and how. Pass refresh=true to
    re-run it (e.g. after changing a form's fields)."""
    from app.agents import prefill as prefill_agent
    from app.config import settings as cfg
    data = prefill_agent.mappings(refresh=refresh)
    return {
        "ai_enabled": cfg.AI_MAPPING_ENABLED,
        "model": cfg.AZURE_OPENAI_DEPLOYMENT or None,
        "min_confidence": cfg.AI_MAPPING_MIN_CONFIDENCE,
        "mappings": data,
    }


@router.get("/candidates", response_model=list[CandidateListItem])
def list_candidates(db: Session = Depends(get_db), current: HRUser = Depends(get_current_hr)):
    return db.query(Candidate).order_by(Candidate.created_at.desc()).all()


@router.get("/candidates/{candidate_id}", response_model=CandidateDetailOut)
def get_candidate(candidate_id: int, db: Session = Depends(get_db), current: HRUser = Depends(get_current_hr)):
    candidate = (
        db.query(Candidate)
        .options(
            joinedload(Candidate.profile), joinedload(Candidate.submissions),
            joinedload(Candidate.documents), joinedload(Candidate.cif_details),
            joinedload(Candidate.bgv_details), joinedload(Candidate.doc_details),
            joinedload(Candidate.education), joinedload(Candidate.employment),
            joinedload(Candidate.references),
            joinedload(Candidate.bgv_addresses), joinedload(Candidate.bgv_education),
            joinedload(Candidate.bgv_employment), joinedload(Candidate.bgv_references),
            joinedload(Candidate.bgv_gaps),
        )
        .filter(Candidate.id == candidate_id)
        .first()
    )
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    from app.form_definitions import EDUCATION_COLUMNS, EMPLOYMENT_COLUMNS, REFERENCE_COLUMNS

    return CandidateDetailOut(
        id=candidate.id,
        name=candidate.name,
        email=candidate.email,
        stage=candidate.stage.value,
        candidate_type=candidate.candidate_type.value,
        rejection_reason=candidate.rejection_reason,
        temp_password=candidate.temp_password,
        login_url=_candidate_login_url(candidate),
        profile=candidate.profile,
        submissions=candidate.submissions,
        documents=[_document_out(d) for d in candidate.documents],
        cif_details=_row_dict(candidate.cif_details, CIF_FIELDS, include_id=False) if candidate.cif_details else None,
        bgv_details=_row_dict(candidate.bgv_details, BGV_FIELDS, include_id=False) if candidate.bgv_details else None,
        doc_details=_row_dict(candidate.doc_details, DOC_FIELDS, include_id=False) if candidate.doc_details else None,
        education={
            section: [_row_dict(e, EDUCATION_COLUMNS) for e in candidate.education if e.section == section]
            for section in ("UG_PG", "12TH", "10TH")
        },
        employment=[_row_dict(e, EMPLOYMENT_COLUMNS) for e in candidate.employment],
        references=[_row_dict(r, REFERENCE_COLUMNS) for r in candidate.references],
        bgv_tables={
            key: [_row_dict(r, cols) for r in getattr(candidate, attr)]
            for key, (attr, cols) in BGV_TABLE_SECTIONS.items()
        },
    )


@router.post("/candidates/{candidate_id}/regenerate-link")
def regenerate_invite_link(candidate_id: int, db: Session = Depends(get_db),
                            current: HRUser = Depends(get_current_hr)):
    """Issue a fresh one-click link, immediately invalidating the previous one.
    Use this if a link was sent to the wrong person or has expired."""
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    _issue_invite_token(candidate)
    db.add(FieldEditLog(candidate_id=candidate_id, form_type="ACCOUNT", field_name="invite_token",
                         old_value="(previous link revoked)", new_value="(new link issued)",
                         action="EDIT", edited_by_hr_id=current.id))
    db.commit()
    return {"detail": "New link issued. The previous link no longer works.",
            "login_url": _candidate_login_url(candidate)}


@router.delete("/candidates/{candidate_id}")
def delete_candidate(candidate_id: int, db: Session = Depends(get_db),
                      current: HRUser = Depends(get_current_hr)):
    """Delete an invitation/candidate entirely: login, forms, uploaded files."""
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    for doc in candidate.documents:
        try:
            os.remove(upload_path(doc.stored_filename))
        except OSError:
            pass  # file already gone; still remove the DB rows

    db.query(FieldEditLog).filter(FieldEditLog.candidate_id == candidate_id).delete()
    db.delete(candidate)  # cascades to profile, submissions, documents, detail tables
    db.commit()
    return {"detail": "Invitation deleted. The candidate can no longer log in."}


# ---------------------------------------------------------------------------
# Edit / delete submitted values (all stored relationally)
# ---------------------------------------------------------------------------

# form key -> (model, one-to-one attr on Candidate, editable columns)
_DETAIL_TABLES = {
    "PROFILE": (CandidateProfile, "profile", set(PROFILE_FIELDS)),
    "CIF": (CIFDetails, "cif_details", set(CIF_FIELDS)),
    "BGV": (BGVDetails, "bgv_details", set(BGV_FIELDS)),
    "DOCUMENT_COLLECTION": (DocCollectionDetails, "doc_details", set(DOC_FIELDS)),
}


def _get_detail_row(db: Session, form: str, candidate_id: int, field_name: str):
    if form not in _DETAIL_TABLES:
        raise HTTPException(status_code=400, detail="Unknown form")
    model, _, editable = _DETAIL_TABLES[form]
    if field_name not in editable:
        raise HTTPException(status_code=400, detail="Unknown or non-editable field")
    row = db.query(model).filter(model.candidate_id == candidate_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="No submitted data for this form yet")
    return row


@router.patch("/candidates/{candidate_id}/details/{form}/field")
def edit_detail_field(candidate_id: int, form: str, payload: FieldEditRequest,
                       db: Session = Depends(get_db), current: HRUser = Depends(get_current_hr)):
    row = _get_detail_row(db, form, candidate_id, payload.field_name)
    old_value = getattr(row, payload.field_name)
    setattr(row, payload.field_name, payload.new_value)
    db.add(FieldEditLog(candidate_id=candidate_id, form_type=form, field_name=payload.field_name,
                         old_value=old_value, new_value=payload.new_value, action="EDIT",
                         edited_by_hr_id=current.id))
    db.commit()
    return {"detail": "Field updated"}


@router.delete("/candidates/{candidate_id}/details/{form}/field/{field_name}")
def delete_detail_field(candidate_id: int, form: str, field_name: str,
                         db: Session = Depends(get_db), current: HRUser = Depends(get_current_hr)):
    row = _get_detail_row(db, form, candidate_id, field_name)
    old_value = getattr(row, field_name)
    setattr(row, field_name, None)
    db.add(FieldEditLog(candidate_id=candidate_id, form_type=form, field_name=field_name,
                         old_value=old_value, new_value=None, action="DELETE",
                         edited_by_hr_id=current.id))
    db.commit()
    return {"detail": "Field deleted"}


_ROW_TABLES = {
    "education": EducationDetail,
    "employment": EmploymentDetail,
    "references": ReferenceDetail,
    # BGV verification sections
    "bgv_address_history": BGVAddressHistory,
    "bgv_education_checks": BGVEducationCheck,
    "bgv_employment_checks": BGVEmploymentCheck,
    "bgv_reference_checks": BGVReferenceCheck,
    "bgv_gaps": BGVGap,
}


@router.delete("/rows/{table}/{row_id}")
def delete_table_row(table: str, row_id: int, db: Session = Depends(get_db),
                      current: HRUser = Depends(get_current_hr)):
    """Delete one entry from a repeating section (education/employment/references)."""
    model = _ROW_TABLES.get(table)
    if not model:
        raise HTTPException(status_code=400, detail="Unknown table")
    row = db.query(model).filter(model.id == row_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Row not found")
    db.add(FieldEditLog(candidate_id=row.candidate_id, form_type="CIF", field_name=f"{table}_row",
                         old_value=str({c.name: getattr(row, c.name) for c in model.__table__.columns}),
                         new_value=None, action="DELETE", edited_by_hr_id=current.id))
    db.delete(row)
    db.commit()
    return {"detail": "Row deleted"}


# ---------------------------------------------------------------------------
# Approve / reject after the (final) interview round
# ---------------------------------------------------------------------------

@router.post("/candidates/{candidate_id}/approve")
def approve_candidate(candidate_id: int, payload: DecisionRequest, db: Session = Depends(get_db),
                       current: HRUser = Depends(get_current_hr)):
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if candidate.stage != CandidateStage.CIF_SUBMITTED:
        raise HTTPException(status_code=400, detail="Candidate is not awaiting a decision")

    # Mark the CIF submission reviewed + approved.
    cif = db.query(FormSubmission).filter(FormSubmission.candidate_id == candidate_id,
                                           FormSubmission.form_type == FormType.CIF).first()
    if cif:
        cif.status = FormStatus.APPROVED
        cif.reviewed_at = datetime.now(timezone.utc)
        cif.reviewed_by_hr_id = current.id

    # Sequential flow: Document Collection opens now; BGV stays locked until
    # HR approves those documents. Common fields are pulled live from
    # CandidateProfile, so there is nothing to copy — just flip the status.
    for form_type, status in ((FormType.DOCUMENT_COLLECTION, FormStatus.PENDING),
                               (FormType.BGV, FormStatus.LOCKED)):
        existing = db.query(FormSubmission).filter(FormSubmission.candidate_id == candidate_id,
                                                     FormSubmission.form_type == form_type).first()
        if not existing:
            db.add(FormSubmission(candidate_id=candidate_id, form_type=form_type, status=status))
        elif form_type == FormType.DOCUMENT_COLLECTION and existing.status == FormStatus.LOCKED:
            existing.status = FormStatus.PENDING

    candidate.stage = CandidateStage.APPROVED_FOR_BGV
    db.commit()
    return {"detail": "Candidate approved. The Document Collection form is now unlocked. "
                       "BGV opens once you approve their documents."}


@router.post("/candidates/{candidate_id}/reject")
def reject_candidate(candidate_id: int, payload: DecisionRequest, db: Session = Depends(get_db),
                      current: HRUser = Depends(get_current_hr)):
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if candidate.stage == CandidateStage.REJECTED:
        raise HTTPException(status_code=400, detail="Candidate is already rejected")

    candidate.stage = CandidateStage.REJECTED
    candidate.rejection_reason = payload.reason

    for submission in db.query(FormSubmission).filter(FormSubmission.candidate_id == candidate_id).all():
        if submission.status in (FormStatus.PENDING, FormStatus.SUBMITTED, FormStatus.UNDER_REVIEW):
            submission.status = FormStatus.REJECTED
            submission.reviewed_at = datetime.now(timezone.utc)
            submission.reviewed_by_hr_id = current.id

    db.commit()
    return {"detail": "Candidate rejected. The application will not proceed further."}


@router.post("/candidates/{candidate_id}/mark-complete")
def mark_onboarding_complete(candidate_id: int, db: Session = Depends(get_db),
                              current: HRUser = Depends(get_current_hr)):
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if candidate.stage != CandidateStage.APPROVED_FOR_BGV:
        raise HTTPException(status_code=400, detail="Candidate is not in the BGV/Document stage")
    candidate.stage = CandidateStage.ONBOARDING_COMPLETE
    db.commit()
    return {"detail": "Onboarding marked complete"}


# ---------------------------------------------------------------------------
# Review BGV / Document Collection submissions individually
# ---------------------------------------------------------------------------

@router.post("/submissions/{submission_id}/review")
def review_submission(submission_id: int, payload: ReviewSubmissionRequest, db: Session = Depends(get_db),
                       current: HRUser = Depends(get_current_hr)):
    submission = db.query(FormSubmission).filter(FormSubmission.id == submission_id).first()
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")
    try:
        submission.status = FormStatus(payload.decision)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid decision value")
    submission.review_notes = payload.notes
    submission.reviewed_at = datetime.now(timezone.utc)
    submission.reviewed_by_hr_id = current.id

    # Sequential gate: approving Document Collection unlocks BGV.
    unlocked_bgv = False
    if (submission.form_type == FormType.DOCUMENT_COLLECTION
            and submission.status == FormStatus.APPROVED):
        bgv = db.query(FormSubmission).filter(
            FormSubmission.candidate_id == submission.candidate_id,
            FormSubmission.form_type == FormType.BGV).first()
        if not bgv:
            db.add(FormSubmission(candidate_id=submission.candidate_id,
                                   form_type=FormType.BGV, status=FormStatus.PENDING))
            unlocked_bgv = True
        elif bgv.status == FormStatus.LOCKED:
            bgv.status = FormStatus.PENDING
            unlocked_bgv = True

    db.commit()
    if unlocked_bgv:
        return {"detail": "Documents approved. The BGV form is now unlocked for the candidate."}
    return {"detail": "Submission reviewed"}


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

@router.get("/documents/{document_id}/download")
def download_document(document_id: int, db: Session = Depends(get_db), current: HRUser = Depends(get_current_hr)):
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    path = upload_path(doc.stored_filename)
    # A DB row whose file is gone from disk must not become a 500: an
    # unhandled error is returned without CORS headers, so the browser
    # reports only "Failed to fetch" and hides the real cause.
    if not os.path.isfile(path):
        raise HTTPException(
            status_code=404,
            detail=(f"'{doc.original_filename}' is recorded but its file is missing from "
                     "the server's uploads folder. Ask the candidate to upload it again."),
        )
    return FileResponse(path, filename=doc.original_filename,
                         media_type=doc.content_type or "application/octet-stream")
