"""User administration and the audit log. Administrator only."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from mgrm.auth.roles import Permission, Role
from mgrm.api.feed import new_token
from mgrm.auth.users import UserError, audit, change_role, create_user, set_active, set_password
from mgrm.models import ApiToken, AppUser, AuditEvent, AuthMethod
from mgrm.web.app import render
from mgrm.web.security import get_db, require, verify_csrf

router = APIRouter(prefix="/admin")
admin = require(Permission.MANAGE_USERS)

AUDIT_PAGE_SIZE = 200


def _users_page(request: Request, db: Session, status_code: int = 200, **context):
    users = db.scalars(select(AppUser).order_by(AppUser.is_active.desc(), AppUser.display_name)).all()
    return render(
        request, "users.html", status_code=status_code, users=users, roles=list(Role), methods=list(AuthMethod), **context
    )


def _target(db: Session, user_id: int) -> AppUser:
    user = db.get(AppUser, user_id)
    if user is None:
        raise UserError("That user no longer exists.")
    return user


@router.get("/users")
def users(request: Request, actor: AppUser = Depends(admin), db: Session = Depends(get_db)):
    return _users_page(request, db)


@router.post("/users", dependencies=[Depends(verify_csrf)])
def add_user(
    request: Request,
    email: str = Form(...),
    display_name: str = Form(...),
    role: Role = Form(...),
    auth_method: AuthMethod = Form(...),
    password: str = Form(""),
    actor: AppUser = Depends(admin),
    db: Session = Depends(get_db),
):
    try:
        with db.begin_nested():
            user = create_user(
                db,
                actor=actor,
                email=email,
                display_name=display_name,
                role=role,
                auth_method=auth_method,
                password=password or None,
            )
    except UserError as exc:
        return _users_page(request, db, status_code=400, error=str(exc))
    return _users_page(request, db, notice=f"{user.display_name} added as {user.role.title}.")


@router.post("/users/{user_id}/role", dependencies=[Depends(verify_csrf)])
def update_role(
    request: Request,
    user_id: int,
    role: Role = Form(...),
    actor: AppUser = Depends(admin),
    db: Session = Depends(get_db),
):
    try:
        change_role(db, actor=actor, user=_target(db, user_id), role=role)
    except UserError as exc:
        return _users_page(request, db, status_code=400, error=str(exc))
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/active", dependencies=[Depends(verify_csrf)])
def update_active(
    request: Request,
    user_id: int,
    active: bool = Form(...),
    actor: AppUser = Depends(admin),
    db: Session = Depends(get_db),
):
    try:
        set_active(db, actor=actor, user=_target(db, user_id), active=active)
    except UserError as exc:
        return _users_page(request, db, status_code=400, error=str(exc))
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/password", dependencies=[Depends(verify_csrf)])
def reset_password(
    request: Request,
    user_id: int,
    password: str = Form(...),
    actor: AppUser = Depends(admin),
    db: Session = Depends(get_db),
):
    try:
        target = _target(db, user_id)
        set_password(db, actor=actor, user=target, password=password)
    except UserError as exc:
        return _users_page(request, db, status_code=400, error=str(exc))
    return _users_page(request, db, notice=f"Password reset for {target.display_name}. They will choose a new one when they next sign in.")


@router.get("/audit")
def audit_log(request: Request, actor: AppUser = Depends(admin), db: Session = Depends(get_db)):
    events = db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(AUDIT_PAGE_SIZE)).all()
    names = {u.id: u.display_name for u in db.scalars(select(AppUser))}
    return render(request, "audit.html", events=events, names=names)


# ---------------------------------------------------------------- feed tokens


def _tokens_page(request: Request, db: Session, **context):
    tokens = db.scalars(select(ApiToken).order_by(ApiToken.id.desc())).all()
    names = {u.id: u.display_name for u in db.scalars(select(AppUser))}
    return render(request, "feed_tokens.html", tokens=tokens, names=names, **context)


@router.get("/feed-tokens")
def feed_tokens(request: Request, actor: AppUser = Depends(admin), db: Session = Depends(get_db)):
    return _tokens_page(request, db)


@router.post("/feed-tokens", dependencies=[Depends(verify_csrf)])
def create_feed_token(request: Request, name: str = Form(...), actor: AppUser = Depends(admin), db: Session = Depends(get_db)):
    token, token_hash = new_token()
    record = ApiToken(name=name.strip() or "unnamed", token_hash=token_hash, created_by_id=actor.id)
    db.add(record)
    db.flush()
    audit(db, actor, "feed.token_create", record.name, token_id=record.id)
    return _tokens_page(request, db, new_token=token, new_token_name=record.name)


@router.post("/feed-tokens/{token_id}/revoke", dependencies=[Depends(verify_csrf)])
def revoke_feed_token(token_id: int, actor: AppUser = Depends(admin), db: Session = Depends(get_db)):
    record = db.get(ApiToken, token_id)
    if record is not None and record.revoked_at is None:
        record.revoked_at = datetime.now(UTC)
        audit(db, actor, "feed.token_revoke", record.name, token_id=record.id)
    return RedirectResponse("/admin/feed-tokens", status_code=303)
