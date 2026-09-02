"""Field-level edit access for already-submitted forms.

Every form — CIF, Document Collection, BGV — is read-only to the candidate the
moment it is submitted. If something later turns out to be wrong, HR opens
*that one value* rather than the whole form. The candidate changes it, gives a
reason, and the change is written to the audit log.

Three kinds of value can be opened, and between them they cover every column
of every form:

    FIELD      a column on a one-row detail table (candidate_profiles,
               cif_details, bgv_details, doc_collection_details)
    DOCUMENT   one uploaded file on a form
    ROW_FIELD  one column of one entry in a repeating section (an education
               row, an employment row, a BGV address, ...)

This module owns the rules shared by both routers: what may be opened, and
where a granted value actually lives.
"""

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.form_definitions import (
    BGV_ADDRESS_COLUMNS,
    BGV_EDUCATION_COLUMNS,
    BGV_EMPLOYMENT_COLUMNS,
    BGV_FIELDS,
    BGV_FILE_FIELDS,
    BGV_GAP_COLUMNS,
    BGV_REFERENCE_COLUMNS,
    CIF_FIELDS,
    CIF_FILE_FIELDS,
    DOC_FIELDS,
    DOC_FILE_FIELDS,
    EDUCATION_COLUMNS,
    EMPLOYMENT_COLUMNS,
    PROFILE_FIELDS,
    REFERENCE_COLUMNS,
)
from app.models import (
    BGVAddressHistory,
    BGVDetails,
    BGVEducationCheck,
    BGVEmploymentCheck,
    BGVGap,
    BGVReferenceCheck,
    Candidate,
    CandidateProfile,
    CIFDetails,
    DocCollectionDetails,
    EditPermissionStatus,
    EducationDetail,
    EmploymentDetail,
    FieldEditPermission,
    FormStatus,
    FormSubmission,
    FormType,
    ReferenceDetail,
)

# The three forms a candidate fills in. Each locks on submission.
FORMS = ("CIF", "DOCUMENT_COLLECTION", "BGV")

# Filled in by HR for their own tracking, never by the candidate — opening
# these to the candidate would make no sense.
_HR_ONLY_CIF_FIELDS = {"hr_candidate_id", "hr_candidate_email"}

# Storage key -> the columns HR may open. "PROFILE" and "CIF" are two tables
# behind one form: both are shown to the candidate as part of their CIF.
GRANTABLE_FIELDS: dict[str, list[str]] = {
    "PROFILE": list(PROFILE_FIELDS),
    "CIF": [f for f in CIF_FIELDS if f not in _HR_ONLY_CIF_FIELDS],
    "BGV": list(BGV_FIELDS),
    "DOCUMENT_COLLECTION": list(DOC_FIELDS),   # uploads only — no text columns
}

# Uploads that may be replaced under a grant.
GRANTABLE_DOCUMENTS: dict[str, list[str]] = {
    "CIF": list(CIF_FILE_FIELDS),
    "BGV": list(BGV_FILE_FIELDS),
    "DOCUMENT_COLLECTION": list(DOC_FILE_FIELDS),
}

# Repeating sections: table key -> (model, editable columns, owning form,
# section title, the column that best identifies one entry to a human).
ROW_TABLES: dict[str, tuple] = {
    "education": (EducationDetail, EDUCATION_COLUMNS, "CIF", "Education", "qualification"),
    "employment": (EmploymentDetail, EMPLOYMENT_COLUMNS, "CIF", "Employment", "company_name"),
    "references": (ReferenceDetail, REFERENCE_COLUMNS, "CIF", "Reference", "employee_name"),
    "bgv_address_history": (BGVAddressHistory, BGV_ADDRESS_COLUMNS, "BGV",
                             "Address History", "city"),
    "bgv_education_checks": (BGVEducationCheck, BGV_EDUCATION_COLUMNS, "BGV",
                              "Education Verification", "qualification"),
    "bgv_employment_checks": (BGVEmploymentCheck, BGV_EMPLOYMENT_COLUMNS, "BGV",
                               "Employment Verification", "company_name"),
    "bgv_reference_checks": (BGVReferenceCheck, BGV_REFERENCE_COLUMNS, "BGV",
                              "Professional Reference", "name"),
    "bgv_gaps": (BGVGap, BGV_GAP_COLUMNS, "BGV", "Gap", "gap_type"),
}

