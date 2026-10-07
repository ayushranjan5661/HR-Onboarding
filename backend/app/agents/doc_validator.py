"""
Document validation — is this upload the document its field asks for?

Candidates routinely attach the wrong file to a slot: a PAN card under
"Aadhaar", a signature under "PAN", a transfer certificate as the "10th
marksheet". Nothing about the file itself (name, type, size) reveals that,
so every upload is read and scored, and the score is stored on the document
row where the candidate and HR can both see it.

Everything runs on this server. Identity documents are sensitive, so no
part of the file — and none of the profile — is sent to any AI service:

  1. EXTRACT  — the file's text. A PDF's own text layer when it has one
                (e-Aadhaar, bank statements); otherwise Tesseract OCR on the
                image or the rasterised pages, in English and Hindi so the
                Devanagari on Aadhaar/PAN cards counts too. DOCX text is
                read straight from the file.
  2. SCORE    — every document type in RULES is a list of weighted
                keyword/pattern rules ("Income Tax Department", a PAN-shaped
                number, "Class X", a board name, ...). A type's score is the
                share of its total weight that matched, 0-100. Negative
                weights let a 12th marksheet lose points as a 10th one.
                Photos and signatures have no text, so they are scored from
                image statistics instead (ink coverage, colour, word count).
  3. DECIDE   — the field's expected types (EXPECTED) give the score that
                matters. >= MATCH_SCORE: MATCH. < MISMATCH_SCORE: MISMATCH,
                naming the type that scored best if one stands out. In
                between: UNCERTAIN, for HR to look at. Anything that could
                not be read (OCR not installed, a legacy .doc, a hung OCR)
                is UNVERIFIED and always accepted — a broken checker must
                never lock a candidate out of submitting.

A PAN or Aadhaar number found in the text is also checked for shape (PAN
regex, Aadhaar Verhoeff checksum) and compared with the number on the
candidate's profile, which catches someone else's card.

Results are cached by file hash for a short while: the portal checks a
file the moment it is picked, then again when the draft or form is saved,
and the second look must not cost a second OCR pass.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import subprocess
import threading
import time
import zipfile
from dataclasses import dataclass, field
from typing import Callable
from xml.etree import ElementTree

from app.agents.doc_lexicon import LANGUAGES as _LEXICON_LANGUAGES, alt
from app.config import settings

# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------
_MARKSHEETS = {"MARKSHEET_10", "MARKSHEET_12", "DIPLOMA_MARKSHEET",
               "UG_MARKSHEET", "PG_MARKSHEET"}
_CERTIFICATES = {"UG_DEGREE_CERTIFICATE", "PG_DEGREE_CERTIFICATE",
                 "DIPLOMA_MARKSHEET", "COURSE_CERTIFICATE"}
_ID_CARDS = {"AADHAAR", "PAN", "PASSPORT", "VOTER_ID", "DRIVING_LICENCE"}
_IMAGE_ONLY = {"PHOTO", "SIGNATURE"}       # scored from pixels, not text

# field_key -> the document types that satisfy it. A field missing here is
# never checked (there is nothing to compare against), so adding a new
# upload to a form is safe by default.
EXPECTED: dict[str, set[str]] = {
    # CIF
    "profile_picture": {"PHOTO"},
    "signature": {"SIGNATURE"},
    # BGV
    "signed_consent_form": {"CONSENT_FORM"},
    "passport_copy_bgv": {"PASSPORT"},
    "form16_last_year": {"FORM16"},
    "form16_previous_year": {"FORM16"},
    "bank_statement_salary": {"BANK_STATEMENT"},
    "police_clearance_certificate": {"POLICE_CLEARANCE"},
    "bgv_signature": {"SIGNATURE"},
    "ref_signature": {"SIGNATURE"},
    # Document collection — education
    "marksheet_10": {"MARKSHEET_10"},
    "marksheet_12_diploma": {"MARKSHEET_12", "DIPLOMA_MARKSHEET"},
    "ug_consolidated_marksheet": {"UG_MARKSHEET"},
    "ug_certificate": {"UG_DEGREE_CERTIFICATE"},
    "pg_consolidated_marksheet": {"PG_MARKSHEET"},
    "pg_certificate": {"PG_DEGREE_CERTIFICATE"},
    "other_edu_marksheet": set(_MARKSHEETS),
    "other_edu_certificate": set(_CERTIFICATES),
    "additional_certifications": {"COURSE_CERTIFICATE"},
    # Document collection — identity
    "passport_size_photo": {"PHOTO"},
    "pan_card": {"PAN"},
    "aadhar_card": {"AADHAAR"},
    "passport_copy": {"PASSPORT"},
    "address_proof": _ID_CARDS | {"ADDRESS_PROOF_OTHER", "BANK_STATEMENT"},
    "id_proof": set(_ID_CARDS),
}
# Employment blocks: the same five documents for the current and each of
# four previous companies. Companies often issue one experience-cum-
# relieving letter, so either type satisfies either slot.
for _prefix in ("cc", "pc1", "pc2", "pc3", "pc4"):
    EXPECTED[f"{_prefix}_offer_letter"] = {"OFFER_LETTER"}
    EXPECTED[f"{_prefix}_role_change_letter"] = {"ROLE_CHANGE_LETTER"}
    EXPECTED[f"{_prefix}_experience_letter"] = {"EXPERIENCE_LETTER", "RELIEVING_LETTER"}
    EXPECTED[f"{_prefix}_relieving_letter"] = {"RELIEVING_LETTER", "EXPERIENCE_LETTER"}
    EXPECTED[f"{_prefix}_pay_slips"] = {"PAY_SLIP"}

# What people see in messages.
_TYPE_LABEL = {
    "AADHAAR": "an Aadhaar card", "PAN": "a PAN card", "PASSPORT": "a passport",
    "VOTER_ID": "a voter ID", "DRIVING_LICENCE": "a driving licence",
    "PHOTO": "a photograph", "SIGNATURE": "a signature",
    "MARKSHEET_10": "a 10th marksheet", "MARKSHEET_12": "a 12th marksheet",
    "DIPLOMA_MARKSHEET": "a diploma marksheet/certificate",
    "UG_MARKSHEET": "a UG marksheet", "UG_DEGREE_CERTIFICATE": "a UG degree certificate",
    "PG_MARKSHEET": "a PG marksheet", "PG_DEGREE_CERTIFICATE": "a PG degree certificate",
    "TRANSFER_CERTIFICATE": "a transfer/leaving certificate",
    "COURSE_CERTIFICATE": "a course/training certificate",
    "OFFER_LETTER": "an offer/appointment letter",
    "ROLE_CHANGE_LETTER": "a promotion/role-change letter",
    "EXPERIENCE_LETTER": "an experience letter", "RELIEVING_LETTER": "a relieving letter",
    "PAY_SLIP": "a pay slip", "BANK_STATEMENT": "a bank statement",
    "FORM16": "a Form 16 / ITR", "POLICE_CLEARANCE": "a police clearance certificate",
    "CONSENT_FORM": "a consent form", "ADDRESS_PROOF_OTHER": "an address proof",
    "RESUME": "a resume",
}


def describe_type(doc_type: str | None) -> str:
    return _TYPE_LABEL.get(doc_type or "", "a different kind of document")


# ---------------------------------------------------------------------------
# Rules. (label, weight, pattern). Patterns run case-insensitively over the
# whitespace-collapsed text; Devanagari is matched as-is. A type's score is
# matched weight / positive weight * 100, clamped to 0-100, so the weights
# only have to be right relative to each other within one type.
# ---------------------------------------------------------------------------
_PAN_TOKEN = r"(?<![A-Z0-9])[A-Z]{5}[0-9]{4}[A-Z](?![A-Z0-9])"
_AADHAAR_TOKEN = r"(?<!\d)\d{4}[\s\-]?\d{4}[\s\-]?\d{4}(?!\d)"
_AADHAAR_MASKED = r"(?<![\dX])[X\*x]{4}[\s\-]?[X\*x]{4}[\s\-]?\d{4}(?!\d)"
# Dotted forms (B.E., M.A., M.Arch) match freely; without the dot only the
# unambiguous compounds do — otherwise "March", "Beds" or "Meal" read as
# M.Arch, B.Ed and M.E. and sink every marksheet dated March.
_DEGREE_UG = (r"\bb\.\s?(tech|e|sc|com|a|ca|ba|arch|pharm|ed)\b|\bb(tech|sc|com|ca|ba|arch|pharm)\b|"
              r"\bbachelor")
_DEGREE_PG = (r"\bm\.\s?(tech|e|sc|com|a|ca|ba|arch|pharm|ed|phil)\b|\bm(tech|sc|com|ca|ba|phil|pharm)\b|"
              r"\bmaster|\bpgdm\b|post.?graduate")
# Multilingual vocabulary lives in doc_lexicon.py: every state board by
# name, what each state calls its 10th/12th exams, and "marks", "Aadhaar",
# "Government of India", "date of birth" etc. in every Indian script.
_MARKS_WORDS = alt("marks")
_SCHOOL_BOARD = alt("board")
_CLASS10 = alt("class10")
_CLASS12 = alt("class12")
_MARKSHEET_WORD = alt("marksheet_word")
# "Secondary School Leaving Certificate" (SSLC) and "High School Leaving
# Certificate" (HSLC) are board exams, not transfer certificates.
_TC_WORDS = (r"transfer certificate|(?<!school )leaving certificate|migration certificate|date of leaving|"
             r"स्थानांतरण|ट्रांसफर")
_ROLL = (r"\broll\b|seat no|registration|register(ed)? no|index no|hall ticket|enrol(l)?ment|"
         r"अनुक्रमांक|क्रमांक|\b\d{7,10}\b")
# "Pre-University" (Karnataka's 12th) is school, not a degree.
_DEGREE_DOC = _DEGREE_UG + "|" + _DEGREE_PG + r"|semester|(?<!pre-)(?<!pre )university"

RULES: dict[str, list[tuple[str, int, str]]] = {
    "AADHAAR": [
        # Cards are English plus the state language; the lexicon carries the
        # word in every script. The English word only appears on newer
        # cards and OCR does not always get the regional one, so the number
        # and the layout checks (CHECKS below) carry most of the weight.
        ("the word Aadhaar", 20, alt("aadhaar")),
        # Only on the back of the card, so it cannot carry the front.
        ("UIDAI / Unique Identification", 15, alt("uidai")),
        ("12-digit number", 20, _AADHAAR_TOKEN + "|" + _AADHAAR_MASKED),
        ("Government of India", 10, alt("govt_of_india")),
        ("date/year of birth", 5, alt("dob")),
        ("gender", 5, alt("gender")),
        # Back of the card: helpline, website, address block.
        ("UIDAI helpline / website / address", 10, r"\b1947\b|uidai\.gov\.in|help@uidai|" + alt("address")),
        ("looks like a PAN card", -25, r"income tax|permanent account number"),
    ],
    "PAN": [
        # OCR on the card's coloured header often drops the first letters
        # ("OME TAX DEPARTMENT"), so match the tail of the phrase too.
        ("Income Tax Department", 25, alt("income_tax")),
        ("Permanent Account Number", 20, r"permanent account|account number card|स्थायी लेखा"),
        ("PAN-format number", 30, _PAN_TOKEN),
        ("Government of India", 10, alt("govt_of_india")),
        ("father's name", 5, r"father'?s name|पिता का नाम"),
        ("date of birth", 5, alt("dob")),
        ("signature", 5, r"signature|हस्ताक्षर"),
        ("looks like an Aadhaar card", -25, alt("aadhaar", "uidai")),
    ],
    "PASSPORT": [
        ("the word Passport", 25, r"passport|passeport|pasaporte|पासपोर्ट"),
        ("issuing country", 15, r"republic of india|भारत गणराज्य|united states|united kingdom|"
                                 r"republic of|kingdom of|federal republic|commonwealth of"),
        ("passport-format number", 15, r"(?<![A-Z0-9])[A-Z][0-9]{7}(?![A-Z0-9])"),
        ("machine-readable zone", 15, r"P<[A-Z]{3}|<<<"),
        ("issue / expiry / place of birth / authority", 20,
         r"date of (issue|expiry|expiration)|expir|nationality|place of (birth|issue)|authority|"
         r"date of birth|date de naissance"),
        ("surname / given name", 10, r"surname|given name|type|code"),
    ],
    "VOTER_ID": [
        ("Election Commission / elector / EPIC", 60, alt("election")),
        ("Government of India", 10, alt("govt_of_india")),
        ("date of birth / age", 15, alt("dob") + r"|\bage\b"),
        ("gender", 15, alt("gender")),
    ],
    "DRIVING_LICENCE": [
        ("Driving Licence", 40, alt("driving")),
        ("transport / RTO", 30, r"transport|\brto\b|motor vehicle|licensing authority|परिवहन"),
        ("DL number / validity", 20, r"\bdl\s?no|valid (till|upto)|date of issue"),
        ("vehicle class", 10, r"\b(lmv|mcwg|mcwog|hmv|trans)\b"),
    ],
    # The two school marksheets mirror each other: what earns one 30 points
    # costs the other 40, so a 12th sheet in the 10th slot cannot coast on
    # the board name and marks table they share. The exam names per state
    # (SSC, SSLC, HSLC, Madhyamik, Matric, Intermediate, PUC, Plus Two, HS,
    # Uchcha Madhyamik ...) are catalogued in doc_lexicon.py.
    "MARKSHEET_10": [
        ("Class 10 (SSC / SSLC / HSLC / Matric / Madhyamik / High School)", 30, _CLASS10),
        ("a school board", 20, _SCHOOL_BOARD),
        ("marks / grades", 20, _MARKS_WORDS),
        ("marks statement / memo / certificate", 15, _MARKSHEET_WORD),
        ("roll / register number", 10, _ROLL),
        ("year", 5, r"\b(19|20)\d{2}\b"),
        ("says Class 12 (Intermediate / HS / PUC / Plus Two / senior secondary)", -40, _CLASS12),
        ("is a transfer / leaving certificate", -40, _TC_WORDS),
        ("is a degree document", -30, _DEGREE_DOC),
    ],
    "MARKSHEET_12": [
        ("Class 12 (Intermediate / HS / PUC / Plus Two / senior secondary)", 30, _CLASS12),
        ("a school board", 20, _SCHOOL_BOARD),
        ("marks / grades", 20, _MARKS_WORDS),
        ("marks statement / memo / certificate", 15, _MARKSHEET_WORD),
        ("roll / register number", 10, _ROLL),
        ("year", 5, r"\b(19|20)\d{2}\b"),
        ("says Class 10 (SSC / SSLC / HSLC / Matric / Madhyamik / High School)", -40, _CLASS10),
        ("is a transfer / leaving certificate", -40, _TC_WORDS),
        ("is a degree document", -30, _DEGREE_DOC),
    ],
    "DIPLOMA_MARKSHEET": [
        ("the word Diploma", 40, r"\bdiploma\b"),
        ("polytechnic / technical board", 30, r"polytechnic|board of technical education|technical (board|education)|\bsbte\b|\bdte\b"),
        ("marks / grades / semester", 20, _MARKS_WORDS + r"|semester"),
        ("roll / registration number", 10, r"roll\s?no|registration|enrol(l)?ment"),
    ],
    "UG_MARKSHEET": [
        ("a bachelor's degree name", 25, _DEGREE_UG),
        ("semester / consolidated / transcript", 25, r"semester|consolidated|transcript|grade card|statement of marks|marks? statement"),
        ("university / college", 15, r"university|college|institute|autonomous"),
        ("marks / credits / grades", 20, _MARKS_WORDS + r"|credits?|grade points?"),
        ("register / roll number", 10, r"register no|roll\s?no|usn|enrol(l)?ment|hall ticket"),
        ("year", 5, r"\b(19|20)\d{2}\b"),
        ("is a master's document", -20, _DEGREE_PG),
        ("is a degree certificate, not marks", -20, r"conferred|admitted to the degree|convocation|has been awarded the degree"),
        ("is a school document", -30, r"class\s?(x|xii|10|12)\b|\b(10th|12th)\b|\bcbse\b|\bicse\b|secondary|"
                                      r"pre.?university|\bpuc\b|\bsslc\b|\bhslc\b|\bssc\b|matric|intermediate"),
    ],
    "UG_DEGREE_CERTIFICATE": [
        ("a bachelor's degree name", 25, _DEGREE_UG),
        ("degree conferred / awarded", 30, r"conferred|admitted to the degree|has been awarded|degree of|provisional (degree )?certificate|"
                                          r"passed the .* examination|declared (to have )?passed|convocation"),
        ("university / authority", 15, r"university|vice.?chancellor|registrar|controller of examinations|deemed"),
        ("the word degree/certificate", 15, r"\bdegree\b|\bcertificate\b"),
        ("class / division", 10, r"first class|second class|distinction|division|\bcgpa\b"),
        ("year", 5, r"\b(19|20)\d{2}\b"),
        ("is a master's document", -20, _DEGREE_PG),
        ("is a marksheet, not a certificate", -20, r"semester|subject|marks obtained|max(imum)? marks|grade points"),
    ],
    "PG_MARKSHEET": [
        ("a master's degree name", 30, _DEGREE_PG),
        ("semester / consolidated / transcript", 25, r"semester|consolidated|transcript|grade card|statement of marks|marks? statement"),
        ("university / college", 10, r"university|college|institute|autonomous"),
        ("marks / credits / grades", 20, _MARKS_WORDS + r"|credits?|grade points?"),
        ("register / roll number", 10, r"register no|roll\s?no|usn|enrol(l)?ment|hall ticket"),
        ("year", 5, r"\b(19|20)\d{2}\b"),
        ("is a degree certificate, not marks", -20, r"conferred|admitted to the degree|convocation|has been awarded the degree"),
        ("is a school document", -30, r"class\s?(x|xii|10|12)\b|\b(10th|12th)\b|\bcbse\b|\bicse\b|secondary|"
                                      r"pre.?university|\bpuc\b|\bsslc\b|\bhslc\b|\bssc\b|matric|intermediate"),
    ],
    "PG_DEGREE_CERTIFICATE": [
        ("a master's degree name", 30, _DEGREE_PG),
        ("degree conferred / awarded", 30, r"conferred|admitted to the degree|has been awarded|degree of|provisional (degree )?certificate|"
                                          r"passed the .* examination|declared (to have )?passed|convocation"),
        ("university / authority", 15, r"university|vice.?chancellor|registrar|controller of examinations|deemed"),
        ("the word degree/certificate", 10, r"\bdegree\b|\bcertificate\b"),
        ("class / division", 10, r"first class|second class|distinction|division|\bcgpa\b"),
        ("year", 5, r"\b(19|20)\d{2}\b"),
        ("is a marksheet, not a certificate", -20, r"semester|subject|marks obtained|max(imum)? marks|grade points"),
    ],
    "TRANSFER_CERTIFICATE": [
        ("Transfer / Leaving / Migration Certificate", 45,
         r"transfer certificate|(?<!school )leaving certificate|migration certificate|character certificate|bonafide|\bt\.?c\.?\b|स्थानांतरण प्रमाण"),
        ("date of leaving / admission", 20, r"date of (leaving|admission|birth)|reason for leaving|date on which .* left"),
        ("conduct / character", 15, r"conduct|character|behaviou?r"),
        ("school / principal", 15, r"school|principal|head ?master|institution"),
        ("class studied", 5, r"class|standard|std\.?"),
    ],
    "COURSE_CERTIFICATE": [
        ("certificate of completion", 35, r"certificate of (completion|achievement|participation|training)|"
                                         r"has successfully completed|successfully completed|certify that .* completed|certification"),
        ("course / training / provider", 30, r"\bcourse\b|training|workshop|internship|coursera|udemy|nptel|edx|udacity|"
                                              r"\baws\b|microsoft|google|cisco|oracle|red hat|simplilearn|great learning|scaler|hackerrank"),
        ("credential / verify link", 15, r"credential|verify|certificate id|licen[cs]e number|https?://"),
        ("duration / date", 10, r"\bhours?\b|weeks?|duration|date"),
        ("issued / instructor", 10, r"issued|instructor|trainer|authori[sz]ed"),
        ("is an academic document", -30, r"university|\bboard\b|semester|marks|class\s?(x|xii|10|12)"),
    ],
    "OFFER_LETTER": [
        ("Offer / Appointment letter", 35, r"offer letter|letter of offer|appointment letter|letter of appointment|"
                                          r"pleased to offer|offer of employment|offer you|letter of intent"),
        ("CTC / compensation", 25, r"\bctc\b|cost to company|compensation|salary|remuneration|annual package|per annum|lpa"),
        ("date of joining / designation", 20, r"date of joining|joining date|designation|position of|role of"),
        ("terms / probation", 10, r"terms and conditions|probation|notice period|confidentiality"),
        ("acceptance", 10, r"accept(ance)?|sign and return|acknowledg"),
        ("is a relieving / experience letter", -30, r"relieving|relieved|last working day|experience certificate|resignation"),
    ],
    "ROLE_CHANGE_LETTER": [
        ("promotion / revision / appraisal", 40, r"promot(ed|ion)|revis(ed|ion)|appraisal|increment|hike|"
                                                r"designation change|change (of|in) (role|designation)|redesignat"),
        ("effective from / new designation", 25, r"effective (from|date)|with effect from|w\.e\.f|new (designation|role|position)|elevated"),
        ("compensation", 15, r"\bctc\b|compensation|salary|remuneration|package"),
        ("congratulations", 10, r"congratulat|pleased to (inform|announce)|recognition|performance"),
        ("employee reference", 10, r"employee (id|code|no)|emp\.? ?(id|no)"),
        ("is an offer letter", -20, r"offer (letter|of employment)|pleased to offer|appointment letter"),
        ("is a relieving / experience letter", -30, r"relieving|relieved|last working day|experience certificate|resignation"),
    ],
    "EXPERIENCE_LETTER": [
        ("Experience / Service certificate", 35, r"experience (certificate|letter)|service certificate|certificate of (employment|service|experience)"),
        ("to whom it may concern / this is to certify", 20, r"to whom(so)?ever it may concern|this is to certify"),
        ("employment period", 20, r"(was|has been) (employed|working|associated)|worked (with|in|for)|period of employment|from .* (to|till)"),
        ("designation", 10, r"designation|capacity of|position of|as an?"),
        ("conduct / wishes", 10, r"conduct|wish (him|her|them) (all the best|success)|good character|satisfactory"),
        ("company identity", 5, r"pvt\.? ltd|private limited|limited|llp|inc\b|technologies|solutions"),
        ("is an offer letter", -20, r"offer (letter|of employment)|pleased to offer|appointment letter|date of joining"),
        ("is a pay slip", -30, r"pay ?slip|salary slip|net pay|deductions"),
    ],
    "RELIEVING_LETTER": [
        ("Relieving letter / relieved", 40, r"reliev(ing|ed)|relieve(s|d)? (you|him|her)|resignation (acceptance|accepted)|acceptance of resignation"),
        ("last working day", 25, r"last (working )?day|last date of (working|employment)|final day|closing of (his|her|your) employment"),
        ("full and final / dues", 15, r"full and final|f ?& ?f|settlement|dues|no dues|handover"),
        ("wishes", 10, r"wish (you|him|her) (all the best|success)|future endeavou?rs"),
        ("company identity", 10, r"pvt\.? ltd|private limited|limited|llp|inc\b|technologies|solutions|human resources|\bhr\b"),
        ("is an offer letter", -30, r"offer (letter|of employment)|pleased to offer|appointment letter"),
        ("is a pay slip", -30, r"pay ?slip|salary slip|net pay|deductions"),
    ],
    "PAY_SLIP": [
        ("Pay slip / Salary slip", 30, r"pay ?slip|salary slip|salary statement|pay statement|payslip|salary for the month|वेतन"),
        ("earnings & deductions", 25, r"earn\w*|deduction|gross ?(pay|salary)|net ?(pay|salary|amount)|take.?home"),
        ("salary components", 20, r"\bbasic\b|\bhra\b|house rent|provident fund|\bpf\b|professional tax|\bpt\b|\btds\b|allowance|\bda\b|\blta\b"),
        ("employee / pay period", 15, r"employee (id|code|no|name)|emp\.? ?(id|code|no)|pay period|pay date|month of|days? (paid|worked)|\bLOP\b"),
        ("bank / UAN / PAN", 10, r"\buan\b|bank (a/c|account)|\bpan\b|ifsc|esi"),
        ("amount in words / signatures", 10, r"amount in words|employer signature|employee signature|authori[sz]ed signatory"),
        ("is a bank statement", -30, r"statement of account|account statement|closing balance|opening balance|withdrawal"),
        ("is a Form 16", -30, r"form no\.? ?16|form 16|certificate under section 203"),
    ],
    "BANK_STATEMENT": [
        ("Account statement", 30, r"statement of account|account statement|bank statement|statement (for|period)|transaction (statement|details)|passbook"),
        ("transactions / balances", 30, r"opening balance|closing balance|withdrawal|deposit|\bdebit\b|\bcredit\b|balance|\bdr\b|\bcr\b|\bupi\b|\bneft\b|\bimps\b|\brtgs\b"),
        ("account number / IFSC / branch", 20, r"account (no|number)|a/c (no|number)|ifsc|micr|branch|customer id|cif"),
        ("bank name", 15, r"\bbank\b|\bsbi\b|\bhdfc\b|\bicici\b|axis|kotak|\bpnb\b|canara|\bbob\b|union|indusind|yes bank|idfc|federal|indian bank|बैंक"),
        ("date range", 5, r"from .* to|period|as on"),
        ("is a pay slip", -20, r"pay ?slip|salary slip|earnings|deductions|net pay"),
    ],
    "FORM16": [
        ("Form 16 / TDS certificate", 35, r"form no\.? ?16|form 16|form ?16a|certificate under section 203|tds certificate|tax deducted at source"),
        ("ITR / acknowledgement", 25, r"income tax return|\bitr\b|itr-?v|acknowledgement number|e-filing|acknowledg"),
        ("assessment year / deductor", 20, r"assessment year|financial year|deductor|deductee|\btan\b|traces"),
        ("income tax department", 10, r"income tax department|income-tax|आयकर"),
        ("PAN / gross total income", 10, r"\bpan\b|gross total income|taxable income|tax payable|refund"),
        ("is a pay slip", -20, r"pay ?slip|salary slip|net pay|days? paid"),
    ],
    "POLICE_CLEARANCE": [
        ("Police clearance / verification", 45, r"police (clearance|verification)|clearance certificate|verification certificate|character certificate"),
        ("police / superintendent", 25, r"\bpolice\b|superintendent|commissioner|police station|पुलिस"),
        ("no adverse / criminal record", 20, r"no (adverse|criminal)|not involved|no case|criminal (record|case)|antecedent"),
        ("applicant details", 10, r"applicant|s/o|d/o|w/o|resident of|address"),
    ],
    "CONSENT_FORM": [
        ("consent / authorisation", 40, r"consent|authori[sz](e|ation)|hereby (authori[sz]e|consent|agree|declare)"),
        ("background verification", 30, r"background (verification|check|screening)|\bbgv\b|verification (of|process)|verify my"),
        ("declaration / signature", 20, r"declar|signature|signed|date:|place:"),
        ("the candidate", 10, r"\bi,?\s|undersigned|applicant|candidate"),
    ],
    "ADDRESS_PROOF_OTHER": [
        ("utility bill", 35, r"electricity|power|energy|water bill|gas|piped|telephone|broadband|landline|\bbill\b|consumer (no|number|id)|units? consumed|meter"),
        ("rent / lease agreement", 35, r"rent(al)? agreement|lease (deed|agreement)|leave and licen[cs]e|lessor|lessee|tenant|landlord|owner"),
        ("ration card", 20, r"ration card|राशन कार्ड|public distribution|food (and )?civil supplies"),
        ("address / due date", 10, r"address|due date|bill date|billing period|amount (due|payable)"),
    ],
    "RESUME": [
        ("Resume / CV", 35, r"\bresume\b|curriculum vitae|\bcv\b|career objective|professional summary|summary"),
        ("sections", 35, r"\bskills\b|work experience|experience|education|projects|achievements|certifications|hobbies|interests"),
        ("contact / links", 20, r"linkedin|github|email|mobile|phone|@"),
        ("technologies", 10, r"python|java|javascript|sql|react|node|aws|azure|docker|html|css"),
    ],
}

DOC_TYPES: set[str] = set(RULES) | _IMAGE_ONLY

_COMPILED: dict[str, list[tuple[str, int, re.Pattern]]] = {
    t: [(label, w, re.compile(p, re.I)) for label, w, p in rules] for t, rules in RULES.items()
}

# Rules a regex cannot express: (label, weight, predicate over the text).
# Declared after the number helpers below; filled in at the end of that
# section.
CHECKS: dict[str, list[tuple[str, int, "Callable[[str], bool]"]]] = {}


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
MATCH, MISMATCH, UNCERTAIN, UNVERIFIED = "MATCH", "MISMATCH", "UNCERTAIN", "UNVERIFIED"


@dataclass
class Verdict:
    status: str                         # MATCH | MISMATCH | UNCERTAIN | UNVERIFIED
    doc_type: str | None = None         # the best-scoring type
    score: int = 0                      # 0-100 for the field's expected type(s)
    id_number: str | None = None        # number read off a PAN / Aadhaar
    id_match: bool | None = None        # vs. the profile; None = not compared
    note: str = ""                      # one line for the candidate / HR
    blocked: bool = False               # True only when the upload was refused
    evidence: list[str] = field(default_factory=list)   # rule labels that matched
    scores: dict[str, int] = field(default_factory=dict)  # every type's score

    def as_dict(self) -> dict:
        return {
            "status": self.status, "doc_type": self.doc_type, "score": self.score,
            "id_match": self.id_match, "note": self.note, "blocked": self.blocked,
            "evidence": self.evidence,
        }


def _ocr_available() -> bool:
    cmd = settings.TESSERACT_CMD
    if cmd and os.path.isfile(cmd):
        return True
    import shutil
    return shutil.which("tesseract") is not None


def enabled() -> bool:
    mode = (settings.DOC_VALIDATION_MODE or "off").lower()
    return mode in ("warn", "block") and _ocr_available()


def blocking() -> bool:
    return (settings.DOC_VALIDATION_MODE or "").lower() == "block"


# ---------------------------------------------------------------------------
# Identity numbers: shape and comparison
# ---------------------------------------------------------------------------
_PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")

# Verhoeff tables — the checksum UIDAI appends as the 12th Aadhaar digit.
_V_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6], [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4], [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_V_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2], [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def normalize_pan(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def normalize_aadhaar(value: str | None) -> str:
    """Digits only; masked positions become X so 'XXXX XXXX 1234' survives."""
    return re.sub(r"[^0-9X]", "", (value or "").upper().replace("*", "X"))


def is_valid_pan(value: str | None) -> bool:
    return bool(_PAN_RE.match(normalize_pan(value)))


def is_valid_aadhaar(value: str | None) -> bool:
    """12 digits with a correct Verhoeff check digit. A masked number (XXXX
    XXXX 1234) is well-formed by definition — the checksum can't be tested."""
    digits = normalize_aadhaar(value)
    if len(digits) != 12:
        return False
    if "X" in digits:
        return digits[-4:].isdigit() and digits[:-4].strip("X") == ""
    if digits[0] in "01":
        return False   # UIDAI never issues numbers starting with 0 or 1
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _V_D[c][_V_P[i % 8][int(ch)]]
    return c == 0


