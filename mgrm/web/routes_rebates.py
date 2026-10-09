"""The rebate master screens (spec §4.5): input, evidence, review, change log and extraction.

Everyone signed in sees agreements, rates, rate cards and the change log, and
can open evidence. Sales amounts recorded by the legacy workbooks are shown
only to roles with VIEW_SALES.
"""

from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from mgrm.auth.roles import Permission, Role, can
from mgrm.auth.users import audit
from mgrm.domain.currency import format_amount
from mgrm.domain.entities import FUNCTIONAL_CURRENCY, TRADING_ENTITIES, Entity
from mgrm.models import (
    AccrualMode,
    AppUser,
    AuditEvent,
    CustomerGroup,
    EvidenceFile,
    RateType,
    RebateAgreement,
    RebateApproverScope,
    RebateBasis,
    RebateRate,
    RebateWorkbookMonth,
    ReviewStatus,
)
from mgrm.rebates.service import (
    RebateError,
    Upload,
    agreements_with_rates,
    approval_refusal,
    approve,
    attach_evidence,
    expiry_and_renewal,
    propose_rate,
    reject,
    update_agreement,
)
from mgrm.web.app import render
from mgrm.web.security import get_db, require, verify_csrf

router = APIRouter(prefix="/rebates")
reader = require(Permission.VIEW)
editor = require(Permission.EDIT_REBATES)
approver = require(Permission.APPROVE_REBATES)
administrator = require(Permission.MANAGE_USERS)

HISTORY_MONTHS_SHOWN = 24
CHANGE_LOG_SHOWN = 500
PERCENT = Decimal("100")
RATE_SHOWN = Decimal("0.00001")  # an applied rate to a thousandth of a percent
CHANGE_ACTIONS = ("rebate.propose", "rebate.approve", "rebate.reject", "rebate.agreement_update", "rebate.evidence")


def pct(rate: Decimal | None) -> str:
    if rate is None:
        return ""
    return f"{(Decimal(str(rate)) * PERCENT).normalize():f}%"


def _parse_date(text: str) -> date | None:
    text = text.strip()
    return date.fromisoformat(text) if text else None


def _groups(db: Session) -> list[CustomerGroup]:
    return db.scalars(select(CustomerGroup).order_by(CustomerGroup.name)).all()


def _names(db: Session) -> dict[int, str]:
    return {u.id: u.display_name for u in db.scalars(select(AppUser))}


async def _uploads(files: list[UploadFile] | None, description: str = "") -> list[Upload]:
    uploads = []
    for file in files or []:
        if file is not None and file.filename:
            uploads.append(Upload(file.filename, await file.read(), description))
    return uploads


def _evidence_by_rate(db: Session, agreement_ids) -> tuple[dict[int, list[EvidenceFile]], dict[int, list[EvidenceFile]]]:
    """(evidence keyed by rate id, evidence keyed by agreement id for agreement-level files). Content not loaded."""
    rows = db.execute(
        select(EvidenceFile.id, EvidenceFile.agreement_id, EvidenceFile.rate_id, EvidenceFile.file_name,
               EvidenceFile.size_bytes, EvidenceFile.sha256, EvidenceFile.uploaded_by_id, EvidenceFile.uploaded_at)
        .where(EvidenceFile.agreement_id.in_(list(agreement_ids)))
    ).all()
    by_rate: dict[int, list] = {}
    by_agreement: dict[int, list] = {}
    for row in rows:
        (by_rate.setdefault(row.rate_id, []) if row.rate_id else by_agreement.setdefault(row.agreement_id, [])).append(row)
    return by_rate, by_agreement


# ---------------------------------------------------------------- the list


@router.get("")
def agreements(
    request: Request,
    entity: str = "",
    group: str = "",
    search: str = "",
    show: str = "active",
    user: AppUser = Depends(reader),
    db: Session = Depends(get_db),
):
    today = date.today()
    rows = agreements_with_rates(db, today, entity_code=entity or None, customer_group_code=group or None)
    if search.strip():
        needle = search.strip().lower()
        rows = [r for r in rows if needle in f"{r.agreement.customer_label} {r.agreement.product_scope} {r.agreement.brand_code or ''} {r.agreement.code}".lower()]
    if show == "active":
        rows = [r for r in rows if r.rate is not None or r.pending]
    elif show == "pending":
        rows = [r for r in rows if r.pending]
    elif show == "ungrouped":
        rows = [r for r in rows if r.agreement.customer_group_code is None]
    any_pending = db.scalar(select(RebateRate.id).where(RebateRate.status == ReviewStatus.PROPOSED).limit(1)) is not None
    return render(
        request, "rebates.html", rows=rows, entities=TRADING_ENTITIES, groups=_groups(db), entity=entity, group=group,
        search=search, show=show, pct=pct, today=today, any_pending=any_pending,
    )


