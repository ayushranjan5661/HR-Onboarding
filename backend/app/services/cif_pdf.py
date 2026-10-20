"""Candidate Details (CIF) as a PDF: profile fields, CIF answers, then each
education / employment / reference entry as its own block. Reuses the
Referee Check report's drawing primitives so both PDFs look alike.
"""
import os

import pymupdf

from app.models import Candidate, FormType
from app.services.reference_pdf import _LEFT, _LINE, _RIGHT, _Report, _rgb
from app.utils.file_storage import upload_path

_PERSONAL = ("#0d9488", "#f0fdfa")      # teal, matches the CIF card accent
_POSITION = ("#4f46e5", "#eef2ff")      # indigo
_EDUCATION = ("#7c3aed", "#f5f3ff")     # violet
_EMPLOYMENT = ("#db2777", "#fdf2f8")    # pink
_REFERENCES = ("#d97706", "#fffbeb")    # amber

# Mirrors frontend/js/field-labels.js (the labels HR sees on the CIF card).
_PROFILE = [
    ("full_name", "Full Name"), ("email", "Email"), ("contact_number", "Mobile Number"),
    ("alternate_number", "Alternate Contact"), ("date_of_birth", "Date of Birth"),
    ("gender", "Gender"), ("current_address", "Present/Communication Address"),
    ("permanent_address", "Permanent Address"), ("pan_number", "PAN Number"),
    ("aadhaar_number", "Aadhaar Number"),
]
_CIF_SECTIONS = [
    ("Position Details", _POSITION, [
        ("position_applied_for", "Position Applied For"),
        ("skills_technologies", "Skill/Technologies Worked"),
    ]),
    ("Other Details", _PERSONAL, [
        ("alternate_email", "Alternate E-Mail ID"), ("blood_group", "Blood Group"),
        ("linkedin_link", "LinkedIn Page Link"), ("marital_status", "Marital Status"),
        ("any_backlogs", "Any Backlogs in the Education"), ("source", "Source"),
        ("worked_in_levelshift_before", "Worked in LevelShift Before"),
        ("levelshift_employee_id", "Previous LevelShift Employee ID"),
        ("levelshift_experience_yrs", "Years Worked at LevelShift"),
    ]),
    ("Experience & Compensation", _POSITION, [
        ("total_experience_yrs", "Total Experience (Yrs)"),
        ("relevant_skill_exp_yrs", "Relevant Skill Exp. (Yrs)"),
        ("current_ctc_lpa", "Current CTC (LPA)"), ("expected_ctc_lpa", "Expected CTC (LPA)"),
        ("additional_allowance", "Additional Allowance (Rs)"),
        ("variable_comp", "Variable Comp (Rs)"), ("notice_period_days", "Notice Period (Days)"),
        ("other_offers", "Holds Other Offers"),
    ]),
    ("About the Candidate", _PERSONAL, [
        ("technical_certifications", "Technical Certifications"),
        ("understanding_of_levelshift", "Understanding of LevelShift"),
        ("aspirations", "Aspirations"),
    ]),
    ("Declaration", _POSITION, [
        ("declaration_accepted", "Declaration Accepted"),
        ("declaration_place", "Declaration Place"), ("declaration_date", "Declaration Date"),
    ]),
]
_EDU_SECTIONS = [("UG_PG", "UG and PG Education Details"),
                 ("12TH", "12th / Diploma Education Details"),
                 ("10TH", "10th Education Details")]
_EDU_COLS = [("qualification", "Qualification"), ("college_name", "College/University Name"),
             ("specialization", "Specialization"), ("cgpa_percent", "% CGPA"),
             ("year_of_passing", "Year of Passing"), ("has_marksheet", "Has Mark Sheet"),
             ("gaps", "Gaps")]
_EMP_COLS = [("company_name", "Company"), ("position_held", "Position Held"),
             ("from_date", "From"), ("to_date", "To"), ("currently_working", "Currently Working"),
             ("reason_for_leaving", "Reason for Leaving"), ("offer_letter", "Offer Letter"),
             ("relieving_letter_status", "Relieving Letter"),
             ("experience_certificate", "Experience Certificate"), ("gaps", "Gaps")]
_REF_COLS = [("employee_name", "Employee Name"), ("email_id", "Email ID"),
             ("technology", "Technology"), ("experience", "Experience"),
             ("contact_number", "Contact Number")]


