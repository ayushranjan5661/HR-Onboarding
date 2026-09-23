"""Super Admin only: the whole staff hierarchy and the global audit trail.
Candidate lists and actions come from the /hr endpoints, which already show
the Super Admin everything; candidate reassignment across teams goes through
/manager/candidates/{id}/assign, which is unrestricted for the Super Admin."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import staff_service
from app.database import get_db
from app.deps import get_current_super_admin
from app.models import HRUser, StaffAuditLog, StaffRole
from app.schemas import (CreatedStaffResponse, CreateStaffRequest, StaffAuditOut, StaffOut,
                         UpdateStaffRequest)

router = APIRouter(prefix="/admin", tags=["admin"])


def _target(db: Session, staff_id: int) -> HRUser:
    user = db.query(HRUser).filter(HRUser.id == staff_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Staff account not found")
    return user


@router.get("/staff", response_model=list[StaffOut])
def list_staff(db: Session = Depends(get_db), current: HRUser = Depends(get_current_super_admin)):
    """Everyone, the Super Admin included, with owned-candidate and team counts.
    The one-time password is shown for accounts that have not changed it yet."""
    users = db.query(HRUser).order_by(HRUser.role, HRUser.name).all()
    return staff_service.staff_out(db, users, include_temp_password=True)


@router.post("/staff", response_model=CreatedStaffResponse)
def create_staff(payload: CreateStaffRequest, db: Session = Depends(get_db),
                 current: HRUser = Depends(get_current_super_admin)):
    """Create a Manager, or an HR Executive under a Manager. An HR Executive
    may be created without a team; they are then visible only here until
    moved under a Manager."""
    try:
        role = StaffRole(payload.role)
    except ValueError:
        raise HTTPException(status_code=400, detail="Role must be MANAGER or HR")
    manager = staff_service.get_manager(db, payload.manager_id) if role == StaffRole.HR else None
    if role == StaffRole.MANAGER and payload.manager_id:
        raise HTTPException(status_code=400, detail="A Manager does not report to another Manager")
    user, temp_password = staff_service.create_staff(
        db, current, name=payload.name, email=payload.email, role=role, manager=manager)
    db.commit()
    return CreatedStaffResponse(staff=staff_service.staff_out(db, [user])[0],
                                temp_password=temp_password)


@router.patch("/staff/{staff_id}", response_model=StaffOut)
def update_staff(staff_id: int, payload: UpdateStaffRequest, db: Session = Depends(get_db),
                 current: HRUser = Depends(get_current_super_admin)):
    """Rename, deactivate/reactivate, or move an HR Executive to another
    Manager (manager_id 0 leaves them without a team)."""
    target = _target(db, staff_id)
    if target.role == StaffRole.SUPER_ADMIN.value and payload.is_active is False:
        raise HTTPException(status_code=400, detail="The Super Admin account cannot be deactivated")
    if payload.name is not None:
        staff_service.rename(db, current, target, payload.name)
    if payload.is_active is not None:
        staff_service.set_active(db, current, target, payload.is_active)
    if payload.manager_id is not None:
        manager = None if payload.manager_id == 0 else staff_service.get_manager(db, payload.manager_id)
        staff_service.move_hr(db, current, target, manager)
    db.commit()
    return staff_service.staff_out(db, [target], include_temp_password=True)[0]


@router.post("/staff/{staff_id}/reset-password")
def reset_staff_password(staff_id: int, db: Session = Depends(get_db),
                         current: HRUser = Depends(get_current_super_admin)):
    target = _target(db, staff_id)
    temp_password = staff_service.reset_password(db, current, target)
    db.commit()
    return {"detail": f"New password issued for {target.name}. They must change it on next login.",
            "temp_password": temp_password}


@router.delete("/staff/{staff_id}")
def delete_staff(staff_id: int, db: Session = Depends(get_db),
                 current: HRUser = Depends(get_current_super_admin)):
    target = _target(db, staff_id)
    staff_service.delete_staff(db, current, target)
    db.commit()
    return {"detail": "Staff account deleted"}


@router.get("/audit", response_model=list[StaffAuditOut])
def global_audit(limit: int = 500, db: Session = Depends(get_db),
                 current: HRUser = Depends(get_current_super_admin)):
    """Every staff-hierarchy and ownership change, newest first."""
    return (db.query(StaffAuditLog)
              .order_by(StaffAuditLog.created_at.desc(), StaffAuditLog.id.desc())
              .limit(min(limit, 2000)).all())
