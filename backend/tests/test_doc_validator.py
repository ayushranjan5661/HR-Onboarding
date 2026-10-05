"""Rule layer of the document check — text in, verdict out. No OCR here:
`decide()` is fed the text an OCR pass would have produced.

Run from backend/:  ../myenv/Scripts/python.exe -m pytest tests -q
"""
import io
import zipfile

from app.agents import doc_validator as dv
from app.form_definitions import BGV_FILE_FIELDS, CIF_FILE_FIELDS, DOC_FILE_FIELDS


def _ex(text, image=None, source="ocr"):
    return dv.Extracted(text=text, source=source, pages=1, image=image or {})


def test_every_file_field_has_an_expectation():
    for key in CIF_FILE_FIELDS + BGV_FILE_FIELDS + DOC_FILE_FIELDS:
        assert key in dv.EXPECTED, key
    for types in dv.EXPECTED.values():
        assert types <= dv.DOC_TYPES


# --- identity numbers -------------------------------------------------------

def test_pan_shape_and_ocr_fixups():
    assert dv.is_valid_pan("ABCDE1234F")
    assert not dv.is_valid_pan("ABCD12345F")
    assert dv.find_pan("Permanent Account Number ABCDE1234F Name") == "ABCDE1234F"
    assert dv.find_pan("Number ABCDEl234F") == "ABCDE1234F"     # l read for 1
    assert dv.find_pan("Number ABCDE12O4F") == "ABCDE1204F"     # O read for 0
    assert dv.find_pan("nothing here 1234567890") is None


def test_aadhaar_verhoeff_and_extraction():
    assert dv.is_valid_aadhaar("9999 4105 7058")        # UIDAI sample, valid check digit
    assert not dv.is_valid_aadhaar("9999 4105 7059")
    assert not dv.is_valid_aadhaar("1234 5678 9012")    # never starts with 0/1
    assert dv.is_valid_aadhaar("XXXX XXXX 7058")
    assert dv.find_aadhaar("Aadhaar no. 9999 4105 7058 DOB 01/01/1990") == "999941057058"
    assert dv.find_aadhaar("XXXX XXXX 7058") == "XXXXXXXX7058"
    assert dv.find_aadhaar("no number") is None


def test_compare_id():
    assert dv.compare_id("PAN", "abcde1234f", "ABCDE 1234 F") is True
    assert dv.compare_id("PAN", "ABCDE1234F", "ABCDE1234G") is False
    assert dv.compare_id("PAN", None, "ABCDE1234F") is None
    assert dv.compare_id("AADHAAR", "999941057058", "9999 4105 7058") is True
    assert dv.compare_id("AADHAAR", "XXXXXXXX7058", "999941057058") is True
    assert dv.compare_id("AADHAAR", "XXXXXXXX1111", "999941057058") is False
    assert dv.compare_id("AADHAAR", "999941057058", None) is None


# --- scoring ----------------------------------------------------------------

AADHAAR_TEXT = """भारत सरकार Government of India
Unique Identification Authority of India
Test Person  जन्म तिथि / DOB: 01/01/1990  पुरुष / MALE
9999 4105 7058
मेरा आधार, मेरी पहचान   help@uidai.gov.in"""

PAN_TEXT = """आयकर विभाग INCOME TAX DEPARTMENT   भारत सरकार GOVT. OF INDIA
Permanent Account Number Card
ABCDE1234F
Name TEST PERSON  Father's Name TEST FATHER  Date of Birth 01/01/1990  Signature"""

MARKSHEET_10_TEXT = """CENTRAL BOARD OF SECONDARY EDUCATION
SECONDARY SCHOOL EXAMINATION (CLASS X) 2015
STATEMENT OF MARKS   Roll No. 1234567
Subject  Marks Obtained  Grade
ENGLISH 91 A1  MATHEMATICS 95 A1  SCIENCE 88 A2  Total 452"""

MARKSHEET_12_TEXT = """CENTRAL BOARD OF SECONDARY EDUCATION
SENIOR SCHOOL CERTIFICATE EXAMINATION (CLASS XII) 2017
STATEMENT OF MARKS   Roll No. 7654321
PHYSICS 85  CHEMISTRY 80  MATHEMATICS 92  Total 428"""

