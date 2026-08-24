import os
import re
import secrets

from fastapi import HTTPException, UploadFile

from app.config import settings

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_ext(filename: str) -> str:
    ext = os.path.splitext(filename)[1]
    return _SAFE_CHARS.sub("", ext)[:10]


def save_upload(file: UploadFile, candidate_id: int, form_type: str, field_key: str) -> tuple[str, str]:
    """Saves an uploaded file to disk and returns (original_filename, stored_filename).

    Files are stored with a neutral .dat extension: the original filename and
    content type live in the documents table and are restored on download.
    (Endpoint-protection software on managed Windows machines intermittently
    denies unknown processes writing media extensions like .jpg — storing
    neutral names sidesteps that entirely.)
    """
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)

    contents = file.file.read()
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    if len(contents) > max_bytes:
        raise HTTPException(status_code=413, detail=f"File too large (max {settings.MAX_UPLOAD_MB} MB)")

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
