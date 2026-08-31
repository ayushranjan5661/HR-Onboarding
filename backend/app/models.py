import enum

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import relationship

from app.database import Base


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class CandidateType(str, enum.Enum):
    """Which Document Collection variant applies to this candidate."""
    EXPERIENCED = "EXPERIENCED"
    FRESHER = "FRESHER"


class CandidateStage(str, enum.Enum):
    INVITED = "INVITED"                    # credentials sent, CIF pending
    CIF_SUBMITTED = "CIF_SUBMITTED"        # candidate filled CIF, awaiting HR review
    APPROVED_FOR_BGV = "APPROVED_FOR_BGV"  # HR approved after final interview -> BGV + Doc forms unlocked
    REJECTED = "REJECTED"                  # HR rejected at any gate - terminal
    ONBOARDING_COMPLETE = "ONBOARDING_COMPLETE"  # HR marked BGV + Doc review complete


class FormType(str, enum.Enum):
    CIF = "CIF"
    BGV = "BGV"                             # Background Verification Form
    DOCUMENT_COLLECTION = "DOCUMENT_COLLECTION"


class FormStatus(str, enum.Enum):
    LOCKED = "LOCKED"          # not yet assigned to candidate
    PENDING = "PENDING"        # assigned, candidate has not submitted
    SUBMITTED = "SUBMITTED"    # candidate submitted, awaiting HR review
    UNDER_REVIEW = "UNDER_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


# ---------------------------------------------------------------------------
# HR users
# ---------------------------------------------------------------------------

class HRUser(Base):
    __tablename__ = "hr_users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------

