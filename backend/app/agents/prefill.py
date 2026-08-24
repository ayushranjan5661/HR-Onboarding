"""
Turns the mapping agent's field equivalences into concrete pre-fill data for
a candidate, and carries their uploaded documents forward.

Two things happen here:
  * pre-fill      — values and files the next form can inherit, shown to the
                     candidate so they can see and override them.
  * carry-over    — on submit, any mapped upload the candidate left blank is
                     satisfied by copying the file they already gave us.

Resolved mappings are cached in-process: the form schemas only change when the
code does, so there is no reason to call the LLM per request.
"""
from __future__ import annotations

import shutil
import time

from sqlalchemy.orm import Session

from app.agents import field_mapper
from app.config import settings
from app.models import (
    BGVDetails,
    CandidateProfile,
    CIFDetails,
    DocCollectionDetails,
    Document,
    EmploymentDetail,
    FormType,
)
from app.utils.file_storage import save_bytes, upload_path

_CACHE: dict | None = None
_CACHE_AT: float = 0.0
_CACHE_TTL = 60 * 60 * 6  # schemas are static; refresh a few times a day


def mappings(refresh: bool = False) -> dict:
    global _CACHE, _CACHE_AT
    if refresh or _CACHE is None or (time.time() - _CACHE_AT) > _CACHE_TTL:
        _CACHE = field_mapper.resolve_all(use_llm=settings.AI_MAPPING_ENABLED)
        _CACHE_AT = time.time()
    return _CACHE


def _for(target_form: str, kind: str) -> list[dict]:
    return mappings().get(target_form, {}).get(kind, [])


# ---------------------------------------------------------------------------
# Reading a mapped source value
# ---------------------------------------------------------------------------
def _source_value(db: Session, candidate_id: int, source_form: str, key: str):
    """A mapped field's current value. CIF keys may live on the shared profile
    or on cif_details, so both are consulted."""
    if source_form == "CIF":
        profile = db.query(CandidateProfile).filter(
            CandidateProfile.candidate_id == candidate_id).first()
        if profile is not None and hasattr(profile, key):
            val = getattr(profile, key)
            if val not in (None, ""):
                return val
        cif = db.query(CIFDetails).filter(CIFDetails.candidate_id == candidate_id).first()
        if cif is not None and hasattr(cif, key):
            return getattr(cif, key)
        return None
    if source_form == "DOCUMENT_COLLECTION":
        row = db.query(DocCollectionDetails).filter(
            DocCollectionDetails.candidate_id == candidate_id).first()
        return getattr(row, key, None) if row is not None else None
    if source_form == "BGV":
        row = db.query(BGVDetails).filter(BGVDetails.candidate_id == candidate_id).first()
        return getattr(row, key, None) if row is not None else None
    return None


def _source_document(db: Session, candidate_id: int, source_form: str, key: str):
    return (db.query(Document)
              .filter(Document.candidate_id == candidate_id,
                       Document.form_type == FormType(source_form),
                       Document.field_key == key)
              .order_by(Document.id.desc())
              .first())


# ---------------------------------------------------------------------------
# What the candidate's form should show
# ---------------------------------------------------------------------------
def build_prefill(db: Session, candidate_id: int, target_form: str) -> dict:
    """Values and files the target form can inherit, each labelled with where
    it came from so the candidate knows what was filled in for them."""
    fields, documents = {}, []

    for m in _for(target_form, "field"):
        val = _source_value(db, candidate_id, m["source_form"], m["source_field"])
        if val not in (None, ""):
            fields[m["target_field"]] = {
                "value": val, "source_form": m["source_form"],
                "source_field": m["source_field"], "confidence": m["confidence"],
                "decided_by": m["decided_by"],
            }

    already = {d.field_key for d in db.query(Document).filter(
        Document.candidate_id == candidate_id,
        Document.form_type == FormType(target_form)).all()}

    for m in _for(target_form, "document"):
        if m["target_field"] in already:
            continue                      # candidate already has one here
        doc = _source_document(db, candidate_id, m["source_form"], m["source_field"])
        if not doc:
            continue
        documents.append({
            "target_field": m["target_field"], "source_form": m["source_form"],
            "source_field": m["source_field"], "source_document_id": doc.id,
            "original_filename": doc.original_filename,
            "content_type": doc.content_type,
            "available": True, "confidence": m["confidence"],
            "decided_by": m["decided_by"],
        })

    return {"fields": fields, "documents": documents,
            "company_labels": company_labels(db, candidate_id)}


def company_labels(db: Session, candidate_id: int) -> dict:
    """Name the Document Collection's company sections using the employers the
    candidate already listed on their CIF, so "Previous company 1" reads as the
    actual company instead of a placeholder."""
    rows = (db.query(EmploymentDetail)
              .filter(EmploymentDetail.candidate_id == candidate_id)
              .order_by(EmploymentDetail.id).all())
    if not rows:
        return {}

    current = [r for r in rows if (r.currently_working or "").strip().lower() == "yes"]
    previous = [r for r in rows if r not in current]
    labels = {}
    if current:
        labels["cc"] = current[0].company_name or ""
    elif rows:
        # Nobody flagged as current: treat the most recent as the current one.
        labels["cc"] = rows[-1].company_name or ""
        previous = rows[:-1]
    for i, row in enumerate(previous[:4], start=1):
        labels[f"pc{i}"] = row.company_name or ""
    return {k: v for k, v in labels.items() if v}


# ---------------------------------------------------------------------------
# Carrying documents forward on submit
# ---------------------------------------------------------------------------
def carry_documents(db: Session, candidate_id: int, target_form: str,
                     provided: set[str]) -> list[str]:
    """Copy any mapped upload the candidate did not provide this time.

    The file is duplicated rather than shared, so deleting one form's copy can
    never remove another form's evidence. Returns the fields that were filled.
    """
    carried = []
    existing = {d.field_key for d in db.query(Document).filter(
        Document.candidate_id == candidate_id,
        Document.form_type == FormType(target_form)).all()}

    for m in _for(target_form, "document"):
        target = m["target_field"]
        if target in provided or target in existing:
            continue
        src = _source_document(db, candidate_id, m["source_form"], m["source_field"])
        if not src:
            continue
        src_path = upload_path(src.stored_filename)
        try:
            with open(src_path, "rb") as fh:
                data = fh.read()
        except OSError:
            continue                      # source file gone; nothing to carry
        stored = save_bytes(data, candidate_id, target_form, target)
        db.add(Document(candidate_id=candidate_id, form_type=FormType(target_form),
                         field_key=target, original_filename=src.original_filename,
                         stored_filename=stored, content_type=src.content_type))
        carried.append(target)
    return carried