# ---------------------------------------------------------------- one agreement


def _agreement_page(request: Request, db: Session, agreement: RebateAgreement, user: AppUser, status_code=200, **context):
    rates = db.scalars(select(RebateRate).where(RebateRate.agreement_id == agreement.id)
                       .order_by(RebateRate.effective_from, RebateRate.id)).all()
    history = db.scalars(select(RebateWorkbookMonth).where(RebateWorkbookMonth.agreement_id == agreement.id)
                         .order_by(RebateWorkbookMonth.period.desc()).limit(HISTORY_MONTHS_SHOWN)).all()
    events = db.scalars(select(AuditEvent).where(AuditEvent.subject == agreement.code).order_by(AuditEvent.id.desc())).all()
    by_rate, by_agreement = _evidence_by_rate(db, [agreement.id])
    currency = FUNCTIONAL_CURRENCY[Entity(agreement.entity_code)]
    return render(
        request, "rebate_agreement.html", status_code=status_code, agreement=agreement, rates=rates,
        history=history, events=events, names=_names(db), pct=pct, groups=_groups(db),
        rate_types=list(RateType), bases=list(RebateBasis), modes=list(AccrualMode),
        evidence=by_rate, agreement_evidence=by_agreement.get(agreement.id, []),
        show_money=can(user.role, Permission.VIEW_SALES),
        money=lambda v: format_amount(v, currency),
        applied=lambda h: pct((h.rebate_due / h.sales).quantize(RATE_SHOWN)) if h.sales else "",
        **context,
    )


def _get(db: Session, agreement_id: int) -> RebateAgreement:
    agreement = db.get(RebateAgreement, agreement_id)
    if agreement is None:
        raise HTTPException(status_code=404, detail="No such agreement")
    return agreement


@router.get("/agreement/{agreement_id}")
def agreement_detail(request: Request, agreement_id: int, user: AppUser = Depends(reader), db: Session = Depends(get_db)):
    return _agreement_page(request, db, _get(db, agreement_id), user)


@router.post("/agreement/{agreement_id}/rates", dependencies=[Depends(verify_csrf)])
async def propose(
    request: Request,
    agreement_id: int,
    rate_percent: str = Form(...),
    effective_from: str = Form(...),
    effective_to: str = Form(""),
    source_reference: str = Form(""),
    reason: str = Form(""),
    evidence: list[UploadFile] | None = File(None),
    user: AppUser = Depends(editor),
    db: Session = Depends(get_db),
):
    agreement = _get(db, agreement_id)
    try:
        rate = Decimal(rate_percent.strip().rstrip("%")) / PERCENT
        start, end = _parse_date(effective_from), _parse_date(effective_to)
        if start is None:
            raise RebateError("Give the date the rate starts.")
        with db.begin_nested():
            propose_rate(db, actor=user, agreement=agreement, rate=rate, effective_from=start, effective_to=end,
                         source_reference=source_reference, reason=reason, evidence=await _uploads(evidence))
    except (InvalidOperation, ValueError) as exc:
        message = str(exc) if isinstance(exc, RebateError) else "The rate must be a number, such as 25.3, and dates must be valid."
        return _agreement_page(request, db, agreement, user, 400, error=message)
    return _agreement_page(request, db, agreement, user, notice="Rate change proposed with its evidence. It takes effect once someone else approves it.")


@router.post("/agreement/{agreement_id}/evidence", dependencies=[Depends(verify_csrf)])
async def add_evidence(
    request: Request,
    agreement_id: int,
    rate_id: int | None = Form(None),
    description: str = Form(""),
    evidence: list[UploadFile] | None = File(None),
    user: AppUser = Depends(editor),
    db: Session = Depends(get_db),
):
    """More evidence for a proposed rate (or for the agreement in general). Files are never removed."""
    agreement = _get(db, agreement_id)
    rate = db.get(RebateRate, rate_id) if rate_id else None
    try:
        if rate is not None and (rate.agreement_id != agreement.id or rate.status is not ReviewStatus.PROPOSED):
            raise RebateError("Evidence can be added only to a rate that is still awaiting review.")
        uploads = await _uploads(evidence, description)
        if not uploads:
            raise RebateError("Choose a file to attach.")
        with db.begin_nested():
            attach_evidence(db, actor=user, agreement=agreement, uploads=uploads, rate=rate)
    except RebateError as exc:
        return _agreement_page(request, db, agreement, user, 400, error=str(exc))
    return _agreement_page(request, db, agreement, user, notice="Evidence attached.")