class Candidate(Base):
    __tablename__ = "candidates"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    # The candidate's permanent password, encrypted with a key derived from
    # JWT_SECRET_KEY (see security.encrypt_password) so HR can view it at any
    # time without the DB ever holding it in plaintext.
    temp_password_enc = Column(Text, nullable=True)
    must_reset_password = Column(Boolean, default=False, nullable=False)

    # One-click invite link. The token authenticates this candidate on its own,
    # so treat it as a credential: it is revocable (regenerate) and expires.
    invite_token = Column(String(64), unique=True, index=True, nullable=True)
    invite_token_expires_at = Column(DateTime(timezone=True), nullable=True)

    stage = Column(Enum(CandidateStage), default=CandidateStage.INVITED, nullable=False)
    candidate_type = Column(Enum(CandidateType), default=CandidateType.EXPERIENCED, nullable=False)
    rejection_reason = Column(Text, nullable=True)

    created_by_hr_id = Column(Integer, ForeignKey("hr_users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    profile = relationship("CandidateProfile", uselist=False, back_populates="candidate", cascade="all, delete-orphan")
    submissions = relationship("FormSubmission", back_populates="candidate", cascade="all, delete-orphan")
    documents = relationship("Document", back_populates="candidate", cascade="all, delete-orphan")
    cif_details = relationship("CIFDetails", uselist=False, cascade="all, delete-orphan")
    bgv_details = relationship("BGVDetails", uselist=False, cascade="all, delete-orphan")
    doc_details = relationship("DocCollectionDetails", uselist=False, cascade="all, delete-orphan")
    education = relationship("EducationDetail", cascade="all, delete-orphan")
    employment = relationship("EmploymentDetail", cascade="all, delete-orphan")
    references = relationship("ReferenceDetail", cascade="all, delete-orphan")
    bgv_addresses = relationship("BGVAddressHistory", cascade="all, delete-orphan")
    bgv_education = relationship("BGVEducationCheck", cascade="all, delete-orphan")
    bgv_employment = relationship("BGVEmploymentCheck", cascade="all, delete-orphan")
    bgv_references = relationship("BGVReferenceCheck", cascade="all, delete-orphan")
    bgv_gaps = relationship("BGVGap", cascade="all, delete-orphan")


class CandidateProfile(Base):
    """
    Common fields captured once on the CIF form and auto-mapped into every
    later form (BGV, Document Collection, ...). HR can edit/delete any value.
    """
    __tablename__ = "candidate_profiles"

    id = Column(Integer, primary_key=True, index=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id"), unique=True, nullable=False)

    full_name = Column(String(150))
    email = Column(String(255))
    contact_number = Column(String(20))
    alternate_number = Column(String(20))
    date_of_birth = Column(String(20))
    gender = Column(String(20))
    current_address = Column(Text)
    permanent_address = Column(Text)

    aadhaar_number = Column(String(20))
    pan_number = Column(String(20))

    highest_qualification = Column(String(150))
    university_name = Column(String(150))
    graduation_year = Column(String(10))

    bank_account_number = Column(String(30))
    bank_ifsc_code = Column(String(20))

    emergency_contact_name = Column(String(150))
    emergency_contact_number = Column(String(20))

    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    candidate = relationship("Candidate", back_populates="profile")


class FormSubmission(Base):
    """Tracks lifecycle status of each form. Actual answers live in the
    relational detail tables below (cif_details, bgv_details, ...)."""
    __tablename__ = "form_submissions"

    id = Column(Integer, primary_key=True, index=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id"), nullable=False)
    form_type = Column(Enum(FormType), nullable=False)
    status = Column(Enum(FormStatus), default=FormStatus.LOCKED, nullable=False)

    submitted_at = Column(DateTime(timezone=True), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    reviewed_by_hr_id = Column(Integer, ForeignKey("hr_users.id"), nullable=True)
    review_notes = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    candidate = relationship("Candidate", back_populates="submissions")


# ---------------------------------------------------------------------------
# Form answer tables (one column per field — no JSON)
# ---------------------------------------------------------------------------

class CIFDetails(Base):
    """CIF fields that are not shared profile fields. One row per candidate."""
    __tablename__ = "cif_details"

    id = Column(Integer, primary_key=True, index=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id"), unique=True, nullable=False)

    # Position Details
    position_applied_for = Column(String(150))
    skills_technologies = Column(Text)
    # Candidate Details
    alternate_email = Column(String(255))
    blood_group = Column(String(10))
    linkedin_link = Column(Text)
    marital_status = Column(String(20))
    any_backlogs = Column(String(10))
    source = Column(String(50))
    worked_in_levelshift_before = Column(String(10))
    total_experience_yrs = Column(String(10))
    relevant_skill_exp_yrs = Column(String(10))
    current_ctc_lpa = Column(String(30))
    expected_ctc_lpa = Column(String(30))
    additional_allowance = Column(String(30))
    variable_comp = Column(String(30))
    notice_period_days = Column(String(10))
    other_offers = Column(String(10))
    # Certifications / describe
    technical_certifications = Column(Text)
    understanding_of_levelshift = Column(Text)
    aspirations = Column(Text)
    # Declaration
    declaration_accepted = Column(String(10))
    declaration_place = Column(String(100))
    declaration_date = Column(String(20))
    # HR Team Purpose
    hr_candidate_id = Column(String(50))
    hr_candidate_email = Column(String(255))


class EducationDetail(Base):
    """One row per education entry; section = UG_PG | 12TH | 10TH."""
    __tablename__ = "education_details"

    id = Column(Integer, primary_key=True, index=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id"), nullable=False)
    section = Column(String(10), nullable=False)
    qualification = Column(String(150))
    course_college = Column(String(255))
    cgpa_percent = Column(String(20))
    year_of_passing = Column(String(10))
    has_marksheet = Column(String(10))
    gaps = Column(String(255))


class EmploymentDetail(Base):
    __tablename__ = "employment_details"

    id = Column(Integer, primary_key=True, index=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id"), nullable=False)
    company_name = Column(String(150))
    position_held = Column(String(150))
    from_date = Column(String(20))
    to_date = Column(String(20))
    currently_working = Column(String(10))
    reason_for_leaving = Column(String(255))
    offer_letter = Column(String(10))
    relieving_letter_status = Column(String(10))
    experience_certificate = Column(String(10))
    gaps = Column(String(255))


class ReferenceDetail(Base):
    __tablename__ = "reference_details"

    id = Column(Integer, primary_key=True, index=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id"), nullable=False)
    employee_name = Column(String(150))
    email_id = Column(String(255))
    technology = Column(String(150))
    experience = Column(String(20))
    contact_number = Column(String(20))


class BGVDetails(Base):
    """Background Verification answers. One row per candidate.
    Identity/contact details are NOT duplicated here — they live on
    CandidateProfile and are shown to the candidate read-only."""
    __tablename__ = "bgv_details"

    id = Column(Integer, primary_key=True, index=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id"), unique=True, nullable=False)

    consent_bgv = Column(String(255))
    consent_criminal_check = Column(String(255))
    may_contact_current_employer = Column(String(255))
    name_as_per_records = Column(String(255))
    other_names_used = Column(Text)
    passport_number = Column(String(255))
    passport_expiry = Column(String(255))
    driving_licence_number = Column(String(255))
    has_gaps = Column(String(255))
    ever_convicted = Column(String(255))
    conviction_details = Column(Text)
    pending_case = Column(String(255))
    pending_case_details = Column(Text)
    ever_terminated = Column(String(255))
    termination_details = Column(Text)
    disciplinary_action = Column(String(255))
    disciplinary_details = Column(Text)
    has_bond_noncompete = Column(String(255))
    bond_details = Column(Text)
    dual_employment = Column(String(255))
    dual_employment_details = Column(Text)
    declaration_accepted = Column(String(255))
    declaration_place = Column(String(255))
    declaration_date = Column(String(255))


class DocCollectionDetails(Base):
    """Document Collection form answers. One row per candidate."""
    __tablename__ = "doc_collection_details"

    id = Column(Integer, primary_key=True, index=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id"), unique=True, nullable=False)
    # The form is uploads-only (DOC_FIELDS is empty); no text columns yet.


class Document(Base):
    """Any uploaded file: Aadhaar/PAN scan, mark sheets, resume, photos, etc."""
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id"), nullable=False)
    form_type = Column(Enum(FormType), nullable=False)
    field_key = Column(String(100), nullable=False)   # e.g. "aadhaar_file", "marksheet_10"
    original_filename = Column(String(255), nullable=False)
    stored_filename = Column(String(255), nullable=False)
    content_type = Column(String(100), nullable=True)
    uploaded_at = Column(DateTime(timezone=True), server_default=func.now())

    candidate = relationship("Candidate", back_populates="documents")


class FieldEditLog(Base):
    """Audit trail whenever HR edits or deletes a candidate-submitted value."""
    __tablename__ = "field_edit_log"

    id = Column(Integer, primary_key=True, index=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id"), nullable=False)
    form_type = Column(String(50), nullable=True)
    field_name = Column(String(100), nullable=False)
    old_value = Column(Text, nullable=True)
    new_value = Column(Text, nullable=True)
    action = Column(String(20), nullable=False)  # EDIT | DELETE
    edited_by_hr_id = Column(Integer, ForeignKey("hr_users.id"), nullable=False)
    edited_at = Column(DateTime(timezone=True), server_default=func.now())


class BGVAddressHistory(Base):
    """One row per address the candidate has lived at."""
    __tablename__ = "bgv_address_history"

    id = Column(Integer, primary_key=True, index=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id"), nullable=False)
    address_type = Column(String(255))
    full_address = Column(Text)
    city = Column(String(255))
    state = Column(String(255))
    pin_code = Column(String(255))
    from_date = Column(String(255))
    to_date = Column(String(255))
    residence_type = Column(String(255))
    verifier_name = Column(String(255))
    verifier_contact = Column(String(255))
    nearest_police_station = Column(String(255))


class BGVEducationCheck(Base):
    """One row per qualification to be verified."""
    __tablename__ = "bgv_education_checks"

    id = Column(Integer, primary_key=True, index=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id"), nullable=False)
    qualification = Column(String(255))
    institution = Column(String(255))
    university_board = Column(String(255))
    roll_number = Column(String(255))
    registration_number = Column(String(255))
    year_of_passing = Column(String(255))
    study_mode = Column(String(255))
    verification_contact = Column(String(255))


class BGVEmploymentCheck(Base):
    """One row per employer, with who can verify it."""
    __tablename__ = "bgv_employment_checks"

    id = Column(Integer, primary_key=True, index=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id"), nullable=False)
    company_name = Column(String(255))
    company_address = Column(Text)
    employee_id = Column(String(255))
    designation_joining = Column(String(255))
    designation_leaving = Column(String(255))
    from_date = Column(String(255))
    to_date = Column(String(255))
    employment_type = Column(String(255))
    payroll_company = Column(String(255))
    last_drawn_ctc = Column(String(255))
    manager_name = Column(String(255))
    manager_designation = Column(String(255))
    manager_email = Column(String(255))
    manager_phone = Column(String(255))
    hr_name = Column(String(255))
    hr_email = Column(String(255))
    hr_phone = Column(String(255))
    reason_for_leaving = Column(Text)
    eligible_for_rehire = Column(String(255))
    may_contact_now = Column(String(255))


class BGVReferenceCheck(Base):
    """Professional references (not relatives)."""
    __tablename__ = "bgv_reference_checks"

    id = Column(Integer, primary_key=True, index=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id"), nullable=False)
    name = Column(String(255))
    designation = Column(String(255))
    company = Column(String(255))
    relationship = Column(String(255))
    email = Column(String(255))
    phone = Column(String(255))
    years_known = Column(String(255))


class BGVGap(Base):
    """Any gap over 60 days, with the candidate's explanation."""
    __tablename__ = "bgv_gaps"

    id = Column(Integer, primary_key=True, index=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id"), nullable=False)
    gap_type = Column(String(255))
    from_date = Column(String(255))
    to_date = Column(String(255))
    duration = Column(String(255))
    reason = Column(Text)

