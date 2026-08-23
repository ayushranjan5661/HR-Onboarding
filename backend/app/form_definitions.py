"""
Declares which submitted field goes to which table/column.

- PROFILE_FIELDS      -> candidate_profiles (shared across all forms)
- CIF_FIELDS          -> cif_details        (one row per candidate)
- BGV_FIELDS          -> bgv_details        (one row per candidate)
- DOC_FIELDS          -> doc_collection_details
- EDUCATION_COLUMNS / EMPLOYMENT_COLUMNS / REFERENCE_COLUMNS
                      -> row tables (one row per entry the candidate adds)
- <FORM>_FILE_FIELDS  -> documents table + file on disk
"""

PROFILE_FIELDS = [
    "full_name", "email", "contact_number", "alternate_number", "date_of_birth",
    "gender", "current_address", "permanent_address", "aadhaar_number", "pan_number",
    "highest_qualification", "university_name", "graduation_year",
    "bank_account_number", "bank_ifsc_code",
    "emergency_contact_name", "emergency_contact_number",
]

CIF_FIELDS = [
    "position_applied_for", "skills_technologies",
    "alternate_email", "blood_group", "linkedin_link", "marital_status",
    "any_backlogs", "source", "worked_in_levelshift_before",
    "total_experience_yrs", "relevant_skill_exp_yrs",
    "current_ctc_lpa", "expected_ctc_lpa", "additional_allowance",
    "variable_comp", "notice_period_days", "other_offers",
    "technical_certifications", "understanding_of_levelshift", "aspirations",
    "declaration_accepted", "declaration_place", "declaration_date",
    "hr_candidate_id", "hr_candidate_email",
]
CIF_FILE_FIELDS = ["profile_picture", "signature"]

EDUCATION_COLUMNS = ["qualification", "course_college", "cgpa_percent",
                      "year_of_passing", "has_marksheet", "gaps"]
EDUCATION_SECTIONS = {"education_ug_pg": "UG_PG", "education_12th": "12TH", "education_10th": "10TH"}

EMPLOYMENT_COLUMNS = ["company_name", "position_held", "from_date", "to_date",
                       "currently_working", "reason_for_leaving", "offer_letter",
                       "relieving_letter_status", "experience_certificate", "gaps"]

REFERENCE_COLUMNS = ["employee_name", "email_id", "technology", "experience", "contact_number"]

# --- Background Verification -----------------------------------------------
# BGV asks what the CIF did NOT: who can verify each claim. Identity/contact
# details are pulled from CandidateProfile, never re-asked.
BGV_FIELDS = [
    "consent_bgv",
    "consent_criminal_check",
    "may_contact_current_employer",
    "name_as_per_records",
    "other_names_used",
    "passport_number",
    "passport_expiry",
    "driving_licence_number",
    "has_gaps",
    "ever_convicted",
    "conviction_details",
    "pending_case",
    "pending_case_details",
    "ever_terminated",
    "termination_details",
    "disciplinary_action",
    "disciplinary_details",
    "has_bond_noncompete",
    "bond_details",
    "dual_employment",
    "dual_employment_details",
    "declaration_accepted",
    "declaration_place",
    "declaration_date",
]

BGV_FILE_FIELDS = [
    "signed_consent_form",
    "passport_copy_bgv",
    "form16_last_year",
    "form16_previous_year",
    "bank_statement_salary",
    "police_clearance_certificate",
    "bgv_signature",
]

# Repeating sections, one DB row per entry (same pattern as the CIF tables).
BGV_ADDRESS_COLUMNS = [
    "address_type",
    "full_address",
    "city",
    "state",
    "pin_code",
    "from_date",
    "to_date",
    "residence_type",
    "verifier_name",
    "verifier_contact",
    "nearest_police_station",
]

BGV_EDUCATION_COLUMNS = [
    "qualification",
    "institution",
    "university_board",
    "roll_number",
    "registration_number",
    "year_of_passing",
    "study_mode",
    "verification_contact",
]

BGV_EMPLOYMENT_COLUMNS = [
    "company_name",
    "company_address",
    "employee_id",
    "designation_joining",
    "designation_leaving",
    "from_date",
    "to_date",
    "employment_type",
    "payroll_company",
    "last_drawn_ctc",
    "manager_name",
    "manager_designation",
    "manager_email",
    "manager_phone",
    "hr_name",
    "hr_email",
    "hr_phone",
    "reason_for_leaving",
    "eligible_for_rehire",
    "may_contact_now",
]

BGV_REFERENCE_COLUMNS = [
    "name",
    "designation",
    "company",
    "relationship",
    "email",
    "phone",
    "years_known",
]

BGV_GAP_COLUMNS = [
    "gap_type",
    "from_date",
    "to_date",
    "duration",
    "reason",
]

# form key -> (model attr on Candidate, columns)
BGV_TABLE_SECTIONS = {
    "bgv_address_history": ("bgv_addresses", BGV_ADDRESS_COLUMNS),
    "bgv_education_checks": ("bgv_education", BGV_EDUCATION_COLUMNS),
    "bgv_employment_checks": ("bgv_employment", BGV_EMPLOYMENT_COLUMNS),
    "bgv_reference_checks": ("bgv_references", BGV_REFERENCE_COLUMNS),
    "bgv_gaps": ("bgv_gaps", BGV_GAP_COLUMNS),
}

DOC_FIELDS = []  # the Document Collection form is uploads only — no text fields

# --- Document Collection: the real LevelShift form (40 uploads) -------------
# Two variants share the same fields; only which ones are mandatory differs.
#   EXPERIENCED -> "Experienced Candidate Document collection"
#   FRESHER     -> "Candidate Document collection - Fresher/trainee"
_EMPLOYMENT_BLOCKS = [("cc", "Current company")] + [
    (f"pc{i}", f"Previous company {i}") for i in (1, 2, 3, 4)
]
_EMPLOYMENT_DOCS = ["offer_letter", "role_change_letter", "experience_letter",
                     "relieving_letter", "pay_slips"]

DOC_FILE_FIELDS = (
    [f"{prefix}_{doc}" for prefix, _ in _EMPLOYMENT_BLOCKS for doc in _EMPLOYMENT_DOCS]
    + ["marksheet_10", "marksheet_12_diploma"]
    + ["ug_consolidated_marksheet", "ug_certificate"]
    + ["pg_consolidated_marksheet", "pg_certificate"]
    + ["other_edu_marksheet", "other_edu_certificate"]
    + ["additional_certifications"]
    + ["passport_size_photo", "pan_card", "aadhar_card", "passport_copy",
        "address_proof", "id_proof"]
)

# Mandatory everywhere: education + personal identity documents.
_DOC_REQUIRED_COMMON = [
    "marksheet_10", "marksheet_12_diploma",
    "ug_consolidated_marksheet", "ug_certificate",
    "passport_size_photo", "pan_card", "aadhar_card", "address_proof", "id_proof",
]
# An experienced hire must also evidence their current employment.
DOC_REQUIRED_BY_TYPE = {
    "EXPERIENCED": _DOC_REQUIRED_COMMON + ["cc_offer_letter", "cc_pay_slips"],
    "FRESHER": list(_DOC_REQUIRED_COMMON),
}
