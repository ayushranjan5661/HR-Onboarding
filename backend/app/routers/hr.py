import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.database import get_db
from app.deps import get_current_hr
from app import edit_access
from app.form_definitions import (BGV_FIELDS, BGV_FILE_FIELDS, BGV_TABLE_SECTIONS,
                                    CIF_FIELDS, CIF_FILE_FIELDS, DOC_FIELDS,
                                    DOC_FILE_FIELDS, PROFILE_FIELDS)
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
    DocumentSnapshot,
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
    HRUser,
    ReferenceDetail,
)
from app.schemas import (
    AuditEntryOut,
    AuditFileOut,
    CandidateDetailOut,
    ChangeSetRequest,
    DocumentOut,
    CandidateListItem,
    DecisionRequest,
    EditPermissionOut,
    GrantEditAccessRequest,
    InviteCandidateRequest,
    InviteCandidateResponse,
    ReviewSubmissionRequest,
    RevokeEditAccessRequest,
)
from app.security import (decrypt_password, encrypt_password, generate_invite_token,
                            generate_temp_password, hash_password)
from app.utils.file_storage import save_upload, snapshot_document, upload_path

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


def _clean_reason(reason: str | None) -> str | None:
    """Blank-only reasons are noise in an audit trail — store nothing instead."""
    reason = (reason or "").strip()
    return reason or None


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
        temp_password_enc=encrypt_password(temp_password),
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
    try:
        db.commit()
    except IntegrityError:
        # Two invites for the same email racing past the check above: the
        # unique constraint wins — report it as the duplicate it is, not a 500.
        db.rollback()
        raise HTTPException(status_code=400, detail="A candidate with this email already exists")

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


@router.get("/candidates/{candidate_id}/insights")
def candidate_insights(candidate_id: int, db: Session = Depends(get_db),
                        current: HRUser = Depends(get_current_hr)):
    """AI summary + anomaly flags for one candidate's CIF submission.
    Computed live on each call (candidate data changes; unlike the field
    mapper, there is nothing static here worth caching)."""
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if not candidate.cif_details:
        raise HTTPException(status_code=400, detail="Candidate has not submitted the CIF yet")

    from app.agents import insights as insights_agent
    return insights_agent.generate(db, candidate_id)


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
        temp_password=decrypt_password(candidate.temp_password_enc),
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


@router.post("/candidates/{candidate_id}/reset-password")
def reset_candidate_password(candidate_id: int, db: Session = Depends(get_db),
                              current: HRUser = Depends(get_current_hr)):
    """Issue a fresh temporary password for a candidate who lost theirs.
    Only the bcrypt hash is stored, so the new password appears exactly once —
    in this response. The previous password stops working immediately."""
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    temp_password = generate_temp_password()
    candidate.password_hash = hash_password(temp_password)
    candidate.temp_password_enc = encrypt_password(temp_password)
    db.add(FieldEditLog(candidate_id=candidate_id, form_type="ACCOUNT", field_name="password",
                         old_value="(previous password revoked)", new_value="(new password issued)",
                         action="EDIT", edited_by_hr_id=current.id))
    db.commit()
    return {"detail": "New password issued. The previous one no longer works.",
            "temp_password": temp_password}


@router.delete("/candidates/{candidate_id}")
def delete_candidate(candidate_id: int, db: Session = Depends(get_db),
                      current: HRUser = Depends(get_current_hr)):
    """Delete an invitation/candidate entirely: login, forms, uploaded files."""
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")

    # Snapshots keep replaced files alive for the audit trail, so they have to
    # be swept up here too — the live documents alone no longer cover them.
    snapshots = db.query(DocumentSnapshot).filter(
        DocumentSnapshot.candidate_id == candidate_id).all()
    for stored in {d.stored_filename for d in candidate.documents} | {
            s.stored_filename for s in snapshots}:
        try:
            os.remove(upload_path(stored))
        except OSError:
            pass  # file already gone; still remove the DB rows

    # Tables that reference the candidate without a cascading relationship —
    # the log first, since its rows point at the permissions.
    db.query(FieldEditLog).filter(FieldEditLog.candidate_id == candidate_id).delete()
    db.query(DocumentSnapshot).filter(
        DocumentSnapshot.candidate_id == candidate_id).delete()
    db.query(FieldEditPermission).filter(
        FieldEditPermission.candidate_id == candidate_id).delete()
    db.query(FormDraftDocument).filter(FormDraftDocument.candidate_id == candidate_id).delete()
    db.query(FormDraft).filter(FormDraft.candidate_id == candidate_id).delete()
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