TC_TEXT = """ST. XAVIER'S HIGH SCHOOL
TRANSFER CERTIFICATE
Name of pupil: Test Person   Date of admission: 01/04/2005
Class in which studying: X   Date of leaving: 30/04/2015
Conduct: Good   Reason for leaving: Completed course   Principal"""

PAYSLIP_TEXT = """ACME TECHNOLOGIES PVT LTD   Pay Slip for the month of March 2026
Employee Code E123  Days Paid 31   UAN 1001
Earnings: Basic 40000  HRA 16000  Special Allowance 9000
Deductions: Provident Fund 1800  Professional Tax 200  TDS 3000
Gross Pay 65000  Net Pay 60000"""


def test_aadhaar_scores_high_and_number_matches():
    v = dv.decide("aadhar_card", _ex(AADHAAR_TEXT),
                  {"aadhaar_number": "999941057058", "full_name": "Test Person"})
    assert v.status == dv.MATCH and v.score >= 80 and v.id_match is True
    assert "Number matches" in v.note


def test_pan_in_aadhaar_slot_is_a_mismatch_naming_pan():
    v = dv.decide("aadhar_card", _ex(PAN_TEXT), {})
    assert v.status == dv.MISMATCH
    assert v.doc_type == "PAN" and "PAN card" in v.note and "Aadhaar" in v.note


def test_pan_matches_and_cross_checks_profile():
    ok = dv.decide("pan_card", _ex(PAN_TEXT), {"pan_number": "ABCDE1234F", "full_name": "Test Person"})
    assert ok.status == dv.MATCH and ok.id_match is True
    other = dv.decide("pan_card", _ex(PAN_TEXT), {"pan_number": "ZZZZZ9999Z"})
    assert other.status == dv.UNCERTAIN and other.id_match is False
    assert "does not match" in other.note


def test_marksheets_tell_10_from_12():
    assert dv.decide("marksheet_10", _ex(MARKSHEET_10_TEXT), {}).status == dv.MATCH
    assert dv.decide("marksheet_12_diploma", _ex(MARKSHEET_12_TEXT), {}).status == dv.MATCH
    twelve_in_ten = dv.decide("marksheet_10", _ex(MARKSHEET_12_TEXT), {})
    assert twelve_in_ten.status in (dv.MISMATCH, dv.UNCERTAIN)
    assert twelve_in_ten.score < 80


def test_transfer_certificate_is_not_a_marksheet():
    v = dv.decide("marksheet_10", _ex(TC_TEXT), {})
    assert v.status == dv.MISMATCH
    assert v.doc_type == "TRANSFER_CERTIFICATE" and "transfer" in v.note


def test_pay_slip_and_wrong_slot():
    assert dv.decide("cc_pay_slips", _ex(PAYSLIP_TEXT), {}).status == dv.MATCH
    v = dv.decide("cc_experience_letter", _ex(PAYSLIP_TEXT), {})
    assert v.status == dv.MISMATCH and "pay slip" in v.note


def test_alternates_satisfy_the_slot():
    tc_like_relieving = """ACME TECHNOLOGIES PVT LTD  RELIEVING LETTER
    This is to confirm that Test Person has been relieved from the services of the company.
    Last working day: 30 June 2025. We wish you all the best for your future endeavours. HR"""
    assert dv.decide("cc_experience_letter", _ex(tc_like_relieving), {}).status == dv.MATCH
    assert dv.decide("id_proof", _ex(PAN_TEXT), {}).status == dv.MATCH
    assert dv.decide("address_proof", _ex(AADHAAR_TEXT), {}).status == dv.MATCH


def test_blank_scan_is_uncertain_not_mismatch():
    v = dv.decide("pan_card", _ex("  \n .. "), {})
    assert v.status == dv.UNCERTAIN and "readable text" in v.note


