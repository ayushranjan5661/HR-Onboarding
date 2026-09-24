"""Mutations on the staff hierarchy and on candidate ownership, shared by the
Super Admin and Manager routers. The routers decide *who may* do a thing;
this module does it and writes the audit entry. Nothing here commits — the
caller owns the transaction."""
from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (Candidate, FieldEditLog, FieldEditPermission, FormSubmission,
                        HRUser, StaffAuditLog, StaffRole)
from app.schemas import StaffOut
from app.security import decrypt_password, encrypt_password, generate_temp_password, hash_password


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def _is_master(user: HRUser) -> bool:
    return user.role == StaffRole.MASTER_ADMIN.value


def team_of(user: HRUser | None) -> int | None:
    """The Manager id a staff user's actions file under: their Manager for an
    HR Executive, themselves for a Manager, nothing for the admins."""
    if user is None:
        return None
    if user.role == StaffRole.MANAGER.value:
        return user.id
    if user.role == StaffRole.HR.value:
        return user.manager_id
    return None


def audit(db: Session, actor: HRUser, action: str, *, target_type: str,
          target: HRUser | Candidate | None = None, detail: str | None = None,
          team_manager_id: int | None = None) -> None:
    db.add(StaffAuditLog(
        actor_id=actor.id, actor_name=actor.name, actor_role=actor.role,
        action=action, target_type=target_type,
        target_id=target.id if target is not None else None,
        target_name=target.name if target is not None else None,
        detail=detail, team_manager_id=team_manager_id))


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def staff_out(db: Session, users: list[HRUser], include_temp_password: bool = False,
              include_current_password: bool = False) -> list[StaffOut]:
    """Serialise staff rows with their owned-candidate and team counts, in a
    fixed number of queries whatever the list size. `include_current_password`
    is for the Master Admin only: it decrypts the password now in force."""
    ids = [u.id for u in users]
    if not ids:
        return []
    owned = dict(db.query(Candidate.assigned_hr_id, func.count(Candidate.id))
                   .filter(Candidate.assigned_hr_id.in_(ids))
                   .group_by(Candidate.assigned_hr_id).all())
    teams = dict(db.query(HRUser.manager_id, func.count(HRUser.id))
                   .filter(HRUser.manager_id.in_(ids))
                   .group_by(HRUser.manager_id).all())
    manager_ids = {u.manager_id for u in users if u.manager_id}
    managers = ({m.id: m.name for m in db.query(HRUser).filter(HRUser.id.in_(manager_ids)).all()}
                if manager_ids else {})
    out = []
    for u in users:
        out.append(StaffOut(
            id=u.id, name=u.name, email=u.email, role=u.role, is_active=u.is_active,
            manager_id=u.manager_id, manager_name=managers.get(u.manager_id),
            must_reset_password=u.must_reset_password,
            temp_password=(decrypt_password(u.temp_password_enc)
                           if include_temp_password and u.must_reset_password else None),
            current_password=(decrypt_password(u.password_enc)
                              if include_current_password else None),
            candidate_count=owned.get(u.id, 0), team_size=teams.get(u.id, 0),
            created_at=u.created_at))
    return out


def get_manager(db: Session, manager_id: int | None) -> HRUser | None:
    """The active Manager with this id, or 400 if there is no such Manager."""
    if manager_id is None:
        return None
    manager = db.query(HRUser).filter(HRUser.id == manager_id,
                                      HRUser.role == StaffRole.MANAGER.value,
                                      HRUser.is_active.is_(True)).first()
    if not manager:
        raise HTTPException(status_code=400, detail="That Manager does not exist or is deactivated")
    return manager


# ---------------------------------------------------------------------------
# Staff accounts
# ---------------------------------------------------------------------------