@router.post("/candidates/{candidate_id}/changes")
def apply_changes(candidate_id: int, payload: ChangeSetRequest, db: Session = Depends(get_db),
                   current: HRUser = Depends(get_current_hr)):
    """Apply everything one HR save touched — flat fields and repeating-section
    cells alike — as a single audited action.

    All of it lands in one transaction under one change-set id, so a partial
    failure changes nothing and the audit trail shows the save as one entry
    rather than a run of unrelated ones."""
    edit_access.require_candidate(db, candidate_id)
    if not payload.fields and not payload.rows:
        raise HTTPException(status_code=400, detail="Nothing to change")

    reason = _clean_reason(payload.reason)
    change_set_id = uuid4().hex
    applied = 0

    def log(form_type: str, field_name: str, old_value, new_value, action="EDIT"):
        db.add(FieldEditLog(
            candidate_id=candidate_id, form_type=form_type, field_name=field_name,
            old_value=old_value, new_value=new_value, action=action, reason=reason,
            actor_role="HR", edited_by_hr_id=current.id, change_set_id=change_set_id))

    for edit in payload.fields:
        row = _get_detail_row(db, edit.form, candidate_id, edit.field_name)
        old_value = getattr(row, edit.field_name)
        if (old_value or "") == (edit.new_value or ""):
            continue   # unchanged: nothing to record
        setattr(row, edit.field_name, edit.new_value)
        log(edit.form, edit.field_name, old_value, edit.new_value,
            "DELETE" if edit.new_value in (None, "") else "EDIT")
        applied += 1

    for row_edit in payload.rows:
        model = _ROW_TABLES.get(row_edit.table)
        if not model:
            raise HTTPException(status_code=400, detail="Unknown table")
        # Scoped to this candidate: a row id alone must not reach another's data.
        row = db.query(model).filter(model.id == row_edit.row_id,
                                      model.candidate_id == candidate_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="Row not found")
        editable = {c.name for c in model.__table__.columns} - {"id", "candidate_id", "section"}
        unknown = set(row_edit.values) - editable
        if unknown:
            raise HTTPException(status_code=400,
                                 detail=f"Unknown or non-editable field(s): {', '.join(sorted(unknown))}")
        for name, value in row_edit.values.items():
            old_value = getattr(row, name)
            if (old_value or "") == (value or ""):
                continue
            setattr(row, name, value)
            log(_row_form_type(row_edit.table), f"{row_edit.table}.{name}", old_value, value)
            applied += 1

    db.commit()
    return {"detail": f"{applied} change(s) saved", "changed": applied,
            "change_set_id": change_set_id}


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


def _row_form_type(table: str) -> str:
    return "BGV" if table.startswith("bgv_") else "CIF"


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
# Field-level edit access on a submitted form
#
# The CIF is read-only to the candidate once submitted. When something needs
# correcting later, HR opens the individual field rather than the whole form;
# the candidate's change then has to carry a reason and lands in the audit log.
# ---------------------------------------------------------------------------

def _permission_out(db: Session, perm: FieldEditPermission,
                     hr_names: dict[int, str]) -> EditPermissionOut:
    return EditPermissionOut(
        id=perm.id, form=edit_access.owning_form(perm.form_type),
        form_type=perm.form_type, field_kind=perm.field_kind,
        field_name=perm.field_name, row_table=perm.row_table, row_id=perm.row_id,
        row_label=edit_access.row_label_for(db, perm.candidate_id, perm),
        status=perm.status, hr_note=perm.hr_note,
        granted_by=hr_names.get(perm.granted_by_hr_id),
        granted_at=perm.granted_at, resolved_at=perm.resolved_at,
        current_value=_as_text(edit_access.current_value(db, perm.candidate_id, perm)),
    )


def _as_text(value) -> str | None:
    return None if value is None else str(value)


def _hr_names(db: Session) -> dict[int, str]:
    return {u.id: u.name for u in db.query(HRUser).all()}


