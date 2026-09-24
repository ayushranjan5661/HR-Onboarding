"""
Run once to create all tables and seed the first HR login:
    python init_db.py
"""
from sqlalchemy import text

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.models import HRUser, StaffRole
from app.security import hash_password



def _sync_columns(conn):
    """Add any model column that is missing from an existing table.

    Base.metadata.create_all() creates missing *tables* but never alters
    existing ones, so adding a field to a model that already has a table
    silently produces "column does not exist" at query time. This closes
    that gap for the plain-column detail tables.
    """
    from sqlalchemy import String, Text, Integer, Boolean, DateTime
    from app.database import Base

    type_sql = {}
    for table in Base.metadata.sorted_tables:
        existing = {r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :t AND table_schema = current_schema()"
        ), {"t": table.name})}
        if not existing:
            continue  # brand-new table; create_all handles it
        for col in table.columns:
            if col.name in existing:
                continue
            t = col.type
            if isinstance(t, String) and not isinstance(t, Text):
                ddl = f"VARCHAR({t.length})" if t.length else "TEXT"
            elif isinstance(t, Text):
                ddl = "TEXT"
            elif isinstance(t, Boolean):
                ddl = "BOOLEAN"
            elif isinstance(t, Integer):
                ddl = "INTEGER"
            elif isinstance(t, DateTime):
                ddl = "TIMESTAMPTZ"
            else:
                print(f"  ! skipping {table.name}.{col.name} ({t}) - add it manually")
                continue
            conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN IF NOT EXISTS "{col.name}" {ddl}'))
            print(f"  + {table.name}.{col.name} {ddl}")


