"""Who is signed in, what they may do, and protection for forms."""

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from mgrm.auth.roles import Permission, can
from mgrm.models import AppUser

SESSION_USER_KEY = "user_id"
SESSION_CSRF_KEY = "csrf"

MAX_FAILED_SIGN_INS = 5
LOCKOUT_SECONDS = 15 * 60


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


def current_user_or_none(request: Request, db: Session = Depends(get_db)) -> AppUser | None:
    user_id = request.session.get(SESSION_USER_KEY)
    if user_id is None:
        return None
    user = db.get(AppUser, user_id)
    if user is None or not user.is_active:
        request.session.clear()  # deactivated users are signed out on their next click
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


@dataclass
class SignInThrottle:
    """Locks an email address for a while after repeated failed sign-ins."""

    failures: dict[str, list[float]] = field(default_factory=dict)

    def locked(self, email: str) -> bool:
        recent = [t for t in self.failures.get(email, []) if time.monotonic() - t < LOCKOUT_SECONDS]
        self.failures[email] = recent
        return len(recent) >= MAX_FAILED_SIGN_INS

    def record_failure(self, email: str) -> None:
        self.failures.setdefault(email, []).append(time.monotonic())

    def clear(self, email: str) -> None:
        self.failures.pop(email, None)