@router.post("/agreement/{agreement_id}", dependencies=[Depends(verify_csrf)])
async def change_agreement(
    request: Request,
    agreement_id: int,
    customer_group_code: str = Form(""),
    brand_code: str = Form(""),
    product_scope: str = Form(...),
    rate_type: RateType = Form(...),
    basis: RebateBasis = Form(...),
    accrual_mode: AccrualMode = Form(...),
    agreed_by: str = Form(""),
    reason: str = Form(""),
    evidence: list[UploadFile] | None = File(None),
    user: AppUser = Depends(editor),
    db: Session = Depends(get_db),
):
    agreement = _get(db, agreement_id)
    changes = {
        "customer_group_code": customer_group_code or None, "brand_code": brand_code.strip().upper() or None,
        "product_scope": product_scope.strip(), "rate_type": rate_type, "basis": basis,
        "accrual_mode": accrual_mode, "agreed_by": agreed_by.strip(),
    }
    try:
        with db.begin_nested():
            changed = update_agreement(db, actor=user, agreement=agreement, changes=changes, reason=reason,
                                       evidence=await _uploads(evidence))
    except RebateError as exc:
        db.refresh(agreement)
        return _agreement_page(request, db, agreement, user, 400, error=str(exc))
    return _agreement_page(request, db, agreement, user,
                           notice="Agreement updated." if changed else "Nothing was different, so nothing changed.")


@router.get("/evidence/{evidence_id}")
def download_evidence(evidence_id: int, user: AppUser = Depends(reader), db: Session = Depends(get_db)):
    evidence = db.get(EvidenceFile, evidence_id)
    if evidence is None:
        raise HTTPException(status_code=404, detail="No such evidence file")
    audit(db, user, "rebate.evidence_opened", evidence.file_name, evidence_id=evidence.id)
    return Response(
        content=evidence.content,
        media_type=evidence.content_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(evidence.file_name)}",
                 "X-Content-SHA256": evidence.sha256},
    )


# ---------------------------------------------------------------- review


def _review_page(request: Request, db: Session, user: AppUser, status_code=200, **context):
    proposed = db.scalars(select(RebateRate).where(RebateRate.status == ReviewStatus.PROPOSED)
                          .order_by(RebateRate.agreement_id, RebateRate.effective_from)).all()
    agreements = {a.id: a for a in db.scalars(select(RebateAgreement).where(RebateAgreement.id.in_({r.agreement_id for r in proposed})))}
    by_rate, _ = _evidence_by_rate(db, agreements.keys())
    items = [(r, agreements[r.agreement_id], approval_refusal(db, user, r, agreements[r.agreement_id])) for r in proposed]
    return render(request, "rebate_review.html", status_code=status_code, items=items, names=_names(db), pct=pct,
                  evidence=by_rate, **context)


@router.get("/review")
def review(request: Request, user: AppUser = Depends(approver), db: Session = Depends(get_db)):
    return _review_page(request, db, user)


@router.post("/rates/{rate_id}/decision", dependencies=[Depends(verify_csrf)])
def decide(
    request: Request,
    rate_id: int,
    decision: str = Form(...),
    note: str = Form(""),
    user: AppUser = Depends(approver),
    db: Session = Depends(get_db),
):
    rate = db.get(RebateRate, rate_id)
    try:
        if rate is None:
            raise RebateError("That rate no longer exists.")
        with db.begin_nested():
            (approve if decision == "approve" else reject)(db, actor=user, rate=rate, note=note)
    except RebateError as exc:
        return _review_page(request, db, user, 400, error=str(exc))
    return RedirectResponse("/rebates/review", status_code=303)


@router.post("/review/approve-selected", dependencies=[Depends(verify_csrf)])
async def approve_selected(request: Request, user: AppUser = Depends(approver), db: Session = Depends(get_db)):
    form = await request.form()
    if form.get("checked") != "yes":
        return _review_page(request, db, user, 400, error="Tick the box to confirm you have checked each selected rate against its source.")
    approved, problems = 0, []
    for value in form.getlist("rate_id"):
        rate = db.get(RebateRate, int(value))
        try:
            with db.begin_nested():
                approve(db, actor=user, rate=rate, note="Approved in a batch after checking against the source")
            approved += 1
        except RebateError as exc:
            problems.append(f"Rate {value}: {exc}")
    return _review_page(request, db, user, notice=f"{approved} rate(s) approved.", error=" ".join(problems) if problems else None)


# ---------------------------------------------------------------- the change log