def create_staff(db: Session, actor: HRUser, *, name: str, email: str, role: StaffRole,
                 manager: HRUser | None) -> tuple[HRUser, str]:
    """Create a Super Admin (Master Admin only), Manager or HR Executive with
    a system-generated password that must be changed on first login. Returns
    the user and that password."""
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    if role == StaffRole.MASTER_ADMIN:
        raise HTTPException(status_code=400,
                             detail="The Master Admin is seeded from .env and cannot be created here")
    if role == StaffRole.SUPER_ADMIN:
        if not _is_master(actor):
            raise HTTPException(status_code=403, detail="Only the Master Admin can create Super Admins")
        if manager is not None:
            raise HTTPException(status_code=400, detail="A Super Admin does not report to a Manager")
    if role == StaffRole.MANAGER and manager is not None:
        raise HTTPException(status_code=400, detail="A Manager does not report to another Manager")
    if db.query(HRUser).filter(HRUser.email == email).first():
        raise HTTPException(status_code=400, detail="A staff account with this email already exists")

    temp_password = generate_temp_password(12)
    user = HRUser(
        name=name, email=email, role=role.value,
        manager_id=manager.id if manager else None,
        password_hash=hash_password(temp_password),
        temp_password_enc=encrypt_password(temp_password),
        password_enc=encrypt_password(temp_password),
        must_reset_password=True, created_by_id=actor.id, is_active=True,
    )
    db.add(user)
    db.flush()
    detail = f"Created {role.value.replace('_', ' ').title()} {user.email}"
    if manager:
        detail += f" in {manager.name}'s team"
    audit(db, actor, "STAFF_CREATED", target_type="STAFF", target=user, detail=detail,
          team_manager_id=team_of(user))
    return user, temp_password


def reset_password(db: Session, actor: HRUser, target: HRUser) -> str:
    """Issue a fresh one-time password. The old one stops working now, and
    the user has to pick their own on next login."""
    _guard_admin_target(actor, target, "reset the password of")
    temp_password = generate_temp_password(12)
    target.password_hash = hash_password(temp_password)
    target.temp_password_enc = encrypt_password(temp_password)
    target.password_enc = encrypt_password(temp_password)
    target.must_reset_password = True
    audit(db, actor, "STAFF_PASSWORD_RESET", target_type="STAFF", target=target,
          detail=f"Password reset for {target.email}", team_manager_id=team_of(target))
    return temp_password


def record_own_password(user: HRUser, plain: str) -> None:
    """A staff user chose their own password: keep the encrypted copy in
    step so the Master Admin's view stays accurate."""
    user.password_hash = hash_password(plain)
    user.password_enc = encrypt_password(plain)
    user.must_reset_password = False
    user.temp_password_enc = None


def _guard_admin_target(actor: HRUser, target: HRUser, verb: str) -> None:
    """Nobody touches the Master Admin's account but the Master Admin, and
    only the Master Admin touches a Super Admin's."""
    if target.role == StaffRole.MASTER_ADMIN.value and target.id != actor.id:
        raise HTTPException(status_code=403, detail=f"You cannot {verb} the Master Admin")
    if target.role == StaffRole.SUPER_ADMIN.value and not _is_master(actor):
        raise HTTPException(status_code=403, detail=f"Only the Master Admin can {verb} a Super Admin")


def set_active(db: Session, actor: HRUser, target: HRUser, active: bool) -> None:
    if target.is_active == active:
        return
    if not active:
        if target.id == actor.id:
            raise HTTPException(status_code=400, detail="You cannot deactivate your own account")
        if target.role == StaffRole.MASTER_ADMIN.value:
            raise HTTPException(status_code=400, detail="The Master Admin account cannot be deactivated")
        _guard_admin_target(actor, target, "deactivate")
    target.is_active = active
    audit(db, actor, "STAFF_ACTIVATED" if active else "STAFF_DEACTIVATED", target_type="STAFF",
          target=target, detail=f"{'Reactivated' if active else 'Deactivated'} {target.email}",
          team_manager_id=team_of(target))


def rename(db: Session, actor: HRUser, target: HRUser, name: str) -> None:
    name = (name or "").strip()
    if not name or name == target.name:
        return
    old = target.name
    target.name = name
    audit(db, actor, "STAFF_RENAMED", target_type="STAFF", target=target,
          detail=f"Renamed {old} to {name}", team_manager_id=team_of(target))