def not_ready_reason(candidate: Candidate):
    """Why the PDF can't be generated yet, or None when it can."""
    if not candidate.cif_details:
        return "The candidate has not submitted the CIF yet"
    return None


_IMAGES = [("profile_picture", "Candidate Profile Picture"), ("signature", "Signature")]
_IMG_H = 150          # height of each image box


def _image_stream(doc) -> bytes | None:
    """The upload as PNG/JPEG bytes MuPDF can place; a PDF upload gives its
    first page. None when the file is missing or unreadable."""
    path = upload_path(doc.stored_filename)
    if not os.path.isfile(path):
        return None
    try:
        with pymupdf.open(path) as src:
            if src.is_pdf:
                return src[0].get_pixmap(dpi=150).tobytes("png")
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        return None


def _images(rep: _Report, candidate: Candidate, colors) -> None:
    """Profile picture and signature side by side, each in a labelled box."""
    docs = {d.field_key: d for d in candidate.documents
            if d.form_type == FormType.CIF and d.field_key in dict(_IMAGES)}
    accent, tint = colors
    rep.section("Photo & Signature", accent, keep_with=_IMG_H + 24)
    gap = 16
    w = (_RIGHT - _LEFT - gap) / 2
    top = rep.y + 4
    for i, (key, label) in enumerate(_IMAGES):
        x = _LEFT + i * (w + gap)
        rep._text(x, top + 10, label, "hebo", 8.8, accent)
        box = pymupdf.Rect(x, top + 16, x + w, top + 16 + _IMG_H)
        rep.page.draw_rect(box, color=_rgb(_LINE), fill=_rgb(tint), width=0.7, radius=0.04)
        data = _image_stream(docs[key]) if key in docs else None
        placed = False
        if data:
            try:
                rep.page.insert_image(box + (8, 8, -8, -8), stream=data, keep_proportion=True)
                placed = True
            except Exception:
                pass
        if not placed:
            msg = "Not provided" if key not in docs else "Preview unavailable"
            rep._text(box.x0 + 10, box.y0 + 20, msg, "heit", 10, "#9ca3af")
    rep.y = top + 16 + _IMG_H + 20


def _rows(rep: _Report, obj, fields, colors) -> None:
    accent, tint = colors
    for key, label in fields:
        rep.row(label, getattr(obj, key, None) if obj is not None else None, accent, tint)


def _entries(rep: _Report, title: str, entries, cols, colors, noun: str) -> None:
    rep.section(title, colors[0])
    if not entries:
        rep.note("No entries provided.", "#6b7280", "#f9fafb")
    for i, e in enumerate(entries, 1):
        if len(entries) > 1:
            rep.subheading(f"{noun} {i}", colors[0])
        _rows(rep, e, cols, colors)
    rep.gap(16)


def build(candidate: Candidate) -> bytes:
    """The CIF as PDF bytes. Caller ensures cif_details exists."""
    p, d = candidate.profile, candidate.cif_details
    name = (p.full_name if p else None) or candidate.name or ""

    rep = _Report("Candidate Details (CIF)")
    rep.banner(name)
    _images(rep, candidate, _PERSONAL)

    rep.section("Personal Details", _PERSONAL[0], keep_with=4 * 28)
    _rows(rep, p, _PROFILE, _PERSONAL)
    rep.gap(16)
    # The LevelShift follow-ups are only asked when they answered "Yes".
    skip = set() if str(getattr(d, "worked_in_levelshift_before", "") or "").strip().lower() == "yes" \
        else {"levelshift_employee_id", "levelshift_experience_yrs"}
    for title, colors, fields in _CIF_SECTIONS:
        fields = [f for f in fields if f[0] not in skip]
        rep.section(title, colors[0])
        _rows(rep, d, fields, colors)
        rep.gap(16)

    by_id = lambda rows: sorted(rows, key=lambda r: r.id or 0)
    for section, title in _EDU_SECTIONS:
        _entries(rep, title, by_id(e for e in candidate.education if e.section == section),
                 _EDU_COLS, _EDUCATION, "Entry")
    _entries(rep, "Employment Details", by_id(candidate.employment), _EMP_COLS, _EMPLOYMENT, "Employer")
    _entries(rep, "References", by_id(candidate.references), _REF_COLS, _REFERENCES, "Reference")

    return rep.finish()
