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


class FieldEditRequest(BaseModel):
    field_name: str
    new_value: Optional[str] = None


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
