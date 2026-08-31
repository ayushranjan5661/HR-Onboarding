"""
Canonical catalogue of every form field, with the human label the
candidate actually sees. The cross-form mapping agent reasons over
these labels, which is how it matches fields whose internal names
differ (e.g. CIF 'Candidate Profile Picture' -> Document Collection
'Passport size photo').

GENERATED - regenerate with scratchpad/gen_catalog.py after changing
any form definition.
"""

# form -> {'field': [(key, label)], 'document': [(key, label)]}
CATALOG = {
    "CIF": {
        "field": [
            ("full_name", "Full Name"),
            ("email", "Email"),
            ("contact_number", "Mobile Number"),
            ("alternate_number", "Alternate Contact"),
            ("date_of_birth", "Date of Birth"),
            ("gender", "Gender"),
            ("current_address", "Present/Communication Address"),
            ("permanent_address", "Permanent Address"),
            ("aadhaar_number", "Aadhaar Number"),
            ("pan_number", "PAN Number"),
            ("highest_qualification", "Highest Qualification"),
            ("university_name", "University Name"),
            ("graduation_year", "Graduation Year"),
            ("bank_account_number", "Bank Account Number"),
            ("bank_ifsc_code", "Bank Ifsc Code"),
            ("emergency_contact_name", "Emergency Contact Name"),
            ("emergency_contact_number", "Emergency Contact Number"),
            ("position_applied_for", "Position Applied For"),
            ("skills_technologies", "Skill/Technologies Worked"),
            ("alternate_email", "Alternate E-Mail ID"),
            ("blood_group", "Blood Group"),
            ("linkedin_link", "LinkedIn Page Link"),
            ("marital_status", "Marital Status"),
            ("any_backlogs", "Any Backlogs in the Education"),
            ("source", "Source"),
            ("worked_in_levelshift_before", "Worked in LevelShift Before"),
            ("total_experience_yrs", "Total Experience (Yrs)"),
            ("relevant_skill_exp_yrs", "Relevant Skill Exp. (Yrs)"),
            ("current_ctc_lpa", "Current CTC (LPA)"),
            ("expected_ctc_lpa", "Expected CTC (LPA)"),
            ("additional_allowance", "Additional Allowance (Rs)"),
            ("variable_comp", "Variable Comp (Rs)"),
            ("notice_period_days", "Notice Period (Days)"),
            ("other_offers", "Holds Other Offers"),
            ("technical_certifications", "Technical Certifications"),
            ("understanding_of_levelshift", "Understanding of LevelShift"),
            ("aspirations", "Aspirations"),
            ("declaration_accepted", "Declaration accepted"),
            ("declaration_place", "Place"),
            ("declaration_date", "Date"),
            ("hr_candidate_id", "HR: Candidate ID"),
            ("hr_candidate_email", "HR: Candidate Email ID"),
        ],
        "document": [
            ("profile_picture", "Candidate Profile Picture"),
            ("signature", "Signature"),
        ],
    },
    "DOCUMENT_COLLECTION": {
        "field": [
        ],
        "document": [
            ("cc_offer_letter", "Current company — Offer Letter"),
            ("cc_role_change_letter", "Current company — Role Change Letter"),
            ("cc_experience_letter", "Current company — Experience Letter"),
            ("cc_relieving_letter", "Current company — Relieving Letter"),
            ("cc_pay_slips", "Current company — Last 3 Months Pay Slips"),
            ("pc1_offer_letter", "Previous company 1 — Offer Letter"),
            ("pc1_role_change_letter", "Previous company 1 — Role Change Letter"),
            ("pc1_experience_letter", "Previous company 1 — Experience Letter"),
            ("pc1_relieving_letter", "Previous company 1 — Relieving Letter"),
            ("pc1_pay_slips", "Previous company 1 — Last 3 Months Pay Slips"),
            ("pc2_offer_letter", "Previous company 2 — Offer Letter"),
            ("pc2_role_change_letter", "Previous company 2 — Role Change Letter"),
            ("pc2_experience_letter", "Previous company 2 — Experience Letter"),
            ("pc2_relieving_letter", "Previous company 2 — Relieving Letter"),
            ("pc2_pay_slips", "Previous company 2 — Last 3 Months Pay Slips"),
            ("pc3_offer_letter", "Previous company 3 — Offer Letter"),
            ("pc3_role_change_letter", "Previous company 3 — Role Change Letter"),
            ("pc3_experience_letter", "Previous company 3 — Experience Letter"),
            ("pc3_relieving_letter", "Previous company 3 — Relieving Letter"),
            ("pc3_pay_slips", "Previous company 3 — Last 3 Months Pay Slips"),
            ("pc4_offer_letter", "Previous company 4 — Offer Letter"),
            ("pc4_role_change_letter", "Previous company 4 — Role Change Letter"),
            ("pc4_experience_letter", "Previous company 4 — Experience Letter"),
            ("pc4_relieving_letter", "Previous company 4 — Relieving Letter"),
            ("pc4_pay_slips", "Previous company 4 — Last 3 Months Pay Slips"),
            ("marksheet_10", "10th Marks Sheet"),
            ("marksheet_12_diploma", "12th Marks Sheet / Diploma"),
            ("ug_consolidated_marksheet", "UG – Consolidated Marksheet"),
            ("ug_certificate", "UG Certificate"),
            ("pg_consolidated_marksheet", "PG – Consolidated Marksheet"),
            ("pg_certificate", "PG Certificate"),
            ("other_edu_marksheet", "Other Educational Marksheet"),
            ("other_edu_certificate", "Other Educational Certificate"),
            ("additional_certifications", "Additional Certifications"),
            ("passport_size_photo", "Passport Size Photo"),
            ("pan_card", "Pan Card"),
            ("aadhar_card", "Aadhar Card"),
            ("passport_copy", "Passport Copy"),
            ("address_proof", "Address Proof"),
            ("id_proof", "ID Proof"),
        ],
    },
    "BGV": {
        "field": [
            ("consent_bgv", "I authorise background verification"),
            ("consent_criminal_check", "I consent to a criminal record check"),
            ("may_contact_current_employer", "May we contact your current employer now?"),
            ("name_as_per_records", "Full name as per government records"),
            ("other_names_used", "Any other/former name used (maiden name, alias)"),
            ("passport_number", "Passport Number"),
            ("passport_expiry", "Passport Expiry"),
            ("driving_licence_number", "Driving Licence Number"),
            ("has_gaps", "Any gap of more than 60 days in education or employment?"),
            ("ever_convicted", "Ever convicted of a criminal offence?"),
            ("conviction_details", "Conviction details"),
            ("pending_case", "Any pending court case or investigation?"),
            ("pending_case_details", "Pending case details"),
            ("ever_terminated", "Ever terminated or asked to resign?"),
            ("termination_details", "Termination details"),
            ("disciplinary_action", "Ever subject to disciplinary action?"),
            ("disciplinary_details", "Disciplinary action details"),
            ("has_bond_noncompete", "Under any bond or non-compete agreement?"),
            ("bond_details", "Bond / non-compete details"),
            ("dual_employment", "Currently hold any other employment?"),
            ("dual_employment_details", "Other employment details"),
            ("declaration_accepted", "Declaration accepted"),
            ("declaration_place", "Place"),
            ("declaration_date", "Date"),
        ],
        "document": [
            ("signed_consent_form", "Signed BGV Consent Form"),
            ("passport_copy_bgv", "Passport Copy"),
            ("form16_last_year", "Form 16 / ITR — Last Year"),
            ("form16_previous_year", "Form 16 / ITR — Previous Year"),
            ("bank_statement_salary", "Bank Statement (salary credits, 3–6 months)"),
            ("police_clearance_certificate", "Police Clearance Certificate (if available)"),
            ("bgv_signature", "Signature"),
        ],
    },
}


def labels_for(form: str, kind: str) -> dict[str, str]:
    """key -> human label for one form and kind."""
    return dict(CATALOG.get(form, {}).get(kind, []))


def all_labels() -> dict[str, str]:
    out = {}
    for form in CATALOG.values():
        for kind in form.values():
            out.update(dict(kind))
    return out