# Education rows are split into three sections on the form; say which.
_EDUCATION_SECTION_TITLES = {
    "UG_PG": "UG / PG Education",
    "12TH": "12th / Diploma Education",
    "10TH": "10th Education",
}

# Which detail table each storage key maps to.
_DETAIL_MODELS = {
    "PROFILE": CandidateProfile,
    "CIF": CIFDetails,
    "BGV": BGVDetails,
    "DOCUMENT_COLLECTION": DocCollectionDetails,
}

# A storage key belongs to exactly one form the candidate fills in. Profile
# columns are collected on the CIF, so that is where HR grants them.
OWNING_FORM = {
    "PROFILE": "CIF",
    "CIF": "CIF",
    "BGV": "BGV",
    "DOCUMENT_COLLECTION": "DOCUMENT_COLLECTION",
}


# How each form is named to a human, in errors and in the portals.
FORM_TITLES = {
    "CIF": "Candidate Information Form",
    "DOCUMENT_COLLECTION": "Document Collection",
    "BGV": "Background Verification",
}


def owning_form(form_type: str) -> str:
    return OWNING_FORM.get(form_type, form_type)


def form_title(form: str) -> str:
    return FORM_TITLES.get(form, form.replace("_", " "))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_target(db: Session, candidate_id: int, form_type: str, field_kind: str,
                     field_name: str, row_table: str | None = None,
                     row_id: int | None = None) -> None:
    """Reject anything outside the grantable surface, so a crafted request can
    never unlock a column HR is not allowed to hand over — or one row entry
    through another candidate's grant."""
    if field_kind == "ROW_FIELD":
        entry = ROW_TABLES.get(row_table or "")
        if not entry:
            raise HTTPException(status_code=400, detail="Unknown repeating section")
        model, columns, form, _, _ = entry
        if field_name not in columns:
            raise HTTPException(
                status_code=400,
                detail=f"'{field_name}' is not a column of '{row_table}'")
        if form != owning_form(form_type):
            raise HTTPException(status_code=400,
                                 detail=f"'{row_table}' does not belong to the {form_type} form")
        row = db.query(model).filter(model.id == row_id,
                                      model.candidate_id == candidate_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="That entry no longer exists")
        return

    if field_kind == "DOCUMENT":
        allowed = GRANTABLE_DOCUMENTS.get(form_type, [])
    elif field_kind == "FIELD":
        allowed = GRANTABLE_FIELDS.get(form_type, [])
    else:
        raise HTTPException(status_code=400,
                             detail="field_kind must be FIELD, DOCUMENT or ROW_FIELD")
    if field_name not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"'{field_name}' is not a field that can be opened for candidate editing",
        )


def detail_row(db: Session, form_type: str, candidate_id: int):
    """The row holding a granted column's value, or None if the candidate has
    not submitted that part of the form yet."""
    model = _DETAIL_MODELS.get(form_type)
    if not model:
        raise HTTPException(status_code=400, detail="Unknown form")
    return db.query(model).filter(model.candidate_id == candidate_id).first()


def table_row(db: Session, row_table: str, candidate_id: int, row_id: int):
    """One entry of a repeating section, scoped to its own candidate."""
    entry = ROW_TABLES.get(row_table or "")
    if not entry:
        raise HTTPException(status_code=400, detail="Unknown repeating section")
    model = entry[0]
    return db.query(model).filter(model.id == row_id,
                                   model.candidate_id == candidate_id).first()


# ---------------------------------------------------------------------------
# Form state
# ---------------------------------------------------------------------------

def submitted_forms(db: Session, candidate_id: int) -> dict[str, bool]:
    """Which forms the candidate has submitted. A value can only be *re*-opened
    once there is a submitted answer to correct — before that the candidate
    still has the whole form."""
    statuses = {
        s.form_type.value: s.status
        for s in db.query(FormSubmission).filter(
            FormSubmission.candidate_id == candidate_id).all()
    }
    return {
        form: statuses.get(form) not in (None, FormStatus.LOCKED, FormStatus.PENDING)
        for form in FORMS
    }


def form_is_submitted(db: Session, candidate_id: int, form: str) -> bool:
    return submitted_forms(db, candidate_id).get(form, False)


