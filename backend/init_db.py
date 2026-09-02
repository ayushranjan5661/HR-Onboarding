"""
Run once to create all tables and seed the first HR login:
    python init_db.py
"""
from sqlalchemy import text

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.models import HRUser
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
        # Sequential flow: BGV must not sit open before the candidate's
        # Document Collection has been approved. Candidates approved under the
        # earlier rule (both forms opened at once) are corrected here. Only
        # untouched (PENDING) BGV forms are re-locked, so nothing is lost.
        conn.execute(text("""
            UPDATE form_submissions bgv
               SET status = 'LOCKED'
             WHERE bgv.form_type = 'BGV'
               AND bgv.status = 'PENDING'
               AND NOT EXISTS (
                     SELECT 1 FROM form_submissions doc
                      WHERE doc.candidate_id = bgv.candidate_id
                        AND doc.form_type = 'DOCUMENT_COLLECTION'
                        AND doc.status = 'APPROVED')
        """))
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

        # Bring every existing table up to date with its model.
        _sync_columns(conn)

    db = SessionLocal()
    try:
        existing = db.query(HRUser).filter(HRUser.email == settings.SEED_HR_EMAIL).first()
        if not existing:
            db.add(HRUser(
                name=settings.SEED_HR_NAME,
                email=settings.SEED_HR_EMAIL,
                password_hash=hash_password(settings.SEED_HR_PASSWORD),
            ))
            db.commit()
            print(f"Tables created. Seeded HR login -> {settings.SEED_HR_EMAIL} / {settings.SEED_HR_PASSWORD}")
            print("Change this password after first login (set SEED_HR_* in .env before first run to customize).")
        else:
            print("Tables created/verified. Seed HR user already exists.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