def test_signature_and_photo_from_pixels():
    sig = _ex("", image={"dark": 0.04, "bright": 0.9, "saturation": 0.05, "aspect": 3.0})
    v = dv.decide("signature", sig, {})
    assert v.status == dv.MATCH and v.doc_type == "SIGNATURE"

    photo = _ex("", image={"dark": 0.3, "bright": 0.2, "saturation": 0.4, "aspect": 0.8})
    v = dv.decide("passport_size_photo", photo, {})
    assert v.status == dv.MATCH and v.doc_type == "PHOTO"

    # A PAN card in the signature slot: lots of text, so no signature score.
    v = dv.decide("signature", _ex(PAN_TEXT, image={"dark": 0.2, "bright": 0.5, "saturation": 0.3, "aspect": 1.6}), {})
    assert v.status == dv.MISMATCH and "PAN card" in v.note

    # A signature in the PAN slot is named as such; a blank/blurry scan is not.
    v = dv.decide("pan_card", sig, {})
    assert v.status == dv.MISMATCH and "signature" in v.note
    blank = _ex("", image={"dark": 0.0, "bright": 0.99, "saturation": 0.0, "aspect": 1.4})
    assert dv.decide("pan_card", blank, {}).status == dv.UNCERTAIN


def test_aadhaar_card_front_as_ocr_reads_it():
    # The front of a real card, with the OCR damage seen on samples: no UIDAI
    # (that is on the back), "भारत" lost, Devanagari आधार present.
    text = """fia] सरकार Government of India निरंजन कुमार Test Person
    जन्म तिथि / DOB : 12/04/2000 पुरुष / Male 9999 4105 7058 आधार -. आम आदमी का अधिकार"""
    v = dv.decide("aadhar_card", _ex(text), {})
    assert v.status == dv.MATCH and "checksum" in " ".join(v.evidence)
    # The same layout with a phone-number-like 12 digits is not enough.
    weak = text.replace("9999 4105 7058", "9876 5432 1098")
    assert dv.decide("aadhar_card", _ex(weak), {}).status == dv.UNCERTAIN


def test_pan_card_as_ocr_reads_it():
    # Coloured header loses its first letters; Hindi labels survive.
    text = """OME TAX DEPARTMENT GOVT. OF IND स्थायी लेखा संख्या कार्ड Permanent Account Number Card
    ABCDE1234F APPLICANT NAME नाम / Father's Name FATHER NAME /06/1995 हस्ताक्षर | Signature"""
    v = dv.decide("pan_card", _ex(text), {"pan_number": "ABCDE1234F"})
    assert v.status == dv.MATCH and v.id_match is True


def test_state_board_marksheet_without_the_word_board():
    text = """2972081 हाईस्कूल परीक्षा -२०१७ High School Examination - 2017
    प्रमाणपत्र-सह-अंकपत्र (CERTIFICATE-CUM-MARKS SHEET) This is to certify that SHAHANA BEGUM
    Uttar Pradesh  Hindi 72  English 65  Mathematics 80  Science 70  Total 380 marks obtained"""
    assert dv.decide("marksheet_10", _ex(text), {}).status == dv.MATCH


def test_andhra_pradesh_documents():
    # BSEAP SSC certificate — bilingual, English side as printed.
    ssc = """BOARD OF SECONDARY EDUCATION, ANDHRA PRADESH  మాధ్యమిక విద్యా మండలి
    SECONDARY SCHOOL CERTIFICATE  Roll No. 2012345  March 2016
    Telugu 85  English 78  Mathematics 92  General Science 80  Social Studies 84
    Total Marks 419  GPA 8.5  పదవ తరగతి"""
    v = dv.decide("marksheet_10", _ex(ssc), {})
    assert v.status == dv.MATCH
    # Same file in the 12th slot is a clear mismatch, not a coin toss.
    assert dv.decide("marksheet_12_diploma", _ex(ssc), {}).status == dv.MISMATCH

    inter = """BOARD OF INTERMEDIATE EDUCATION, ANDHRA PRADESH  ఇంటర్మీడియట్ విద్యా మండలి
    INTERMEDIATE PUBLIC EXAMINATION  MARKS MEMO  Hall Ticket No. 1812345678  March 2018
    Mathematics 75 75  Physics 58 60  Chemistry 57 60  Total 981 / 1000"""
    assert dv.decide("marksheet_12_diploma", _ex(inter), {}).status == dv.MATCH

    # Aadhaar issued in AP: Telugu + English, no Latin "Aadhaar" on the front.
    aadhaar = """భారత ప్రభుత్వం Government of India  Test Person  పుట్టిన తేదీ / DOB : 01/01/1990
    పురుషుడు / MALE  9999 4105 7058  ఆధార్ - సామాన్య మానవుని హక్కు"""
    v = dv.decide("aadhar_card", _ex(aadhaar), {"aadhaar_number": "999941057058"})
    assert v.status == dv.MATCH and v.id_match is True