# OCR reads O for 0, I/l for 1, S for 5, B for 8 and back. A PAN has a fixed
# letter/digit layout, so each position can be corrected with confidence.
_TO_DIGIT = str.maketrans("OoIl|SsBbZzQD", "0011155882200")
_TO_ALPHA = str.maketrans("0158", "OISB")


def find_pan(text: str) -> str | None:
    """The first PAN-shaped token in OCR text, after fixing look-alike characters."""
    for tok in re.findall(r"(?<![A-Za-z0-9])[A-Za-z0-9]{10}(?![A-Za-z0-9])", text):
        fixed = (tok[:5].upper().translate(_TO_ALPHA) + tok[5:9].translate(_TO_DIGIT)
                 + tok[9].upper().translate(_TO_ALPHA))
        if _PAN_RE.match(fixed):
            return fixed
    return None


def find_aadhaar(text: str) -> str | None:
    """The first 12-digit group (Verhoeff-valid preferred) or masked number."""
    fixed = text.translate(_TO_DIGIT) if re.search(r"\d{3}[OoIl]", text) else text
    candidates = re.findall(_AADHAAR_TOKEN, fixed)
    valid = [c for c in candidates if is_valid_aadhaar(c)]
    if valid:
        return normalize_aadhaar(valid[0])
    masked = re.search(_AADHAAR_MASKED, text)
    if masked:
        return normalize_aadhaar(masked.group(0))
    return normalize_aadhaar(candidates[0]) if candidates else None


