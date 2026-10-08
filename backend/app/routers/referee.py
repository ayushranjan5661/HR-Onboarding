"""Public referee feedback form. No login: the signed token in the link the
reference received is the only credential (see app/referee_links.py)."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import referee_links
from app.database import get_db
from app.models import Candidate, RefereeFeedback

router = APIRouter(prefix="/referee", tags=["referee"])

_INVALID = "This feedback link is invalid or no longer active. Please contact the LevelShift HR team."


def _resolve(db: Session, token: str) -> tuple[Candidate, int]:
    parsed = referee_links.parse_token(token)
    if parsed:
        candidate_id, ref_index, sig = parsed
        candidate = db.get(Candidate, candidate_id)
        if candidate and referee_links.verify(candidate, ref_index, sig):
            return candidate, ref_index
    raise HTTPException(status_code=404, detail=_INVALID)


def _existing(db: Session, candidate_id: int, ref_index: int) -> Optional[RefereeFeedback]:
    return (db.query(RefereeFeedback)
            .filter_by(candidate_id=candidate_id, ref_index=ref_index).first())


@router.get("/form")
def get_form(token: str, db: Session = Depends(get_db)):
    """What the page shows before the questions: who the feedback is about."""
    candidate, n = _resolve(db, token)
    d = candidate.ref_check_details
    return {
        "candidate_name": (d.candidate_name or candidate.name),
        "position_applied_for": d.position_applied_for or "",
        "referee_name": getattr(d, f"ref{n}_name") or "",
        "referee_email": getattr(d, f"ref{n}_email") or "",
        "submitted": _existing(db, candidate.id, n) is not None,
    }


Rating = Field(..., ge=1, le=4)


class FeedbackIn(BaseModel):
    token: str
    relationship_with_candidate: str = Field(..., min_length=1, max_length=2000)
    candidate_strengths: str = Field(..., min_length=1, max_length=3000)
    candidate_development_areas: str = Field(..., min_length=1, max_length=3000)
    rating_reliability: int = Rating
    rating_punctuality: int = Rating
    rating_attendance: int = Rating
    rating_professionalism: int = Rating
    additional_comments: str = Field("", max_length=3000)


@router.post("/feedback")
def submit_feedback(payload: FeedbackIn, db: Session = Depends(get_db)):
    candidate, n = _resolve(db, payload.token)
    if _existing(db, candidate.id, n):
        raise HTTPException(status_code=409, detail="Feedback has already been submitted for this link. Thank you!")
    d = candidate.ref_check_details
    db.add(RefereeFeedback(
        candidate_id=candidate.id, ref_index=n,
        referee_name=getattr(d, f"ref{n}_name"), referee_email=getattr(d, f"ref{n}_email"),
        **{k: (v.strip() if isinstance(v, str) else v)
           for k, v in payload.model_dump(exclude={"token"}).items()},
    ))
    try:
        db.commit()
    except IntegrityError:   # double-click: the first submit already landed
        db.rollback()
        raise HTTPException(status_code=409, detail="Feedback has already been submitted for this link. Thank you!")
    return {"ok": True}
