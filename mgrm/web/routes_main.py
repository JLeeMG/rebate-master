"""The home page, the help pages and the health check."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mgrm.auth.roles import ROLE_PERMISSIONS, Role
from mgrm.models import AppUser, RebateAgreement, RebateChangeRequest, RebateRate, ReviewStatus
from mgrm.web.app import render
from mgrm.web.security import current_user, get_db

router = APIRouter()


@router.get("/healthz")
def health():
    return {"status": "ok"}


HELP_ROLES = [Role.REBATE_MAINTAINER, Role.REBATE_EDITOR, Role.REBATE_REVIEWER, Role.BRAND_APPROVER, Role.VIEWER, Role.ADMIN]


@router.get("/help")
def help_page(request: Request, role: str = "", user: AppUser = Depends(current_user)):
    """How to use the platform, written for each role; opens on the reader's own."""
    shown = Role(role) if role in {r.value for r in Role} else user.role
    return render(request, "help.html", shown=shown, roles=HELP_ROLES, permissions=ROLE_PERMISSIONS[shown])


@router.get("/")
def home(request: Request, user: AppUser = Depends(current_user), db: Session = Depends(get_db)):
    pending = sum(
        db.scalar(select(func.count()).select_from(model).where(model.status == ReviewStatus.PROPOSED))
        for model in (RebateRate, RebateChangeRequest)
    )
    agreements = db.scalar(select(func.count()).select_from(RebateAgreement))
    return render(request, "home.html", pending=pending, agreements=agreements)