@router.get("/candidates/{candidate_id}/edit-access")
def list_edit_access(candidate_id: int, db: Session = Depends(get_db),
                      current: HRUser = Depends(get_current_hr)):
    """What HR may open for this candidate, and what is already open.

    The grantable set covers every column of every form: the one-row detail
    fields, the uploads, and one entry at a time of each repeating section."""
    edit_access.require_candidate(db, candidate_id)
    hr_names = _hr_names(db)
    permissions = (db.query(FieldEditPermission)
                     .filter(FieldEditPermission.candidate_id == candidate_id)
                     .order_by(FieldEditPermission.granted_at.desc()).all())
    return {
        "submitted": edit_access.submitted_forms(db, candidate_id),
        "grantable": edit_access.grantable_items(db, candidate_id),
        "permissions": [_permission_out(db, p, hr_names) for p in permissions],
    }


@router.post("/candidates/{candidate_id}/edit-access")
def grant_edit_access(candidate_id: int, payload: GrantEditAccessRequest,
                       db: Session = Depends(get_db), current: HRUser = Depends(get_current_hr)):
    """Open one or more individual values for the candidate to correct."""
    edit_access.require_candidate(db, candidate_id)
    if not payload.grants:
        raise HTTPException(status_code=400, detail="Select at least one field to open")

    submitted = edit_access.submitted_forms(db, candidate_id)
    note = _clean_reason(payload.hr_note)
    already_open = {(p.form_type, p.field_kind, p.field_name, p.row_table, p.row_id)
                     for p in edit_access.active_permissions(db, candidate_id)}
    opened = 0
    for grant in payload.grants:
        edit_access.validate_target(db, candidate_id, grant.form_type, grant.field_kind,
                                     grant.field_name, grant.row_table, grant.row_id)
        form = edit_access.owning_form(grant.form_type)
        if not submitted.get(form):
            raise HTTPException(
                status_code=400,
                detail=f"The candidate has not submitted their "
                        f"{edit_access.form_title(form)} form yet — "
                        "it is still theirs to fill in.")
        key = (grant.form_type, grant.field_kind, grant.field_name,
                grant.row_table, grant.row_id)
        if key in already_open:
            continue   # re-granting an open value would just duplicate the row
        db.add(FieldEditPermission(
            candidate_id=candidate_id, form_type=grant.form_type,
            field_kind=grant.field_kind, field_name=grant.field_name,
            row_table=grant.row_table, row_id=grant.row_id,
            status=EditPermissionStatus.ACTIVE.value, hr_note=note,
            granted_by_hr_id=current.id))
        already_open.add(key)
        opened += 1
    db.commit()
    if not opened:
        return {"detail": "Those fields are already open for the candidate.", "granted": 0}
    return {"detail": f"{opened} field(s) opened for the candidate to edit.", "granted": opened}


@router.post("/candidates/{candidate_id}/edit-access/revoke")
def revoke_edit_access(candidate_id: int, payload: RevokeEditAccessRequest,
                        db: Session = Depends(get_db),
                        current: HRUser = Depends(get_current_hr)):
    """Withdraw access before the candidate has used it — one grant, several,
    or (with no `permission_ids`) everything still open.

    Taking access away is a decision worth explaining, so the reason goes to
    the audit log; the candidate may already have seen the field open, and this
    is what says why it closed again. One call is one audited action, so
    closing five fields at once reads as one entry, not five."""
    edit_access.require_candidate(db, candidate_id)
    active = edit_access.active_permissions(db, candidate_id)

    if payload.permission_ids:
        wanted = set(payload.permission_ids)
        perms = [p for p in active if p.id in wanted]
        if len(perms) != len(wanted):
            raise HTTPException(
                status_code=400,
                detail="Some of those are already used or revoked. Reload the page.")
    else:
        perms = active
    if not perms:
        raise HTTPException(status_code=400,
                             detail="Nothing is open for this candidate to edit.")

    reason = _clean_reason(payload.reason)
    change_set_id = uuid4().hex
    now = datetime.now(timezone.utc)
    for perm in perms:
        perm.status = EditPermissionStatus.REVOKED.value
        perm.resolved_at = now
        # No value changed — this records the access event itself, so the trail
        # explains a field that was opened and then closed without an edit.
        db.add(FieldEditLog(
            candidate_id=perm.candidate_id, form_type=edit_access.owning_form(perm.form_type),
            field_name=edit_access.audit_field_name(perm), old_value=None, new_value=None,
            action="REVOKE", reason=reason, actor_role="HR", edited_by_hr_id=current.id,
            permission_id=perm.id, change_set_id=change_set_id))
    db.commit()
    return {"detail": f"Edit access revoked for {len(perms)} field(s). "
                       "The candidate can no longer change them.",
            "revoked": len(perms)}


