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
    RebateChangeRequest,
    RebateRate,
    RebateWorkbookMonth,
    ReviewStatus,
)
from mgrm.rebates.service import (
    MAX_EVIDENCE_BYTES,
    RebateError,
    Upload,
    agreements_with_rates,
    approve,
    approve_change,
    attach_evidence,
    expiry_and_renewal,
    propose_agreement_change,
    propose_end,
    propose_new_agreement,
    propose_rate,
    reject,
    reject_change,
    review_refusal,
    withdraw,
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
CHANGE_ACTIONS = (
    "rebate.agreement_create", "rebate.propose", "rebate.approve", "rebate.reject", "rebate.change_propose",
    "rebate.change_approve", "rebate.change_reject", "rebate.withdraw", "rebate.agreement_update", "rebate.evidence",
)


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
            # Read one byte past the limit at most: enough for evidence_type to refuse an oversized file.
            uploads.append(Upload(file.filename, await file.read(MAX_EVIDENCE_BYTES + 1), description))
    return uploads


def _evidence(db: Session, agreement_ids) -> tuple[dict[int, list], dict[int, list], dict[int, list]]:
    """Evidence (content not loaded) keyed by rate id, by change request id, and by agreement for general files."""
    rows = db.execute(
        select(EvidenceFile.id, EvidenceFile.agreement_id, EvidenceFile.rate_id, EvidenceFile.change_request_id,
               EvidenceFile.file_name, EvidenceFile.size_bytes, EvidenceFile.sha256, EvidenceFile.uploaded_by_id,
               EvidenceFile.uploaded_at)
        .where(EvidenceFile.agreement_id.in_(list(agreement_ids)))
    ).all()
    by_rate: dict[int, list] = {}
    by_change: dict[int, list] = {}
    by_agreement: dict[int, list] = {}
    for row in rows:
        if row.rate_id:
            by_rate.setdefault(row.rate_id, []).append(row)
        elif row.change_request_id:
            by_change.setdefault(row.change_request_id, []).append(row)
        else:
            by_agreement.setdefault(row.agreement_id, []).append(row)
    return by_rate, by_change, by_agreement


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
    changes = db.scalars(select(RebateChangeRequest).where(RebateChangeRequest.agreement_id == agreement.id)
                         .order_by(RebateChangeRequest.id.desc())).all()
    by_rate, by_change, by_agreement = _evidence(db, [agreement.id])
    currency = FUNCTIONAL_CURRENCY[Entity(agreement.entity_code)]
    return render(
        request, "rebate_agreement.html", status_code=status_code, agreement=agreement, rates=rates,
        history=history, events=events, names=_names(db), pct=pct, groups=_groups(db),
        rate_types=list(RateType), bases=list(RebateBasis), modes=list(AccrualMode),
        evidence=by_rate, change_evidence=by_change, agreement_evidence=by_agreement.get(agreement.id, []),
        changes=changes, today=date.today(),
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
        if rate is not None and rate.entered_by_id != user.id:
            raise RebateError("Only the person who proposed this rate can add evidence to it. "
                              "If you are reviewing it, say what is missing in your review note.")
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
            proposed = propose_agreement_change(db, actor=user, agreement=agreement, changes=changes, reason=reason,
                                                evidence=await _uploads(evidence))
    except RebateError as exc:
        return _agreement_page(request, db, agreement, user, 400, error=str(exc))
    return _agreement_page(request, db, agreement, user,
                           notice="Change proposed. It takes effect once someone else approves it." if proposed
                           else "Nothing was different, so nothing was proposed.")


@router.post("/agreement/{agreement_id}/end", dependencies=[Depends(verify_csrf)])
async def end_agreement(
    request: Request,
    agreement_id: int,
    effective_to: str = Form(...),
    reason: str = Form(""),
    source_reference: str = Form(""),
    evidence: list[UploadFile] | None = File(None),
    user: AppUser = Depends(editor),
    db: Session = Depends(get_db),
):
    agreement = _get(db, agreement_id)
    try:
        end = _parse_date(effective_to)
        if end is None:
            raise RebateError("Give the last day the agreement applies.")
        with db.begin_nested():
            propose_end(db, actor=user, agreement=agreement, effective_to=end, reason=reason,
                        source_reference=source_reference, evidence=await _uploads(evidence))
    except (ValueError, RebateError) as exc:
        message = str(exc) if isinstance(exc, RebateError) else "The date is not valid."
        return _agreement_page(request, db, agreement, user, 400, error=message)
    return _agreement_page(request, db, agreement, user, notice="Ending proposed. It takes effect once someone else approves it.")


@router.post("/{kind}/{item_id}/withdraw", dependencies=[Depends(verify_csrf)])
def withdraw_own(request: Request, kind: str, item_id: int, note: str = Form(""), user: AppUser = Depends(editor),
                 db: Session = Depends(get_db)):
    model = {"rates": RebateRate, "changes": RebateChangeRequest}.get(kind)
    item = db.get(model, item_id) if model else None
    if item is None:
        raise HTTPException(status_code=404, detail="Nothing to withdraw")
    agreement = _get(db, item.agreement_id)
    try:
        with db.begin_nested():
            withdraw(db, actor=user, item=item, note=note)
    except RebateError as exc:
        return _agreement_page(request, db, agreement, user, 400, error=str(exc))
    return _agreement_page(request, db, agreement, user, notice="Withdrawn. It never took effect, and the record of it stays.")


# ---------------------------------------------------------------- a new agreement


@router.get("/new")
def new_agreement_page(request: Request, user: AppUser = Depends(editor), db: Session = Depends(get_db)):
    return render(request, "rebate_new.html", groups=_groups(db), entities=TRADING_ENTITIES, rate_types=list(RateType),
                  bases=list(RebateBasis), modes=list(AccrualMode), form={})


@router.post("/new", dependencies=[Depends(verify_csrf)])
async def create_agreement(
    request: Request,
    entity: str = Form(...),
    customer_label: str = Form(""),
    customer_group_code: str = Form(""),
    brand_code: str = Form(""),
    product_scope: str = Form(""),
    rate_type: RateType = Form(...),
    basis: RebateBasis = Form(...),
    accrual_mode: AccrualMode = Form(...),
    rate_percent: str = Form(...),
    effective_from: str = Form(...),
    effective_to: str = Form(""),
    source_reference: str = Form(""),
    reason: str = Form(""),
    evidence: list[UploadFile] | None = File(None),
    user: AppUser = Depends(editor),
    db: Session = Depends(get_db),
):
    form = dict(entity=entity, customer_label=customer_label, customer_group_code=customer_group_code, brand_code=brand_code,
                product_scope=product_scope, rate_percent=rate_percent, effective_from=effective_from,
                effective_to=effective_to, source_reference=source_reference, reason=reason)
    try:
        if entity not in {e.value for e in TRADING_ENTITIES}:
            raise RebateError("Choose MGAU or MGNZ.")
        start = _parse_date(effective_from)
        if start is None:
            raise RebateError("Give the date the rate starts.")
        agreement = propose_new_agreement(
            db, actor=user, entity=entity, customer_label=customer_label, customer_group_code=customer_group_code or None,
            brand_code=brand_code, product_scope=product_scope, rate_type=rate_type, basis=basis, accrual_mode=accrual_mode,
            rate=Decimal(rate_percent.strip().rstrip("%")) / PERCENT, effective_from=start, effective_to=_parse_date(effective_to),
            source_reference=source_reference, reason=reason, evidence=await _uploads(evidence),
        )
    except (InvalidOperation, ValueError) as exc:
        message = str(exc) if isinstance(exc, RebateError) else "The rate must be a number, such as 12.5, and dates must be valid."
        return render(request, "rebate_new.html", status_code=400, error=message, groups=_groups(db),
                      entities=TRADING_ENTITIES, rate_types=list(RateType), bases=list(RebateBasis), modes=list(AccrualMode),
                      form=form)
    return _agreement_page(request, db, agreement, user,
                           notice="New agreement proposed with its first rate. It takes effect once someone else approves it.")


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
    changes = db.scalars(select(RebateChangeRequest).where(RebateChangeRequest.status == ReviewStatus.PROPOSED)
                         .order_by(RebateChangeRequest.id)).all()
    ids = {r.agreement_id for r in proposed} | {c.agreement_id for c in changes}
    agreements = {a.id: a for a in db.scalars(select(RebateAgreement).where(RebateAgreement.id.in_(ids)))}
    approved_once = set(db.scalars(select(RebateRate.agreement_id).where(RebateRate.status == ReviewStatus.APPROVED)))
    by_rate, by_change, _ = _evidence(db, agreements.keys())
    items = [(r, agreements[r.agreement_id], review_refusal(db, user, r, agreements[r.agreement_id])) for r in proposed]
    change_items = [(c, agreements[c.agreement_id], review_refusal(db, user, c, agreements[c.agreement_id])) for c in changes]
    return render(request, "rebate_review.html", status_code=status_code, items=items, change_items=change_items,
                  names=_names(db), pct=pct, evidence=by_rate, change_evidence=by_change, new_agreements=set(ids) - approved_once,
                  **context)


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


@router.post("/changes/{change_id}/decision", dependencies=[Depends(verify_csrf)])
def decide_change(
    request: Request,
    change_id: int,
    decision: str = Form(...),
    note: str = Form(""),
    user: AppUser = Depends(approver),
    db: Session = Depends(get_db),
):
    change = db.get(RebateChangeRequest, change_id)
    try:
        if change is None:
            raise RebateError("That change no longer exists.")
        with db.begin_nested():
            (approve_change if decision == "approve" else reject_change)(db, actor=user, request=change, note=note)
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
        rate = db.get(RebateRate, int(value)) if isinstance(value, str) and value.isdigit() else None
        if rate is None:
            problems.append(f"Rate {value}: it no longer exists.")
            continue
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
    change_ids = {e.detail.get("change_id") for e, _ in rows if e.detail.get("change_id")}
    evidence: dict[int, list] = {}
    change_evidence: dict[int, list] = {}
    for row in db.execute(select(EvidenceFile.id, EvidenceFile.rate_id, EvidenceFile.change_request_id, EvidenceFile.file_name)
                          .where(EvidenceFile.rate_id.in_(rate_ids) | EvidenceFile.change_request_id.in_(change_ids))).all():
        if row.rate_id:
            evidence.setdefault(row.rate_id, []).append(row)
        else:
            change_evidence.setdefault(row.change_request_id, []).append(row)
    return render(request, "change_log.html", rows=rows, names=names, pct=pct, evidence=evidence, change_evidence=change_evidence,
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
        AppUser.is_active.is_(True),
        or_(AppUser.role == Role.BRAND_APPROVER, AppUser.role == Role.REBATE_REVIEWER, AppUser.role == Role.REBATE_MAINTAINER)
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
