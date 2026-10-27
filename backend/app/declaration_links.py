"""Signed links for the public Declaration forms.

Each Declaration (education documents; relieving and experience letters) is
filled through its own link, not the candidate login. A token is
"<candidate_id>.<signature>", the signature an HMAC of the id plus that
link's secret. Nothing else is stored for it.

Older candidates have no secret of their own yet; their link rides on the
invite token and its expiry. The first renewal of either link gives the
Declaration link its own secret (see detach / issue), and from then on the
login "New Link" and each Declaration "New Link" never retire each other.
"""
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import settings
from app.models import Candidate
from app.security import generate_invite_token


def parse_token(token: str) -> Optional[tuple[int, str]]:
    """(candidate_id, signature), or None when malformed."""
    parts = (token or "").split(".")
    if len(parts) != 2 or not parts[0].isdigit():
        return None
    return int(parts[0]), parts[1]


class FormLink:
    """One Declaration form's link: which Candidate columns hold its secret
    and expiry, and which page it opens."""

    def __init__(self, purpose: str, secret_attr: str, expiry_attr: str, page: str):
        self.purpose = purpose
        self.secret_attr = secret_attr
        self.expiry_attr = expiry_attr
        self.page = page

    def _secret(self, candidate: Candidate) -> str:
        return (getattr(candidate, self.secret_attr) or candidate.invite_token
                or candidate.email or "")

    def _signature(self, candidate: Candidate) -> str:
        msg = f"{self.purpose}:{candidate.id}:{self._secret(candidate)}".encode()
        return hmac.new(settings.JWT_SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()[:40]

    def make_token(self, candidate: Candidate) -> str:
        return f"{candidate.id}.{self._signature(candidate)}"

    def verify(self, candidate: Candidate, signature: str) -> bool:
        return hmac.compare_digest(signature, self._signature(candidate))

    def expires_at(self, candidate: Candidate) -> Optional[datetime]:
        if getattr(candidate, self.secret_attr):
            exp = getattr(candidate, self.expiry_attr)
        else:
            exp = candidate.invite_token_expires_at if candidate.invite_token else None
        if exp is not None and exp.tzinfo is None:   # written as UTC; SQLite hands back naive datetimes
            exp = exp.replace(tzinfo=timezone.utc)
        return exp

    def expired(self, candidate: Candidate) -> bool:
        exp = self.expires_at(candidate)
        return exp is not None and exp < datetime.now(timezone.utc)

    def detach(self, candidate: Candidate) -> None:
        """Freeze the current link before the invite token rotates, so a login
        "New Link" leaves the Declaration link already sent working."""
        if not getattr(candidate, self.secret_attr):
            setattr(candidate, self.expiry_attr, self.expires_at(candidate))
            setattr(candidate, self.secret_attr, self._secret(candidate))

    def issue(self, candidate: Candidate) -> None:
        """A fresh link; the previous one stops working at once."""
        setattr(candidate, self.secret_attr, generate_invite_token())
        setattr(candidate, self.expiry_attr,
                datetime.now(timezone.utc) + timedelta(days=settings.INVITE_LINK_EXPIRY_DAYS))

    def form_url(self, candidate: Candidate) -> str:
        return f"{settings.PORTAL_BASE_URL}/{self.page}?token={self.make_token(candidate)}"


EDUCATION = FormLink("declaration", "declaration_link_secret", "declaration_link_expires_at",
                     "declaration-form-portal/declaration-form.html")
EXPERIENCE = FormLink("experience-declaration", "exp_declaration_link_secret",
                      "exp_declaration_link_expires_at",
                      "declaration-form-portal/experience-declaration-form.html")
ALL = (EDUCATION, EXPERIENCE)

# The education Declaration predates the second form; callers use these.
make_token = EDUCATION.make_token
verify = EDUCATION.verify
expires_at = EDUCATION.expires_at
expired = EDUCATION.expired
issue = EDUCATION.issue
form_url = EDUCATION.form_url


def detach(candidate: Candidate) -> None:
    for link in ALL:
        link.detach(candidate)
