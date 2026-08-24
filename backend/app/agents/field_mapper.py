"""
Cross-form mapping agent.

The three forms ask for many of the same things under different names — the
CIF's "Candidate Profile Picture" is the Document Collection's "Passport size
photo"; the CIF's declaration "Signature" is the BGV's "Signature". This agent
works out those equivalences from the human labels so a candidate never
re-enters or re-uploads something the system already holds.

How it decides, in order:
  1. CURATED — hand-verified pairs that must always hold. Never overridden.
  2. LLM     — Azure OpenAI reasons over the label lists and proposes the rest.
  3. HEURISTIC — token-overlap matching, used when the LLM is unavailable.

Every LLM proposal is validated against the catalogue (both keys must exist,
the kinds must match) and low-confidence guesses are dropped, so a bad model
response can never inject a nonsense mapping. Results are cached in the
database because the form schemas rarely change.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from app.config import settings
from app.field_catalog import labels_for

# ---------------------------------------------------------------------------
# 1. Curated mappings — verified by hand, always applied.
#    (target_form, target_key) -> (source_form, source_key)
# ---------------------------------------------------------------------------
CURATED: dict[tuple[str, str], tuple[str, str]] = {
    # A profile photo and a passport-size photo are the same artefact.
    ("DOCUMENT_COLLECTION", "passport_size_photo"): ("CIF", "profile_picture"),
    # The signature given on the CIF declaration serves the BGV declaration.
    ("BGV", "bgv_signature"): ("CIF", "signature"),
    # Passport copy is collected once, during document collection.
    ("BGV", "passport_copy_bgv"): ("DOCUMENT_COLLECTION", "passport_copy"),
    # Legal name for verification is the name already on file.
    ("BGV", "name_as_per_records"): ("CIF", "full_name"),
    # Where the candidate signed the CIF is where they'll sign the BGV.
    ("BGV", "declaration_place"): ("CIF", "declaration_place"),
}

# Which forms may feed which. A form can only draw from earlier stages.
UPSTREAM = {
    "DOCUMENT_COLLECTION": ["CIF"],
    "BGV": ["CIF", "DOCUMENT_COLLECTION"],
}

# Labels that look similar but mean different things — never auto-map these.
BLOCKED_TARGETS = {
    # Consent must be given explicitly on each form, never inherited.
    ("BGV", "consent_bgv"), ("BGV", "consent_criminal_check"),
    ("BGV", "declaration_accepted"), ("BGV", "declaration_date"),
    # These are BGV-specific evidence, not something collected earlier.
    ("BGV", "signed_consent_form"), ("BGV", "form16_last_year"),
    ("BGV", "form16_previous_year"), ("BGV", "bank_statement_salary"),
    ("BGV", "police_clearance_certificate"),
}

_STOP = {"the", "of", "a", "an", "any", "if", "your", "no", "and", "or", "for",
          "in", "on", "at", "to", "please", "candidate", "details", "detail"}


def _tokens(label: str) -> set[str]:
    words = re.split(r"[^a-z0-9]+", label.lower())
    return {w for w in words if w and w not in _STOP}


# Words that make two labels mean the same thing even when spelled differently.
_SYNONYMS = [
    {"photo", "photograph", "picture", "photos"},
    {"aadhar", "aadhaar"},
    {"pan", "permanent account number"},
    {"phone", "mobile", "contact", "number"},
    {"address", "residence"},
    {"dob", "birth"},
    {"cv", "resume"},
    {"marksheet", "marks", "sheet"},
    {"certificate", "certification"},
]


def _canon(tokens: set[str]) -> set[str]:
    out = set()
    for t in tokens:
        for group in _SYNONYMS:
            if t in group:
                out.add(sorted(group)[0])
                break
        else:
            out.add(t)
    return out


def _similarity(a: str, b: str) -> float:
    ta, tb = _canon(_tokens(a)), _canon(_tokens(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ---------------------------------------------------------------------------
# 3. Heuristic fallback
# ---------------------------------------------------------------------------
def _heuristic(target_form: str, kind: str, threshold: float = 0.6) -> list[dict]:
    targets = labels_for(target_form, kind)
    proposals = []
    for tkey, tlabel in targets.items():
        if (target_form, tkey) in BLOCKED_TARGETS or (target_form, tkey) in CURATED:
            continue
        best = None
        for src_form in UPSTREAM.get(target_form, []):
            for skey, slabel in labels_for(src_form, kind).items():
                score = _similarity(tlabel, slabel)
                if score >= threshold and (best is None or score > best["confidence"]):
                    best = {"target_field": tkey, "source_form": src_form,
                            "source_field": skey, "confidence": round(score, 2),
                            "reason": f"label similarity: '{tlabel}' ~ '{slabel}'",
                            "decided_by": "heuristic"}
        if best:
            proposals.append(best)
    return proposals


# ---------------------------------------------------------------------------
# 2. LLM proposal
# ---------------------------------------------------------------------------
_PROMPT = """You map fields between HR onboarding forms for an Indian IT company.

A candidate fills these forms in order: CIF (Candidate Information Form), then
Document Collection, then BGV (Background Verification). Fields that mean the
same thing often have different names on each form.

TARGET form: {target_form} ({kind}s that need filling)
{targets}

AVAILABLE from earlier forms:
{sources}