# One 10th and one 12th document per region, worded the way that state's
# board prints them (English side plus regional stems as OCR returns them).
STATE_10TH = {
    "West Bengal Madhyamik": "WEST BENGAL BOARD OF SECONDARY EDUCATION পশ্চিমবঙ্গ মধ্যশিক্ষা পর্ষদ "
                             "MADHYAMIK PARIKSHA 2015 মাধ্যমকি পরীক্ষা Roll 1234567 Bengali 80 English 75 Total 612",
    "Assam HSLC": "BOARD OF SECONDARY EDUCATION, ASSAM  HIGH SCHOOL LEAVING CERTIFICATE EXAMINATION 2016 "
                  "HSLC  Roll B16-1234  Assamese 78 English 70 Mathematics 85 Total marks 480",
    "Odisha HSC (10th)": "BOARD OF SECONDARY EDUCATION, ODISHA  ANNUAL HIGH SCHOOL CERTIFICATE EXAMINATION 2017 "
                         "ମାଧ୍ୟମିକ ଶିକ୍ଷା Roll No 12A345  Odia 82 English 74 Total 470 marks",
    "Bihar Matric": "BIHAR SCHOOL EXAMINATION BOARD, PATNA  ANNUAL SECONDARY SCHOOL EXAMINATION 2014 (MATRIC) "
                    "Roll Code 12345 Roll No 0012345  Hindi 80 English 70 Total 420 marks",
    "UP High School": "BOARD OF HIGH SCHOOL AND INTERMEDIATE EDUCATION UTTAR PRADESH  हाईस्कूल परीक्षा 2017 "
                      "HIGH SCHOOL EXAMINATION  CERTIFICATE-CUM-MARKS SHEET  Roll 2972081  Hindi 72 English 65 Total 380",
    "Kerala SSLC": "GOVERNMENT OF KERALA  BOARD OF PUBLIC EXAMINATIONS  SECONDARY SCHOOL LEAVING CERTIFICATE SSLC "
                   "MARCH 2016  Register No 123456  Malayalam A+ English A  Grade  പത്താം",
    "Tamil Nadu SSLC": "GOVERNMENT OF TAMIL NADU  DIRECTORATE OF GOVERNMENT EXAMINATIONS  S.S.L.C. EXAMINATION "
                       "APRIL 2015  Register No 1234567  Tamil 90 English 85 Mathematics 95 Total 470 marks",
    "Karnataka SSLC": "KARNATAKA SECONDARY EDUCATION EXAMINATION BOARD  ಕರ್ನಾಟಕ ಪ್ರೌಢ ಶಿಕ್ಷಣ ಪರೀಕ್ಷಾ ಮಂಡಳಿ  "
                      "S.S.L.C. EXAMINATION APRIL 2016  Register No 12345678  Kannada 90 English 80 Total 560 marks",
    "Punjab Matric": "PUNJAB SCHOOL EDUCATION BOARD ਪੰਜਾਬ ਸਕੂਲ ਸਿੱਖਿਆ ਬੋਰਡ  MATRICULATION EXAMINATION MARCH 2015 "
                     "Roll No 1234567  Punjabi 80 English 75 Total 420 marks  ਦਸਵੀਂ",
    "J&K 10th": "JAMMU AND KASHMIR STATE BOARD OF SCHOOL EDUCATION  SECONDARY SCHOOL EXAMINATION (CLASS 10th) 2016 "
                "Roll No 1234567  English 80 Urdu 75 Mathematics 85 Total 420 marks",
    # Board names that contain BOTH class words, in English and Marathi.
    "Maharashtra SSC (10th)": "महाराष्ट्र राज्य माध्यमिक व उच्च माध्यमिक शिक्षण मंडळ, पुणे  MAHARASHTRA STATE BOARD OF "
                              "SECONDARY AND HIGHER SECONDARY EDUCATION  माध्यमिक शालान्त प्रमाणपत्र परीक्षा  SECONDARY "
                              "SCHOOL CERTIFICATE EXAMINATION MARCH 2015  STATEMENT OF MARKS  Seat No A123456  Marathi 80 "
                              "English 75 Mathematics 90 Total 450",
    "UP High School (Hindi board name)": "माध्यमिक शिक्षा परिषद, उत्तर प्रदेश  BOARD OF HIGH SCHOOL AND INTERMEDIATE "
                                         "EDUCATION  हाईस्कूल परीक्षा 2017  HIGH SCHOOL EXAMINATION  CERTIFICATE-CUM-MARKS "
                                         "SHEET  Roll 2972081  Hindi 72 English 65 Total 380",
    "CBSE 10th": "CENTRAL BOARD OF SECONDARY EDUCATION  SECONDARY SCHOOL EXAMINATION 2018  STATEMENT OF MARKS "
                 "Roll No 1234567  ENGLISH 91 A1  MATHEMATICS 95 A1  Total 452",
    "ICSE": "COUNCIL FOR THE INDIAN SCHOOL CERTIFICATE EXAMINATIONS  INDIAN CERTIFICATE OF SECONDARY EDUCATION (ICSE) "
            "EXAMINATION 2017  STATEMENT OF MARKS  Index No T/1234/001  English 88 Mathematics 92",
}
STATE_12TH = {
    "West Bengal HS": "WEST BENGAL COUNCIL OF HIGHER SECONDARY EDUCATION  পশ্চিমবঙ্গ উচ্চ মাধ্যমকি শিক্ষা সংসদ  "
                      "HIGHER SECONDARY EXAMINATION 2017  Roll 1234567  Bengali 80 Physics 75 Total 420 marks",
    "Assam HS": "ASSAM HIGHER SECONDARY EDUCATION COUNCIL  H.S. FINAL EXAMINATION 2018  Roll 1234567 "
                "English 75 Physics 70 Chemistry 68 Total marks 380",
    "Odisha CHSE (+2)": "COUNCIL OF HIGHER SECONDARY EDUCATION, ODISHA  ANNUAL HIGHER SECONDARY EXAMINATION 2019 "
                        "(+2 SCIENCE)  Roll No 123AB456  Physics 70 Chemistry 68 Total 400 marks",
    "Bihar Intermediate": "BIHAR SCHOOL EXAMINATION BOARD  INTERMEDIATE ANNUAL EXAMINATION 2016 (SCIENCE) "
                          "Roll Code 12345 Roll No 0012345  Physics 70 Chemistry 68 Total 380 marks",
    "UP Intermediate": "BOARD OF HIGH SCHOOL AND INTERMEDIATE EDUCATION UTTAR PRADESH  इंटरमीडिएट परीक्षा 2019 "
                       "INTERMEDIATE EXAMINATION  CERTIFICATE-CUM-MARKS SHEET  Roll 1234567  Physics 70 Total 380",
    "Kerala Plus Two": "GOVERNMENT OF KERALA  DIRECTORATE OF HIGHER SECONDARY EDUCATION  HIGHER SECONDARY EXAMINATION "
                       "MARCH 2018  Register No 1234567  Physics A Chemistry B  Grade  പ്ലസ് ടു",
    "Tamil Nadu HSC (12th)": "GOVERNMENT OF TAMIL NADU  DIRECTORATE OF GOVERNMENT EXAMINATIONS  HIGHER SECONDARY "
                             "CERTIFICATE EXAMINATION MARCH 2017  Register No 1234567  Physics 180 Chemistry 175 Total 1100 marks",
    "Karnataka PUC": "GOVERNMENT OF KARNATAKA  DEPARTMENT OF PRE-UNIVERSITY EDUCATION  ಪದವಿ ಪೂರ್ವ ಶಿಕ್ಷಣ ಇಲಾಖೆ  "
                     "II PUC EXAMINATION MARCH 2018  Register No 123456  Physics 85 Chemistry 80 Total 540 marks",
    "Punjab 12th": "PUNJAB SCHOOL EDUCATION BOARD  SENIOR SECONDARY EXAMINATION (CLASS XII) MARCH 2017 "
                   "Roll No 1234567  English 75 Physics 70 Total 380 marks",
    "Maharashtra HSC (12th)": "महाराष्ट्र राज्य माध्यमिक व उच्च माध्यमिक शिक्षण मंडळ, पुणे  MAHARASHTRA STATE BOARD OF "
                              "SECONDARY AND HIGHER SECONDARY EDUCATION  उच्च माध्यमिक प्रमाणपत्र परीक्षा - गुणपत्रक  HIGHER "
                              "SECONDARY CERTIFICATE EXAMINATION - STATEMENT OF MARKS  Seat No M123456  Physics 65 Total 277",
    "UP Intermediate (Hindi board name)": "माध्यमिक शिक्षा परिषद, उत्तर प्रदेश  BOARD OF HIGH SCHOOL AND INTERMEDIATE "
                                          "EDUCATION  इंटरमीडिएट परीक्षा 2019  INTERMEDIATE EXAMINATION  CERTIFICATE-CUM-MARKS "
                                          "SHEET  Roll 1234567  Physics 70 Total 380",
    "CBSE 12th": "CENTRAL BOARD OF SECONDARY EDUCATION  SENIOR SCHOOL CERTIFICATE EXAMINATION (CLASS XII) 2020 "
                 "STATEMENT OF MARKS  Roll No 1234567  PHYSICS 85 CHEMISTRY 80 Total 428",
    "ISC": "COUNCIL FOR THE INDIAN SCHOOL CERTIFICATE EXAMINATIONS  INDIAN SCHOOL CERTIFICATE (ISC) EXAMINATION 2019 "
           "STATEMENT OF MARKS  Index No T/1234/001  Physics 88 Chemistry 84",
}


