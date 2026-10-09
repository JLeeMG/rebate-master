"""The monthly accrual: the schedule, and the journal prepared, reviewed and downloaded for NetSuite."""

from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from mgrm.auth.roles import Permission
from mgrm.auth.users import audit
from mgrm.domain.currency import format_amount
from mgrm.domain.entities import FUNCTIONAL_CURRENCY, TRADING_ENTITIES, Entity
from mgrm.models import AppUser, Customer, RebateJournal, ReviewStatus
from mgrm.rebates.accrual import build_schedule
from mgrm.rebates.journal import (
    approve_journal,
    journal_csv,
    journal_refusal,
    prepare_journal,
    reject_journal,
    total_of,
    withdraw_journal,
)
from mgrm.rebates.service import RebateError
from mgrm.web.app import render
from mgrm.web.security import get_db, require, verify_csrf

router = APIRouter(prefix="/rebates")
sales_reader = require(Permission.VIEW_SALES)
editor = require(Permission.EDIT_REBATES)
approver = require(Permission.APPROVE_REBATES)


def _month(text: str) -> date:
    try:
        year, month = (int(part) for part in text.split("-"))
        return date(year, month, 1)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Month must be YYYY-MM") from exc


def _last_month() -> date:
    first = date.today().replace(day=1)
    return date(first.year - (first.month == 1), first.month - 1 or 12, 1)


def _names(db: Session) -> dict[int, str]:
    return {u.id: u.display_name for u in db.scalars(select(AppUser))}


def _accruals_page(request: Request, db: Session, user: AppUser, entity: str, period: date, status_code: int = 200, **context):
    if entity not in TRADING_ENTITIES:
        raise HTTPException(status_code=400, detail="Unknown entity")
    schedule = build_schedule(db, entity, period)
    journals = db.scalars(select(RebateJournal).where(RebateJournal.entity_code == entity, RebateJournal.period == period)
                          .order_by(RebateJournal.id.desc())).all()
    currency = FUNCTIONAL_CURRENCY[Entity(entity)]
    customers = {c.netsuite_customer_id: c.name for c in db.scalars(select(Customer))}
    return render(request, "accruals.html", status_code=status_code, schedule=schedule, journals=journals, entity=entity,
                  period=period, entities=TRADING_ENTITIES, money=lambda v: format_amount(v, currency, cents=True),
                  customers=customers, names=_names(db), **context)


@router.get("/accruals")
def accruals(request: Request, entity: str = "MGAU", month: str = "", user: AppUser = Depends(sales_reader),
             db: Session = Depends(get_db)):
    return _accruals_page(request, db, user, entity, _month(month) if month else _last_month())


@router.post("/accruals/prepare", dependencies=[Depends(verify_csrf)])
def prepare(request: Request, entity: str = Form(...), month: str = Form(...), user: AppUser = Depends(editor),
            db: Session = Depends(get_db)):
    period = _month(month)
    try:
        with db.begin_nested():
            journal = prepare_journal(db, actor=user, entity_code=entity, period=period)
    except RebateError as exc:
        return _accruals_page(request, db, user, entity, period, 400, error=str(exc))
    return RedirectResponse(f"/rebates/journals/{journal.id}", status_code=303)


def _journal(db: Session, journal_id: int) -> RebateJournal:
    journal = db.get(RebateJournal, journal_id)
    if journal is None:
        raise HTTPException(status_code=404, detail="No such journal")
    return journal


def _journal_page(request: Request, db: Session, user: AppUser, journal: RebateJournal, status_code: int = 200, **context):
    currency = FUNCTIONAL_CURRENCY[Entity(journal.entity_code)]
    debit, credit = total_of(journal.lines)
    return render(request, "journal.html", status_code=status_code, journal=journal, names=_names(db),
                  money=lambda v: format_amount(v, currency, cents=True), debit=debit, credit=credit,
                  refusal=journal_refusal(user, journal), **context)


@router.get("/journals")
def journals(request: Request, user: AppUser = Depends(sales_reader), db: Session = Depends(get_db)):
    rows = db.scalars(select(RebateJournal).order_by(RebateJournal.period.desc(), RebateJournal.entity_code,
                                                     RebateJournal.id.desc())).all()
    return render(request, "journals.html", journals=rows, names=_names(db))


@router.get("/journals/{journal_id}")
def journal_detail(request: Request, journal_id: int, user: AppUser = Depends(sales_reader), db: Session = Depends(get_db)):
    return _journal_page(request, db, user, _journal(db, journal_id))


@router.post("/journals/{journal_id}/decision", dependencies=[Depends(verify_csrf)])
def decide(request: Request, journal_id: int, decision: str = Form(...), note: str = Form(""),
           user: AppUser = Depends(approver), db: Session = Depends(get_db)):
    journal = _journal(db, journal_id)
    try:
        with db.begin_nested():
            (approve_journal if decision == "approve" else reject_journal)(db, actor=user, journal=journal, note=note)
    except RebateError as exc:
        return _journal_page(request, db, user, journal, 400, error=str(exc))
    return RedirectResponse(f"/rebates/journals/{journal.id}", status_code=303)


@router.post("/journals/{journal_id}/withdraw", dependencies=[Depends(verify_csrf)])
def withdraw(request: Request, journal_id: int, user: AppUser = Depends(editor), db: Session = Depends(get_db)):
    journal = _journal(db, journal_id)
    try:
        with db.begin_nested():
            withdraw_journal(db, actor=user, journal=journal)
    except RebateError as exc:
        return _journal_page(request, db, user, journal, 400, error=str(exc))
    return RedirectResponse(f"/rebates/journals/{journal.id}", status_code=303)


@router.get("/journals/{journal_id}/netsuite.csv")
def download(journal_id: int, user: AppUser = Depends(sales_reader), db: Session = Depends(get_db)):
    """The NetSuite import file, only once approved."""
    journal = _journal(db, journal_id)
    if journal.status is not ReviewStatus.APPROVED:
        raise HTTPException(status_code=409, detail="Only an approved journal can be downloaded for NetSuite")
    audit(db, user, "journal.download", journal.external_id, journal_id=journal.id)
    return Response(journal_csv(db, journal), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{journal.external_id}.csv"'})
