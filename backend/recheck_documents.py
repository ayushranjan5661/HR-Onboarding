"""Run the document check over stored uploads.

Uploads are checked as they arrive (app/agents/doc_validator.py), so this is
for the rows that predate the check, and for re-scoring everything after the
rules have been tuned:

    cd backend && python recheck_documents.py            # only documents never checked
    cd backend && python recheck_documents.py --all      # every document, fresh
    cd backend && python recheck_documents.py --candidate 42

Verdicts are written to the documents table; nothing is refused or deleted.
"""
import os
import sys
from collections import Counter

from app.agents import doc_validator
from app.database import SessionLocal
from app.models import Candidate, Document
from app.utils.file_storage import upload_path


def main() -> None:
    redo_all = "--all" in sys.argv
    only = None
    if "--candidate" in sys.argv:
        only = int(sys.argv[sys.argv.index("--candidate") + 1])

    if not doc_validator.enabled():
        sys.exit("Document check is off (DOC_VALIDATION_MODE) or Tesseract is not installed.")

    db = SessionLocal()
    q = db.query(Document)
    if only:
        q = q.filter(Document.candidate_id == only)
    if not redo_all:
        q = q.filter(Document.ai_status.is_(None))
    docs = q.order_by(Document.candidate_id, Document.id).all()
    print(f"{len(docs)} document(s) to check")

    profiles: dict[int, dict] = {}
    tally: Counter = Counter()
    for i, doc in enumerate(docs, 1):
        if doc.candidate_id not in profiles:
            cand = db.query(Candidate).filter(Candidate.id == doc.candidate_id).first()
            profiles[doc.candidate_id] = doc_validator.profile_dict(cand.profile if cand else None)
        path = upload_path(doc.stored_filename)
        if not os.path.isfile(path):
            tally["missing file"] += 1
            print(f"  [{i}/{len(docs)}] #{doc.id} {doc.field_key}: file missing, skipped")
            continue
        verdict = doc_validator.validate_path(path, doc.content_type, doc.original_filename,
                                              doc.field_key, profiles[doc.candidate_id])
        doc_validator.apply_to(doc, verdict)
        tally[verdict.status] += 1
        print(f"  [{i}/{len(docs)}] #{doc.id} c{doc.candidate_id} {doc.field_key:28} "
              f"{verdict.status:10} {verdict.score:3}%  {verdict.doc_type or ''}")
        if i % 20 == 0:
            db.commit()   # keep progress if a later file hangs OCR
    db.commit()
    print("done:", dict(tally))


if __name__ == "__main__":
    main()