def test_every_state_10th_matches_its_slot_and_not_the_12th():
    for name, text in STATE_10TH.items():
        right = dv.decide("marksheet_10", _ex(text), {})
        wrong = dv.decide("marksheet_12_diploma", _ex(text), {})
        assert right.status == dv.MATCH, (name, right.score, right.evidence)
        assert wrong.status == dv.MISMATCH, (name, "in 12th slot", wrong.score, wrong.evidence)


def test_every_state_12th_matches_its_slot_and_not_the_10th():
    for name, text in STATE_12TH.items():
        right = dv.decide("marksheet_12_diploma", _ex(text), {})
        wrong = dv.decide("marksheet_10", _ex(text), {})
        assert right.status == dv.MATCH, (name, right.score, right.evidence)
        assert wrong.status == dv.MISMATCH, (name, "in 10th slot", wrong.score, wrong.evidence)


def test_aadhaar_fronts_in_every_script():
    # English half plus the regional half of the front of the card, as
    # printed in each state. None carries the Latin word "Aadhaar".
    fronts = {
        "Tamil":     "இந்திய அரசு Government of India  Test Person  பிறந்த தேதி / DOB: 01/01/1990  ஆண் / MALE  9999 4105 7058  ஆதார்",
        "Bengali":   "ভারত সরকার Government of India  Test Person  জন্ম তারিখ / DOB: 01/01/1990  পুরুষ / MALE  9999 4105 7058  আধার",
        "Kannada":   "ಭಾರತ ಸರ್ಕಾರ Government of India  Test Person  ಜನ್ಮ ದಿನಾಂಕ / DOB: 01/01/1990  ಪುರುಷ / MALE  9999 4105 7058",
        "Malayalam": "ഭാരത സർക്കാർ Government of India  Test Person  ജനന തീയതി / DOB: 01/01/1990  പുരുഷൻ / MALE  9999 4105 7058",
        "Gujarati":  "ભારત સરકાર Government of India  Test Person  જન્મ તારીખ / DOB: 01/01/1990  પુરુષ / MALE  9999 4105 7058",
        "Punjabi":   "ਭਾਰਤ ਸਰਕਾਰ Government of India  Test Person  ਜਨਮ ਤਾਰੀਖ / DOB: 01/01/1990  ਪੁਰਸ਼ / MALE  9999 4105 7058",
        "Odia":      "ଭାରତ ସରକାର Government of India  Test Person  ଜନ୍ମ ତାରିଖ / DOB: 01/01/1990  ପୁରୁଷ / MALE  9999 4105 7058",
        "Assamese":  "ভাৰত চৰকাৰ Government of India  Test Person  জন্ম তাৰিখ / DOB: 01/01/1990  পুৰুষ / MALE  9999 4105 7058",
        "Urdu (J&K)": "حکومت ہند Government of India  Test Person  تاریخ پیدائش / DOB: 01/01/1990  مرد / MALE  9999 4105 7058",
        "Marathi":   "भारत सरकार Government of India  Test Person  जन्म तारीख / DOB: 01/01/1990  पुरुष / MALE  9999 4105 7058  आधार",
        # The English half alone, regional side unreadable — still a match.
        "English only": "Government of India  Test Person  DOB: 01/01/1990  MALE  9999 4105 7058",
    }
    for lang, text in fronts.items():
        v = dv.decide("aadhar_card", _ex(text), {"aadhaar_number": "999941057058"})
        assert v.status == dv.MATCH and v.id_match is True, (lang, v.score, v.evidence)