@router.get("/candidates/{candidate_id}/audit", response_model=list[AuditEntryOut])
def candidate_audit_trail(candidate_id: int, db: Session = Depends(get_db),
                           current: HRUser = Depends(get_current_hr)):
    """Every change made to this candidate's submitted data, newest first."""
    candidate = edit_access.require_candidate(db, candidate_id)
    hr_names = _hr_names(db)
    entries = (db.query(FieldEditLog)
                 .filter(FieldEditLog.candidate_id == candidate_id)
                 .order_by(FieldEditLog.edited_at.desc(), FieldEditLog.id.desc()).all())
    # One lookup for every file either side of every entry references.
    file_ids = {i for e in entries for i in (e.old_file_id, e.new_file_id) if i}
    snapshots = {s.id: s for s in db.query(DocumentSnapshot).filter(
        DocumentSnapshot.id.in_(file_ids)).all()} if file_ids else {}

    def as_file(snapshot_id):
        snap = snapshots.get(snapshot_id)
        if not snap:
            return None
        return AuditFileOut(
            id=snap.id, filename=snap.original_filename, content_type=snap.content_type,
            available=os.path.isfile(upload_path(snap.stored_filename)))

    out = []
    for e in entries:
        # Rows written before the log grew an actor column were all HR edits.
        role = e.actor_role or "HR"
        actor = (candidate.name if role == "CANDIDATE"
                  else hr_names.get(e.edited_by_hr_id))
        out.append(AuditEntryOut(
            id=e.id, form_type=e.form_type, field_name=e.field_name,
            old_value=e.old_value, new_value=e.new_value, action=e.action,
            reason=e.reason, actor_role=role, actor_name=actor, edited_at=e.edited_at,
            change_set_id=e.change_set_id,
            old_file=as_file(e.old_file_id), new_file=as_file(e.new_file_id)))
    return out


@router.get("/document-snapshots/{snapshot_id}/download")
def download_snapshot(snapshot_id: int, db: Session = Depends(get_db),
                       current: HRUser = Depends(get_current_hr)):
    """Open either side of a document change straight from the audit trail —
    the file that was replaced as well as the one that replaced it."""
    snap = db.query(DocumentSnapshot).filter(DocumentSnapshot.id == snapshot_id).first()
    if not snap:
        raise HTTPException(status_code=404, detail="That version is not on record")
    path = upload_path(snap.stored_filename)
    if not os.path.isfile(path):
        raise HTTPException(
            status_code=404,
            detail=f"'{snap.original_filename}' is recorded but its file is missing from "
                    "the server's uploads folder.")
    return FileResponse(path, filename=snap.original_filename,
                         media_type=snap.content_type or "application/octet-stream")


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
    # "Complete" must mean both follow-up forms were actually reviewed and
    # approved — not merely that the stage was reached.
    approved = {s.form_type for s in db.query(FormSubmission).filter(
        FormSubmission.candidate_id == candidate_id,
        FormSubmission.status == FormStatus.APPROVED).all()}
    pending = [ft.value for ft in (FormType.DOCUMENT_COLLECTION, FormType.BGV)
                if ft not in approved]
    if pending:
        raise HTTPException(status_code=400,
                             detail="Cannot mark complete: these forms are not approved yet: "
                                    + ", ".join(pending))
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

    # A review is a decision, not an arbitrary status write: LOCKED/PENDING/
    # SUBMITTED must never be reachable through this endpoint.
    _ALLOWED_DECISIONS = (FormStatus.APPROVED, FormStatus.REJECTED, FormStatus.UNDER_REVIEW)
    try:
        decision = FormStatus(payload.decision)
    except ValueError:
        decision = None
    if decision not in _ALLOWED_DECISIONS:
        raise HTTPException(status_code=400,
                             detail="Decision must be APPROVED, REJECTED, or UNDER_REVIEW")

    # The CIF gate lives at /candidates/{id}/approve|reject, which also moves
    # the candidate's stage; approving a CIF here would leave the stage behind
    # and let the candidate overwrite an approved form.
    if submission.form_type == FormType.CIF:
        raise HTTPException(status_code=400,
                             detail="CIF is reviewed via the candidate Approve/Reject decision, "
                                    "not per-submission review")

    # Nothing to review until the candidate has actually submitted.
    if submission.status in (FormStatus.LOCKED, FormStatus.PENDING):
        raise HTTPException(status_code=400,
                             detail="The candidate has not submitted this form yet")

    submission.status = decision
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


