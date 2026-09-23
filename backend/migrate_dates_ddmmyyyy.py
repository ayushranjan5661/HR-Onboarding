"""One-off: rewrite stored dates from yyyy-mm-dd to the app's DD/MM/YYYY.

Rows written before dates were standardised hold what <input type="date">
produced (yyyy-mm-dd). Everything reading them — the HR portal, the Zoho
exporter, the insights checks — accepts both shapes, so this is a tidy-up, not
a prerequisite. Run it once so old and new records read identically:

    cd backend && python migrate_dates_ddmmyyyy.py          # show what would change
    cd backend && python migrate_dates_ddmmyyyy.py --apply  # write it

Values that are not date-shaped (free text someone typed) are left alone and
listed at the end, so nothing is silently rewritten.
"""
import sys

from app.database import SessionLocal
from app.date_format import DATE_FIELDS, normalize
from app.models import (BGVAddressHistory, BGVDetails, BGVEmploymentCheck, BGVGap,
                        CandidateProfile, CIFDetails, EmploymentDetail)

# Every table holding a date column, paired with the columns to visit.
TARGETS = [
    (CandidateProfile, ["date_of_birth"]),
    (CIFDetails, ["declaration_date"]),
    (BGVDetails, ["passport_expiry", "declaration_date"]),
    (EmploymentDetail, ["from_date", "to_date"]),
    (BGVAddressHistory, ["from_date", "to_date"]),
    (BGVEmploymentCheck, ["from_date", "to_date"]),
    (BGVGap, ["from_date", "to_date"]),
]


def main(apply: bool) -> int:
    unparsed, changed = [], 0
    db = SessionLocal()
    try:
        for model, columns in TARGETS:
            assert set(columns) <= DATE_FIELDS, model.__name__
            for row in db.query(model).all():
                for col in columns:
                    old = getattr(row, col)
                    if not old or not str(old).strip():
                        continue
                    new = normalize(old)
                    if new == old:
                        continue
                    if not _looks_like_date(new):
                        unparsed.append(f"{model.__tablename__}.{col} id={row.id}: {old!r}")
                        continue
                    print(f"{model.__tablename__}.{col} id={row.id}: {old!r} -> {new!r}")
                    if apply:
                        setattr(row, col, new)
                    changed += 1
        if apply:
            db.commit()
    finally:
        db.close()

    print(f"\n{changed} value(s) {'updated' if apply else 'would be updated'}.")
    if unparsed:
        print(f"{len(unparsed)} value(s) left as-is (not date-shaped):")
        for line in unparsed:
            print("  " + line)
    if not apply and changed:
        print("Re-run with --apply to write these changes.")
    return 0


def _looks_like_date(text: str) -> bool:
    parts = str(text).split("/")
    return len(parts) == 3 and all(p.isdigit() for p in parts)


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv))
