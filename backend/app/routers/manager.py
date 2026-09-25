"""Manager (and the admins): run a team of HR Executives and decide who owns
which candidate. A Manager reaches only their own team; the Super Admin and
Master Admin, using the same endpoints, reach every team."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import scope, staff_service
from app.database import get_db
from app.deps import get_current_manager_or_above
from app.models import HRUser, StaffAuditLog, StaffRole
from app.schemas import (AssignCandidateRequest, CreatedStaffResponse, CreateStaffRequest,
                         StaffAuditOut, StaffOut, UpdateStaffRequest)

router = APIRouter(prefix="/manager", tags=["manager"])


def _team_member(db: Session, staff_id: int, current: HRUser) -> HRUser:
    """The HR Executive with this id, if the caller may manage them; 404
    otherwise, so another team's ids reveal nothing."""
    target = db.query(HRUser).filter(HRUser.id == staff_id).first()
    if not scope.staff_in_scope(db, target, current) or target.role != StaffRole.HR.value:
        raise HTTPException(status_code=404, detail="HR Executive not found in your team")
    return target


@router.get("/team", response_model=list[StaffOut])
def list_team(db: Session = Depends(get_db), current: HRUser = Depends(get_current_manager_or_above)):
    """The caller's HR Executives (every HR Executive, for the Super Admin)."""
    q = db.query(HRUser).filter(HRUser.role == StaffRole.HR.value)
    if not scope.is_admin(current):
        q = q.filter(HRUser.manager_id == current.id)
    return staff_service.staff_out(db, q.order_by(HRUser.name).all(), include_temp_password=True)


@router.post("/team", response_model=CreatedStaffResponse)
def create_hr(payload: CreateStaffRequest, db: Session = Depends(get_db),
              current: HRUser = Depends(get_current_manager_or_above)):
    """Add an HR Executive to the caller's team. The Super Admin names the
    team with manager_id."""
    if payload.role not in (StaffRole.HR.value, None, ""):
        raise HTTPException(status_code=403, detail="Only the Super Admin can create Managers")
    if scope.is_admin(current):
        manager = staff_service.get_manager(db, payload.manager_id)
    else:
        if payload.manager_id not in (None, current.id):
            raise HTTPException(status_code=403, detail="You can only add HR Executives to your own team")
        manager = current
    user, temp_password = staff_service.create_staff(
        db, current, name=payload.name, email=payload.email, role=StaffRole.HR, manager=manager)
    db.commit()
    return CreatedStaffResponse(staff=staff_service.staff_out(db, [user])[0],
                                temp_password=temp_password)


@router.patch("/team/{staff_id}", response_model=StaffOut)
def update_hr(staff_id: int, payload: UpdateStaffRequest, db: Session = Depends(get_db),
              current: HRUser = Depends(get_current_manager_or_above)):
    """Rename or deactivate/reactivate one of the caller's HR Executives.
    Moving an HR Executive between teams is the Super Admin's call."""
    target = _team_member(db, staff_id, current)
    if payload.manager_id is not None and not scope.is_admin(current):
        raise HTTPException(status_code=403, detail="Only the Super Admin can move an HR Executive to another team")
    if payload.name is not None:
        staff_service.rename(db, current, target, payload.name)
    if payload.is_active is not None:
        staff_service.set_active(db, current, target, payload.is_active)
    if payload.manager_id is not None:
        manager = None if payload.manager_id == 0 else staff_service.get_manager(db, payload.manager_id)
        staff_service.move_hr(db, current, target, manager)
    db.commit()
    return staff_service.staff_out(db, [target], include_temp_password=True)[0]


@router.post("/team/{staff_id}/reset-password")
def reset_hr_password(staff_id: int, db: Session = Depends(get_db),
                      current: HRUser = Depends(get_current_manager_or_above)):
    target = _team_member(db, staff_id, current)
    temp_password = staff_service.reset_password(db, current, target)
    db.commit()
    return {"detail": f"New password issued for {target.name}. They must change it on next login.",
            "temp_password": temp_password}


@router.delete("/team/{staff_id}")
def delete_hr(staff_id: int, db: Session = Depends(get_db),
              current: HRUser = Depends(get_current_manager_or_above)):
    """Refused while the HR Executive still owns candidates — reassign first."""
    target = _team_member(db, staff_id, current)
    staff_service.delete_staff(db, current, target)
    db.commit()
    return {"detail": "HR Executive deleted"}


@router.post("/candidates/{candidate_id}/assign")
def assign_candidate(candidate_id: int, payload: AssignCandidateRequest, db: Session = Depends(get_db),
                     current: HRUser = Depends(get_current_manager_or_above)):
    """Hand a candidate to another owner. A Manager moves candidates within
    their team (to an HR Executive or to themselves); the Super Admin moves
    them to anyone except the Master Admin; the Master Admin anywhere."""
    candidate = scope.get_scoped_candidate(db, candidate_id, current)
    allowed = {u.id: u for u in scope.assignable_staff(db, current)}
    new_owner = allowed.get(payload.assigned_hr_id)
    if not new_owner:
        raise HTTPException(status_code=400,
                             detail="You can only assign candidates to active staff in your team")
    staff_service.assign_candidate(db, current, candidate, new_owner)
    db.commit()
    return {"detail": f"{candidate.name} is now assigned to {new_owner.name}.",
            "assigned_hr_id": new_owner.id, "assigned_hr_name": new_owner.name}


@router.get("/audit", response_model=list[StaffAuditOut])
def team_audit(limit: int = 500, db: Session = Depends(get_db),
               current: HRUser = Depends(get_current_manager_or_above)):
    """Staff and ownership changes touching the caller's team, newest first."""
    q = db.query(StaffAuditLog)
    if not scope.is_admin(current):
        q = q.filter(or_(StaffAuditLog.team_manager_id == current.id,
                         StaffAuditLog.actor_id == current.id))
    return (q.order_by(StaffAuditLog.created_at.desc(), StaffAuditLog.id.desc())
              .limit(min(limit, 2000)).all())
