from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, EmailStr


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class InviteTokenLoginRequest(BaseModel):
    token: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    name: str
    must_reset_password: bool = False


# ---------------------------------------------------------------------------
# HR managing candidates
# ---------------------------------------------------------------------------

class InviteCandidateRequest(BaseModel):
    name: str
    email: EmailStr
    candidate_type: str = "EXPERIENCED"   # EXPERIENCED | FRESHER


class InviteCandidateResponse(BaseModel):
    candidate_id: int
    email: EmailStr
    temp_password: str
    login_url: str


class CandidateListItem(BaseModel):
    id: int
    name: str
    email: str
    stage: str
    created_at: datetime

    class Config:
        from_attributes = True


# --- Field-level edit access on a submitted form ---------------------------

class GrantItem(BaseModel):
    form_type: str                 # where the value is stored / which form it is on
    field_name: str
    field_kind: str = "FIELD"      # FIELD | DOCUMENT | ROW_FIELD
    # ROW_FIELD only: which repeating section and which entry in it.
    row_table: Optional[str] = None
    row_id: Optional[int] = None


class GrantEditAccessRequest(BaseModel):
    grants: list[GrantItem]
    hr_note: Optional[str] = None  # shown to the candidate as the instruction


class BatchFieldEdit(BaseModel):
    form: str                      # PROFILE | CIF | BGV | DOCUMENT_COLLECTION
    field_name: str
    new_value: Optional[str] = None


class BatchRowEdit(BaseModel):
    """Column -> new value for one entry of a repeating section."""
    table: str
    row_id: int
    values: dict[str, Optional[str]]


class ChangeSetRequest(BaseModel):
    """Everything one HR save touches, applied as a single audited action."""
    fields: list[BatchFieldEdit] = []
    rows: list[BatchRowEdit] = []
    reason: Optional[str] = None


class RevokeEditAccessRequest(BaseModel):
    # Which grants to close. Omit (or leave empty) to close every open one.
    permission_ids: Optional[list[int]] = None
    reason: Optional[str] = None   # recorded in the audit trail


class EditPermissionOut(BaseModel):
    id: int
    form: str                      # CIF | DOCUMENT_COLLECTION | BGV
    form_type: str
    field_kind: str
    field_name: str
    row_table: Optional[str] = None
    row_id: Optional[int] = None
    row_label: Optional[str] = None
    status: str
    hr_note: Optional[str] = None
    granted_by: Optional[str] = None
    granted_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    current_value: Optional[str] = None


class AuditEntryOut(BaseModel):
    id: int
    form_type: Optional[str] = None
    field_name: str
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    action: str
    reason: Optional[str] = None
    actor_role: str
    actor_name: Optional[str] = None
    edited_at: Optional[datetime] = None
    change_set_id: Optional[str] = None   # groups one action's changes
    # For a document change: the file before and after, both still openable.
    old_file: Optional["AuditFileOut"] = None
    new_file: Optional["AuditFileOut"] = None


class AuditFileOut(BaseModel):
    id: int
    filename: str
    content_type: Optional[str] = None
    available: bool = True   # False if the file has gone from the uploads folder


class DecisionRequest(BaseModel):
    reason: Optional[str] = None


class ReviewSubmissionRequest(BaseModel):
    decision: str  # APPROVED | REJECTED | UNDER_REVIEW
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Candidate-facing
# ---------------------------------------------------------------------------

class CIFSubmitRequest(BaseModel):
    profile: dict[str, Any]
    extra: dict[str, Any] = {}


class FormSubmitRequest(BaseModel):
    extra: dict[str, Any] = {}


class DocumentOut(BaseModel):
    id: int
    form_type: str
    field_key: str
    original_filename: str
    content_type: Optional[str] = None   # lets the portal preview inline
    file_available: bool = True          # False if the file vanished from disk
    uploaded_at: datetime

    class Config:
        from_attributes = True


class FormSubmissionOut(BaseModel):
    id: int
    form_type: str
    status: str
    submitted_at: Optional[datetime]
    reviewed_at: Optional[datetime]
    review_notes: Optional[str]

    class Config:
        from_attributes = True


class CandidateProfileOut(BaseModel):
    full_name: Optional[str] = None
    email: Optional[str] = None
    contact_number: Optional[str] = None
    alternate_number: Optional[str] = None
    date_of_birth: Optional[str] = None
    gender: Optional[str] = None
    current_address: Optional[str] = None
    permanent_address: Optional[str] = None
    aadhaar_number: Optional[str] = None
    pan_number: Optional[str] = None
    highest_qualification: Optional[str] = None
    university_name: Optional[str] = None
    graduation_year: Optional[str] = None
    bank_account_number: Optional[str] = None
    bank_ifsc_code: Optional[str] = None
    emergency_contact_name: Optional[str] = None
    emergency_contact_number: Optional[str] = None

    class Config:
        from_attributes = True


class CandidateDetailOut(BaseModel):
    id: int
    name: str
    email: str
    stage: str
    candidate_type: str = "EXPERIENCED"
    rejection_reason: Optional[str] = None
    temp_password: Optional[str] = None   # decrypted on demand for HR view
    login_url: Optional[str] = None
    profile: Optional[CandidateProfileOut] = None
    submissions: list[FormSubmissionOut] = []
    documents: list[DocumentOut] = []
    # Relational form answers (dicts keyed by column name; rows include "id")
    cif_details: Optional[dict[str, Any]] = None
    bgv_details: Optional[dict[str, Any]] = None
    doc_details: Optional[dict[str, Any]] = None
    education: dict[str, list[dict[str, Any]]] = {}
    employment: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    bgv_tables: dict[str, list[dict[str, Any]]] = {}

    class Config:
        from_attributes = True


class MyStatusOut(BaseModel):
    stage: str
    candidate_type: str = "EXPERIENCED"
    forms: list[FormSubmissionOut]
    profile: Optional[CandidateProfileOut] = None
