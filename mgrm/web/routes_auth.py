"""Signing in and out, and changing your own password."""

from datetime import UTC, datetime

from authlib.integrations.base_client.errors import OAuthError
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from mgrm.auth.microsoft import resolve_microsoft_user
from mgrm.auth.passwords import DUMMY_HASH, verify_password
from mgrm.auth.users import UserError, audit, end_sessions, set_password
from mgrm.models import AppUser, AuthMethod
from mgrm.web.app import log, render
from mgrm.web.security import (
    clear_failed_sign_ins,
    client_address,
    current_user_or_none,
    get_db,
    record_failed_sign_in,
    sign_in_refused,
    signed_in_user,
    start_session,
    verify_csrf,
)

router = APIRouter()

# One message whatever went wrong, so the page does not reveal which emails have accounts.
SIGN_IN_FAILED = ("That email and password do not match an active platform account. "
                  "If you sign in with Microsoft 365, use the Microsoft button.")
TOO_MANY_ATTEMPTS = ("Too many failed attempts from this computer. Wait 15 minutes and try again, "
                     "or ask the administrator to reset your password.")


@router.get("/login")
def login_page(request: Request, user: AppUser | None = Depends(current_user_or_none)):
    if user is not None:
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html")


@router.post("/login", dependencies=[Depends(verify_csrf)])
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    email = email.strip().lower()[:254]
    address = client_address(request)
    if sign_in_refused(db, email, address):
        log.warning("Sign-in refused for %s from %s: too many failures", email, address)
        return render(request, "login.html", status_code=429, error=TOO_MANY_ATTEMPTS, email=email)
    user = db.scalar(select(AppUser).where(AppUser.email == email))
    if user is None or user.auth_method is not AuthMethod.LOCAL or not user.is_active:
        verify_password(DUMMY_HASH, password)  # same delay as a real check
        ok = False
    else:
        ok = verify_password(user.password_hash, password)
    if not ok:
        record_failed_sign_in(db, email, address)
        audit(db, None, "sign_in.failed", email)
        return render(request, "login.html", status_code=401, error=SIGN_IN_FAILED, email=email)
    clear_failed_sign_ins(db, email, address)
    user.last_sign_in_at = datetime.now(UTC)
    audit(db, user, "sign_in", email, method="local")
    start_session(request, user)
    return RedirectResponse("/", status_code=303)


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request, user: AppUser | None = Depends(current_user_or_none)):
    if user is not None:
        end_sessions(user)  # signing out here signs out everywhere, so a copied session is useless too
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/account/password")
def password_page(request: Request, user: AppUser = Depends(signed_in_user)):
    return render(request, "password.html", user=user)


@router.post("/account/password", dependencies=[Depends(verify_csrf)])
def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    user: AppUser = Depends(signed_in_user),
    db: Session = Depends(get_db),
):
    def again(error: str):
        return render(request, "password.html", status_code=400, user=user, error=error)

    if user.auth_method is not AuthMethod.LOCAL:
        return again("You sign in with Microsoft 365, so your password is managed by Microsoft.")
    if not verify_password(user.password_hash, current_password):
        return again("Your current password is not right.")
    if new_password != confirm_password:
        return again("The two new passwords do not match.")
    if new_password == current_password:
        return again("Choose a password different from the current one.")
    try:
        set_password(db, actor=user, user=user, password=new_password)
    except UserError as exc:
        return again(str(exc))
    start_session(request, user)  # every other session ends; this one carries on
    return RedirectResponse("/", status_code=303)


@router.get("/auth/microsoft/login")
async def microsoft_login(request: Request):
    oauth = request.app.state.oauth
    if oauth is None:
        return render(
            request,
            "message.html",
            status_code=404,
            title="Microsoft sign-in is not switched on",
            message="IT has not registered the platform with Microsoft 365 yet. Sign in with your platform password.",
        )
    return await oauth.microsoft.authorize_redirect(request, str(request.url_for("microsoft_callback")))


@router.get("/auth/microsoft/callback", name="microsoft_callback")
async def microsoft_callback(request: Request, db: Session = Depends(get_db)):
    oauth = request.app.state.oauth
    if oauth is None:
        return RedirectResponse("/login", status_code=303)
    try:
        token = await oauth.microsoft.authorize_access_token(request)
    except OAuthError as exc:
        log.warning("Microsoft sign-in did not complete from %s: %s", client_address(request), exc.error)
        return render(request, "message.html", status_code=400, title="Microsoft sign-in did not complete",
                      message="Microsoft did not confirm the sign-in, or it took too long. Go back to the sign-in page and try again.")
    claims = token.get("userinfo") or {}
    user = resolve_microsoft_user(db, claims, request.app.state.settings.entra_tenant_id)
    if user is None:
        audit(db, None, "sign_in.refused", str(claims.get("preferred_username", "unknown")), method="microsoft")
        return render(
            request,
            "message.html",
            status_code=403,
            title="No platform access",
            message="Microsoft recognised you, but you have not been given access to this platform. "
            "Ask the administrator to add you.",
        )
    user.last_sign_in_at = datetime.now(UTC)
    audit(db, user, "sign_in", user.email, method="microsoft")
    start_session(request, user)
    return RedirectResponse("/", status_code=303)