def test_two_pass_ocr_keeps_the_better_read(monkeypatch):
    """Pass 1 (eng+hin) decides clear English documents; the all-script pass
    runs only when needed and can only improve the verdict."""
    calls: list[str] = []
    pan_img = _ex(PAN_TEXT)                       # what eng+hin would read
    junk = _ex("garbage from too many models")     # a worse all-script read

    def fake_extract(data, ct, fn, langs=None):
        calls.append(langs)
        return pan_img if langs == dv.primary_languages() else junk

    monkeypatch.setattr(dv, "extract", fake_extract)
    monkeypatch.setattr(dv, "enabled", lambda: True)
    dv._CACHE.clear()
    v = dv.validate(b"pan-bytes", "image/jpeg", "pan.jpg", "pan_card", {})
    assert v.status == dv.MATCH and calls == [dv.primary_languages()]   # one pass only

    # A regional card: pass 1 reads too little, pass 2 rescues it.
    tamil = _ex("இந்திய அரசு Government of India Test Person பிறந்த தேதி / DOB: 01/01/1990 ஆண் / MALE 9999 4105 7058 ஆதார்")
    weak = _ex("Government of India Test Person DOB: 01/01/1990 9999 4105 7058")   # gender/Aadhaar lost

    def fake_extract2(data, ct, fn, langs=None):
        calls.append(langs)
        return weak if langs == dv.primary_languages() else tamil

    calls.clear()
    monkeypatch.setattr(dv, "extract", fake_extract2)
    v = dv.validate(b"aadhaar-bytes", "image/jpeg", "a.jpg", "aadhar_card", {})
    assert v.status == dv.MATCH and len(calls) == 2 and calls[1] == dv.ocr_languages()

    # And a bad second read never lowers a first verdict.
    def fake_extract3(data, ct, fn, langs=None):
        return _ex(MARKSHEET_12_TEXT) if langs == dv.primary_languages() else junk
    monkeypatch.setattr(dv, "extract", fake_extract3)
    v = dv.validate(b"x", "image/jpeg", "x.jpg", "marksheet_10", {})
    assert v.status == dv.MISMATCH and v.doc_type == "MARKSHEET_12"


