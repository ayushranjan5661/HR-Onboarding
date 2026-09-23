from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Candidate, HRUser, StaffRole
from app.security import decode_access_token

staff_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/staff/login", auto_error=False)
candidate_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/candidate/login", auto_error=False)

# Kept for anything still importing the old name.
hr_oauth2_scheme = staff_oauth2_scheme

# Token role claims that identify a staff session. "hr" is what tokens issued
# before the hierarchy existed carry; they stay valid until they expire.
_STAFF_TOKEN_ROLES = {"staff", "hr"}


def _unauthorized(detail: str = "Not authenticated"):
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail,
                          headers={"WWW-Authenticate": "Bearer"})


def _load_staff(token: str | None, db: Session) -> HRUser:
    if not token:
        raise _unauthorized()
    payload = decode_access_token(token)
    if not payload or payload.get("role") not in _STAFF_TOKEN_ROLES:
        raise _unauthorized("Invalid or expired staff session")
    user = db.query(HRUser).filter(HRUser.id == int(payload["sub"]), HRUser.is_active.is_(True)).first()
    if not user:
        raise _unauthorized("Staff account not found or deactivated")
    return user


def get_current_staff_allow_reset(token: str = Depends(staff_oauth2_scheme),
                                  db: Session = Depends(get_db)) -> HRUser:
    """Any active staff user, even one who still has to change their first
    password. Only the change-password and who-am-I endpoints use this."""
    return _load_staff(token, db)


def get_current_staff(token: str = Depends(staff_oauth2_scheme), db: Session = Depends(get_db)) -> HRUser:
    """Any active staff user (Super Admin, Manager or HR Executive). A user
    flagged to reset their password can do nothing else until they have."""
    user = _load_staff(token, db)
    if user.must_reset_password:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                             detail="PASSWORD_RESET_REQUIRED")
    return user


# Old name, same guard: every staff role may use the HR endpoints; what each
# one sees is decided per query by app.scope.
get_current_hr = get_current_staff


def require_roles(*roles: StaffRole):
    """Dependency factory: the caller must hold one of these roles."""
    allowed = {r.value for r in roles}

    def _guard(current: HRUser = Depends(get_current_staff)) -> HRUser:
        if current.role not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                 detail="You do not have permission to do this")
        return current
    return _guard


get_current_super_admin = require_roles(StaffRole.SUPER_ADMIN)
get_current_manager_or_above = require_roles(StaffRole.SUPER_ADMIN, StaffRole.MANAGER)


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