# A number that passes the Verhoeff check is strong evidence on its own: a
# random 12-digit string (phone number, account number) passes 1 time in 10.
# Weights sum to 100 with the AADHAAR rules above: the front of a card
# (आधार, valid number, Govt. of India, DOB, gender) scores 85, the back
# (UIDAI, valid number, Govt. of India) 70, and a front whose number fails
# the checksum 60.
def _has_valid_aadhaar(text: str) -> bool:
    return any(is_valid_aadhaar(c) for c in re.findall(_AADHAAR_TOKEN, text))


_AADHAAR_FRONT = re.compile(alt("govt_of_india"), re.I)
_DOB_ANY = re.compile(alt("dob"), re.I)
_GENDER_ANY = re.compile(alt("gender"), re.I)

_AADHAAR_BACK = re.compile(alt("uidai", "address") + r"|\b1947\b", re.I)

# Points, capped at 100 with the AADHAAR rules above. A front whose
# regional-script "Aadhaar" OCR could not read still reaches 80 (number 20,
# Govt. of India 10, DOB 5, gender 5, checksum 25, front layout 15); the
# back alone reaches 85 (UIDAI 15, helpline/address 10, number 20, checksum
# 25, back layout 15); a front whose number fails the checksum stays 40-60.
CHECKS["AADHAAR"] = [
    ("number passes the Aadhaar checksum", 25, _has_valid_aadhaar),
    # Either side of the card in any state language is a combination no
    # other document has, so it stands in for the word "Aadhaar" when OCR
    # cannot read the script it is printed in.
    ("Govt. of India + DOB + gender + valid number (card front layout)", 15,
     lambda text: bool(_AADHAAR_FRONT.search(text) and _DOB_ANY.search(text)
                       and _GENDER_ANY.search(text) and _has_valid_aadhaar(text))),
    ("UIDAI / address + valid number (card back layout)", 15,
     lambda text: bool(len(set(m.group(0).lower() for m in _AADHAAR_BACK.finditer(text))) >= 2
                       and _has_valid_aadhaar(text))),
]
CHECKS["PAN"] = [
    ("PAN number readable after OCR fix-ups", 10, lambda text: find_pan(text) is not None),
]


