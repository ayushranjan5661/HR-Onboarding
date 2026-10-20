"""Document Collection form as one PDF: every document field with the
uploaded file drawn right in its row — images as they are, PDFs page by page
(kept vector, so text stays sharp). Files MuPDF can't open (e.g. .docx) get a
note instead. Same look as the CIF / Referee PDFs.
"""
import os
from typing import Optional

import pymupdf

from app.form_definitions import DOC_REQUIRED_BY_TYPE
from app.models import Candidate, FormType
from app.services.reference_pdf import (_KEY_W, _LEFT, _LH, _PAD, _RIGHT, _Report,
                                        _wrap)
from app.utils.file_storage import upload_path

_EMPLOYMENT = ("#db2777", "#fdf2f8")    # pink
_EDUCATION = ("#7c3aed", "#f5f3ff")     # violet
_IDENTITY = ("#0d9488", "#f0fdfa")      # teal
_MISSING = "#dc2626"

_EMP_DOCS = [("offer_letter", "Offer Letter"), ("role_change_letter", "Role Change Letter"),
             ("experience_letter", "Experience Letter"), ("relieving_letter", "Relieving Letter"),
             ("pay_slips", "Last 3 Months Pay Slips")]
_EMP_BLOCKS = [("cc", "Current Company")] + [(f"pc{i}", f"Previous Company {i}") for i in (1, 2, 3, 4)]
# Mirrors frontend/js/field-labels.js.
_EDU = [("marksheet_10", "10th Marks Sheet"), ("marksheet_12_diploma", "12th Marks Sheet / Diploma"),
        ("ug_consolidated_marksheet", "UG – Consolidated Marksheet"), ("ug_certificate", "UG Certificate"),
        ("pg_consolidated_marksheet", "PG – Consolidated Marksheet"), ("pg_certificate", "PG Certificate"),
        ("other_edu_marksheet", "Other Educational Marksheet"),
        ("other_edu_certificate", "Other Educational Certificate"),
        ("additional_certifications", "Additional Certifications")]
_ID = [("passport_size_photo", "Passport Size Photo"), ("pan_card", "Pan Card"),
       ("aadhar_card", "Aadhar Card"), ("passport_copy", "Passport Copy"),
       ("address_proof", "Address Proof"), ("id_proof", "ID Proof")]


def not_ready_reason(candidate: Candidate) -> Optional[str]:
    """Why the PDF can't be generated yet, or None when it can."""
    sub = next((s for s in candidate.submissions
                if s.form_type == FormType.DOCUMENT_COLLECTION), None)
    if sub is None or sub.status.value in ("LOCKED", "PENDING"):
        return "The candidate has not submitted the Document Collection form yet"
    return None


def _uploads(candidate: Candidate) -> dict[str, list]:
    """field_key -> its uploaded documents, oldest first."""
    out: dict[str, list] = {}
    for d in sorted(candidate.documents, key=lambda d: d.id or 0):
        if d.form_type == FormType.DOCUMENT_COLLECTION:
            out.setdefault(d.field_key, []).append(d)
    return out


_MAX_H = 300                              # tallest a single image may be drawn
_VAL_W = _RIGHT - _LEFT - _KEY_W - 2 * _PAD  # width of the value column


def _pages(doc):
    """Open the upload; returns (src, page indexes) or (None, []) when it
    can't be drawn (missing file, or a format such as Word)."""
    path = upload_path(doc.stored_filename)
    name = (doc.original_filename or "").lower()
    is_image = (doc.content_type or "").startswith("image/")         or name.rsplit(".", 1)[-1] in ("jpg", "jpeg", "png", "gif", "webp", "bmp")
    try:
        src = pymupdf.open(path) if os.path.isfile(path) else None
    except Exception:
        return None, []
    if src is None or not (src.is_pdf or is_image):
        if src is not None:
            src.close()
        return None, []
    return src, list(range(src.page_count))


def _image_row(rep: _Report, key_lines, accent, tint, src, pno) -> None:
    """One row holding one page/image, scaled to the value column width."""
    r = src[pno].rect
    w = min(_VAL_W, r.width)
    h = min(_MAX_H, w * r.height / r.width if r.width else _MAX_H)
    top = rep._row_frame(key_lines, accent, tint, max(h, len(key_lines) * 10.0 * _LH))
    box = pymupdf.Rect(_LEFT + _KEY_W + _PAD, top + _PAD, _RIGHT - _PAD, top + _PAD + h)
    try:
        if src.is_pdf:
            rep.page.show_pdf_page(box, src, pno, keep_proportion=True)
        else:
            with open(src.name, "rb") as f:
                rep.page.insert_image(box, stream=f.read(), keep_proportion=True)
    except Exception:
        rep._text(box.x0, box.y0 + 12, "Preview unavailable", "heit", 10, "#9ca3af")


def _checklist(rep: _Report, title: str, fields, docs, required, colors) -> None:
    accent, tint = colors
    rep.section(title, accent, keep_with=2 * 28)
    for key, label in fields:
        files = docs.get(key) or []
        if not files:
            if key in required:
                rep.row(f"{label} (Required)", "Missing", _MISSING, "#fef2f2")
            else:
                rep.row(label, None, accent, tint)
            continue
        key_lines = _wrap(label, "hebo", 8.8, _KEY_W - 2 * _PAD)
        for doc in files:
            src, pages = _pages(doc)
            if src is None:
                rep.row(label if key_lines else "", f"{doc.original_filename} — can't be shown "
                        "inside the PDF; download it from the HR portal.", accent, tint)
                key_lines = []
                continue
            with src:
                for pno in pages:
                    _image_row(rep, key_lines, accent, tint, src, pno)
                    key_lines = []     # label only beside the first image
    rep.gap(16)


def build(candidate: Candidate) -> bytes:
    """The checklist plus every uploaded file, as PDF bytes."""
    docs = _uploads(candidate)
    required = set(DOC_REQUIRED_BY_TYPE.get(candidate.candidate_type.value, []))
    name = (candidate.profile.full_name if candidate.profile else None) or candidate.name or ""

    rep = _Report("Document Collection Form")
    rep.banner(name)

    # Employment: only the companies the candidate uploaded something for.
    sections = []
    for prefix, company in _EMP_BLOCKS:
        fields = [(f"{prefix}_{k}", lbl) for k, lbl in _EMP_DOCS]
        if any(k in docs for k, _ in fields):
            sections.append((company, fields, _EMPLOYMENT))
    if not sections:
        rep.section("Employment Documents", _EMPLOYMENT[0])
        rep.note("No employment documents uploaded.", "#6b7280", "#f9fafb")
        rep.gap(16)
    sections += [("Education Documents", _EDU, _EDUCATION),
                 ("Identity & Address Documents", _ID, _IDENTITY)]
    for title, fields, colors in sections:
        _checklist(rep, title, fields, docs, required, colors)

    return rep.finish()