# ---------------------------------------------------------------------------
# What HR may open, and what a grant currently points at
# ---------------------------------------------------------------------------

def _row_label(row_table: str, row, index: int) -> str:
    """A human handle for one entry, e.g. "Employment #2 — Acme Ltd"."""
    _, _, _, title, name_column = ROW_TABLES[row_table]
    if row_table == "education":
        title = _EDUCATION_SECTION_TITLES.get(row.section, title)
    name = (getattr(row, name_column, None) or "").strip()
    return f"{title} #{index}" + (f" — {name}" if name else "")


def grantable_items(db: Session, candidate_id: int) -> dict[str, list[dict]]:
    """Every value HR could open for this candidate, grouped by the form the
    candidate fills it in on. Repeating rows are listed per existing entry —
    HR opens one cell of one entry, not a whole table."""
    items: dict[str, list[dict]] = {form: [] for form in FORMS}

    def item(storage, kind, name, row_table=None, row_id=None, row_label=None):
        # Every item carries the same shape, row or not, so callers never have
        # to special-case a missing key.
        return {"form_type": storage, "field_kind": kind, "field_name": name,
                "row_table": row_table, "row_id": row_id, "row_label": row_label}

    for storage, names in GRANTABLE_FIELDS.items():
        for name in names:
            items[owning_form(storage)].append(item(storage, "FIELD", name))

    for storage, names in GRANTABLE_DOCUMENTS.items():
        for name in names:
            items[owning_form(storage)].append(item(storage, "DOCUMENT", name))

    for row_table, (model, columns, form, _, _) in ROW_TABLES.items():
        rows = (db.query(model).filter(model.candidate_id == candidate_id)
                  .order_by(model.id.asc()).all())
        for index, row in enumerate(rows, start=1):
            label = _row_label(row_table, row, index)
            for column in columns:
                items[form].append(
                    item(form, "ROW_FIELD", column, row_table, row.id, label))

    return items


def row_label_for(db: Session, candidate_id: int, permission: FieldEditPermission) -> str | None:
    """The label a granted row entry had when it was granted — recomputed, so
    it follows the entry if the candidate corrects the identifying column."""
    if permission.field_kind != "ROW_FIELD" or not permission.row_table:
        return None
    entry = ROW_TABLES.get(permission.row_table)
    if not entry:
        return None
    model = entry[0]
    rows = (db.query(model).filter(model.candidate_id == candidate_id)
              .order_by(model.id.asc()).all())
    for index, row in enumerate(rows, start=1):
        if row.id == permission.row_id:
            return _row_label(permission.row_table, row, index)
    return None


def active_permissions(db: Session, candidate_id: int) -> list[FieldEditPermission]:
    return (
        db.query(FieldEditPermission)
        .filter(FieldEditPermission.candidate_id == candidate_id,
                FieldEditPermission.status == EditPermissionStatus.ACTIVE.value)
        .order_by(FieldEditPermission.granted_at.asc())
        .all()
    )


def current_value(db: Session, candidate_id: int, permission: FieldEditPermission):
    """What the candidate would be changing — a column value, one cell of a
    repeating entry, or the filename of the document currently on record."""
    from app.models import Document   # local import: avoids a cycle at import time

    if permission.field_kind == "DOCUMENT":
        doc = (
            db.query(Document)
            .filter(Document.candidate_id == candidate_id,
                    Document.form_type == FormType(permission.form_type),
                    Document.field_key == permission.field_name)
            .first()
        )
        return doc.original_filename if doc else None
    if permission.field_kind == "ROW_FIELD":
        row = table_row(db, permission.row_table, candidate_id, permission.row_id)
        return getattr(row, permission.field_name) if row else None
    row = detail_row(db, permission.form_type, candidate_id)
    return getattr(row, permission.field_name) if row else None


def audit_field_name(permission: FieldEditPermission) -> str:
    """How the change is written to field_edit_log. Repeating-section columns
    use the same "table.column" shape HR's own row edits already use."""
    if permission.field_kind == "ROW_FIELD":
        return f"{permission.row_table}.{permission.field_name}"
    return permission.field_name


def require_candidate(db: Session, candidate_id: int) -> Candidate:
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return candidate
