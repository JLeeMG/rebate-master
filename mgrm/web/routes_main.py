"""The home page and the health check."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mgrm.models import AppUser, RebateAgreement, RebateRate, ReviewStatus
from mgrm.web.app import render
from mgrm.web.security import current_user, get_db

router = APIRouter()


@router.get("/healthz")
def health():
    return {"status": "ok"}


@router.get("/")
def home(request: Request, user: AppUser = Depends(current_user), db: Session = Depends(get_db)):
    pending = db.scalar(select(func.count()).select_from(RebateRate).where(RebateRate.status == ReviewStatus.PROPOSED))
    agreements = db.scalar(select(func.count()).select_from(RebateAgreement))
    return render(request, "home.html", pending=pending, agreements=agreements)