def main():
    Base.metadata.create_all(bind=engine)

    # Lightweight migrations for DBs created on earlier versions.
    with engine.begin() as conn:
        # Plaintext temp passwords are no longer stored (the bcrypt hash is the
        # only credential kept); drop the column to purge previously saved ones.
        conn.execute(text("ALTER TABLE candidates DROP COLUMN IF EXISTS temp_password"))
        # Form answers moved from JSONB to relational tables.
        conn.execute(text("ALTER TABLE form_submissions DROP COLUMN IF EXISTS extra_data"))
        # One-click invite links.
        conn.execute(text("ALTER TABLE candidates ADD COLUMN IF NOT EXISTS invite_token VARCHAR(64)"))
        conn.execute(text("ALTER TABLE candidates ADD COLUMN IF NOT EXISTS invite_token_expires_at TIMESTAMPTZ"))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_candidates_invite_token "
                           "ON candidates (invite_token)"))
        # Experienced vs Fresher document-collection variant.
        conn.execute(text("DO $$ BEGIN CREATE TYPE candidatetype AS ENUM "
                           "('EXPERIENCED','FRESHER'); EXCEPTION WHEN duplicate_object "
                           "THEN NULL; END $$"))
        conn.execute(text("ALTER TABLE candidates ADD COLUMN IF NOT EXISTS candidate_type "
                           "candidatetype NOT NULL DEFAULT 'EXPERIENCED'"))
        # BGV was redesigned: its old placeholder columns are superseded by the
        # richer bgv_employment_checks / bgv_reference_checks tables.
        for obsolete in ("previous_employer_name", "employee_id", "employment_start_date",
                          "employment_end_date", "reporting_manager_name",
                          "reporting_manager_contact", "hr_contact_previous_employer",
                          "reference1_name", "reference1_relation", "reference1_contact",
                          "reference2_name", "reference2_relation", "reference2_contact",
                          "consent_for_bgv"):
            conn.execute(text(f"ALTER TABLE bgv_details DROP COLUMN IF EXISTS {obsolete}"))
        # The audit log now records candidate edits too, so the HR actor is
        # no longer always set. Add the new columns before relaxing the old
        # constraint, and stamp every pre-existing row as an HR edit.
        conn.execute(text("ALTER TABLE field_edit_log ADD COLUMN IF NOT EXISTS reason TEXT"))
        conn.execute(text("ALTER TABLE field_edit_log ADD COLUMN IF NOT EXISTS "
                           "actor_role VARCHAR(20)"))
        conn.execute(text("ALTER TABLE field_edit_log ADD COLUMN IF NOT EXISTS "
                           "edited_by_candidate_id INTEGER"))
        conn.execute(text("ALTER TABLE field_edit_log ADD COLUMN IF NOT EXISTS "
                           "permission_id INTEGER"))
        conn.execute(text("ALTER TABLE field_edit_log ALTER COLUMN edited_by_hr_id "
                           "DROP NOT NULL"))
        conn.execute(text("UPDATE field_edit_log SET actor_role = 'HR' "
                           "WHERE actor_role IS NULL"))
        # Changes saved together share a change-set id. Rows written before
        # this existed keep NULL and each stands on its own in the trail.
        conn.execute(text("ALTER TABLE field_edit_log ADD COLUMN IF NOT EXISTS "
                           "change_set_id VARCHAR(32)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_field_edit_log_change_set_id "
                           "ON field_edit_log (change_set_id)"))

        # A replaced document keeps its file now, so the audit trail can show
        # both sides of the change.
        conn.execute(text("ALTER TABLE field_edit_log ADD COLUMN IF NOT EXISTS "
                           "old_file_id INTEGER"))
        conn.execute(text("ALTER TABLE field_edit_log ADD COLUMN IF NOT EXISTS "
                           "new_file_id INTEGER"))

        # Staff hierarchy: Super Admin > Manager > HR Executive, one table.
        # Existing rows are HR Executives without a team (visible only to the
        # Super Admin until moved under a Manager); the seed account becomes
        # the Super Admin.
        conn.execute(text("ALTER TABLE hr_users ADD COLUMN IF NOT EXISTS role VARCHAR(20) "
                           "NOT NULL DEFAULT 'HR'"))
        # Databases created before MASTER_ADMIN existed sized these to fit
        # SUPER_ADMIN (11 chars); widen to the model's 20 either way.
        conn.execute(text("ALTER TABLE hr_users ALTER COLUMN role TYPE VARCHAR(20)"))
        conn.execute(text("ALTER TABLE staff_audit_log ALTER COLUMN actor_role TYPE VARCHAR(20)"))
        conn.execute(text("ALTER TABLE hr_users ADD COLUMN IF NOT EXISTS manager_id INTEGER"))
        conn.execute(text("ALTER TABLE hr_users ADD COLUMN IF NOT EXISTS created_by_id INTEGER"))
        conn.execute(text("ALTER TABLE hr_users ADD COLUMN IF NOT EXISTS temp_password_enc TEXT"))
        conn.execute(text("ALTER TABLE hr_users ADD COLUMN IF NOT EXISTS must_reset_password "
                           "BOOLEAN NOT NULL DEFAULT FALSE"))
        conn.execute(text("UPDATE hr_users SET role = 'SUPER_ADMIN' WHERE email = :e"),
                     {"e": settings.SEED_HR_EMAIL})
        # Candidate ownership: who invited stays fixed; who owns can move.
        conn.execute(text("ALTER TABLE candidates ADD COLUMN IF NOT EXISTS assigned_hr_id INTEGER"))
        conn.execute(text("UPDATE candidates SET assigned_hr_id = created_by_hr_id "
                           "WHERE assigned_hr_id IS NULL"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_candidates_assigned_hr_id "
                           "ON candidates (assigned_hr_id)"))
        for name, ddl in (
            ("fk_hr_users_manager", "ALTER TABLE hr_users ADD CONSTRAINT fk_hr_users_manager "
                                    "FOREIGN KEY (manager_id) REFERENCES hr_users(id)"),
            ("fk_hr_users_created_by", "ALTER TABLE hr_users ADD CONSTRAINT fk_hr_users_created_by "
                                       "FOREIGN KEY (created_by_id) REFERENCES hr_users(id)"),
            ("fk_candidates_assigned_hr", "ALTER TABLE candidates ADD CONSTRAINT "
                                          "fk_candidates_assigned_hr FOREIGN KEY (assigned_hr_id) "
                                          "REFERENCES hr_users(id)"),
        ):
            conn.execute(text(f"DO $$ BEGIN {ddl}; EXCEPTION WHEN duplicate_object "
                              "THEN NULL; END $$"))
        # A deleted staff user must not be pinned in place by permissions they
        # once granted; the audit trail keeps the grant, minus the name.
        conn.execute(text("ALTER TABLE field_edit_permissions ALTER COLUMN granted_by_hr_id "
                           "DROP NOT NULL"))

        # Bring every existing table up to date with its model.
        _sync_columns(conn)

    db = SessionLocal()
    try:
        _seed_admin(db, role=StaffRole.SUPER_ADMIN, name=settings.SEED_HR_NAME,
                    email=settings.SEED_HR_EMAIL, password=settings.SEED_HR_PASSWORD,
                    env_prefix="SEED_HR_")
        if settings.SEED_MASTER_PASSWORD:
            _seed_admin(db, role=StaffRole.MASTER_ADMIN, name=settings.SEED_MASTER_NAME,
                        email=settings.SEED_MASTER_EMAIL, password=settings.SEED_MASTER_PASSWORD,
                        env_prefix="SEED_MASTER_")
        else:
            print("No Master Admin seeded: set SEED_MASTER_EMAIL / SEED_MASTER_PASSWORD in .env "
                  "and run again to create the developer account.")
    finally:
        db.close()


def _seed_admin(db, *, role, name, email, password, env_prefix):
    """Create the seeded admin login, or bring an existing row up to its
    role. The seed password is also stored encrypted so the Master Admin's
    password view covers the seeded accounts too (only while the row still
    uses that password; a changed password is never overwritten)."""
    from app.security import encrypt_password, verify_password

    label = role.value.replace("_", " ").title()
    existing = db.query(HRUser).filter(HRUser.email == email).first()
    if not existing:
        db.add(HRUser(name=name, email=email, role=role.value,
                      password_hash=hash_password(password),
                      password_enc=encrypt_password(password)))
        db.commit()
        print(f"Seeded {label} login -> {email} (password: {env_prefix}PASSWORD in .env)")
        print("Change this password after first login.")
        return
    changed = False
    # Never demote: a Master Admin row keeps its role even if its email is
    # also given as the Super Admin seed.
    if existing.role != role.value and existing.role != StaffRole.MASTER_ADMIN.value:
        existing.role = role.value
        changed = True
    if existing.password_enc is None and verify_password(password, existing.password_hash):
        existing.password_enc = encrypt_password(password)
        changed = True
    if changed:
        db.commit()
    print(f"Tables created/verified. Seed {label} already exists.")


if __name__ == "__main__":
    main()