# Which uploads each form expects — HR may only replace a file the form
# actually asks for, so an arbitrary field_key can't be injected.
_FORM_FILE_FIELDS = {
    FormType.CIF: set(CIF_FILE_FIELDS),
    FormType.BGV: set(BGV_FILE_FIELDS),
    FormType.DOCUMENT_COLLECTION: set(DOC_FILE_FIELDS),
}


@router.post("/candidates/{candidate_id}/documents/{form}/{field_key}")
def upload_document(candidate_id: int, form: str, field_key: str,
                     file: UploadFile = File(...), reason: str | None = Form(None),
                     db: Session = Depends(get_db),
                     current: HRUser = Depends(get_current_hr)):
    """Attach (or replace) one uploaded file on a candidate's form.

    Mirrors the candidate-side upload path: the new file is written and
    validated first, the old row is dropped, and the old file is removed from
    disk only after the transaction commits — a mid-request failure must not
    destroy the document already on record.
    """
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    try:
        form_type = FormType(form)
    except ValueError:
        raise HTTPException(status_code=400, detail="Unknown form")
    if field_key not in _FORM_FILE_FIELDS.get(form_type, set()):
        raise HTTPException(status_code=400, detail="This form does not collect that document")

    original, stored = save_upload(file, candidate_id, form, field_key)
    try:
        replaced = db.query(Document).filter(Document.candidate_id == candidate_id,
                                              Document.form_type == form_type,
                                              Document.field_key == field_key).all()
        # The file being replaced is kept on disk and pinned by a snapshot, so
        # the audit trail can still open what the document used to contain.
        old_snapshot = snapshot_document(db, replaced[0]) if replaced else None
        old_name = replaced[0].original_filename if replaced else None
        for doc in replaced:
            db.delete(doc)
        db.flush()

        new_doc = Document(candidate_id=candidate_id, form_type=form_type, field_key=field_key,
                            original_filename=original, stored_filename=stored,
                            content_type=file.content_type)
        db.add(new_doc)
        db.flush()
        new_snapshot = snapshot_document(db, new_doc)

        db.add(FieldEditLog(candidate_id=candidate_id, form_type=form, field_name=field_key,
                             old_value=old_name, new_value=original, action="EDIT",
                             reason=_clean_reason(reason), actor_role="HR",
                             edited_by_hr_id=current.id,
                             old_file_id=old_snapshot.id if old_snapshot else None,
                             new_file_id=new_snapshot.id))
        db.commit()
    except Exception:
        try:
            os.remove(upload_path(stored))
        except OSError:
            pass
        raise
    return {"detail": "Document uploaded"}


@router.delete("/documents/{document_id}")
def delete_document(document_id: int, reason: str | None = None,
                     db: Session = Depends(get_db),
                     current: HRUser = Depends(get_current_hr)):
    """Remove one uploaded file. The form then shows it as not submitted."""
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    # Pinned before deleting the row: the file stays on disk so the trail can
    # still show what was removed.
    snapshot = snapshot_document(db, doc)
    db.add(FieldEditLog(candidate_id=doc.candidate_id, form_type=doc.form_type.value,
                         field_name=doc.field_key, old_value=doc.original_filename,
                         new_value=None, action="DELETE", reason=_clean_reason(reason),
                         actor_role="HR", edited_by_hr_id=current.id,
                         old_file_id=snapshot.id))
    db.delete(doc)
    db.commit()
    return {"detail": "Document deleted"}
