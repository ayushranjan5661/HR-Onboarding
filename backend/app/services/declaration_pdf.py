"""Declarations for the submission of pending documents, as PDFs: the
educational documents one, and the relieving / experience letter one.

Follows the LevelShift template letter: From / To block, the undertaking
paragraph with the agreed number of days, the "Need to Submit" checklist and
the employee's signature. Drawn directly with PyMuPDF, reusing the Reference
Check report's primitives.
"""
import logging
from typing import Optional

import pymupdf

from app.form_definitions import DECLARATION_DOCS, EXP_DECLARATION_DOCS
from app.models import Candidate, FormType
from app.services.cif_pdf import _image_stream
from app.services.reference_pdf import (_BOLD, _INK, _LEFT, _LINE, _MUTED, _PAGE, _REG,
                                        _RIGHT, _rgb, _wrap)

_TITLE = "Declaration Form for the submission of educational Documents"
_EXP_TITLE = "Declaration Form for the submission of Pending Documents"
_ACCENT = "#312e81"
_SIZE, _LH = 11.0, 1.5
_LOGO_H, _LOGO_W = 44.0, 182   # certification strip: height in pt, width in layout units
_GPTW_PERIOD = "SEP 2026-SEP 2027"   # update when the certification renews
_log = logging.getLogger("uvicorn.error")   # shows in the uvicorn console
_WORDMARK_H = 16.0   # LevelShift wordmark cap height, pt
_WM_BG, _WM_DARK, _WM_CYAN = "#ffffff", "#111111", "#1ca3dc"   # light-background variant
_WM_T = 17           # stroke, in units of a 100-unit cap height
# Each letter: (width, bars as (x, y, w, h)), in units of a 100-unit cap height.
_WM_GLYPHS = {
    "L": (90, [(0, 0, _WM_T, 100), (0, 100 - _WM_T, 90, _WM_T)]),
    "E": (90, [(0, 0, _WM_T, 100), (0, 0, 90, _WM_T), (0, 50 - _WM_T / 2, 80, _WM_T),
               (0, 100 - _WM_T, 90, _WM_T)]),
    "S": (100, [(0, 0, 100, _WM_T), (0, 0, _WM_T, 50 + _WM_T / 2),
                (0, 50 - _WM_T / 2, 100, _WM_T), (100 - _WM_T, 50 - _WM_T / 2, _WM_T, 50 + _WM_T / 2),
                (0, 100 - _WM_T, 100, _WM_T)]),
    "H": (100, [(0, 0, _WM_T, 100), (100 - _WM_T, 0, _WM_T, 100), (0, 50 - _WM_T / 2, 100, _WM_T)]),
    "I": (_WM_T, [(0, 0, _WM_T, 100)]),
    "F": (90, [(0, 0, _WM_T, 100), (0, 0, 90, _WM_T), (0, 50 - _WM_T / 2, 80, _WM_T)]),
    "T": (100, [(0, 0, 100, _WM_T), (50 - _WM_T / 2, 0, _WM_T, 100)]),
}


def not_ready_reason(candidate: Candidate) -> Optional[str]:
    """Why the PDF can't be generated yet, or None when it can."""
    if not candidate.declaration_details:
        return "The candidate has not submitted the Declaration form yet"
    return None


def experience_not_ready_reason(candidate: Candidate) -> Optional[str]:
    if not candidate.exp_declaration_details:
        return "The candidate has not submitted the Declaration form yet"
    return None


def _signature(candidate: Candidate, form_type: FormType, field_key: str) -> Optional[bytes]:
    doc = next((d for d in candidate.documents
                if d.form_type == form_type and d.field_key == field_key), None)
    return _image_stream(doc) if doc else None


def _draw_wordmark(page) -> None:
    """The LevelShift logo at the left of the header, in its light-background
    form: dark LE·EL, blue SHIFT and the striped blue triangle for the V.
    Drawn as vectors, like the certification strip."""
    s, gap, vw = _WORDMARK_H / 100, 24, 130
    x0, y0 = _LEFT, 52 - _WORDMARK_H
    letters = [("L", _WM_DARK), ("E", _WM_DARK), ("V", _WM_CYAN), ("E", _WM_DARK),
               ("L", _WM_DARK), ("S", _WM_CYAN), ("H", _WM_CYAN), ("I", _WM_CYAN),
               ("F", _WM_CYAN), ("T", _WM_CYAN)]

    def pt(x, y):
        return pymupdf.Point(x0 + x * s, y0 + y * s)

    def bar(x, y, w, h, color):
        page.draw_rect(pymupdf.Rect(pt(x, y), pt(x + w, y + h)), color=None, fill=_rgb(color))

    cx = 0
    for c, color in letters:
        if c == "V":
            # Inverted triangle, solid at the top, striped towards the apex.
            page.draw_polyline([pt(cx, 0), pt(cx + vw, 0), pt(cx + vw / 2, 100), pt(cx, 0)],
                               color=None, fill=_rgb(color), closePath=True)
            for sy in (40, 56, 72, 88):
                bar(cx, sy, vw, 6, _WM_BG)
            cx += vw + gap
            continue
        w, bars = _WM_GLYPHS[c]
        for bx, by, bw, bh in bars:
            bar(cx + bx, by, bw, bh, color)
        cx += w + gap


