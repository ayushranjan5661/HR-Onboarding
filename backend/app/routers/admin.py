"""Master Admin and Super Admin: the whole staff hierarchy and the global
audit trail. Candidate lists and actions come from the /hr endpoints, which
already show both admins everything; candidate reassignment across teams goes
through /manager/candidates/{id}/assign, which is unrestricted for them,
except that the Super Admin may not assign a candidate to the Master Admin.

The Master Admin (developer account, seeded from .env) additionally sees the
password in force on every account, and is the only one who may create,
deactivate, delete or reset a Super Admin. A Super Admin never sees the
Master Admin's row at all."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import scope, staff_service
from app.database import get_db
from app.deps import get_current_admin
from app.models import HRUser, StaffAuditLog, StaffRole
from app.schemas import (CreatedStaffResponse, CreateStaffRequest, StaffAuditOut, StaffOut,
                         UpdateStaffRequest)

router = APIRouter(prefix="/admin", tags=["admin"])


def _target(db: Session, staff_id: int, current: HRUser) -> HRUser:
    """The staff account with this id, if the caller may manage it (or it is
    the caller's own row); 404 otherwise, so a Super Admin cannot even
    confirm the Master Admin's id."""
    user = db.query(HRUser).filter(HRUser.id == staff_id).first()
    if not user or not (user.id == current.id or scope.staff_in_scope(db, user, current)):
        raise HTTPException(status_code=404, detail="Staff account not found")
    return user


def _out(db: Session, users: list[HRUser], current: HRUser) -> list[StaffOut]:
    return staff_service.staff_out(db, users, include_temp_password=True,
                                   include_current_password=scope.is_master_admin(current))


@router.get("/staff", response_model=list[StaffOut])
def list_staff(db: Session = Depends(get_db), current: HRUser = Depends(get_current_admin)):
    """Everyone the caller may see, themselves included, with owned-candidate
    and team counts. One-time passwords are shown until changed; the Master
    Admin also sees the password currently in force on every account."""
    q = db.query(HRUser)
    if not scope.is_master_admin(current):
        q = q.filter(HRUser.role != StaffRole.MASTER_ADMIN.value)
    return _out(db, q.order_by(HRUser.role, HRUser.name).all(), current)


@router.post("/staff", response_model=CreatedStaffResponse)
def create_staff(payload: CreateStaffRequest, db: Session = Depends(get_db),
                 current: HRUser = Depends(get_current_admin)):
    """Create a Manager, or an HR Executive under a Manager. An HR Executive
    may be created without a team; they are then visible only here until
    moved under a Manager. The Master Admin may also create Super Admins."""
    try:
        role = StaffRole(payload.role)
    except ValueError:
        raise HTTPException(status_code=400, detail="Role must be SUPER_ADMIN, MANAGER or HR")
    manager = staff_service.get_manager(db, payload.manager_id) if role == StaffRole.HR else None
    if role == StaffRole.MANAGER and payload.manager_id:
        raise HTTPException(status_code=400, detail="A Manager does not report to another Manager")
    user, temp_password = staff_service.create_staff(
        db, current, name=payload.name, email=payload.email, role=role, manager=manager)
    db.commit()
    return CreatedStaffResponse(staff=_out(db, [user], current)[0], temp_password=temp_password)


@router.patch("/staff/{staff_id}", response_model=StaffOut)
def update_staff(staff_id: int, payload: UpdateStaffRequest, db: Session = Depends(get_db),
                 current: HRUser = Depends(get_current_admin)):
    """Rename, deactivate/reactivate, or move an HR Executive to another
    Manager (manager_id 0 leaves them without a team). Deactivating an admin
    is the Master Admin's call; the Master Admin itself cannot be deactivated."""
    target = _target(db, staff_id, current)
    if payload.name is not None:
        staff_service.rename(db, current, target, payload.name)
    if payload.is_active is not None:
        staff_service.set_active(db, current, target, payload.is_active)
    if payload.manager_id is not None:
        manager = None if payload.manager_id == 0 else staff_service.get_manager(db, payload.manager_id)
        staff_service.move_hr(db, current, target, manager)
    db.commit()
    return _out(db, [target], current)[0]


@router.post("/staff/{staff_id}/reset-password")
def reset_staff_password(staff_id: int, db: Session = Depends(get_db),
                         current: HRUser = Depends(get_current_admin)):
    """Issue a fresh one-time password. Only the Master Admin may reset a
    Super Admin's."""
    target = _target(db, staff_id, current)
    temp_password = staff_service.reset_password(db, current, target)
    db.commit()
    return {"detail": f"New password issued for {target.name}. They must change it on next login.",
            "temp_password": temp_password}


@router.delete("/staff/{staff_id}")
def delete_staff(staff_id: int, db: Session = Depends(get_db),
                 current: HRUser = Depends(get_current_admin)):
    target = _target(db, staff_id, current)
    staff_service.delete_staff(db, current, target)
    db.commit()
    return {"detail": "Staff account deleted"}


@router.get("/audit", response_model=list[StaffAuditOut])
def global_audit(limit: int = 500, db: Session = Depends(get_db),
                 current: HRUser = Depends(get_current_admin)):
    """Every staff-hierarchy and ownership change, newest first."""
    return (db.query(StaffAuditLog)
              .order_by(StaffAuditLog.created_at.desc(), StaffAuditLog.id.desc())
              .limit(min(limit, 2000)).all())