def test_regional_pan_and_voter_id():
    pan_ta = "வருமான வரித் துறை INCOME TAX DEPARTMENT  GOVT. OF INDIA  Permanent Account Number Card  ABCDE1234F  Name TEST"
    assert dv.decide("pan_card", _ex(pan_ta), {}).status == dv.MATCH
    voter_bn = "ELECTION COMMISSION OF INDIA  নির্বাচন কমিশন  IDENTITY CARD  Elector's Name TEST PERSON  Sex MALE  Age 30"
    assert dv.decide("id_proof", _ex(voter_bn), {}).status == dv.MATCH
    assert dv.decide("id_proof", _ex(voter_bn), {}).doc_type == "VOTER_ID"


def test_text_heavy_page_never_scores_as_photo_or_signature():
    passport_like = _ex("REPUBLIC OF INDIA PASSPORT Type P Code IND Passport No. A1234567 "
                        "Surname TEST Given Name PERSON Nationality INDIAN "
                        "Date of birth 01/01/1990 Place of birth DELHI Date of issue 01/01/2020 "
                        "Date of expiry 01/01/2030 Authority DELHI P<INDTEST<<PERSON<<<<<<<<<",
                        image={"dark": 0.3, "bright": 0.2, "saturation": 0.4, "aspect": 0.7})
    v = dv.decide("passport_copy", passport_like, {})
    assert v.status == dv.MATCH and v.scores["PHOTO"] == 0 and v.scores["SIGNATURE"] == 0