def compare_id(doc_type: str | None, read: str | None, on_profile: str | None) -> bool | None:
    """Does the number on the card agree with what the candidate typed?
    None when there is nothing to compare (no number read, nothing on the
    profile, or not an ID whose number the profile holds)."""
    if doc_type == "PAN":
        a, b = normalize_pan(read), normalize_pan(on_profile)
        if not (a and b and is_valid_pan(a)):
            return None
        return a == b
    if doc_type == "AADHAAR":
        a, b = normalize_aadhaar(read), normalize_aadhaar(on_profile)
        if not (a and b and is_valid_aadhaar(a) and len(b) == 12):
            return None
        if "X" in a:          # masked card: only the visible tail can be checked
            return a[-4:] == b[-4:]
        return a == b
    return None


def name_found(text: str, full_name: str | None) -> bool | None:
    """Does any part of the candidate's name (2+ letters) appear in the text?
    None when there is no name to look for. OCR mangles names, so a miss is
    only ever a hint."""
    tokens = {t for t in re.split(r"[^a-z]+", (full_name or "").lower()) if len(t) > 2}
    if not tokens or not text:
        return None
    low = text.lower()
    return any(t in low for t in tokens)


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------
_MAX_EDGE = 2600        # px; ~300 dpi for an A4 scan, plenty for a card
_MIN_EDGE = 2000        # small thumbnails are upscaled: Tesseract wants ~30 px letters


