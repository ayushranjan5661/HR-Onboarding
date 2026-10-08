"""Reference Check report as a PDF: candidate details, then each referee's
details with the feedback they gave on the public referee form.

Drawn directly with PyMuPDF (MuPDF's HTML tables don't honour column widths
and leave stray backgrounds across page breaks). Text is wrapped by hand;
a row that would cross the page bottom moves to the next page whole, and a
single answer longer than a page is split line by line.
"""
from datetime import datetime
from typing import Optional

import pymupdf

from app.models import Candidate, RefereeFeedback


def _rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


# One accent per section, so each block is recognisable at a glance.
_CANDIDATE = ("#4f46e5", "#eef2ff")                     # indigo
_REFEREES = {1: ("#0d9488", "#f0fdfa"), 2: ("#db2777", "#fdf2f8")}   # teal, pink
_FEEDBACK = "#7c3aed"                                    # violet
_MESSAGE = ("#d97706", "#fffbeb")                        # amber
_INK, _MUTED, _LINE = "#1e2330", "#6b7280", "#e5e7eb"

_RATINGS = [
    ("rating_reliability", "Reliability"),
    ("rating_punctuality", "Punctuality"),
    ("rating_attendance", "Attendance"),
    ("rating_professionalism", "Professionalism"),
]
_RATING_WORD = {1: "Poor", 2: "Fair", 3: "Good", 4: "Excellent"}
_RATING_COLOR = {1: "#dc2626", 2: "#f59e0b", 3: "#0ea5e9", 4: "#16a34a"}

_REG, _BOLD, _ITAL = "helv", "hebo", "heit"
_PAGE = pymupdf.paper_rect("a4")
_LEFT, _RIGHT, _TOP, _BOTTOM = 40, _PAGE.width - 40, 40, _PAGE.height - 46
_KEY_W = 170          # label column width
_PAD = 7              # cell padding
_LH = 1.35            # line height factor


def _wrap(text: str, font: str, size: float, width: float) -> list[str]:
    """Greedy word wrap; words wider than the box are broken by character."""
    lines: list[str] = []
    for para in str(text).replace("\r", "").split("\n"):
        line = ""
        for word in para.split(" "):
            trial = f"{line} {word}" if line else word
            if pymupdf.get_text_length(trial, font, size) <= width:
                line = trial
                continue
            if line:
                lines.append(line)
            while pymupdf.get_text_length(word, font, size) > width:
                cut = len(word)
                while cut > 1 and pymupdf.get_text_length(word[:cut], font, size) > width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            line = word
        lines.append(line)
    return lines