def _draw_logos(page) -> None:
    """The certification strip (CMMI, ISO 27001, Great Place to Work),
    right-aligned above the header rule. Drawn as vectors so it stays sharp at
    any zoom. Layout is in units of a 100-unit-high box, scaled to _LOGO_H."""
    s = _LOGO_H / 100
    x0, y0 = _RIGHT - _LOGO_W * s, 60 - _LOGO_H

    def pt(x, y):
        return pymupdf.Point(x0 + x * s, y0 + y * s)

    def text(x, y, t, font, size, color, rotate=0):
        page.insert_text(pt(x, y), t, fontname=font, fontsize=size * s,
                         color=_rgb(color), rotate=rotate)

    def centred(cx, y, t, font, size, color):
        text(cx - pymupdf.get_text_length(t, font, size) / 2, y, t, font, size, color)

    # CMMI Level 3: swoosh, wordmark, dot over the I.
    for width, color in ((5.0, "#9fdcf7"), (2.4, "#29abe2")):
        page.draw_bezier(pt(70, 27), pt(50, 12), pt(4, 20), pt(3, 47),
                         color=_rgb(color), width=width * s)
        page.draw_bezier(pt(3, 47), pt(2, 66), pt(20, 79), pt(42, 76),
                         color=_rgb(color), width=width * s)
    text(6, 58, "CMMI", "hebi", 22, "#4a4a4a")
    page.draw_circle(pt(63.5, 34.5), 2.6 * s, color=None, fill=_rgb("#29abe2"))
    text(42, 66, "level", "heit", 6.5, "#4a4a4a")
    text(60, 71, "3", "hebo", 11, "#1d75bc")

    # ISO 27001: coral panel of ticks with the vertical caption.
    ix = 86
    page.draw_rect(pymupdf.Rect(pt(ix, 0), pt(ix + 32, 58)), color=None, fill=_rgb("#ec7068"))
    text(ix + 8, 55, "Certified System", "helv", 5.6, "#ffffff", rotate=90)
    for i in range(5):
        c = 7 + i * 11
        page.draw_polyline([pt(ix + 13, c), pt(ix + 18, c + 4.5), pt(ix + 28, c - 4.5)],
                           color=_rgb("#ffffff"), width=2.2 * s)
    for i, line in enumerate(("Information", "Security", "ISO 27001")):
        text(ix, 67 + i * 8, line, "helv", 6, "#6b6b6b")

    # Great Place to Work: red panel, blue "Certified" pennant.
    gx, gw = 132, 46
    page.draw_rect(pymupdf.Rect(pt(gx, 0), pt(gx + gw, 52)), color=None, fill=_rgb("#e3202b"))
    for i, line in enumerate(("Great", "Place", "To", "Work.")):
        text(gx + 4, 12.5 + i * 11.5, line, "hebo", 11.5, "#ffffff")
    text(gx + 4 + pymupdf.get_text_length("Work.", "hebo", 11.5), 47, "®", "helv", 4, "#ffffff")
    page.draw_polyline([pt(gx, 52), pt(gx + gw, 52), pt(gx + gw, 86), pt(gx + gw / 2, 96),
                        pt(gx, 86), pt(gx, 52)],
                       color=None, fill=_rgb("#1b3f8b"), closePath=True)
    centred(gx + gw / 2, 64, "Certified", "hebo", 8.6, "#ffffff")
    centred(gx + gw / 2, 72, _GPTW_PERIOD, "helv", 3.6, "#ffffff")
    centred(gx + gw / 2, 79, "INDIA", "hebo", 3.6, "#ffffff")
    text(gx + gw + 1, 94, "TM", "helv", 3, "#6b6b6b")


def build(candidate: Candidate) -> bytes:
    """The declaration as PDF bytes. Caller ensures declaration_details exists."""
    d = candidate.declaration_details
    days = d.submission_within_days or ""
    para = ("I hereby give you assurance that I will produce all my below documents "
            f"pertaining to the highest qualification within {days} days from the "
            "declaration date of final semester results. Also, I acknowledge that failing "
            "to provide the documents within the given timeframe leads to obeying the norms "
            "as per company policy.")
    docs = []
    for key, label in DECLARATION_DOCS:
        if key == "semester_marksheets_pending" and getattr(d, key) == "Yes" and d.pending_semesters:
            label = f"{label} - Pending: {d.pending_semesters}"
        docs.append((label, getattr(d, key) == "Yes"))
    return _render(candidate, d, _TITLE, para, docs,
                   _signature(candidate, FormType.DECLARATION, "declaration_signature"))


