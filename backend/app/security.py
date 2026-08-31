import base64
import hashlib
import secrets
import string
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet, InvalidToken
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def generate_temp_password(length: int = 10) -> str:
    """Used by HR to auto-generate a candidate's initial login password."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


# --- Candidate password kept on record for HR ------------------------------
# Product decision: HR must always be able to see each candidate's permanent
# password. It is stored encrypted (key derived from JWT_SECRET_KEY), never as
# plaintext, so a database dump alone cannot expose the logins — the server
# secret is also required. Login verification still uses the bcrypt hash.

def _password_cipher() -> Fernet:
    digest = hashlib.sha256(
        (settings.JWT_SECRET_KEY + ":candidate-password-encryption").encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_password(plain: str) -> str:
    return _password_cipher().encrypt(plain.encode()).decode()


def decrypt_password(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return _password_cipher().decrypt(token.encode()).decode()
    except InvalidToken:
        return None   # encrypted under a different JWT_SECRET_KEY — unrecoverable


def generate_invite_token() -> str:
    """Token embedded in the one-click invite link. 43 URL-safe chars (~256
    bits) so it cannot be guessed or brute-forced."""
    return secrets.token_urlsafe(32)


def create_access_token(subject: str, role: str, extra: dict | None = None) -> str:
    to_encode = {"sub": subject, "role": role}
    if extra:
        to_encode.update(extra)
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode["exp"] = expire
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        return None
