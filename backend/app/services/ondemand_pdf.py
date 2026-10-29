"""On-Demand Candidate form as one PDF: personal and bank details, then the
Aadhaar, PAN and photo uploads drawn in their rows. Same look as the
Document Collection PDF.
"""
from typing import Optional

from app.models import Candidate, FormType
from app.services.doc_pdf import _IDENTITY, _checklist
from app.services.reference_pdf import _Report

_PERSONAL = ("#2563eb", "#eff6ff")      # blue
_BANK = ("#d97706", "#fffbeb")          # amber

# Mirrors frontend/js/field-labels.js.
_PERSONAL_FIELDS = [("full_name", "Name"), ("email", "Email"),
                    ("alternate_email", "Alternate Email"), ("contact_number", "Contact Number"),
                    ("alternate_number", "Alternate Contact Number"),
                    ("linkedin_link", "LinkedIn"), ("date_of_birth", "Date of Birth")]
_BANK_FIELDS = [("bank_account_name", "Name as per Bank"), ("bank_name", "Bank Name"),
                ("bank_account_number", "Account Number"), ("bank_ifsc_code", "IFSC Code"),
                ("bank_account_type", "Savings / Current Account"), ("bank_branch", "Branch")]
_DOCS = [("aadhar_card", "Aadhar Card"), ("pan_card", "Pan Card"),
         ("passport_size_photo", "Candidate Photo")]


def not_ready_reason(candidate: Candidate) -> Optional[str]:
    """Why the PDF can't be generated yet, or None when it can."""
    sub = next((s for s in candidate.submissions if s.form_type == FormType.ONDEMAND), None)
    if sub is None or sub.status.value in ("LOCKED", "PENDING") or not candidate.ondemand_details:
        return "The candidate has not submitted the On-Demand Candidate form yet"
    return None


def build(candidate: Candidate) -> bytes:
    """The form's answers plus every uploaded file, as PDF bytes."""
    details = candidate.ondemand_details
    docs: dict[str, list] = {}
    for d in sorted(candidate.documents, key=lambda d: d.id or 0):
        if d.form_type == FormType.ONDEMAND:
            docs.setdefault(d.field_key, []).append(d)

    rep = _Report("On-Demand Candidate Form")
    rep.banner(details.full_name or candidate.name or "")
    for title, fields, (accent, tint) in (("Personal Details", _PERSONAL_FIELDS, _PERSONAL),
                                          ("Bank Details", _BANK_FIELDS, _BANK)):
        rep.section(title, accent, keep_with=2 * 28)
        for key, label in fields:
            rep.row(label, getattr(details, key), accent, tint)
        rep.gap(16)
    _checklist(rep, "Documents", _DOCS, docs, {k for k, _ in _DOCS}, _IDENTITY)
    return rep.finish()
