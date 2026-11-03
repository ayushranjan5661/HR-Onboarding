"""Connect Zoho flow (app/services/zoho_oauth.py), with Zoho's HTTP mocked."""
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models import HRUser
from app.services import zoho_oauth as z


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(settings, "ZOHO_AUTH_MODE", "user")
    monkeypatch.setattr(settings, "ZOHO_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setattr(settings, "ZOHO_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setattr(settings, "ZOHO_OAUTH_REDIRECT_URI", "http://127.0.0.1:8000/zoho/oauth/callback")
    monkeypatch.setattr(settings, "ZOHO_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(z._zc, "log", lambda *a, **k: None)
    z._cache.clear()
    engine = create_engine("sqlite://")
    HRUser.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    session.add(HRUser(name="Sangamitra", email="sangamitra@levelshift.com",
                       password_hash="x", role="HR"))
    session.commit()
    yield session
    session.close()


def fake_zoho(email="sangamitra@levelshift.com", employees=None, refresh_error=None):
    """Stand-in for zoho_client._http / find_by_email; records every call."""
    calls = []
    employees = [{"4100": [{"EmployeeID": "LS-123"}]}] if employees is None else employees

    def http(method, url, data=None, token=None, **_):
        calls.append((method, url, data, token))
        if url.endswith("/oauth/v2/token") and data["grant_type"] == "authorization_code":
            return {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600}
        if url.endswith("/oauth/v2/token") and data["grant_type"] == "refresh_token":
            return {"error": refresh_error} if refresh_error else {"access_token": "at-2", "expires_in": 3600}
        if url.endswith("/oauth/user/info"):
            return {"Email": email}
        if "/revoke" in url:
            return {}
        raise AssertionError(url)

    def find(token, form, value, field):
        calls.append(("find", form, value, token))
        return employees
    return http, find, calls


def connect(db, monkeypatch, **kw):
    http, find, calls = fake_zoho(**kw)
    monkeypatch.setattr(z._zc, "_http", http)
    monkeypatch.setattr(z._zc, "find_by_email", find)
    user = db.query(HRUser).one()
    url = z.start_url(db, user, "hr-portal/candidate.html?id=4")
    state = parse_qs(urlsplit(url).query)["state"][0]
    return user, state, calls


def test_connect_stores_encrypted_token_and_employee(db, monkeypatch):
    user, state, calls = connect(db, monkeypatch)
    z.finish(db, state, "code-1", settings.ZOHO_ACCOUNTS_BASE_URL)
    assert user.zoho_employee_record_id == "4100" and user.zoho_employee_id == "LS-123"
    assert user.zoho_refresh_token_enc and "rt-1" not in user.zoho_refresh_token_enc
    assert z._decrypt(user) == "rt-1"
    # The employee lookup ran on the HR's own token.
    assert ("find", "P_Employee", "sangamitra@levelshift.com", "at-1") in calls


def test_state_is_single_use(db, monkeypatch):
    _, state, _ = connect(db, monkeypatch)
    z.finish(db, state, "code-1", None)
    with pytest.raises(z.ZohoAuthError, match="already used"):
        z.finish(db, state, "code-1", None)


def test_other_zoho_account_is_rejected_and_revoked(db, monkeypatch):
    user, state, calls = connect(db, monkeypatch, email="ramya@levelshift.com")
    with pytest.raises(z.ZohoAuthError, match="signed in to Zoho as ramya"):
        z.finish(db, state, "code-1", None)
    assert user.zoho_refresh_token_enc is None
    assert any("/revoke" in c[1] for c in calls if c[0] == "POST")


def test_no_employee_record_is_rejected(db, monkeypatch):
    user, state, _ = connect(db, monkeypatch, employees=[])
    with pytest.raises(z.ZohoAuthError, match="No Zoho People employee"):
        z.finish(db, state, "code-1", None)
    assert user.zoho_refresh_token_enc is None


def test_other_data_centre_never_gets_the_secret(db, monkeypatch):
    _, state, calls = connect(db, monkeypatch)
    with pytest.raises(z.ZohoAuthError, match="another data centre"):
        z.finish(db, state, "code-1", "https://accounts.evil.example")
    assert not any(c[0] == "POST" for c in calls)


def test_push_without_connection_never_uses_admin_token(db, monkeypatch):
    monkeypatch.setattr(z._zc, "access_token", lambda **k: pytest.fail("admin token used"))
    with pytest.raises(z.ZohoNotConnected):
        z.actor_for(db, db.query(HRUser).one())


def test_actor_uses_user_token_and_employee(db, monkeypatch):
    user, state, _ = connect(db, monkeypatch)
    z.finish(db, state, "code-1", None)
    z._cache.clear()
    actor = z.actor_for(db, user)
    assert (actor.token, actor.employee_record_id, actor.mode) == ("at-2", "4100", "user")


def test_revoked_refresh_token_disconnects(db, monkeypatch):
    user, state, _ = connect(db, monkeypatch, refresh_error="invalid_code")
    z.finish(db, state, "code-1", None)
    z._cache.clear()
    with pytest.raises(z.ZohoNotConnected, match="revoked"):
        z.actor_for(db, user)
    assert user.zoho_refresh_token_enc is None


def test_return_path_cannot_leave_the_portal():
    assert z._safe_return("hr-portal/candidate.html?id=4") == "hr-portal/candidate.html?id=4"
    for bad in ("https://evil.example/x", "//evil.example", "hr-portal/../x", "candidate-portal/home.html"):
        assert z._safe_return(bad) == z.DEFAULT_RETURN


@pytest.mark.parametrize("mode,expected", [("per_user", True), ("user", True),
                                           (" PER_USER ", True), ("admin", False)])
def test_auth_modes(monkeypatch, mode, expected):
    monkeypatch.setattr(settings, "ZOHO_AUTH_MODE", mode)
    assert z.user_mode() is expected


def test_unknown_auth_mode_refused(monkeypatch):
    monkeypatch.setattr(settings, "ZOHO_AUTH_MODE", "amdin")
    with pytest.raises(z.ZohoAuthError):
        z.user_mode()


def test_per_user_never_falls_back_to_admin_token(db, monkeypatch):
    monkeypatch.setattr(settings, "ZOHO_AUTH_MODE", "per_user")
    monkeypatch.setattr(z._zc, "access_token",
                        lambda **_: pytest.fail("admin token requested"))
    with pytest.raises(z.ZohoNotConnected):
        z.actor_for(db, db.query(HRUser).one())