Return ONLY a JSON array. Each element:
{{"target_field": "<target key>", "source_form": "<CIF|DOCUMENT_COLLECTION>",
  "source_field": "<source key>", "confidence": <0.0-1.0>, "reason": "<short>"}}

Rules:
- Map ONLY when the two fields hold genuinely the same information, so reusing
  the earlier value is always correct.
- A passport-size photo and a profile picture ARE the same. A passport copy is
  NOT a photo. An ID proof is NOT a PAN card.
- Never map consents, declarations, signatures-of-agreement or dates that must
  be given afresh on each form.
- Never map a "current employer" field to a "previous employer" field.
- Omit anything you are unsure about. An empty array is a valid answer.
- No prose, no markdown fences. JSON array only."""


def _llm_propose(target_form: str, kind: str) -> list[dict]:
    endpoint = (settings.AZURE_OPENAI_ENDPOINT or "").rstrip("/")
    key = settings.AZURE_OPENAI_API_KEY
    deployment = settings.AZURE_OPENAI_DEPLOYMENT
    if not (endpoint and key and deployment):
        return []

    targets = labels_for(target_form, kind)
    targets = {k: v for k, v in targets.items()
               if (target_form, k) not in BLOCKED_TARGETS
               and (target_form, k) not in CURATED}
    if not targets:
        return []

    src_lines = []
    for src_form in UPSTREAM.get(target_form, []):
        for skey, slabel in labels_for(src_form, kind).items():
            src_lines.append(f"- {src_form}.{skey}: {slabel}")
    if not src_lines:
        return []

    prompt = _PROMPT.format(
        target_form=target_form, kind=kind,
        targets="\n".join(f"- {k}: {v}" for k, v in targets.items()),
        sources="\n".join(src_lines),
    )
    url = (f"{endpoint}/openai/deployments/{deployment}/chat/completions"
           f"?api-version={settings.AZURE_OPENAI_API_VERSION}")
    body = json.dumps({
        "messages": [
            {"role": "system", "content": "You return only valid JSON arrays."},
            {"role": "user", "content": prompt},
        ],
        "max_completion_tokens": 2000,
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=settings.AI_MAPPING_TIMEOUT) as resp:
            payload = json.loads(resp.read())
        text = payload["choices"][0]["message"]["content"].strip()
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError,
            json.JSONDecodeError, TimeoutError, OSError) as exc:
        print(f"[field_mapper] LLM unavailable ({type(exc).__name__}); using heuristics")
        return []

    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        proposals = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", text, re.S)
        if not m:
            return []
        try:
            proposals = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
    if not isinstance(proposals, list):
        return []
    for p in proposals:
        if isinstance(p, dict):
            p["decided_by"] = "llm"
    return proposals


# ---------------------------------------------------------------------------
# Validation — nothing reaches a form until it passes this
# ---------------------------------------------------------------------------
def _validate(target_form: str, kind: str, proposals: list[dict],
               min_confidence: float) -> list[dict]:
    target_keys = set(labels_for(target_form, kind))
    valid, seen = [], set()
    for p in proposals:
        if not isinstance(p, dict):
            continue
        tkey = p.get("target_field")
        sform = p.get("source_form")
        skey = p.get("source_field")
        if not (tkey and sform and skey) or tkey in seen:
            continue
        if tkey not in target_keys:
            continue                                   # invented target
        if sform not in UPSTREAM.get(target_form, []):
            continue                                   # not an upstream form
        if skey not in labels_for(sform, kind):
            continue                                   # invented source / wrong kind
        if (target_form, tkey) in BLOCKED_TARGETS:
            continue                                   # must be answered afresh
        try:
            conf = float(p.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0.0
        if conf < min_confidence:
            continue
        seen.add(tkey)
        valid.append({"target_field": tkey, "source_form": sform, "source_field": skey,
                       "confidence": round(conf, 2), "reason": str(p.get("reason", ""))[:200],
                       "decided_by": p.get("decided_by", "llm")})
    return valid


def resolve(target_form: str, kind: str, use_llm: bool = True) -> list[dict]:
    """Mappings for one form and kind ('field' or 'document'), best-effort."""
    if target_form not in UPSTREAM:
        return []

    out = []
    # 1. curated first, and they win any conflict
    for (tform, tkey), (sform, skey) in CURATED.items():
        if tform != target_form:
            continue
        if tkey in labels_for(target_form, kind) and skey in labels_for(sform, kind):
            out.append({"target_field": tkey, "source_form": sform, "source_field": skey,
                         "confidence": 1.0, "reason": "verified equivalent",
                         "decided_by": "curated"})
    claimed = {m["target_field"] for m in out}

    proposals = _llm_propose(target_form, kind) if use_llm else []
    decided_by_llm = bool(proposals)
    if not proposals:
        proposals = _heuristic(target_form, kind)

    for m in _validate(target_form, kind, proposals,
                        settings.AI_MAPPING_MIN_CONFIDENCE if decided_by_llm else 0.0):
        if m["target_field"] not in claimed:
            out.append(m)
            claimed.add(m["target_field"])
    return out


def resolve_all(use_llm: bool = True) -> dict:
    """Every mapping, keyed by form then kind."""
    return {
        form: {kind: resolve(form, kind, use_llm=use_llm)
               for kind in ("field", "document")}
        for form in UPSTREAM
    }