def test_unknown_or_unreadable_inputs():
    assert dv.decide("not_a_field", _ex(PAN_TEXT), {}).status == dv.UNVERIFIED
    assert dv.decide("pan_card", None, {}).status == dv.UNVERIFIED
    v = dv.decide("pan_card", dv.Extracted(reason="legacy .doc files are not checked"), {})
    assert v.status == dv.UNVERIFIED and ".doc" in v.note


# --- extraction ---------------------------------------------------------------

def _docx(text: str, images: list[bytes] = ()) -> bytes:
    xml = ('<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
           'wordprocessingml/2006/main"><w:body>'
           + "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in text.split("\n"))
           + "</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
        for i, img in enumerate(images, 1):
            z.writestr(f"word/media/image{i}.jpg", img)
    return buf.getvalue()


def test_docx_with_a_pasted_scan_is_ocrd():
    """A Word file that is just a photo of the card pasted in (very common)
    must be read through OCR, not dismissed as 'no text'."""
    import pytest
    if not dv._ocr_available():
        pytest.skip("Tesseract not installed")
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1400, 500), "white")
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 36)
    for i, line in enumerate(["INCOME TAX DEPARTMENT   GOVT. OF INDIA",
                              "Permanent Account Number Card", "ABCDE1234F"]):
        d.text((40, 40 + i * 70), line, fill="black", font=font)
    buf = io.BytesIO(); img.save(buf, format="JPEG")
    ex = dv.extract(_docx("", [buf.getvalue()]), None, "pan.docx")
    assert ex.source == "ocr" and "ABCDE1234F" in ex.text
    assert dv.decide("pan_card", ex, {"pan_number": "ABCDE1234F"}).status == dv.MATCH


def test_docx_text_extraction_scores_without_ocr():
    data = _docx(PAYSLIP_TEXT)
    ex = dv.extract(data, None, "slip.docx")
    assert ex.source == "docx" and "Net Pay" in ex.text
    assert dv.decide("cc_pay_slips", ex, {}).status == dv.MATCH


def test_pdf_text_layer_is_used_without_ocr():
    import fitz
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((50, 72), PAN_TEXT.replace("आयकर विभाग", "").replace("भारत सरकार", ""), fontsize=11)
    ex = dv.extract(pdf.tobytes(), "application/pdf", "pan.pdf")
    assert ex.source == "pdf-text"
    assert dv.decide("pan_card", ex, {"pan_number": "ABCDE1234F"}).status == dv.MATCH


def test_legacy_doc_and_corrupt_files_never_raise():
    ex = dv.extract(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 64, None, "x.doc")
    assert not ex.source and ".doc" in ex.reason
    ex = dv.extract(b"%PDF-1.4 not really a pdf", "application/pdf", "bad.pdf")
    assert not ex.source and ex.reason
