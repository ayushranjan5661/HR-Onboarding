"""One-off: move the old education blob into the two columns that replaced it.

The CIF used to ask one question, "Course Name / Specialization and College",
stored in education_details.course_college. It now asks two: College/University
Name and Specialization. Existing rows hold the single blob, which cannot be
split reliably, so this copies it verbatim into college_name and leaves
specialization blank for HR to fill:

    cd backend && python migrate_split_course_college.py          # preview
    cd backend && python migrate_split_course_college.py --apply  # write

A row whose new columns are already filled is left alone, so re-running is
safe. course_college itself is never cleared — it stays as the record of what
the candidate originally typed.
"""
import sys

from app.database import SessionLocal
from app.models import EducationDetail


def main(apply: bool) -> int:
    moved = skipped = 0
    db = SessionLocal()
    try:
        for row in db.query(EducationDetail).order_by(EducationDetail.id).all():
            old = (row.course_college or "").strip()
            if not old:
                continue
            if (row.college_name or "").strip() or (row.specialization or "").strip():
                skipped += 1        # already split — leave it as HR set it
                continue
            print(f"education_details id={row.id} ({row.section}): "
                  f"course_college -> college_name ({len(old)} chars)")
            if apply:
                row.college_name = old
            moved += 1
        if apply:
            db.commit()
    finally:
        db.close()

    print(f"\n{moved} row(s) {'moved' if apply else 'would be moved'}; "
          f"{skipped} already split.")
    if moved:
        print("Specialization is left blank on every moved row - the old value is a "
              "single blob and splitting it automatically would guess wrong.")
    if not apply and moved:
        print("Re-run with --apply to write these changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv))