@dataclass
class Extracted:
    text: str = ""
    source: str = ""              # "pdf-text" | "ocr" | "docx" | ""
    pages: int = 0
    image: dict = field(default_factory=dict)   # stats of the first image, for PHOTO/SIGNATURE
    reason: str = ""              # why nothing could be extracted


def _load_image(data: bytes):
    from PIL import Image, ImageOps
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)            # phone photos carry rotation in EXIF
    return img.convert("RGB")


def image_stats(img) -> dict:
    """Cheap pixel statistics used to tell a photo from a signature from a
    document, without any text: how much of the frame is ink, how bright the
    background is, how colourful the image is, and its shape."""
    from PIL import ImageStat
    small = img.copy()
    small.thumbnail((256, 256))
    gray = small.convert("L")
    hist = gray.histogram()
    total = sum(hist) or 1
    dark = sum(hist[:96]) / total          # ink-ish pixels
    bright = sum(hist[200:]) / total       # paper-ish pixels
    hsv = small.convert("HSV")
    sat = ImageStat.Stat(hsv.getchannel("S")).mean[0] / 255.0
    w, h = img.size
    return {"dark": round(dark, 3), "bright": round(bright, 3),
            "saturation": round(sat, 3), "aspect": round(w / h, 3) if h else 1.0}


