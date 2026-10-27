"""Signed links for the public referee feedback form.

A token is "<candidate_id>.<ref_index>.<signature>", the signature an HMAC of
those two plus the reference's current mail ID. Nothing is stored for it, and
correcting a reference's mail ID automatically retires the old link.
"""
import hashlib
import hmac
from typing import Optional

from app.config import settings
from app.models import Candidate, RefereeFeedback

FEEDBACK_FIELDS = [
    "relationship_with_candidate", "candidate_strengths", "candidate_development_areas",
    "rating_reliability", "rating_punctuality", "rating_attendance", "rating_professionalism",
    "additional_comments",
]


def _signature(candidate_id: int, ref_index: int, email: str) -> str:
    msg = f"referee:{candidate_id}:{ref_index}:{email.strip().lower()}".encode()
    return hmac.new(settings.JWT_SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()[:40]


def reference_email(candidate: Candidate, ref_index: int) -> str:
    details = candidate.ref_check_details
    return (getattr(details, f"ref{ref_index}_email", None) or "").strip() if details else ""


def make_token(candidate: Candidate, ref_index: int) -> Optional[str]:
    email = reference_email(candidate, ref_index)
    if not email:
        return None
    return f"{candidate.id}.{ref_index}.{_signature(candidate.id, ref_index, email)}"


def parse_token(token: str) -> Optional[tuple[int, int, str]]:
    """(candidate_id, ref_index, signature), or None when malformed."""
    parts = (token or "").split(".")
    if len(parts) != 3 or not parts[0].isdigit() or parts[1] not in ("1", "2"):
        return None
    return int(parts[0]), int(parts[1]), parts[2]


def verify(candidate: Candidate, ref_index: int, signature: str) -> bool:
    email = reference_email(candidate, ref_index)
    return bool(email) and hmac.compare_digest(signature, _signature(candidate.id, ref_index, email))


def form_url(candidate: Candidate, ref_index: int) -> Optional[str]:
    token = make_token(candidate, ref_index)
    return f"{settings.REFEREE_FORM_URL}?token={token}" if token else None


def feedback_for_hr(candidate: Candidate) -> list[dict]:
    """Link + answers for each reference the candidate named."""
    given = {f.ref_index: f for f in candidate.referee_feedback}
    out = []
    for n in (1, 2):
        url = form_url(candidate, n)
        if not url:
            continue
        fb: Optional[RefereeFeedback] = given.get(n)
        out.append({
            "ref_index": n,
            "form_url": url,
            "submitted": fb is not None,
            "submitted_at": fb.submitted_at.isoformat() if fb and fb.submitted_at else None,
            "answers": {k: getattr(fb, k) for k in FEEDBACK_FIELDS} if fb else None,
        })
    return out
