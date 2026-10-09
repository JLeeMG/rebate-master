"""Creating and changing users. Every change is written to the audit log."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mgrm.auth.passwords import hash_password
from mgrm.auth.roles import Role
from mgrm.models import AppUser, AuditEvent, AuthMethod


class UserError(ValueError):
    """A user change that is not allowed, with a message fit to show on screen."""


def normalise_email(email: str) -> str:
    email = email.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise UserError(f"'{email}' is not an email address.")
    return email


def audit(session: Session, actor: AppUser | None, action: str, subject: str, **detail) -> None:
    session.add(AuditEvent(actor_id=actor.id if actor else None, action=action, subject=subject, detail=detail))


def create_user(
    session: Session,
    *,
    actor: AppUser | None,
    email: str,
    display_name: str,
    role: Role,
    auth_method: AuthMethod,
    password: str | None = None,
) -> AppUser:
    email = normalise_email(email)
    if session.scalar(select(AppUser).where(AppUser.email == email)):
        raise UserError(f"A user with email {email} already exists.")
    if not display_name.strip():
        raise UserError("A name is required.")
    if auth_method is AuthMethod.LOCAL and not password:
        raise UserError("A platform account needs a starting password.")
    try:
        password_hash = hash_password(password) if auth_method is AuthMethod.LOCAL else None
    except ValueError as exc:
        raise UserError(str(exc)) from exc
    user = AppUser(
        email=email,
        display_name=display_name.strip(),
        role=role,
        auth_method=auth_method,
        password_hash=password_hash,
        # Someone else chose the password, so the user replaces it on first sign-in.
        must_change_password=auth_method is AuthMethod.LOCAL and actor is not None,
        created_by_id=actor.id if actor else None,
    )
    session.add(user)
    session.flush()
    audit(session, actor, "user.create", email, role=role.value, auth_method=auth_method.value)
    return user


def active_admin_count(session: Session) -> int:
    return session.scalar(
        select(func.count()).select_from(AppUser).where(AppUser.role == Role.ADMIN, AppUser.is_active.is_(True))
    )


def _guard_last_admin(session: Session, user: AppUser) -> None:
    if user.role is Role.ADMIN and user.is_active and active_admin_count(session) <= 1:
        raise UserError("This is the only active administrator. Make someone else an administrator first.")


def change_role(session: Session, *, actor: AppUser, user: AppUser, role: Role) -> None:
    if role is user.role:
        return
    if role is not Role.ADMIN:
        _guard_last_admin(session, user)
    audit(session, actor, "user.change_role", user.email, old=user.role.value, new=role.value)
    user.role = role


def set_active(session: Session, *, actor: AppUser, user: AppUser, active: bool) -> None:
    if active == user.is_active:
        return
    if not active:
        _guard_last_admin(session, user)
    audit(session, actor, "user.activate" if active else "user.deactivate", user.email)
    user.is_active = active


def set_password(session: Session, *, actor: AppUser, user: AppUser, password: str) -> None:
    if user.auth_method is not AuthMethod.LOCAL:
        raise UserError("This user signs in with Microsoft 365; their password is managed by Microsoft.")
    try:
        user.password_hash = hash_password(password)
    except ValueError as exc:
        raise UserError(str(exc)) from exc
    user.must_change_password = actor.id != user.id
    audit(session, actor, "user.set_password", user.email, by_self=actor.id == user.id)