def _tesseract_cmd() -> str:
    if settings.TESSERACT_CMD and os.path.isfile(settings.TESSERACT_CMD):
        return settings.TESSERACT_CMD
    import shutil
    return shutil.which("tesseract") or "tesseract"


_LANG_CACHE: dict[str, str] = {}


def primary_languages() -> str:
    """The fast first pass: English plus Hindi by default. Fewer models
    means a cleaner read of English, which is what most documents and every
    rule's heaviest evidence are in."""
    return (settings.OCR_PRIMARY_LANGUAGES or "eng").strip()


def ocr_languages() -> str:
    """The `-l` argument for the full pass. "auto" means every pack
    installed in TESSDATA_DIR that the lexicon knows words for, English
    first — reads a bilingual card whatever state issued it. Resolved once."""
    configured = (settings.OCR_LANGUAGES or "auto").strip()
    if configured.lower() != "auto":
        return configured
    if "auto" not in _LANG_CACHE:
        installed = set()
        try:
            installed = {f[:-len(".traineddata")] for f in os.listdir(settings.TESSDATA_DIR)
                         if f.endswith(".traineddata") and f != "osd.traineddata"}
        except OSError:
            pass
        langs = [l for l in _LEXICON_LANGUAGES if l in installed] or ["eng"]
        _LANG_CACHE["auto"] = "+".join(langs)
    return _LANG_CACHE["auto"]


