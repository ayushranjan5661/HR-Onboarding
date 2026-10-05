"""
Words the document check looks for, in every language a candidate's papers
may be printed in.

Indian government IDs and board certificates are bilingual: English plus
the state language. Candidates come from every state, so the same idea —
"Government of India", "date of birth", "marks", "class 10" — has to be
recognised in Devanagari (Hindi, Marathi, Nepali), Bengali/Assamese, Odia,
Gurmukhi, Gujarati, Tamil, Telugu, Kannada, Malayalam and Urdu.

Two rules of thumb shape what is written here:

  * STEMS, not whole words. Tesseract often drops or reorders vowel signs
    and conjuncts in Indic scripts (মাধ্যমিক comes back as "মাধ্যমকি"), so
    each entry is the shortest run of characters that still means only one
    thing. A stem is safe when nothing else on a document would contain it.

  * English carries the most weight. Every state board and every ID prints
    its English name too, so the regional words are extra evidence that
    saves a document whose English side OCR read badly — they are never the
    only route to a match.

`TERMS[key]` is a list of regex fragments; `alt(key)` joins them. Adding a
language means adding its words here and dropping its tessdata_fast pack
into backend/tessdata — nothing in doc_validator.py changes.
"""
from __future__ import annotations

TERMS: dict[str, list[str]] = {
    # --- identity cards -----------------------------------------------------
    "aadhaar": [
        r"aadha?ar|adhaar", r"आधार", r"আধার", r"ஆதார", r"ఆధార", r"ಆಧಾರ", r"ആധാര|ആധാർ",
        r"આધાર", r"ਆਧਾਰ", r"ଆଧାର", r"آدھار",
        r"आम आदमी का अधिकार",                       # the slogan on the front
    ],
    "uidai": [
        r"uidai|unique identification", r"भारतीय विशिष्ट पहचान", r"विशिष्ट पहचान",
        r"বিশিষ্ট", r"தனித்துவ", r"విశిష్ట", r"ವಿಶಿಷ್ಟ", r"വിശിഷ്ട", r"વિશિષ્ટ", r"ਵਿਲੱਖਣ", r"ସ୍ୱତନ୍ତ୍ର",
    ],
    "govt_of_india": [
        r"government of ind|govt\.? of ind",
        r"भारत सरकार|सरकार", r"ভারত সরকার|সরকার", r"ভাৰত চৰকাৰ|চৰকাৰ",
        r"இந்திய அரசு|அரசு", r"భారత ప్రభుత్వ|ప్రభుత్వ", r"ಭಾರತ ಸರ್ಕಾರ|ಸರ್ಕಾರ",
        r"ഭാരത സര|സർക്കാർ|സര്ക്കാര്", r"ભારત સરકાર|સરકાર", r"ਭਾਰਤ ਸਰਕਾਰ|ਸਰਕਾਰ",
        r"ଭାରତ ସରକାର|ସରକାର", r"حکومت ہند|حکومت",
    ],
    "dob": [
        r"date of birth|year of birth|\bdob\b|\byob\b",
        r"जन्म", r"জন্ম", r"পிறந்த|பிறந்த", r"పుట్టిన|జనన", r"ಜನ್ಮ|ಹುಟ್ಟಿದ", r"ജനന|ജന്മ",
        r"જન્મ", r"ਜਨਮ", r"ଜନ୍ମ", r"پیدائش",
    ],
    "gender": [
        r"\b(male|female|transgender)\b",
        r"पुरुष|महिला|स्त्री", r"পুরুষ|মহিলা|স্ত্রী", r"ஆண்|பெண்", r"పురుష|స్త్రీ|మహిళ",
        r"ಪುರುಷ|ಮಹಿಳೆ|ಸ್ತ್ರೀ", r"പുരുഷ|സ്ത്രീ", r"પુરુષ|સ્ત્રી|મહિલા", r"ਪੁਰਸ਼|ਮਹਿਲਾ|ਔਰਤ|ਇਸਤਰੀ",
        r"ପୁରୁଷ|ମହିଳା|ସ୍ତ୍ରୀ", r"مرد|عورت|خاتون",
    ],
    "address": [
        r"\baddress\b", r"पता", r"ঠিকানা", r"முகவரி", r"చిరునామా", r"ವಿಳಾಸ", r"വിലാസ",
        r"સરનામ", r"ਪਤਾ", r"ଠିକଣା", r"پتہ",
    ],
    "income_tax": [
        r"income.?tax|tax department", r"आयकर", r"আয়কর", r"வருமான வரி", r"ఆదాయపు పన్ను",
        r"ಆದಾಯ ತೆರಿಗೆ", r"ആദായ നികുതി", r"આવકવેરા", r"ਆਮਦਨ ਕਰ", r"ଆୟକର", r"انکم ٹیکس",
    ],
    "election": [
        r"election commission|elector|\bepic\b|voter",
        r"निर्वाचन|मतदाता", r"নির্বাচন|ভোটার", r"தேர்தல்|வாக்காளர்", r"ఎన్నికల|ఓటరు",
        r"ಚುನಾವಣ|ಮತದಾರ", r"തിരഞ്ഞെടുപ്പ്|വോട്ടർ", r"ચૂંટણી|મતદાર", r"ਚੋਣ|ਵੋਟਰ", r"ନିର୍ବାଚନ|ଭୋଟର",
        r"الیکشن|ووٹر",
    ],
    "driving": [
        r"driving licen[cs]e", r"ड्राइविंग|चालक अनुज्ञप्ति", r"ড্রাইভিং", r"ஓட்டுநர்", r"డ్రైవింగ్",
        r"ಚಾಲನಾ|ಚಾಲನ", r"ഡ്രൈവിംഗ്", r"ડ્રાઇવિંગ", r"ਡਰਾਈਵਿੰਗ", r"ଡ୍ରାଇଭିଂ", r"ڈرائیونگ",
    ],
    # --- education ------------------------------------------------------------
    "marks": [
        r"marks?\b|grade|percentage|cgpa|sgpa|subject|total|obtained|max(imum)?|\bscore\b|\bresult\b",
        r"अंक|मार्क|प्राप्तांक|विषय", r"নম্বর|নাম্বার|মার্ক|বিষয়", r"மதிப்பெண்|பாடம்", r"అంక|మార్కు",
        r"ಅಂಕ|ವಿಷಯ", r"മാർക്ക്|മാര്ക്ക്|ഗ്രേഡ്", r"ગુણ|વિષય", r"ਅੰਕ|ਵਿਸ਼ਾ", r"ମାର୍କ|ବିଷୟ", r"نمبر|مضمون",
    ],
    "marksheet_word": [
        r"statement of marks|marks? statement|marks? sheet|grade sheet|marks? memo|memorandum of marks|"
        r"marks? card|grade card|certificate|statement of grades|transcript",
        r"अंक ?पत्र|अंकसूची|प्रमाण ?पत्र", r"মার্কশিট|নম্বরপত্র|প্রশংসাপত্র|শংসাপত্র", r"மதிப்பெண் சான்றிதழ்|சான்றிதழ்",
        r"అంకపత్రం|ధృవీకరణ", r"ಅಂಕಪಟ್ಟಿ|ಪ್ರಮಾಣ", r"മാർക്ക് ലിസ്റ്റ്|സർട്ടിഫിക്കറ്റ്", r"ગુણપત્રક|પ્રમાણપત્ર",
        r"ਅੰਕ ਸੂਚੀ|ਸਰਟੀਫਿਕੇਟ", r"ମାର୍କ ସିଟ|ପ୍ରମାଣ", r"سند|مارک شیٹ",
    ],
    # What each state calls its exams. Traps worth knowing:
    #   Odisha's 10th is the "High School Certificate (HSC)"; Maharashtra,
    #   Gujarat, Tamil Nadu and Goa's 12th is the "Higher Secondary
    #   Certificate (HSC)" — so a bare "HSC" decides nothing here.
    #   West Bengal: Madhyamik = 10th, Uchcha Madhyamik / Higher Secondary = 12th.
    #   Assam: HSLC = 10th, HS = 12th. Karnataka: SSLC = 10th, PUC = 12th.
    #   Kerala/TN: SSLC = 10th, Plus Two / Higher Secondary = 12th.
    #   Bihar/UP/AP/Telangana: Matric or High School = 10th, Intermediate = 12th.
    "class10": [
        r"class\s?(x|10)\b|\b10th\b|\btenth\b|std\.?\s?(x|10)\b|\b(x|10)\s?(std|standard)\b",
        r"secondary school (examination|certificate|leaving)|(?<!higher )(?<!senior )secondary school\b",
        r"indian certificate of secondary education|\bicse\b",
        r"\bs\.?s\.?c\b|\bs\.?s\.?l\.?c\b|\bh\.?s\.?l\.?c\b|matric",
        # UP's board is the "Board of High School and Intermediate Education":
        # that phrase names the board, not the exam, so it decides nothing.
        r"high school (certificate|examination|leaving)|(?<!higher )(?<!senior )high school\b(?! and intermediate)",
        # Board names contain the class words too — "Madhyamik Shiksha
        # Parishad" (UP), "माध्यमिक व उच्च माध्यमिक शिक्षण मंडळ" (Maharashtra) —
        # so the word only counts when it is not part of "... Education".
        r"(?<!uchcha )(?<!higher )madhyamik\b(?! shiksha)",
        r"दसवीं|दशम|हाई ?स्कूल|(?<!उच्च )(?<!उच्चतर )माध्यमिक(?! व उच्च)(?! और उच्च)(?! शिक्ष)",
        r"(?<!উচ্চ )(?<!উচ্চ)মাধ্যম(?!িক শিক্ষা)|দশম|হাইস্কুল",
        r"பத்தாம்|பத்தாவது", r"పదవ|పదో", r"ಹತ್ತನೇ|ಹತ್ತನೆ", r"പത്താം", r"ધોરણ ?૧૦|ધોરણ ?10|દસમ",
        r"ਦਸਵੀਂ|ਮੈਟ੍ਰਿਕ", r"ଦଶମ|ମାଟ୍ରିକ", r"دسویں|میٹرک",
    ],
    "class12": [
        r"class\s?(xii|12)\b|\b12th\b|\btwelfth\b|std\.?\s?(xii|12)\b|\b(xii|12)\s?(std|standard)\b",
        # "Intermediate Education" is a board's name (UP, AP, Telangana),
        # not an exam; the exam prints as "Intermediate Examination".
        # "... Higher Secondary Education" / "Intermediate Education" name a
        # board (Maharashtra, WB, Kerala, UP, AP) and appear on its 10th
        # papers too; the exams print as "Higher Secondary Examination /
        # Certificate", "Intermediate Examination".
        r"senior secondary(?! education)|senior school certificate|higher secondary(?! education)|"
        r"intermediate(?! education)|"
        r"\bpuc\b|\bp\.?u\.?c\b|pre.?university|plus.?two|\+ ?2\b|\bh\.?s\.? (final|examination)",
        # The council is the "Council for the Indian School Certificate
        # Examinations" on 10th and 12th papers alike; only the bare exam
        # name "Indian School Certificate (ISC)" means the 12th.
        r"indian school certificate(?! examinations)|\bisc\b", r"uchcha madhyamik(?! shiksha)|uccha madhyamik",
        r"बारहवीं|द्वादश|इंटर(मीडिएट)?(?! शिक्ष)|उच्च(तर)? माध्यमिक(?! शिक्ष)", r"উচ্চ ?মাধ্যম(?!িক শিক্ষা)|দ্বাদশ",
        r"பன்னிரண்டாம்|மேல்நிலை", r"ఇంటర్", r"ಪಿಯುಸಿ|ದ್ವಿತೀಯ ಪಿಯು|ಹನ್ನೆರಡನೇ", r"പ്ലസ് ടു|ഹയർ സെക്കൻഡറി|പന്ത്രണ്ടാം",
        r"ધોરણ ?૧૨|ધોરણ ?12|બારમ", r"ਬਾਰ੍ਹਵੀਂ|ਬਾਰਵੀਂ", r"ଯୁକ୍ତ ଦୁଇ|ଦ୍ୱାଦଶ", r"بارہویں|انٹرمیڈیٹ",
    ],
    "board": [
        # Generic words, English and regional.
        r"board of|state board|\bboard\b|school education|secondary education|higher secondary education|"
        r"intermediate education|pre.?university education|council of|pariksha|examination board",
        r"बोर्ड|परिषद|शिक्षा|परीक्षा|मंडल", r"পর্ষদ|পরিষদ|শিক্ষা|পরীক্ষা|সংসদ", r"বোর্ড|শিক্ষা|পরীক্ষা",
        r"வாரியம்|கல்வி|தேர்வு", r"బోర్డు|విద్యా|మండలి|పరీక్ష", r"ಮಂಡಳಿ|ಶಿಕ್ಷಣ|ಪರೀಕ್ಷ", r"ബോർഡ്|വിദ്യാഭ്യാസ|പരീക്ഷ",
        r"બોર્ડ|શિક્ષણ|પરીક્ષા", r"ਬੋਰਡ|ਸਿੱਖਿਆ|ਪ੍ਰੀਖਿਆ", r"ବୋର୍ଡ|ଶିକ୍ଷା|ପରୀକ୍ଷା", r"بورڈ|تعلیم|امتحان",
        # National
        r"\bcbse\b|central board of secondary|\bcisce\b|\bicse\b|\bisc\b|council for the indian school|"
        r"\bnios\b|national institute of open schooling",
        # State boards, by abbreviation and by name (north to south).
        r"\bjkbose\b|jammu (and|&) kashmir",
        r"\bhpbose\b|himachal pradesh", r"\bpseb\b|punjab school education", r"\bbseh\b|\bhbse\b|haryana",
        r"\bubse\b|uttarakhand", r"\bupmsp\b|uttar pradesh|board of high school and intermediate|madhyamik shiksha parishad",
        r"\brbse\b|\bbser\b|rajasthan", r"\bmpbse\b|madhya pradesh", r"\bcgbse\b|chhattisgarh",
        r"\bbseb\b|bihar", r"\bjac\b|jharkhand academic|jharkhand",
        r"\bwbbse\b|\bwbchse\b|west bengal", r"\bseba\b|\bahsec\b|assam", r"\bbsem\b|\bcohsem\b|manipur",
        r"\bmbose\b|meghalaya", r"\bmbse\b|mizoram", r"\bnbse\b|nagaland", r"\btbse\b|tripura", r"sikkim|arunachal",
        r"\bbse,? odisha|\bchse\b|odisha|orissa",
        r"\bmsbshse\b|maharashtra", r"\bgseb\b|\bgshseb\b|gujarat", r"\bgbshse\b|\bgoa\b",
        r"\bbseap\b|\bbieap\b|andhra", r"\btsbie\b|\bbie ?ts\b|\bbse ?ts\b|telangana",
        r"\bkseeb\b|\bkseab\b|karnataka|department of pre.?university",
        r"kerala|pareeksha bhavan|board of public examinations|\bdhse\b|\bvhse\b|directorate of higher secondary",
        r"tamil ?nadu|\btnbse\b|directorate of government examinations",
        r"puducherry|pondicherry|delhi|chandigarh|ladakh|andaman",
    ],
}


def alt(*keys: str) -> str:
    """One regex alternation covering every fragment of the given keys."""
    parts: list[str] = []
    for key in keys:
        parts.extend(TERMS[key])
    return "|".join(f"(?:{p})" for p in parts)


# Tesseract language codes this lexicon has words for, in the order they
# are passed to Tesseract (English first: it is on every document).
LANGUAGES = ["eng", "hin", "mar", "nep", "ben", "asm", "ori", "pan", "guj",
             "tam", "tel", "kan", "mal", "urd"]
