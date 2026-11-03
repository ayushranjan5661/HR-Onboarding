"""
Each HR's own Zoho connection — backs the "Connect Zoho" button.

Server-based OAuth (authorization code grant, offline access):
  1. start_url(): the HR's browser goes to Zoho's consent page; `state` is a
     short-lived JWT naming the HR, plus a nonce stored on their row.
  2. finish(): the callback swaps the code for a refresh token, reads the
     Zoho account's email, requires it to be the HR's portal email, finds
     their P_Employee record and stores token (encrypted) + record ids.
  3. actor_for(): every push asks for a ZohoActor — the HR's access token and
     employee record id. Not connected = ZohoNotConnected, never the admin
     token (unless ZOHO_AUTH_MODE=admin, which is all-admin, see config.py).
"""
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet, InvalidToken
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.config import settings
from app.models import HRUser
from app.services import zoho_push
from app.services.zoho_push import _zc

STATE_PURPOSE = "zoho_oauth"
STATE_TTL_MINUTES = 10
EMPLOYEE_FORM = "P_Employee"
EMPLOYEE_EMAIL_FIELD = "EmailID"
EMPLOYEE_ID_FIELD = "EmployeeID"
# Pages the callback may send the browser back to (first path segment).
RETURN_SECTIONS = {"hr-portal", "manager", "super-admin"}
DEFAULT_RETURN = "hr-portal/dashboard.html"


class ZohoNotConnected(RuntimeError):
    """The HR has to (re)connect Zoho. Message is safe to show as-is."""


class ZohoAuthError(RuntimeError):
    """Anything else that stopped a connect or token refresh. Safe to show."""


@dataclass
class ZohoActor:
    token: str
    employee_record_id: str     # "" only in admin mode, for an HR with no Employee ID
    mode: str                   # "user" | "admin"


USER_MODES = {"user", "per_user"}


def user_mode() -> bool:
    """True for per-HR tokens. Only an exact "admin" selects the shared admin
    token; anything unrecognised is refused rather than guessed at."""
    mode = settings.ZOHO_AUTH_MODE.strip().lower()
    if mode == "admin":
        return False
    if mode in USER_MODES:
        return True
    raise ZohoAuthError(f"ZOHO_AUTH_MODE={settings.ZOHO_AUTH_MODE!r} is not recognised; "
                        "use per_user or admin.")


def missing_settings() -> list[str]:
    return [n for n in ("ZOHO_OAUTH_CLIENT_ID", "ZOHO_OAUTH_CLIENT_SECRET",
                        "ZOHO_OAUTH_REDIRECT_URI", "ZOHO_TOKEN_ENCRYPTION_KEY")
            if not getattr(settings, n).strip()]


def _accounts_url(path: str) -> str:
    return settings.ZOHO_ACCOUNTS_BASE_URL.rstrip("/") + path


def _cipher() -> Fernet:
    try:
        return Fernet(settings.ZOHO_TOKEN_ENCRYPTION_KEY.strip().encode())
    except ValueError as exc:
        raise ZohoAuthError("ZOHO_TOKEN_ENCRYPTION_KEY is not a valid Fernet key.") from exc


def _network(exc) -> ZohoAuthError:
    return ZohoAuthError(f"Could not reach Zoho from this server: {exc}")


# ---------------------------------------------------------------------------
# Connect
# ---------------------------------------------------------------------------

def _safe_return(return_to: str | None) -> str:
    """A page path relative to the portal root, e.g. hr-portal/candidate.html?id=4.
    Anything else (absolute URLs, //host, ..) falls back to the dashboard."""
    value = (return_to or "").strip().lstrip("/")
    parts = urllib.parse.urlsplit(value)
    if (parts.scheme or parts.netloc or ".." in parts.path or "\\" in value
            or parts.path.split("/")[0] not in RETURN_SECTIONS):
        return DEFAULT_RETURN
    return value


