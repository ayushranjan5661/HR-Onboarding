"""The one date format this app stores: DD/MM/YYYY.

Dates arrive as free text — the forms send what the candidate typed, and the
corrections page is a plain text box — so every value is normalised here on
the way in. Rows written before this change hold yyyy-mm-dd; normalise()
accepts those too, so old and new data read the same.
"""

import re

DATE_DISPLAY_FORMAT = "DD/MM/YYYY"

_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_DMY = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$")

# Every column holding a date, flat or inside a repeating table. The CIF and
# BGV reuse these names, so one set covers both.
DATE_FIELDS = frozenset({
    "date_of_birth",
    "declaration_date",
    "passport_expiry",
    "from_date",
    "to_date",
})

_DAYS_IN_MONTH = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _is_real_date(day: int, month: int, year: int) -> bool:
    if not (1 <= month <= 12 and 1900 <= year <= 2999 and day >= 1):
        return False
    limit = _DAYS_IN_MONTH[month - 1]
    if month == 2 and not (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)):
        limit = 28
    return day <= limit


def normalize(value):
    """Anything date-shaped -> DD/MM/YYYY.

    A value we cannot read is returned untouched rather than blanked: losing
    what someone typed is worse than storing an odd string, and the HR portal
    shows it as-is so the problem stays visible.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return value
    iso = _ISO.match(text)
    if iso:
        year, month, day = int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
        if _is_real_date(day, month, year):
            return "%02d/%02d/%04d" % (day, month, year)
    dmy = _DMY.match(text)
    if dmy:
        day, month, year = int(dmy.group(1)), int(dmy.group(2)), int(dmy.group(3))
        if _is_real_date(day, month, year):
            return "%02d/%02d/%04d" % (day, month, year)
    return text


def normalize_field(name: str, value):
    """normalize() applied only to the columns that hold a date."""
    return normalize(value) if name in DATE_FIELDS else value


# What the candidate sees each date field called, for the rejection message.
DATE_FIELD_LABELS = {
    "date_of_birth": "Date of Birth",
    "declaration_date": "Declaration Date",
    "passport_expiry": "Passport Expiry",
    "from_date": "From",
    "to_date": "To",
}


def is_valid(value) -> bool:
    """True when the value is a real calendar day in DD/MM/YYYY.

    Blank passes: whether a field may be left empty is a separate question,
    answered by the form's own required flags.
    """
    text = str(value or "").strip()
    if not text:
        return True
    m = _DMY.match(normalize(text))
    return bool(m) and _is_real_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def invalid_labels(values) -> list[str]:
    """Labels of the date columns in `values` that are not a real DD/MM/YYYY.

    Dates reach us as free text, so "09/22/2026" (month 22, a US-format date)
    would otherwise be stored verbatim and only surface much later — in a Zoho
    push or during BGV. Callers reject the submission instead.
    """
    return [DATE_FIELD_LABELS.get(name, name)
            for name, value in (values or {}).items()
            if name in DATE_FIELDS and not is_valid(value)]
