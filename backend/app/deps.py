from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Candidate, HRUser
from app.security import decode_access_token

hr_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/hr/login", auto_error=False)
candidate_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/candidate/login", auto_error=False)


def _unauthorized(detail: str = "Not authenticated"):
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail,
                          headers={"WWW-Authenticate": "Bearer"})


def get_current_hr(token: str = Depends(hr_oauth2_scheme), db: Session = Depends(get_db)) -> HRUser:
    if not token:
        raise _unauthorized()
    payload = decode_access_token(token)
    if not payload or payload.get("role") != "hr":
        raise _unauthorized("Invalid or expired HR session")
    user = db.query(HRUser).filter(HRUser.id == int(payload["sub"]), HRUser.is_active.is_(True)).first()
    if not user:
        raise _unauthorized("HR account not found")
    return user


def get_current_candidate(token: str = Depends(candidate_oauth2_scheme), db: Session = Depends(get_db)) -> Candidate:
    if not token:
        raise _unauthorized()
    payload = decode_access_token(token)
    if not payload or payload.get("role") != "candidate":
        raise _unauthorized("Invalid or expired candidate session")
    user = db.query(Candidate).filter(Candidate.id == int(payload["sub"])).first()
    if not user:
        raise _unauthorized("Candidate account not found")
    # Rejected candidates can still log in to see their status; the stage
    # checks on each form-submit endpoint are what actually stop progression.
    return user