def build_experience(candidate: Candidate) -> bytes:
    """The relieving / experience letter declaration as PDF bytes. Caller
    ensures exp_declaration_details exists."""
    d = candidate.exp_declaration_details
    days = d.submission_within_days or "45"
    para = ("I hereby give you assurance that I will produce Experience letter and "
            f"relieving letter within {days} days from the Date of joining. Also, I "
            "acknowledge that failing to provide the documents within the given timeframe "
            "leads to obey the norms as per company policy.")
    docs = [(label, getattr(d, key) == "Yes") for key, label in EXP_DECLARATION_DOCS]
    return _render(candidate, d, _EXP_TITLE, para, docs,
                   _signature(candidate, FormType.EXPERIENCE_DECLARATION,
                              "exp_declaration_signature"))


def _render(candidate: Candidate, d, title: str, para: str,
            docs: list[tuple[str, bool]], sig: Optional[bytes]) -> bytes:
    """The shared letter: header, title, From / To, undertaking, checklist,
    signature. d carries candidate_name and declaration_date."""
    name = d.candidate_name or candidate.name or ""
    doc = pymupdf.open()
    page = doc.new_page(width=_PAGE.width, height=_PAGE.height)

    def text(x, y, s, font=_REG, size=_SIZE, color=_INK):
        page.insert_text((x, y), s, fontname=font, fontsize=size, color=_rgb(color))

    # Header: company logo, certifications and a rule, as on the letterhead.
    _draw_wordmark(page)
    _draw_logos(page)
    page.draw_line((_LEFT, 64), (_RIGHT, 64), color=_rgb(_ACCENT), width=1.5)

    y = 104
    w = pymupdf.get_text_length(title, _BOLD, 13)
    text((_PAGE.width - w) / 2, y, title, _BOLD, 13)
    page.draw_line(((_PAGE.width - w) / 2, y + 3), ((_PAGE.width + w) / 2, y + 3),
                   color=_rgb(_INK), width=0.7)

    y += 40
    for label, value in (("From:", name), ("To:", "The Team HR, Levelshift")):
        text(_LEFT, y, label, _BOLD)
        text(_LEFT + 44, y, value)
        y += _SIZE * _LH + 4

    y += 14
    for ln in _wrap(para, _REG, _SIZE, _RIGHT - _LEFT):
        text(_LEFT, y, ln)
        y += _SIZE * _LH

    # Checklist table.
    y += 18
    text(_LEFT, y, "Tick the checkbox if applicable", _BOLD, 10, _MUTED)
    y += 10
    col = _RIGHT - 120
    head = pymupdf.Rect(_LEFT, y, _RIGHT, y + 24)
    page.draw_rect(head, color=_rgb(_INK), fill=_rgb("#eef2ff"), width=0.7)
    page.draw_line((col, head.y0), (col, head.y1), color=_rgb(_INK), width=0.7)
    text(_LEFT + 8, head.y0 + 16, "Document Name", _BOLD, 10.5)
    text(col + 8, head.y0 + 16, "Need to Submit", _BOLD, 10.5)
    y = head.y1
    for label, ticked in docs:
        lines = _wrap(label, _REG, 10.5, col - _LEFT - 16)
        h = max(26, len(lines) * 10.5 * 1.35 + 12)
        r = pymupdf.Rect(_LEFT, y, _RIGHT, y + h)
        page.draw_rect(r, color=_rgb(_INK), width=0.7)
        page.draw_line((col, r.y0), (col, r.y1), color=_rgb(_INK), width=0.7)
        for i, ln in enumerate(lines):
            text(_LEFT + 8, r.y0 + 6 + (i + 1) * 10.5 * 1.35 - 3, ln, size=10.5)
        # Checkbox, ticked with two strokes (the base-14 fonts have no check glyph).
        cx, cy = (col + _RIGHT) / 2, r.y0 + h / 2
        box = pymupdf.Rect(cx - 6, cy - 6, cx + 6, cy + 6)
        page.draw_rect(box, color=_rgb(_INK), width=0.8)
        if ticked:
            page.draw_polyline([(cx - 4, cy), (cx - 1, cy + 3.5), (cx + 4.5, cy - 4)],
                               color=_rgb("#16a34a"), width=1.8)
        y = r.y1

    # Signature block: the image sits above the line, so leave room for it
    # below the table.
    y += 90
    text(_LEFT, y, f"Date: {d.declaration_date or ''}")
    sig_rect = pymupdf.Rect(_RIGHT - 170, y - 50, _RIGHT, y + 4)
    if sig:
        try:
            page.insert_image(sig_rect, stream=sig, keep_proportion=True)
        except Exception:
            pass
    page.draw_line((_RIGHT - 170, y + 8), (_RIGHT, y + 8), color=_rgb(_INK), width=0.6)
    text(_RIGHT - 170, y + 22, "Signature of the Employee", _BOLD, 10)
    text(_RIGHT - 170, y + 36, name, size=10, color=_MUTED)

    footer = "Confidential"
    w = pymupdf.get_text_length(footer, _REG, 8)
    page.draw_line((_LEFT, _PAGE.height - 32), (_RIGHT, _PAGE.height - 32),
                   color=_rgb(_LINE), width=0.6)
    text((_PAGE.width - w) / 2, _PAGE.height - 20, footer, size=8, color=_MUTED)

    data = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return data