def run_tesseract(png: bytes, langs: str | None = None) -> str:
    """OCR one PNG image by piping it through tesseract.exe.

    Deliberately not pytesseract: that library writes the image to a
    temporary .png and reads Tesseract's output back from a temporary
    file, and endpoint-protection on managed Windows machines denies
    unknown processes writing media files (the same reason uploads are
    stored with a .dat extension). stdin -> stdout touches no files.
    """
    env = dict(os.environ)
    # Language packs live in the project (Program Files is not writable
    # without admin); TESSDATA_PREFIX is how Tesseract is told where.
    if settings.TESSDATA_DIR and os.path.isdir(settings.TESSDATA_DIR):
        env["TESSDATA_PREFIX"] = settings.TESSDATA_DIR
    proc = subprocess.run(
        [_tesseract_cmd(), "stdin", "stdout", "-l", langs or ocr_languages()],
        input=png, capture_output=True, timeout=settings.OCR_TIMEOUT, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"tesseract exited {proc.returncode}: "
                           f"{proc.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return proc.stdout.decode("utf-8", "replace")


def _ocr_image(img, langs: str | None = None) -> str:
    gray = img.convert("L")
    w, h = gray.size
    longest = max(w, h)
    if longest < _MIN_EDGE:
        scale = _MIN_EDGE / longest
        gray = gray.resize((int(w * scale), int(h * scale)))
    elif longest > _MAX_EDGE:
        gray.thumbnail((_MAX_EDGE, _MAX_EDGE))

    def run(im):
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return run_tesseract(buf.getvalue(), langs)

    text = run(gray)
    # A phone photo taken sideways reads as gibberish; if the first pass
    # found almost nothing, try the other orientations before giving up.
    if len(_words(text)) < 6:
        for angle in (90, 270, 180):
            alt = run(gray.rotate(angle, expand=True))
            if len(_words(alt)) > len(_words(text)):
                text = alt
            if len(_words(text)) >= 6:
                break
    return text


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[A-Za-zऀ-ॿ]{2,}|\d{2,}", text or "")]


def docx_text(data: bytes, limit: int = 20000) -> str:
    """Plain text of a .docx (word/document.xml paragraphs), no dependencies."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError):
        return ""
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paras = []
    for p in ElementTree.fromstring(xml).iter(f"{ns}p"):
        line = "".join(t.text or "" for t in p.iter(f"{ns}t")).strip()
        if line:
            paras.append(line)
    return "\n".join(paras)[:limit]


def docx_images(data: bytes) -> list[bytes]:
    """Pictures embedded in a .docx (word/media/*), in document order."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = sorted(n for n in z.namelist()
                           if n.startswith("word/media/")
                           and n.lower().rsplit(".", 1)[-1] in ("png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff"))
            return [z.read(n) for n in names[:settings.DOC_VALIDATION_MAX_PAGES]]
    except (zipfile.BadZipFile, KeyError):
        return []


def _extract_docx(data: bytes, langs: str | None = None) -> Extracted:
    """Typed text first; when the document is really just a pasted scan or
    photo of the card, OCR the embedded pictures instead."""
    text = docx_text(data)
    if len(_words(text)) >= 8:
        return Extracted(text, "docx", 1)
    texts, stats = [text] if text else [], {}
    for blob in docx_images(data):
        try:
            img = _load_image(blob)
        except Exception:
            continue
        if not stats:
            stats = image_stats(img)
        texts.append(_ocr_image(img, langs))
    if not texts and not stats:
        return Extracted(reason="no text or pictures in document")
    return Extracted("\n".join(texts), "ocr" if stats else "docx", 1, stats)


def extract(data: bytes, content_type: str | None, filename: str,
            langs: str | None = None) -> Extracted:
    """Text (and first-image statistics) of one file. `langs` is the
    Tesseract language list for any OCR involved; None means the full set."""
    head, ext = data[:16], (filename or "").lower().rsplit(".", 1)[-1]
    try:
        if head.startswith(b"%PDF-"):
            return _extract_pdf(data, langs)
        if head.startswith(b"PK\x03\x04") or ext == "docx":
            return _extract_docx(data, langs)
        if head.startswith(b"\xd0\xcf\x11\xe0"):
            return Extracted(reason="legacy .doc files are not checked")
        img = _load_image(data)
        stats = image_stats(img)
        return Extracted(_ocr_image(img, langs), "ocr", 1, stats)
    except Exception as exc:              # a corrupt file must not break the upload
        # Logged in full so an environment problem (OCR denied, missing
        # language pack) is diagnosable from the server console; the row
        # only carries the short version.
        print(f"[doc_validator] could not read {filename!r}: {type(exc).__name__}: {exc}")
        return Extracted(reason=f"could not read file ({type(exc).__name__})")


def _extract_pdf(data: bytes, langs: str | None = None) -> Extracted:
    import fitz  # PyMuPDF
    limit = settings.DOC_VALIDATION_MAX_PAGES
    texts, stats, pages = [], {}, 0
    with fitz.open(stream=data, filetype="pdf") as pdf:
        for page in pdf:
            if pages >= limit:
                break
            pages += 1
            layer = page.get_text().strip()
            if len(_words(layer)) >= 8:          # a real text layer, not a stray watermark
                texts.append(layer)
                continue
            pix = page.get_pixmap(dpi=200)
            img = _load_image(pix.tobytes("png"))
            if not stats:
                stats = image_stats(img)
            texts.append(_ocr_image(img, langs))
    if not pages:
        return Extracted(reason="empty PDF")
    source = "pdf-text" if not stats else "ocr"
    return Extracted("\n".join(texts), source, pages, stats)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def score_text(text: str) -> tuple[dict[str, int], dict[str, list[str]]]:
    """Every text-based type's score (0-100) and the rule labels that matched."""
    """Weights are points, summed and capped at 100 — not a share of every
    rule's weight — so a type whose rules total more than 100 offers
    alternative routes to a full score (the front of an Aadhaar card reads
    differently from its back; both are Aadhaar cards). Most types' rules
    total exactly 100, which makes the two readings identical for them."""
    flat = re.sub(r"\s+", " ", text or "")
    scores, evidence = {}, {}
    for doc_type, rules in _COMPILED.items():
        got, hits = 0, []
        for label, weight, pattern in rules:
            if pattern.search(flat):
                got += weight
                hits.append(label if weight > 0 else f"but: {label}")
        for label, weight, predicate in CHECKS.get(doc_type, []):
            if predicate(flat):
                got += weight
                hits.append(label)
        scores[doc_type] = max(0, min(100, got))
        evidence[doc_type] = hits
    return scores, evidence


def score_image_only(stats: dict, word_count: int) -> tuple[dict[str, int], dict[str, list[str]]]:
    """PHOTO and SIGNATURE from pixels. Both start from "hardly any printed
    text"; then a signature is ink on paper and a photo is a colourful,
    roughly portrait frame that is not mostly white."""
    scores, evidence = {"PHOTO": 0, "SIGNATURE": 0}, {"PHOTO": [], "SIGNATURE": []}
    # A page of printed text is a document, whatever its colours: a passport
    # data page must not score as a photo because it is portrait and dark.
    if not stats or word_count > 8:
        return scores, evidence
    little_text = word_count <= 4
    dark, bright, sat = stats["dark"], stats["bright"], stats["saturation"]
    aspect = stats["aspect"]

    s, ev = 0, []
    if little_text:
        s += 40; ev.append("no printed text")
    if 0.003 <= dark <= 0.30:
        s += 25; ev.append("ink strokes on a blank background")
    if bright >= 0.65:
        s += 20; ev.append("mostly white paper")
    if sat <= 0.25:
        s += 15; ev.append("black/blue ink, no colour")
    scores["SIGNATURE"], evidence["SIGNATURE"] = min(100, s), ev

    s, ev = 0, []
    if little_text:
        s += 40; ev.append("no printed text")
    if bright < 0.55:
        s += 25; ev.append("filled frame, not a white page")
    if sat > 0.15:
        s += 20; ev.append("colour photograph")
    if 0.6 <= aspect <= 1.1:
        s += 15; ev.append("portrait-shaped")
    scores["PHOTO"], evidence["PHOTO"] = min(100, s), ev
    return scores, evidence


def decide(field_key: str, extracted: Extracted | None, profile: dict | None) -> Verdict:
    """Turn extracted text/pixels into a Verdict. Pure — no I/O — so the
    rules are unit-testable with plain strings."""
    expected = EXPECTED.get(field_key)
    if not expected:
        return Verdict(UNVERIFIED, note="This field is not checked.")
    if extracted is None or (not extracted.source and not extracted.image):
        reason = (extracted.reason if extracted else "") or "the document could not be read"
        return Verdict(UNVERIFIED, note=f"Not checked: {reason}.")

    text = extracted.text or ""
    words = _words(text)
    scores, evidence = score_text(text)
    img_scores, img_evidence = score_image_only(extracted.image, len(words))
    scores.update(img_scores)
    evidence.update(img_evidence)
    profile = profile or {}

    # The score that matters is the best among what the slot accepts.
    exp_type = max(expected, key=lambda t: scores.get(t, 0))
    score = scores.get(exp_type, 0)
    best_type = max(scores, key=scores.get)
    best = scores[best_type]
    ev = [e for e in evidence.get(exp_type, []) if not e.startswith("but:")]
    # "; " because some evidence labels contain commas themselves — the HR
    # badge splits on it to show one chip per sign.
    found = "; ".join(ev[:4])
    match_at, mismatch_at = settings.DOC_VALIDATION_MATCH_SCORE, settings.DOC_VALIDATION_MISMATCH_SCORE

    # Identity number and name, only when the slot wanted an ID card.
    id_number = id_match = None
    if exp_type == "PAN":
        id_number = find_pan(text)
        id_match = compare_id("PAN", id_number, profile.get("pan_number"))
    elif exp_type == "AADHAAR":
        id_number = find_aadhaar(text)
        id_match = compare_id("AADHAAR", id_number, profile.get("aadhaar_number"))
    name_ok = name_found(text, profile.get("full_name")) if exp_type in _ID_CARDS else None

    common = dict(doc_type=best_type, score=score, id_number=id_number, id_match=id_match,
                  evidence=ev, scores=scores)

    # No text where text was expected. If the pixels say it is a signature
    # or a photo, say so; otherwise it is a bad scan, not a wrong document.
    if len(words) < 4 and exp_type not in _IMAGE_ONLY:
        if best_type in _IMAGE_ONLY and best >= match_at:
            return Verdict(MISMATCH, note=f"This looks like {describe_type(best_type)} (score {best}%), "
                                          f"not {describe_type(exp_type)}. Please upload the correct "
                                          "document here.", **common)
        return Verdict(UNCERTAIN, note="No readable text was found — the scan may be blurry, "
                                       "dark or sideways. Please upload a clearer copy, or HR "
                                       "may ask for it again.", **common)

    if score >= match_at:
        if id_match is False:
            label = "PAN" if exp_type == "PAN" else "Aadhaar"
            return Verdict(UNCERTAIN, note=f"This is {describe_type(exp_type)} (score {score}%), but "
                                           f"its number does not match the {label} number on your profile. "
                                           "Check you uploaded your own card and typed the number correctly.",
                           **common)
        note = f"Looks like {describe_type(exp_type)} (score {score}%: {found})."
        if id_match:
            note += " Number matches your profile."
        elif name_ok is False:
            note += " Your name could not be read on it — HR will verify."
        return Verdict(MATCH, note=note, **common)

    if score < mismatch_at:
        if best_type != exp_type and best >= match_at and best_type not in expected:
            hint = f"This looks like {describe_type(best_type)} (score {best}%), not " \
                   f"{describe_type(exp_type)} (score {score}%)."
        else:
            hint = f"This does not look like {describe_type(exp_type)} (score {score}%" \
                   + (f": only {found}" if found else "") + ")."
        return Verdict(MISMATCH, note=hint + " Please upload the correct document here.", **common)

    note = f"This may be {describe_type(exp_type)} (score {score}%: {found}), but the check was not certain."
    if best_type != exp_type and best_type not in expected and best > score:
        note += f" It also resembles {describe_type(best_type)} ({best}%)."
    return Verdict(UNCERTAIN, note=note + " HR will confirm.", **common)


# ---------------------------------------------------------------------------
# Entry point + cache
# ---------------------------------------------------------------------------
_CACHE: dict[str, tuple[float, Verdict]] = {}
_CACHE_TTL = 15 * 60
_CACHE_MAX = 500
_lock = threading.Lock()


def _cache_key(data: bytes, field_key: str, profile: dict | None) -> str:
    h = hashlib.sha256(data)
    h.update(field_key.encode())
    for k in ("pan_number", "aadhaar_number", "full_name"):
        h.update(((profile or {}).get(k) or "").encode())
    return h.hexdigest()


def _cache_get(key: str) -> Verdict | None:
    with _lock:
        hit = _CACHE.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        _CACHE.pop(key, None)
        return None


def _cache_put(key: str, verdict: Verdict) -> None:
    with _lock:
        if len(_CACHE) >= _CACHE_MAX:
            now = time.time()
            for k in [k for k, (exp, _) in _CACHE.items() if exp <= now]:
                del _CACHE[k]
            while len(_CACHE) >= _CACHE_MAX:
                del _CACHE[next(iter(_CACHE))]
        _CACHE[key] = (time.time() + _CACHE_TTL, verdict)


def validate(data: bytes, content_type: str | None, filename: str, field_key: str,
             profile: dict | None = None) -> Verdict:
    """Score one uploaded file against the field it was uploaded to.

    Never raises: any failure — OCR missing, unreadable file, unknown field —
    comes back as UNVERIFIED so the caller can store it and move on.
    `blocked` is set when the mode is "block" and the score says mismatch;
    refusing the upload is the caller's job (it knows how to clean up)."""
    if not enabled():
        return Verdict(UNVERIFIED, note="Document check is switched off.")
    if field_key not in EXPECTED:
        return Verdict(UNVERIFIED, note="This field is not checked.")

    key = _cache_key(data, field_key, profile)
    cached = _cache_get(key)
    if cached:
        return cached

    # Pass 1: English (+Hindi). Most documents are decided here in ~0.3 s,
    # and fewer language models give the cleanest read of the English.
    first = extract(data, content_type, filename, primary_languages())
    verdict = decide(field_key, first, profile)
    # Pass 2, only when pass 1 was not a clear match and OCR was actually
    # involved: every installed script, so the regional half of a card or a
    # state-board certificate counts too. Whichever pass scores higher for
    # the field's expected type wins — a wider net must never make a
    # document look worse than the narrow one did.
    if (verdict.status != MATCH and first.source == "ocr"
            and ocr_languages() != primary_languages()):
        second = decide(field_key, extract(data, content_type, filename, ocr_languages()), profile)
        if second.score > verdict.score or (second.score == verdict.score
                                            and second.status == MATCH):
            verdict = second
    verdict.blocked = blocking() and verdict.status == MISMATCH
    _cache_put(key, verdict)
    return verdict


def validate_path(path: str, content_type: str | None, filename: str, field_key: str,
                  profile: dict | None = None) -> Verdict:
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return Verdict(UNVERIFIED, note="Not checked: file could not be read.")
    return validate(data, content_type, filename, field_key, profile)


def profile_dict(profile) -> dict:
    """The three profile values the rules compare against, from a
    CandidateProfile row (or None)."""
    if profile is None:
        return {}
    return {k: getattr(profile, k, None) for k in ("pan_number", "aadhaar_number", "full_name")}


def apply_to(doc, verdict: Verdict) -> None:
    """Record a verdict on a Document / FormDraftDocument row. (`ai_confidence`
    holds the 0-100 score.)"""
    from datetime import datetime, timezone
    doc.ai_status = verdict.status
    doc.ai_doc_type = verdict.doc_type
    doc.ai_confidence = verdict.score
    doc.ai_id_match = verdict.id_match
    doc.ai_note = verdict.note[:1000] if verdict.note else None
    doc.ai_checked_at = datetime.now(timezone.utc)


def copy_between(src, dst) -> None:
    """Carry a stored verdict from one row to another (draft -> document,
    or a document copied to a later form) without re-running OCR."""
    for col in ("ai_status", "ai_doc_type", "ai_confidence", "ai_id_match",
                "ai_note", "ai_checked_at"):
        setattr(dst, col, getattr(src, col, None))