def move_hr(db: Session, actor: HRUser, target: HRUser, manager: HRUser | None) -> None:
    """Put an HR Executive under another Manager (or under none). Their
    candidates move with them, since ownership is by person, not by team."""
    if target.role != StaffRole.HR.value:
        raise HTTPException(status_code=400, detail="Only HR Executives report to a Manager")
    new_id = manager.id if manager else None
    if target.manager_id == new_id:
        return
    old = db.query(HRUser).filter(HRUser.id == target.manager_id).first() if target.manager_id else None
    target.manager_id = new_id
    audit(db, actor, "STAFF_MOVED", target_type="STAFF", target=target,
          detail=f"Moved {target.email} from {old.name if old else 'no team'} "
                 f"to {manager.name if manager else 'no team'}",
          team_manager_id=new_id)
    if old is not None:
        # The team that lost them should see it in their trail as well.
        audit(db, actor, "STAFF_MOVED_OUT", target_type="STAFF", target=target,
              detail=f"{target.email} moved to {manager.name if manager else 'no team'}",
              team_manager_id=old.id)


def delete_staff(db: Session, actor: HRUser, target: HRUser) -> None:
    """Remove a staff login for good. Refused while they still own candidates
    or lead HR Executives — reassign those first, or deactivate instead."""
    if target.id == actor.id:
        raise HTTPException(status_code=400, detail="You cannot delete your own account")
    if target.role == StaffRole.MASTER_ADMIN.value:
        raise HTTPException(status_code=400, detail="The Master Admin account cannot be deleted")
    _guard_admin_target(actor, target, "delete")
    owned = db.query(func.count(Candidate.id)).filter(Candidate.assigned_hr_id == target.id).scalar()
    if owned:
        raise HTTPException(
            status_code=400,
            detail=f"{target.name} still owns {owned} candidate(s). Reassign them first, "
                   "or deactivate the account instead.")
    team = db.query(func.count(HRUser.id)).filter(HRUser.manager_id == target.id).scalar()
    if team:
        raise HTTPException(
            status_code=400,
            detail=f"{target.name} still has {team} HR Executive(s) in their team. Move them "
                   "to another Manager first, or deactivate the account instead.")

    # History stays; only the pointer to the deleted login is cleared.
    db.query(Candidate).filter(Candidate.created_by_hr_id == target.id).update(
        {Candidate.created_by_hr_id: None})
    db.query(FormSubmission).filter(FormSubmission.reviewed_by_hr_id == target.id).update(
        {FormSubmission.reviewed_by_hr_id: None})
    db.query(FieldEditLog).filter(FieldEditLog.edited_by_hr_id == target.id).update(
        {FieldEditLog.edited_by_hr_id: None})
    db.query(FieldEditPermission).filter(FieldEditPermission.granted_by_hr_id == target.id).update(
        {FieldEditPermission.granted_by_hr_id: None})
    db.query(HRUser).filter(HRUser.created_by_id == target.id).update({HRUser.created_by_id: None})

    audit(db, actor, "STAFF_DELETED", target_type="STAFF", target=target,
          detail=f"Deleted {target.role.replace('_', ' ').title()} {target.email}",
          team_manager_id=team_of(target))
    db.delete(target)


# ---------------------------------------------------------------------------
# Candidate ownership
# ---------------------------------------------------------------------------

def assign_candidate(db: Session, actor: HRUser, candidate: Candidate, new_owner: HRUser) -> None:
    if not new_owner.is_active:
        raise HTTPException(status_code=400, detail="That staff account is deactivated")
    if candidate.assigned_hr_id == new_owner.id:
        return
    old = (db.query(HRUser).filter(HRUser.id == candidate.assigned_hr_id).first()
           if candidate.assigned_hr_id else None)
    candidate.assigned_hr_id = new_owner.id
    detail = f"Reassigned from {old.name if old else 'nobody'} to {new_owner.name}"
    audit(db, actor, "CANDIDATE_REASSIGNED", target_type="CANDIDATE", target=candidate,
          detail=detail, team_manager_id=team_of(new_owner))
    old_team = team_of(old)
    if old_team is not None and old_team != team_of(new_owner):
        audit(db, actor, "CANDIDATE_MOVED_OUT", target_type="CANDIDATE", target=candidate,
              detail=detail, team_manager_id=old_team)
