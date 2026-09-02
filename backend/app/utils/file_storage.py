import os
import re
import secrets

from fastapi import HTTPException, UploadFile

from app.config import settings

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")

# Only images, PDFs and Word documents may be uploaded. Enforced here (not
# just via the frontend's <input accept> hint, which anyone can bypass) by
# checking BOTH the extension and the browser-declared MIME type, and then
# sniffing the actual file header — a renamed .exe won't pass the magic-byte
# check even if its extension and declared type are faked.
_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf", ".doc", ".docx"}
_ALLOWED_CONTENT_TYPES = {
    "image/jpeg", "image/png", "image/gif", "image/webp",
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _sniff_matches_extension(ext: str, head: bytes) -> bool:
    """Does the file's actual content match what its extension claims?"""
    if ext in (".jpg", ".jpeg"):
        return head.startswith(b"\xff\xd8\xff")
    if ext == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if ext == ".gif":
        return head.startswith((b"GIF87a", b"GIF89a"))
    if ext == ".webp":
        return head.startswith(b"RIFF") and head[8:12] == b"WEBP"
    if ext == ".pdf":
        return head.startswith(b"%PDF-")
    if ext == ".docx":
        return head.startswith(b"PK\x03\x04")          # .docx is a zip archive
    if ext == ".doc":
        return head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")  # OLE compound file
    return False


def _safe_ext(filename: str) -> str:
    ext = os.path.splitext(filename)[1]
    return _SAFE_CHARS.sub("", ext)[:10]


def _validate_file_type(filename: str, content_type: str | None, contents: bytes) -> None:
    ext = _safe_ext(filename).lower()
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Only images (JPG/PNG/GIF/WEBP), PDF, and Word documents (.doc/.docx) are allowed.",
        )
    if content_type and content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"File type '{content_type}' is not allowed. "
                    "Only images, PDF, and Word documents are accepted.",
        )
    if not _sniff_matches_extension(ext, contents[:16]):
        raise HTTPException(
            status_code=400,
            detail="This file's content doesn't match its extension — it may be "
                    "corrupted or mislabeled. Please upload a genuine image, PDF, or Word document.",
        )


def save_upload(file: UploadFile, candidate_id: int, form_type: str, field_key: str) -> tuple[str, str]:
    """Saves an uploaded file to disk and returns (original_filename, stored_filename).

    Files are stored with a neutral .dat extension: the original filename and
    content type live in the documents table and are restored on download.
    (Endpoint-protection software on managed Windows machines intermittently
    denies unknown processes writing media extensions like .jpg — storing
    neutral names sidesteps that entirely.)
    """
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)

    # Read in chunks and stop at the cap — never buffer an arbitrarily large
    # body into memory just to find out it is over the limit.
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    buf = bytearray()
    while chunk := file.file.read(1024 * 1024):
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise HTTPException(status_code=413, detail=f"File too large (max {settings.MAX_UPLOAD_MB} MB)")
    contents = bytes(buf)
    _validate_file_type(file.filename or "", file.content_type, contents)

    stored_name = f"{candidate_id}_{form_type}_{field_key}_{secrets.token_hex(6)}.dat"
    path = os.path.join(settings.UPLOAD_DIR, stored_name)
    with open(path, "wb") as f:
        f.write(contents)

    return file.filename or stored_name, stored_name


def upload_path(stored_filename: str) -> str:
    return os.path.join(settings.UPLOAD_DIR, stored_filename)


def save_bytes(data: bytes, candidate_id: int, form_type: str, field_key: str) -> str:
    """Store raw bytes as a new upload and return its stored filename.
    Used when a document is carried over from an earlier form."""
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    stored_name = f"{candidate_id}_{form_type}_{field_key}_{secrets.token_hex(6)}.dat"
    with open(os.path.join(settings.UPLOAD_DIR, stored_name), "wb") as f:
        f.write(data)
    return stored_name


def snapshot_document(db, doc) -> "DocumentSnapshot":
    """Pin a stored file so the audit trail can still open it after the
    document it belongs to has been replaced or removed.

    One row per stored file: the file that is the "new" side of one change is
    the same row referenced as the "old" side of the next, so a chain of
    replacements does not accumulate duplicate rows.
    """
    from app.models import DocumentSnapshot

    existing = db.query(DocumentSnapshot).filter(
        DocumentSnapshot.stored_filename == doc.stored_filename).first()
    if existing:
        return existing
    snapshot = DocumentSnapshot(
        candidate_id=doc.candidate_id,
        form_type=doc.form_type.value if hasattr(doc.form_type, "value") else doc.form_type,
        field_key=doc.field_key,
        original_filename=doc.original_filename,
        stored_filename=doc.stored_filename,
        content_type=doc.content_type,
    )
    db.add(snapshot)
    db.flush()   # the caller needs its id for the audit entry
    return snapshot