@router.get("/change-log")
def change_log(
    request: Request,
    entity: str = "",
    search: str = "",
    person: str = "",
    since: str = "",
    user: AppUser = Depends(reader),
    db: Session = Depends(get_db),
):
    """Every rate change and agreement change: who, when, what, why, the evidence, and the decision."""
    events = db.scalars(select(AuditEvent).where(AuditEvent.action.in_(CHANGE_ACTIONS)).order_by(AuditEvent.id.desc())).all()
    agreements = {a.code: a for a in db.scalars(select(RebateAgreement))}
    names = _names(db)
    start = _parse_date(since)
    rows = []
    for event in events:
        agreement = agreements.get(event.subject)
        if agreement is None:
            continue
        if entity and agreement.entity_code != entity:
            continue
        if search.strip() and search.strip().lower() not in f"{agreement.customer_label} {agreement.product_scope} {agreement.brand_code or ''}".lower():
            continue
        if person and str(event.actor_id) != person:
            continue
        if start and event.occurred_at.date() < start:
            continue
        rows.append((event, agreement))
        if len(rows) >= CHANGE_LOG_SHOWN:
            break
    rate_ids = {e.detail.get("rate_id") for e, _ in rows if e.detail.get("rate_id")}
    evidence: dict[int, list] = {}
    for row in db.execute(select(EvidenceFile.id, EvidenceFile.rate_id, EvidenceFile.file_name)
                          .where(EvidenceFile.rate_id.in_(rate_ids))).all():
        evidence.setdefault(row.rate_id, []).append(row)
    return render(request, "change_log.html", rows=rows, names=names, pct=pct, evidence=evidence,
                  entities=TRADING_ENTITIES, entity=entity, search=search, person=person, since=since,
                  people=sorted(names.items(), key=lambda p: p[1]), limit=CHANGE_LOG_SHOWN)


# ---------------------------------------------------------------- extraction


@router.get("/expiry")
def expiry(request: Request, user: AppUser = Depends(reader), db: Session = Depends(get_db)):
    ending, open_ended = expiry_and_renewal(db, date.today())
    return render(request, "rebate_expiry.html", ending=ending, open_ended=open_ended, pct=pct)


@router.get("/rate-card")
def rate_card(
    request: Request,
    entity: str = "MGAU",
    group: str = "",
    brand: str = "",
    as_of: str = "",
    user: AppUser = Depends(reader),
    db: Session = Depends(get_db),
):
    on = _parse_date(as_of) or date.today()
    rows = [r for r in agreements_with_rates(db, on, entity_code=entity or None, customer_group_code=group or None) if r.rate]
    if brand.strip():
        needle = brand.strip().lower()
        rows = [r for r in rows if needle in f"{r.agreement.brand_code or ''} {r.agreement.product_scope}".lower()]
    return render(
        request, "rebate_card.html", rows=rows, entities=TRADING_ENTITIES, groups=_groups(db), entity=entity,
        group=group, brand=brand, on=on, pct=pct, names=_names(db),
    )


# ---------------------------------------------------------------- brand approvers


@router.get("/approvers")
def approvers(request: Request, user: AppUser = Depends(administrator), db: Session = Depends(get_db)):
    scopes = db.scalars(select(RebateApproverScope).order_by(RebateApproverScope.brand_code)).all()
    candidates = db.scalars(select(AppUser).where(
        AppUser.is_active.is_(True), or_(AppUser.role == Role.BRAND_APPROVER, AppUser.role == Role.REBATE_REVIEWER, AppUser.role == Role.ADMIN)
    ).order_by(AppUser.display_name)).all()
    brands = sorted({a.brand_code for a in db.scalars(select(RebateAgreement)) if a.brand_code and a.brand_code != "ALL"})
    return render(request, "rebate_approvers.html", scopes=scopes, candidates=candidates, brands=brands, names=_names(db))


@router.post("/approvers", dependencies=[Depends(verify_csrf)])
def add_approver(
    brand_code: str = Form(...),
    user_id: int = Form(...),
    actor: AppUser = Depends(administrator),
    db: Session = Depends(get_db),
):
    code = brand_code.strip().upper()
    exists = db.scalar(select(RebateApproverScope).where(RebateApproverScope.user_id == user_id, RebateApproverScope.brand_code == code))
    if code and not exists and db.get(AppUser, user_id):
        db.add(RebateApproverScope(user_id=user_id, brand_code=code))
        audit(db, actor, "rebate.approver_add", code, user_id=user_id)
    return RedirectResponse("/rebates/approvers", status_code=303)


@router.post("/approvers/{scope_id}/remove", dependencies=[Depends(verify_csrf)])
def remove_approver(scope_id: int, actor: AppUser = Depends(administrator), db: Session = Depends(get_db)):
    scope = db.get(RebateApproverScope, scope_id)
    if scope is not None:
        audit(db, actor, "rebate.approver_remove", scope.brand_code, user_id=scope.user_id)
        db.delete(scope)
    return RedirectResponse("/rebates/approvers", status_code=303)
