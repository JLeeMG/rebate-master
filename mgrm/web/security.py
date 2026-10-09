"""Who is signed in, what they may do, and protection for forms and for sign-in."""

import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from fastapi import Depends, Request
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from mgrm.auth.roles import Permission, can
from mgrm.models import AppUser, FailedSignIn

SESSION_USER_KEY = "user_id"
SESSION_VERSION_KEY = "session_version"
SESSION_STARTED_KEY = "signed_in_at"
SESSION_CSRF_KEY = "csrf"
SESSION_ABSOLUTE_SECONDS = 12 * 60 * 60  # signed out 12 hours after signing in, however busy

# Password guessing is slowed per network address, so a stranger cannot lock someone else out.
MAX_FAILED_SIGN_INS = 5  # for one email, from one address
MAX_FAILED_FROM_ADDRESS = 20  # for any emails, from one address
THROTTLE_WINDOW = timedelta(minutes=15)
FAILURES_KEPT = timedelta(days=1)


class SignInRequired(Exception):
    """Raised when a screen needs a signed-in user; the app redirects to the sign-in page."""


class PasswordChangeRequired(Exception):
    """Raised until a user with a password someone else chose has replaced it."""


class NotPermitted(Exception):
    def __init__(self, permission: Permission) -> None:
        self.permission = permission


class BadFormToken(Exception):
    """A form was submitted without this session's token (cross-site request forgery)."""


def get_db(request: Request):
    yield from request.app.state.db_session_scope()


def client_address(request: Request) -> str:
    """The caller's network address. Behind the HTTPS proxy, uvicorn takes it from the proxy's header."""
    return request.client.host if request.client else "unknown"


def current_user_or_none(request: Request, db: Session = Depends(get_db)) -> AppUser | None:
    user_id = request.session.get(SESSION_USER_KEY)
    if user_id is None:
        return None
    user = db.get(AppUser, user_id)
    expired = time.time() - request.session.get(SESSION_STARTED_KEY, 0) > SESSION_ABSOLUTE_SECONDS
    if user is None or not user.is_active or expired or request.session.get(SESSION_VERSION_KEY) != user.session_version:
        # Deactivated, signed out elsewhere, password or role changed, or simply too old: sign in again.
        request.session.clear()
        return None
    request.state.user = user  # lets every page show who is signed in
    return user


def signed_in_user(user: AppUser | None = Depends(current_user_or_none)) -> AppUser:
    """Any signed-in user, whether or not they still owe a password change."""
    if user is None:
        raise SignInRequired
    return user


def current_user(user: AppUser = Depends(signed_in_user)) -> AppUser:
    if user.must_change_password:
        raise PasswordChangeRequired
    return user


def require(permission: Permission) -> Callable[..., AppUser]:
    def dependency(user: AppUser = Depends(current_user)) -> AppUser:
        if not can(user.role, permission):
            raise NotPermitted(permission)
        return user

    return dependency


def csrf_token(request: Request) -> str:
    token = request.session.get(SESSION_CSRF_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_CSRF_KEY] = token
    return token


async def verify_csrf(request: Request) -> None:
    form = await request.form()
    expected = request.session.get(SESSION_CSRF_KEY)
    submitted = form.get("csrf_token")
    if not expected or not isinstance(submitted, str) or not secrets.compare_digest(expected, submitted):
        raise BadFormToken


def start_session(request: Request, user: AppUser) -> None:
    request.session.clear()  # a fresh session on every sign-in
    request.session[SESSION_USER_KEY] = user.id
    request.session[SESSION_VERSION_KEY] = user.session_version
    request.session[SESSION_STARTED_KEY] = int(time.time())


def sign_in_refused(db: Session, email: str, address: str) -> bool:
    """True while this address has failed too often, for this email or for any."""
    since = datetime.now(UTC) - THROTTLE_WINDOW
    failures = select(func.count()).select_from(FailedSignIn).where(FailedSignIn.address == address, FailedSignIn.at >= since)
    if db.scalar(failures) >= MAX_FAILED_FROM_ADDRESS:
        return True
    return db.scalar(failures.where(FailedSignIn.email == email)) >= MAX_FAILED_SIGN_INS


def record_failed_sign_in(db: Session, email: str, address: str) -> None:
    db.execute(delete(FailedSignIn).where(FailedSignIn.at < datetime.now(UTC) - FAILURES_KEPT))
    db.add(FailedSignIn(email=email[:254], address=address[:45]))


def clear_failed_sign_ins(db: Session, email: str, address: str | None = None) -> None:
    """After a successful sign-in from an address, or a password reset (every address)."""
    query = delete(FailedSignIn).where(FailedSignIn.email == email)
    if address is not None:
        query = query.where(FailedSignIn.address == address)
    db.execute(query)
