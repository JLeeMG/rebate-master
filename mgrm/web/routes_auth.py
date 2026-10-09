"""Signing in and out, and changing your own password."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from mgrm.auth.microsoft import resolve_microsoft_user
from mgrm.auth.passwords import DUMMY_HASH, verify_password
from mgrm.auth.users import UserError, audit, set_password
from mgrm.models import AppUser, AuthMethod
from mgrm.web.app import render
from mgrm.web.security import current_user_or_none, get_db, signed_in_user, start_session, verify_csrf

router = APIRouter()


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
    email = email.strip().lower()
    throttle = request.app.state.throttle
    failed = "That email and password do not match an active platform account."
    if throttle.locked(email):
        return render(
            request,
            "login.html",
            status_code=429,
            error="Too many failed attempts. Wait 15 minutes, or ask the administrator to reset your password.",
            email=email,
        )
    user = db.scalar(select(AppUser).where(AppUser.email == email))
    if user is None or user.auth_method is not AuthMethod.LOCAL:
        verify_password(DUMMY_HASH, password)  # same delay as a real check
        ok = False
    else:
        ok = user.is_active and verify_password(user.password_hash, password)
    if not ok:
        throttle.record_failure(email)
        audit(db, None, "sign_in.failed", email)
        if user is not None and user.auth_method is AuthMethod.MICROSOFT:
            failed = "This account signs in with Microsoft 365. Use the Microsoft button."
        return render(request, "login.html", status_code=401, error=failed, email=email)
    throttle.clear(email)
    user.last_sign_in_at = datetime.now(UTC)
    audit(db, user, "sign_in", email, method="local")
    start_session(request, user)
    return RedirectResponse("/", status_code=303)


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request):
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
    token = await oauth.microsoft.authorize_access_token(request)
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
