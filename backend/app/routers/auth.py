from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_candidate, get_current_staff, get_current_staff_allow_reset
from app.models import Candidate, HRUser
from app.schemas import (ChangePasswordRequest, InviteTokenLoginRequest, LoginRequest,
                         TokenResponse)
from app.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])

STAFF_PASSWORD_MIN_LENGTH = 8


def _staff_login(payload: LoginRequest, db: Session) -> TokenResponse:
    """One login for every staff role. The response carries the role so the
    login page can send the user to the right portal; the token carries it
    too, but every guard re-reads the role from the database on each call,
    so a role change or deactivation takes effect immediately."""
    user = db.query(HRUser).filter(HRUser.email == payload.email).first()
    if not user or not verify_password(payload.password, user.password_hash) or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    token = create_access_token(subject=str(user.id), role="staff", extra={"staff_role": user.role})
    return TokenResponse(access_token=token, role=user.role, name=user.name,
                          must_reset_password=user.must_reset_password)


@router.post("/staff/login", response_model=TokenResponse)
def staff_login(payload: LoginRequest, db: Session = Depends(get_db)):
    return _staff_login(payload, db)


@router.post("/hr/login", response_model=TokenResponse, deprecated=True)
def hr_login(payload: LoginRequest, db: Session = Depends(get_db)):
    """Alias of /auth/staff/login kept for one release for older clients."""
    return _staff_login(payload, db)


@router.get("/staff/me")
def staff_me(current: HRUser = Depends(get_current_staff_allow_reset)):
    """Works even while a password reset is pending, so the reset page can
    still show who is logged in."""
    return {"id": current.id, "name": current.name, "email": current.email,
            "role": current.role, "manager_id": current.manager_id,
            "must_reset_password": current.must_reset_password}


@router.post("/staff/change-password")
def staff_change_password(payload: ChangePasswordRequest, db: Session = Depends(get_db),
                          current: HRUser = Depends(get_current_staff_allow_reset)):
    """Staff replace their own password. Mandatory on first login with a
    system-generated password; available at any time after that."""
    if not verify_password(payload.current_password, current.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    new = payload.new_password or ""
    if len(new) < STAFF_PASSWORD_MIN_LENGTH:
        raise HTTPException(status_code=400,
                             detail=f"New password must be at least {STAFF_PASSWORD_MIN_LENGTH} characters")
    if new == payload.current_password:
        raise HTTPException(status_code=400, detail="New password must differ from the current one")
    current.password_hash = hash_password(new)
    current.must_reset_password = False
    # The generated password is no longer anyone's to look up.
    current.temp_password_enc = None
    db.commit()
    return {"detail": "Password changed", "role": current.role}


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


@router.post("/staff/logout")
def staff_logout(_: HRUser = Depends(get_current_staff_allow_reset)):
    # JWTs are stateless; the frontend deletes the token client-side.
    # Endpoint exists so logout is an explicit, auditable action.
    return {"detail": "Logged out"}


@router.post("/hr/logout", deprecated=True)
def hr_logout(_: HRUser = Depends(get_current_staff_allow_reset)):
    return {"detail": "Logged out"}


@router.post("/candidate/logout")
def candidate_logout(_: Candidate = Depends(get_current_candidate)):
    return {"detail": "Logged out"}

# Note: candidates cannot change or generate their own password —
# HR issues the credential and it stays as issued.
