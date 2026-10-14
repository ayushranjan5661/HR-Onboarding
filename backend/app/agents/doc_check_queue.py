"""Run the document check after the response, not during it.

OCR of one file can take many seconds (two passes, rotations, up to 14
language packs), and the candidate is never shown the verdict anyway — it is
for HR's checklist. So in "warn" mode a candidate's Save Draft / Submit
stores the file with no verdict (ai_status NULL, which HR sees as "Not
checked yet") and queues the row here; the verdict lands a few seconds later.

"block" mode cannot defer: a mismatch must be refused in the request.

One worker thread: Tesseract already uses several cores per run, and running
checks side by side would only slow every request on the server. The queue
lives in memory — if the server restarts first, the rows simply stay
unchecked and HR's "Re-check documents" picks them up.
"""
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.orm import Session

from app.agents import doc_validator
from app.database import SessionLocal
from app.models import Candidate, Document, FormDraftDocument
from app.utils.file_storage import upload_path

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="doc-check")

_MODELS = {"Document": Document, "FormDraftDocument": FormDraftDocument}


def deferred() -> bool:
    """True when uploads should be checked in the background."""
    return doc_validator.enabled() and not doc_validator.blocking()


def mark(db: Session, row) -> None:
    """Remember a just-added row for `flush_pending` to queue after commit."""
    db.info.setdefault("doc_checks", []).append(row)


def flush_pending(db: Session) -> None:
    """Queue every row `mark`ed on this session. Call only after commit, so
    the worker's own session can see the rows."""
    for row in db.info.pop("doc_checks", []):
        try:
            row_id = row.id
        except Exception:      # row was deleted / rolled back
            continue
        if row_id is not None:
            _executor.submit(_check, type(row).__name__, row_id)


def _check(model_name: str, row_id: int) -> None:
    db = SessionLocal()
    try:
        row = db.get(_MODELS[model_name], row_id)
        if row is None or row.ai_checked_at is not None:
            return             # gone (draft discarded / replaced), or already checked
        candidate = db.get(Candidate, row.candidate_id)
        verdict = doc_validator.validate_path(
            upload_path(row.stored_filename), row.content_type, row.original_filename or "",
            row.field_key, doc_validator.profile_dict(candidate.profile if candidate else None))
        verdict.blocked = False  # nothing to refuse after the fact
        # Re-select (not db.get, which answers from the identity map): the row
        # may have been deleted while OCR ran.
        model = _MODELS[model_name]
        if db.query(model.id).filter(model.id == row_id).first() is None:
            return
        doc_validator.apply_to(row, verdict)
        db.commit()
    except Exception as exc:   # a failed check must never take the worker down
        db.rollback()
        print(f"[doc_check_queue] {model_name} {row_id}: {type(exc).__name__}: {exc}")
    finally:
        db.close()
