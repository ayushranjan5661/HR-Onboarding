from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_candidate, get_current_hr
from app.models import Candidate, HRUser
from app.schemas import InviteTokenLoginRequest, LoginRequest, TokenResponse
from app.security import create_access_token, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/hr/login", response_model=TokenResponse)
def hr_login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(HRUser).filter(HRUser.email == payload.email).first()
    if not user or not verify_password(payload.password, user.password_hash) or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    token = create_access_token(subject=str(user.id), role="hr")
    return TokenResponse(access_token=token, role="hr", name=user.name)


@router.post("/candidate/login", response_model=TokenResponse)
def candidate_login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(Candidate).filter(Candidate.email == payload.email).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    token = create_access_token(subject=str(user.id), role="candidate")
    return TokenResponse(access_token=token, role="candidate", name=user.name,
                          must_reset_password=user.must_reset_password)


@router.post("/candidate/invite-token", response_model=TokenResponse)
def candidate_invite_token_login(payload: InviteTokenLoginRequest, db: Session = Depends(get_db)):
    """Exchange a one-click invite-link token for a normal session token.

    The link token is a credential in its own right, so it is matched in full,
    must not be expired, and grants exactly the same access as a password
    login — nothing more.
    """
    user = db.query(Candidate).filter(Candidate.invite_token == payload.token).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                             detail="This invite link is not valid. Please log in with the email and password HR sent you.")

    expires = user.invite_token_expires_at
    if expires is not None:
        # Stored value may be naive if the DB column lost its timezone.
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < datetime.now(timezone.utc):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                 detail="This invite link has expired. Please ask HR to send you a new one.")

    token = create_access_token(subject=str(user.id), role="candidate")
    return TokenResponse(access_token=token, role="candidate", name=user.name,
                          must_reset_password=user.must_reset_password)


@router.post("/hr/logout")
def hr_logout(_: HRUser = Depends(get_current_hr)):
    # JWTs are stateless; the frontend deletes the token client-side.
    # Endpoint exists so logout is an explicit, auditable action.
    return {"detail": "Logged out"}


@router.post("/candidate/logout")
def candidate_logout(_: Candidate = Depends(get_current_candidate)):
    return {"detail": "Logged out"}

# Note: candidates cannot change or generate their own password —
# HR issues the credential and it stays as issued.
