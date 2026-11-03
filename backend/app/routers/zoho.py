"""
Connect Zoho: each staff user links their own Zoho account, so their pushes
run on their token and land assigned to them. See app/services/zoho_oauth.py.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_staff
from app.models import HRUser
from app.services import zoho_oauth
from app.staff_service import audit, team_of

router = APIRouter(tags=["zoho"])


class ConnectRequest(BaseModel):
    # Portal page to come back to, relative to the portal root
    # (e.g. "hr-portal/candidate.html?id=4").
    return_to: Optional[str] = None


@router.get("/hr/zoho/connection")
def zoho_connection(current: HRUser = Depends(get_current_staff)):
    return zoho_oauth.status(current)


@router.post("/hr/zoho/connect")
def zoho_connect(payload: ConnectRequest, db: Session = Depends(get_db),
                 current: HRUser = Depends(get_current_staff)):
    """The Zoho consent URL the browser should go to."""
    if not zoho_oauth.user_mode():
        raise HTTPException(status_code=400,
                            detail="Zoho pushes use the shared admin connection (ZOHO_AUTH_MODE=admin).")
    try:
        return {"url": zoho_oauth.start_url(db, current, payload.return_to)}
    except zoho_oauth.ZohoAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/hr/zoho/disconnect")
def zoho_disconnect(db: Session = Depends(get_db), current: HRUser = Depends(get_current_staff)):
    if current.zoho_refresh_token_enc:
        email = current.zoho_email
        zoho_oauth.disconnect(current)
        audit(db, current, "ZOHO_DISCONNECTED", target_type="STAFF", target=current,
              detail=f"Disconnected Zoho account {email or ''}".strip(),
              team_manager_id=team_of(current))
        db.commit()
    return zoho_oauth.status(current)


@router.get("/zoho/oauth/callback")
def zoho_oauth_callback(request: Request, db: Session = Depends(get_db)):
    """Zoho redirects the browser here after consent. No session header comes
    with it: the signed `state` names the staff user. Always ends in a
    redirect back to the portal, carrying the outcome."""
    params = request.query_params
    claims = zoho_oauth.read_state(params.get("state", ""))
    ret = claims.get("ret") if claims else None
    if params.get("error"):
        reason = ("You declined access in Zoho." if params["error"] == "access_denied"
                  else f"Zoho returned an error: {params['error']}")
        return RedirectResponse(zoho_oauth.return_url(ret, zoho_error=reason), status_code=302)
    try:
        user = zoho_oauth.finish(db, params.get("state", ""), params.get("code", ""),
                                 params.get("accounts-server"))
    except zoho_oauth.ZohoAuthError as exc:
        db.rollback()
        return RedirectResponse(zoho_oauth.return_url(ret, zoho_error=str(exc)), status_code=302)
    audit(db, user, "ZOHO_CONNECTED", target_type="STAFF", target=user,
          detail=f"Connected Zoho account {user.zoho_email} "
                 f"(employee {user.zoho_employee_id or user.zoho_employee_record_id})",
          team_manager_id=team_of(user))
    db.commit()
    return RedirectResponse(zoho_oauth.return_url(ret, zoho="connected"), status_code=302)
