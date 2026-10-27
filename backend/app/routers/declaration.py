"""Public Declaration forms. No login: the signed token in the link HR sent is
the only credential (see app/declaration_links.py)."""
import json
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app import declaration_links, edit_access
from app.agents import prefill as prefill_agent
from app.database import get_db
from app.models import Candidate, Document, FormStatus, FormSubmission, FormType
from app.routers.candidate import _apply_granted_edits, _submit_followup_form, my_edit_access
from app.utils.file_storage import upload_path

_INVALID = "This Declaration link is invalid or no longer active. Please contact the LevelShift HR team."
_EXPIRED = "This Declaration link has expired. Please ask the LevelShift HR team for a new one."


def _make_router(prefix: str, links: declaration_links.FormLink, form_type: FormType,
                 signature_field: str) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["declaration"])

    def _resolve(db: Session, token: str) -> Candidate:
        parsed = declaration_links.parse_token(token)
        if parsed:
            candidate_id, sig = parsed
            candidate = db.get(Candidate, candidate_id)
            if candidate and links.verify(candidate, sig):
                if links.expired(candidate):
                    raise HTTPException(status_code=404, detail=_EXPIRED)
                return candidate
        raise HTTPException(status_code=404, detail=_INVALID)

    def _reused_signature(db: Session, candidate: Candidate):
        """The CIF signature this form will carry over, if any."""
        return next((d for d in prefill_agent.build_prefill(db, candidate.id, form_type.value)["documents"]
                     if d["target_field"] == signature_field and d.get("available", True)), None)

    @router.get("/form")
    def get_form(token: str, db: Session = Depends(get_db)):
        """What the page needs before showing the form."""
        candidate = _resolve(db, token)
        sub = db.query(FormSubmission).filter(
            FormSubmission.candidate_id == candidate.id,
            FormSubmission.form_type == form_type).first()
        status = sub.status.value if sub else FormStatus.LOCKED.value
        signature = _reused_signature(db, candidate)
        return {
            "candidate_name": (candidate.profile.full_name if candidate.profile else None) or candidate.name,
            "status": status,
            "reused_signature": signature["original_filename"] if signature else None,
            # Values HR reopened on the submitted form; the link then leads to them.
            "corrections": len(_open_edits(db, candidate)),
        }

    def _open_edits(db: Session, candidate: Candidate) -> list[dict]:
        """HR-opened values on this form only — the link reaches nothing else."""
        return [p for p in my_edit_access(db, candidate)["permissions"]
                if p["form"] == form_type.value]

    @router.get("/edit-access")
    def get_edit_access(token: str, db: Session = Depends(get_db)):
        candidate = _resolve(db, token)
        return {"permissions": _open_edits(db, candidate)}

    @router.post("/edit-access/apply")
    async def apply_edits(request: Request, db: Session = Depends(get_db)):
        form = await request.form()
        candidate = _resolve(db, str(form.get("token") or ""))
        allowed = {p.id for p in edit_access.active_permissions(db, candidate.id)
                   if edit_access.owning_form(p.form_type) == form_type.value}
        try:
            changes = json.loads(form.get("changes") or "[]")
        except (TypeError, ValueError):
            changes = None
        if not isinstance(changes, list) or any(
                not isinstance(c, dict) or c.get("permission_id") not in allowed for c in changes):
            raise HTTPException(status_code=400, detail="This link can only correct its own form.")
        return await run_in_threadpool(_apply_granted_edits, form, db, candidate)

    @router.get("/signature")
    def view_signature(token: str, db: Session = Depends(get_db)):
        """The carried-over signature, so the candidate can check it before
        submitting. Only the one file the form would reuse is reachable."""
        candidate = _resolve(db, token)
        signature = _reused_signature(db, candidate)
        doc = db.get(Document, signature["source_document_id"]) if signature else None
        if not doc or doc.candidate_id != candidate.id:
            raise HTTPException(status_code=404, detail="No signature on file.")
        path = upload_path(doc.stored_filename)
        if not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="No signature on file.")
        return FileResponse(path, filename=doc.original_filename, content_disposition_type="inline",
                            media_type=doc.content_type or "application/octet-stream")

    @router.post("/submit")
    async def submit(request: Request, db: Session = Depends(get_db)):
        form = await request.form()
        candidate = _resolve(db, str(form.get("token") or ""))
        return await run_in_threadpool(_submit_followup_form, form_type.value, form, db, candidate)

    return router


router = _make_router("/declaration", declaration_links.EDUCATION, FormType.DECLARATION,
                      "declaration_signature")
# Relieving and experience letters still owed (Experienced candidates).
experience_router = _make_router("/experience-declaration", declaration_links.EXPERIENCE,
                                 FormType.EXPERIENCE_DECLARATION, "exp_declaration_signature")
