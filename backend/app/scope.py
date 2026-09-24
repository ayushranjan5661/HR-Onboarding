"""Who may see which candidates and staff.

One rule, applied everywhere a candidate is read or written from the staff
side: an HR Executive sees the candidates assigned to them, a Manager sees
every candidate owned by anyone in their team (themselves included), and the
Super Admin sees all. Anything outside scope answers 404, never 403, so a
guessed id does not even confirm the record exists.
"""
from fastapi import HTTPException
from sqlalchemy.orm import Query, Session

from app.models import Candidate, HRUser, StaffRole


def is_master_admin(user: HRUser) -> bool:
    return user.role == StaffRole.MASTER_ADMIN.value


def is_super_admin(user: HRUser) -> bool:
    return user.role == StaffRole.SUPER_ADMIN.value


def is_admin(user: HRUser) -> bool:
    """Master Admin or Super Admin: no candidate or team restriction."""
    return user.role in (StaffRole.MASTER_ADMIN.value, StaffRole.SUPER_ADMIN.value)


def is_manager(user: HRUser) -> bool:
    return user.role == StaffRole.MANAGER.value


def team_hr_ids(db: Session, manager: HRUser) -> set[int]:
    """Ids of every HR Executive reporting to this Manager, active or not.
    Deactivated HRs still own candidates, which the Manager must still see."""
    rows = db.query(HRUser.id).filter(HRUser.manager_id == manager.id).all()
    return {r[0] for r in rows}


def visible_staff_ids(db: Session, current: HRUser) -> set[int] | None:
    """Staff whose candidates the caller may see. None means no restriction."""
    if is_admin(current):
        return None
    if is_manager(current):
        return {current.id} | team_hr_ids(db, current)
    return {current.id}


def assignable_staff(db: Session, current: HRUser) -> list[HRUser]:
    """Active staff the caller may make the owner of a candidate: an HR only
    themselves, a Manager anyone in their team, the Super Admin anyone."""
    q = db.query(HRUser).filter(HRUser.is_active.is_(True))
    ids = visible_staff_ids(db, current)
    if ids is not None:
        q = q.filter(HRUser.id.in_(ids))
    return q.order_by(HRUser.role, HRUser.name).all()


def filter_candidates(query: Query, db: Session, current: HRUser) -> Query:
    ids = visible_staff_ids(db, current)
    if ids is None:
        return query
    return query.filter(Candidate.assigned_hr_id.in_(ids))


def in_scope(db: Session, candidate: Candidate | None, current: HRUser) -> bool:
    if candidate is None:
        return False
    ids = visible_staff_ids(db, current)
    return ids is None or candidate.assigned_hr_id in ids


def check(db: Session, candidate: Candidate | None, current: HRUser) -> Candidate:
    """Return the candidate if the caller may see it, else 404."""
    if not in_scope(db, candidate, current):
        raise HTTPException(status_code=404, detail="Candidate not found")
    return candidate


def get_scoped_candidate(db: Session, candidate_id: int, current: HRUser) -> Candidate:
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    return check(db, candidate, current)


def staff_in_scope(db: Session, target: HRUser | None, current: HRUser) -> bool:
    """May the caller manage this staff account? The Master Admin anyone; a
    Super Admin the Managers and HR Executives (never another admin); a
    Manager only the HR Executives in their own team. Self-targeting is the
    caller's check."""
    if target is None:
        return False
    if is_master_admin(current):
        return True
    if is_super_admin(current):
        return target.role in (StaffRole.MANAGER.value, StaffRole.HR.value)
    if is_manager(current):
        return target.role == StaffRole.HR.value and target.manager_id == current.id
    return False