class _Report:
    def __init__(self) -> None:
        self.doc = pymupdf.open()
        self.page = None
        self.y = 0.0
        self._new_page(first=True)

    # ---- pages ----
    def _new_page(self, first: bool = False) -> None:
        self.page = self.doc.new_page(width=_PAGE.width, height=_PAGE.height)
        if first:
            self.y = _TOP
            return
        # Continuation pages get a slim reminder of what they belong to.
        self.page.draw_rect(pymupdf.Rect(0, 0, _PAGE.width, 6), color=None, fill=_rgb("#312e81"))
        self.page.insert_text((_LEFT, 24), "Referee Check Form (continued)",
                              fontname=_BOLD, fontsize=8, color=_rgb(_MUTED))
        self.y = _TOP

    def _ensure(self, height: float) -> None:
        if self.y + height > _BOTTOM:
            self._new_page()

    def _text(self, x: float, y: float, s: str, font=_REG, size=10.0, color=_INK) -> None:
        self.page.insert_text((x, y), s, fontname=font, fontsize=size, color=_rgb(color))

    # ---- blocks ----
    def banner(self, candidate_name: str) -> None:
        r = pymupdf.Rect(_LEFT, self.y, _RIGHT, self.y + 70)
        self.page.draw_rect(r, color=None, fill=_rgb("#312e81"), radius=0.12)
        # A soft violet panel on the right for a bit of depth.
        self.page.draw_rect(pymupdf.Rect(_RIGHT - 150, r.y0, _RIGHT, r.y1), color=None,
                            fill=_rgb("#4338ca"), radius=0.12)
        self._text(r.x0 + 18, r.y0 + 30, "Referee Check Form", _BOLD, 20, "#ffffff")
        self._text(r.x0 + 18, r.y0 + 50, f"Candidate: {candidate_name}", _REG, 10, "#c7d2fe")
        stamp = datetime.now().strftime("%d/%m/%Y, %I:%M %p")
        for i, s in enumerate(("LevelShift", f"Generated {stamp}")):
            w = pymupdf.get_text_length(s, _BOLD if i == 0 else _REG, 11 if i == 0 else 8)
            self._text(_RIGHT - 16 - w, r.y0 + 30 + i * 18, s, _BOLD if i == 0 else _REG,
                       11 if i == 0 else 8, "#ffffff" if i == 0 else "#c7d2fe")
        self.page.draw_rect(pymupdf.Rect(_LEFT, r.y1 + 4, _RIGHT, r.y1 + 8), color=None,
                            fill=_rgb("#f59e0b"), radius=0.5)
        self.y = r.y1 + 22

    def section(self, title: str, accent: str, keep_with: float = 30) -> None:
        # Never leave a heading alone at the page bottom: keep_with is how much
        # of what follows must fit beside it.
        self._ensure(26 + keep_with)
        r = pymupdf.Rect(_LEFT, self.y, _RIGHT, self.y + 26)
        self.page.draw_rect(r, color=None, fill=_rgb(accent), radius=0.18)
        self._text(r.x0 + 12, r.y0 + 17.5, title, _BOLD, 12.5, "#ffffff")
        self.y = r.y1 + 2

    def subheading(self, title: str, color: str) -> None:
        self._ensure(24 + 30)
        self.y += 8
        self._text(_LEFT, self.y + 11, title, _BOLD, 11, color)
        self.page.draw_line((_LEFT, self.y + 16), (_RIGHT, self.y + 16), color=_rgb(color), width=1.5)
        self.y += 20

    def row(self, key: str, value, accent: str, tint: str) -> None:
        size = 10.0
        lh = size * _LH
        key_lines = _wrap(key, _BOLD, 8.8, _KEY_W - 2 * _PAD)
        text = "" if value is None else str(value).strip()
        font, color = (_REG, _INK) if text else (_ITAL, "#9ca3af")
        val_w = _RIGHT - _LEFT - _KEY_W - 2 * _PAD
        val_lines = _wrap(text or "Not provided", font, size, val_w)
        self._row_box(key_lines, accent, tint, max(len(key_lines), len(val_lines)) * lh,
                      lambda top, chunk: [self._text(_LEFT + _KEY_W + _PAD, top + (i + 1) * lh - 3,
                                                     ln, font, size, color)
                                          for i, ln in enumerate(chunk)],
                      val_lines, lh)

    def rating_row(self, key: str, raw, accent: str, tint: str) -> None:
        try:
            n = int(raw)
        except (TypeError, ValueError):
            n = None
        if n not in _RATING_WORD:
            self.row(key, None if raw is None else raw, accent, tint)
            return
        key_lines = _wrap(key, _BOLD, 8.8, _KEY_W - 2 * _PAD)
        lh = 10.0 * _LH
        height = max(len(key_lines) * lh, 16)
        top = self._row_frame(key_lines, accent, tint, height)
        cy = top + _PAD + height / 2
        x = _LEFT + _KEY_W + _PAD + 6
        for i in range(4):
            fill = _rgb(_RATING_COLOR[n]) if i < n else _rgb("#d1d5db")
            self.page.draw_circle((x + i * 15, cy), 5, color=None, fill=fill)
        x += 4 * 15 + 4
        self._text(x, cy + 3.5, f"{n}/4", _BOLD, 10)
        x += 28
        word = _RATING_WORD[n]
        w = pymupdf.get_text_length(word, _BOLD, 8.5) + 14
        self.page.draw_rect(pymupdf.Rect(x, cy - 7.5, x + w, cy + 7.5), color=None,
                            fill=_rgb(_RATING_COLOR[n]), radius=0.5)
        self._text(x + 7, cy + 3, word, _BOLD, 8.5, "#ffffff")

    def _row_frame(self, key_lines, accent, tint, height) -> float:
        self._ensure(height + 2 * _PAD)
        top = self.y
        bottom = top + height + 2 * _PAD
        self.page.draw_rect(pymupdf.Rect(_LEFT, top, _LEFT + _KEY_W, bottom), color=None, fill=_rgb(tint))
        for i, ln in enumerate(key_lines):
            self._text(_LEFT + _PAD, top + _PAD + (i + 1) * 10.0 * _LH - 3.5, ln, _BOLD, 8.8, accent)
        self.page.draw_line((_LEFT, bottom), (_RIGHT, bottom), color=_rgb(_LINE), width=0.7)
        self.y = bottom
        return top

    def _row_box(self, key_lines, accent, tint, height, draw_value, val_lines, lh) -> None:
        room = _BOTTOM - _TOP - 2 * _PAD
        if height <= room:
            top = self._row_frame(key_lines, accent, tint, height)
            draw_value(top + _PAD, val_lines)
            return
        # Longer than a whole page: split the value across pages.
        per_page = int(room // lh)
        first = True
        while val_lines:
            if not first:
                self._new_page()
            chunk, val_lines = val_lines[:per_page], val_lines[per_page:]
            top = self._row_frame(key_lines if first else [], accent, tint, len(chunk) * lh)
            draw_value(top + _PAD, chunk)
            first = False

    def note(self, text: str, fg: str, bg: str) -> None:
        lines = _wrap(text, _REG, 9.5, _RIGHT - _LEFT - 24)
        h = len(lines) * 9.5 * _LH + 16
        self._ensure(h)
        self.page.draw_rect(pymupdf.Rect(_LEFT, self.y, _RIGHT, self.y + h), color=None,
                            fill=_rgb(bg), radius=0.15)
        for i, ln in enumerate(lines):
            self._text(_LEFT + 12, self.y + 8 + (i + 1) * 9.5 * _LH - 3, ln, _REG, 9.5, fg)
        self.y += h

    def small(self, text: str) -> None:
        self._ensure(16)
        self._text(_LEFT, self.y + 12, text, _REG, 8, _MUTED)
        self.y += 16

    def gap(self, h: float) -> None:
        self.y += h

    def finish(self) -> bytes:
        total = self.doc.page_count
        for i, page in enumerate(self.doc):
            footer = f"Confidential  ·  For LevelShift hiring team use only  ·  Page {i + 1} of {total}"
            w = pymupdf.get_text_length(footer, _REG, 7.5)
            page.draw_line((_LEFT, _PAGE.height - 32), (_RIGHT, _PAGE.height - 32),
                           color=_rgb(_LINE), width=0.6)
            page.insert_text(((_PAGE.width - w) / 2, _PAGE.height - 20), footer,
                             fontname=_REG, fontsize=7.5, color=_rgb(_MUTED))
        data = self.doc.tobytes(garbage=3, deflate=True)
        self.doc.close()
        return data


def _feedback(rep: _Report, fb: Optional[RefereeFeedback], accent: str, tint: str) -> None:
    rep.subheading("Referee Feedback", _FEEDBACK)
    if fb is None:
        rep.note("The referee has not submitted feedback yet.", "#92400e", "#fffbeb")
        return
    when = fb.submitted_at.strftime("%d/%m/%Y, %I:%M %p") if fb.submitted_at else None
    rep.row("Submitted On", when, accent, tint)
    rep.row("Relationship with the Candidate", fb.relationship_with_candidate, accent, tint)
    rep.row("Strengths", fb.candidate_strengths, accent, tint)
    rep.row("Development Areas", fb.candidate_development_areas, accent, tint)
    for key, label in _RATINGS:
        rep.rating_row(f"{label} (1-4)", getattr(fb, key), accent, tint)
    rep.row("Additional Comments", fb.additional_comments, accent, tint)
    rep.small("Rating scale:  1 = Poor  ·  2 = Fair  ·  3 = Good  ·  4 = Excellent")


def build(candidate: Candidate) -> bytes:
    """The report as PDF bytes. Caller ensures ref_check_details exists."""
    d = candidate.ref_check_details
    feedback = {f.ref_index: f for f in candidate.referee_feedback}
    name = d.candidate_name or candidate.name or ""

    rep = _Report()
    rep.banner(name)

    accent, tint = _CANDIDATE
    rep.section("Candidate Detail", accent)
    rep.row("Name", name, accent, tint)
    rep.row("Date", d.reference_check_date, accent, tint)
    rep.row("Position Applied For", d.position_applied_for, accent, tint)
    rep.gap(16)

    for n in (1, 2):
        accent, tint = _REFEREES[n]
        rep.section(f"Referee Detail {n}", accent, keep_with=4 * 28)
        rep.row("Referee Name", getattr(d, f"ref{n}_name"), accent, tint)
        rep.row("Title", getattr(d, f"ref{n}_title"), accent, tint)
        rep.row("Referee Mail ID", getattr(d, f"ref{n}_email"), accent, tint)
        rep.row("Referee Phone No.", getattr(d, f"ref{n}_phone"), accent, tint)
        _feedback(rep, feedback.get(n), accent, tint)
        rep.gap(16)

    if (d.message_to_hiring_team or "").strip():
        accent, tint = _MESSAGE
        rep.section("Message to Hiring Team", accent)
        rep.row("Candidate's Note", d.message_to_hiring_team, accent, tint)

    return rep.finish()