def start_url(db: Session, user: HRUser, return_to: str | None) -> str:
    """Zoho consent URL for this HR. Stores a fresh nonce, so only the latest
    Connect click can complete."""
    missing = missing_settings()
    if missing:
        raise ZohoAuthError("Zoho connection is not set up on the server yet "
                            f"(missing {', '.join(missing)}). Ask the administrator.")
    nonce = secrets.token_urlsafe(24)
    user.zoho_oauth_nonce = nonce
    db.commit()
    state = jwt.encode({"sub": str(user.id), "purpose": STATE_PURPOSE, "nonce": nonce,
                        "ret": _safe_return(return_to),
                        "exp": datetime.now(timezone.utc) + timedelta(minutes=STATE_TTL_MINUTES)},
                       settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    query = urllib.parse.urlencode({
        "scope": settings.ZOHO_OAUTH_SCOPES,
        "client_id": settings.ZOHO_OAUTH_CLIENT_ID,
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",        # always issue a refresh token, even on reconnect
        "redirect_uri": settings.ZOHO_OAUTH_REDIRECT_URI,
        "state": state,
    })
    return _accounts_url("/oauth/v2/auth?") + query


def read_state(state: str) -> dict | None:
    try:
        claims = jwt.decode(state or "", settings.JWT_SECRET_KEY,
                            algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        return None
    return claims if claims.get("purpose") == STATE_PURPOSE else None


def return_url(ret: str | None, **params) -> str:
    ret = _safe_return(ret)
    sep = "&" if "?" in ret else "?"
    return f"{settings.PORTAL_BASE_URL.rstrip('/')}/{ret}{sep}{urllib.parse.urlencode(params)}"


def _revoke(refresh_token: str) -> None:
    """Best effort: a token Zoho still honours is harmless once we forget it."""
    try:
        _zc._http("POST", _accounts_url("/oauth/v2/token/revoke?")
                  + urllib.parse.urlencode({"token": refresh_token}))
    except _zc.ZohoError:
        pass


def _employee_for(token: str, email: str) -> tuple[str, str]:
    """(P_Employee record id, visible Employee ID) for this email, looked up
    with the HR's own token."""
    try:
        hits = _zc.find_by_email(token, EMPLOYEE_FORM, email, EMPLOYEE_EMAIL_FIELD)
    except _zc.ZohoUnreachable as exc:
        raise _network(exc) from exc
    except _zc.ZohoError as exc:
        raise ZohoAuthError(f"Could not look up your Zoho employee record: {exc}") from exc
    if len(hits) > 1:
        raise ZohoAuthError(f"{len(hits)} Zoho employees have the email {email}; "
                            "ask the Zoho admin to fix the duplicates.")
    record_id = zoho_push._hit_id(hits[0]) if hits else ""
    if not record_id:
        raise ZohoAuthError(f"No Zoho People employee record has the email {email}.")
    fields = (hits[0].get(record_id) or [{}])[0]
    return record_id, str(fields.get(EMPLOYEE_ID_FIELD) or "")


def finish(db: Session, state: str, code: str, accounts_server: str | None) -> HRUser:
    """Complete a Connect Zoho click. Raises ZohoAuthError; nothing is stored
    unless every check passes."""
    claims = read_state(state)
    if not claims:
        raise ZohoAuthError("This Zoho sign-in link expired. Click Connect Zoho again.")
    user = db.query(HRUser).filter(HRUser.id == int(claims["sub"]),
                                   HRUser.is_active.is_(True)).first()
    if not user or not user.zoho_oauth_nonce or not secrets.compare_digest(
            user.zoho_oauth_nonce, str(claims.get("nonce", ""))):
        raise ZohoAuthError("This Zoho sign-in was already used or replaced by a newer one. "
                            "Click Connect Zoho again.")
    user.zoho_oauth_nonce = None
    db.commit()

    # Zoho names the data centre the account lives in. The token exchange
    # (which carries the client secret) only ever goes to the configured one.
    if accounts_server and accounts_server.rstrip("/") != settings.ZOHO_ACCOUNTS_BASE_URL.rstrip("/"):
        raise ZohoAuthError(f"Your Zoho account is in another data centre ({accounts_server}); "
                            f"this app uses {settings.ZOHO_ACCOUNTS_BASE_URL}.")
    if not code:
        raise ZohoAuthError("Zoho did not return an authorization code.")

    try:
        grant = _zc._http("POST", _accounts_url("/oauth/v2/token"), {
            "grant_type": "authorization_code",
            "client_id": settings.ZOHO_OAUTH_CLIENT_ID,
            "client_secret": settings.ZOHO_OAUTH_CLIENT_SECRET,
            "redirect_uri": settings.ZOHO_OAUTH_REDIRECT_URI,
            "code": code,
        })
    except _zc.ZohoUnreachable as exc:
        raise _network(exc) from exc
    except _zc.ZohoError as exc:
        raise ZohoAuthError(f"Zoho refused the sign-in: {exc}") from exc
    refresh_token, access_token = grant.get("refresh_token"), grant.get("access_token")
    if not refresh_token or not access_token:
        raise ZohoAuthError(f"Zoho did not issue a refresh token: {grant.get('error', grant)}")

    try:
        try:
            info = _zc._http("GET", _accounts_url("/oauth/user/info"), token=access_token)
        except _zc.ZohoUnreachable as exc:
            raise _network(exc) from exc
        except _zc.ZohoError as exc:
            raise ZohoAuthError(f"Could not read your Zoho account: {exc}") from exc
        zoho_email = str(info.get("Email") or "").strip()
        if not zoho_email:
            raise ZohoAuthError("Zoho did not return the account's email "
                                "(is AaaServer.profile.Read in ZOHO_OAUTH_SCOPES?).")
        if zoho_email.lower() != (user.email or "").strip().lower():
            raise ZohoAuthError(f"You signed in to Zoho as {zoho_email}, but your portal account "
                                f"is {user.email}. Sign in to Zoho with {user.email}.")
        record_id, employee_id = _employee_for(access_token, zoho_email)
    except ZohoAuthError:
        _revoke(refresh_token)
        raise

    user.zoho_refresh_token_enc = _cipher().encrypt(refresh_token.encode()).decode()
    user.zoho_email = zoho_email
    user.zoho_employee_record_id = record_id
    user.zoho_employee_id = employee_id or None
    user.zoho_connected_at = datetime.now(timezone.utc)
    _cache_put(user, access_token, int(grant.get("expires_in", 3600)))
    _zc.log("oauth.connected", hr_user_id=user.id, zoho_email=zoho_email,
            employee_record_id=record_id, employee_id=employee_id,
            scope=grant.get("scope", ""))
    return user


def disconnect(user: HRUser) -> None:
    token = _decrypt(user)
    if token:
        _revoke(token)
    _clear(user)
    _zc.log("oauth.disconnected", hr_user_id=user.id)


def _clear(user: HRUser) -> None:
    user.zoho_refresh_token_enc = None
    user.zoho_email = None
    user.zoho_employee_record_id = None
    user.zoho_employee_id = None
    user.zoho_connected_at = None
    _cache.pop(user.id, None)


def _decrypt(user: HRUser) -> str | None:
    if not user.zoho_refresh_token_enc:
        return None
    try:
        return _cipher().decrypt(user.zoho_refresh_token_enc.encode()).decode()
    except InvalidToken:
        return None     # encrypted under an older ZOHO_TOKEN_ENCRYPTION_KEY


def status(user: HRUser) -> dict:
    return {
        "mode": "user" if user_mode() else "admin",
        "configured": not missing_settings(),
        "connected": bool(user.zoho_refresh_token_enc),
        "zoho_email": user.zoho_email,
        "zoho_employee_id": user.zoho_employee_id,
        "zoho_employee_record_id": user.zoho_employee_record_id,
        "connected_at": user.zoho_connected_at,
    }


# ---------------------------------------------------------------------------
# Tokens for pushes
# ---------------------------------------------------------------------------

# hr_user_id -> (refresh token ciphertext it came from, access token, expiry).
# Per process; a reconnect changes the ciphertext, so stale entries miss.
_cache: dict[int, tuple[str, str, float]] = {}
_cache_lock = threading.Lock()


def _cache_put(user: HRUser, access_token: str, expires_in: int) -> None:
    with _cache_lock:
        _cache[user.id] = (user.zoho_refresh_token_enc or "", access_token,
                           time.time() + expires_in)


def _user_token(db: Session, user: HRUser) -> str:
    with _cache_lock:
        hit = _cache.get(user.id)
    if hit and hit[0] == user.zoho_refresh_token_enc and hit[2] > time.time() + 60:
        return hit[1]

    refresh_token = _decrypt(user)
    if not refresh_token:
        _clear(user)
        db.commit()
        raise ZohoNotConnected("Your Zoho connection can no longer be read. Connect Zoho again.")
    try:
        payload = _zc._http("POST", _accounts_url("/oauth/v2/token"), {
            "grant_type": "refresh_token",
            "client_id": settings.ZOHO_OAUTH_CLIENT_ID,
            "client_secret": settings.ZOHO_OAUTH_CLIENT_SECRET,
            "refresh_token": refresh_token,
        })
    except _zc.ZohoUnreachable as exc:
        raise _network(exc) from exc
    except _zc.ZohoError as exc:
        raise ZohoAuthError(f"Could not refresh your Zoho token: {exc}") from exc
    if "access_token" not in payload:
        # invalid_code: revoked in Zoho, or the user removed the app's access.
        if payload.get("error") == "invalid_code":
            _clear(user)
            db.commit()
            _zc.log("oauth.revoked", hr_user_id=user.id)
            raise ZohoNotConnected("Your Zoho connection was revoked or has expired. "
                                   "Connect Zoho again.")
        raise ZohoAuthError(f"Could not refresh your Zoho token: {payload.get('error', payload)}")
    _cache_put(user, payload["access_token"], int(payload.get("expires_in", 3600)))
    return payload["access_token"]


def _admin_employee_record_id(token: str, employee_id: str | None) -> str:
    """Admin mode only: the HR's P_Employee record id from their portal
    Employee ID, or "" when they have none."""
    employee_id = (employee_id or "").strip()
    if not employee_id:
        return ""
    try:
        hits = _zc.find_by_email(token, EMPLOYEE_FORM, employee_id, EMPLOYEE_ID_FIELD)
    except _zc.ZohoUnreachable as exc:
        raise _network(exc) from exc
    except _zc.ZohoError as exc:
        raise ZohoAuthError(f"Could not look up your Employee ID in Zoho: {exc}") from exc
    if len(hits) > 1:
        raise ZohoAuthError(f"{len(hits)} Zoho employees have the Employee ID {employee_id}; "
                            "fix the duplicates in Zoho, then publish again.")
    record_id = zoho_push._hit_id(hits[0]) if hits else ""
    if not record_id:
        raise ZohoAuthError(f"No Zoho employee has the Employee ID {employee_id}. Ask your "
                            "Manager or an Admin to correct it on your portal account.")
    return record_id


def actor_for(db: Session, user: HRUser) -> ZohoActor:
    """Token and employee record a push by `user` runs as. Raises
    ZohoNotConnected (user must connect) or ZohoAuthError."""
    if not user_mode():
        try:
            token = _zc.access_token(force=True)
        except _zc.ZohoUnreachable as exc:
            raise _network(exc) from exc
        except _zc.ZohoError as exc:
            raise ZohoAuthError(f"Could not authenticate with Zoho: {exc}") from exc
        return ZohoActor(token, _admin_employee_record_id(token, user.employee_id), "admin")

    missing = missing_settings()
    if missing:
        raise ZohoAuthError("Zoho connection is not set up on the server yet "
                            f"(missing {', '.join(missing)}). Ask the administrator.")
    if not user.zoho_refresh_token_enc or not user.zoho_employee_record_id:
        raise ZohoNotConnected("Connect your Zoho account before publishing. Records are "
                               "created in Zoho under your own login and assigned to you.")
    return ZohoActor(_user_token(db, user), user.zoho_employee_record_id, "user")
