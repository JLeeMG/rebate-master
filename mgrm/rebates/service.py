"""Proposing, evidencing, approving and reading rebate rates.

The rules, in one place:
  - A rate change is entered as PROPOSED, with a reason, a source reference and
    at least one evidence file (e.g. the retailer's email saved as PDF). It does
    nothing until approved.
  - The approver must be someone other than the person who entered it (the
    database refuses otherwise too).
  - If a brand has assigned approvers (e.g. its Group Product Manager), only they,
    or an administrator, may approve that brand's rates. Otherwise any rebate
    reviewer or administrator may.
  - Approving a rate closes the approved rate it replaces the day before it
    starts. Nothing is overwritten or deleted (spec §4.5.1).
  - Every input is a proposal that someone other than its author must approve:
    a new agreement (with its first rate), a rate change, a change to an
    agreement's details, and ending an agreement. Each needs a reason and may
    carry evidence (a rate always does). The author may withdraw it before review.
Every step is written to the audit log with who, when, what and why.
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from mgrm.auth.roles import Permission, Role, can
from mgrm.auth.users import audit
from mgrm.data.files import bytes_sha256
from mgrm.models import (
    AppUser,
    ChangeKind,
    EvidenceFile,
    RebateAgreement,
    RebateApproverScope,
    RebateChangeRequest,
    RebateRate,
    ReviewStatus,
)

EXPIRY_WINDOW_DAYS = 90
ONE_DAY = timedelta(days=1)
MAX_EVIDENCE_BYTES = 20 * 1024 * 1024
FILE_NAME_LENGTH = 255

# What evidence may be, recognised by the file's own first bytes, not its name.
PDF, OLE2, PNG, JPEG = b"%PDF-", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"
EMAIL_HEADERS = (b"received:", b"from:", b"return-path:", b"mime-version:", b"date:", b"message-id:", b"subject:", b"to:")


def rate_text(rate: Decimal) -> str:
    """A rate as the audit log records it: the same text however it was read, e.g. '0.2', '0.253'."""
    return format(Decimal(rate).normalize(), "f")


class RebateError(ValueError):
    """A rebate change that is not allowed, with a message fit to show on screen."""


@dataclass(frozen=True)
class Upload:
    file_name: str
    content: bytes
    description: str = ""


def evidence_type(upload: Upload) -> str:
    """The content type of an acceptable evidence file, or RebateError saying why it is not acceptable."""
    content, name = upload.content, upload.file_name.lower()
    if not content:
        raise RebateError(f"'{upload.file_name}' is empty.")
    if len(content) > MAX_EVIDENCE_BYTES:
        raise RebateError(f"'{upload.file_name}' is larger than {MAX_EVIDENCE_BYTES // (1024 * 1024)} MB.")
    if content.startswith(PDF):
        return "application/pdf"
    if content.startswith(OLE2) and name.endswith(".msg"):
        return "application/vnd.ms-outlook"
    if content.startswith(PNG):
        return "image/png"
    if content.startswith(JPEG):
        return "image/jpeg"
    if name.endswith(".eml") and content[:2000].lower().lstrip().startswith(EMAIL_HEADERS):
        return "message/rfc822"
    raise RebateError(
        f"'{upload.file_name}' is not a PDF, an Outlook email (.msg), a saved email (.eml) or a picture. "
        "Save the email as PDF and attach that."
    )


def attach_evidence(
    session: Session, *, actor: AppUser, agreement: RebateAgreement, uploads: list[Upload], rate: RebateRate | None = None,
    change_request=None,
) -> list[EvidenceFile]:
    if not can(actor.role, Permission.EDIT_REBATES):
        raise RebateError("Your role does not add evidence.")
    files = []
    for upload in uploads:
        content_type = evidence_type(upload)
        evidence = EvidenceFile(
            agreement_id=agreement.id, rate_id=rate.id if rate else None,
            change_request_id=change_request.id if change_request else None,
            file_name=upload.file_name.strip()[:FILE_NAME_LENGTH] or "evidence", content_type=content_type,
            size_bytes=len(upload.content), sha256=bytes_sha256(upload.content), content=upload.content,
            description=upload.description.strip(), uploaded_by_id=actor.id,
        )
        session.add(evidence)
        files.append(evidence)
    session.flush()
    for evidence in files:
        audit(session, actor, "rebate.evidence", agreement.code, file=evidence.file_name, sha256=evidence.sha256,
              bytes=evidence.size_bytes, rate_id=rate.id if rate else None,
              change_id=change_request.id if change_request else None, evidence_id=evidence.id)
    return files


# ---------------------------------------------------------------- who may approve


def assigned_approvers(session: Session, brand_code: str | None) -> set[int]:
    if not brand_code or brand_code == "ALL":
        return set()
    return set(session.scalars(select(RebateApproverScope.user_id).where(RebateApproverScope.brand_code == brand_code)))


def review_refusal(session: Session, user: AppUser, item, agreement: RebateAgreement) -> str | None:
    """Why this user may not decide on this proposal (a rate or a change request), or None if they may."""
    if not can(user.role, Permission.APPROVE_REBATES):
        return "Your role does not approve rebate changes."
    if item.status is not ReviewStatus.PROPOSED:
        return f"This has already been {item.status.value}."
    if item.entered_by_id == user.id:
        return "You entered this, so someone else must review it."
    approvers = assigned_approvers(session, agreement.brand_code)
    if approvers:
        return None if user.id in approvers else f"{agreement.brand_code} changes are approved by its assigned approvers."
    if user.role is Role.BRAND_APPROVER:
        return f"You are not an assigned approver for {agreement.brand_code or 'this brand'}."
    return None


approval_refusal = review_refusal  # rates were the first kind of proposal; the rule is the same for all


# ---------------------------------------------------------------- proposing


def rate_in_force(rates: list[RebateRate], on: date) -> RebateRate | None:
    for rate in rates:
        if rate.status is ReviewStatus.APPROVED and rate.effective_from <= on and (rate.effective_to is None or rate.effective_to >= on):
            return rate
    return None


def propose_rate(
    session: Session,
    *,
    actor: AppUser,
    agreement: RebateAgreement,
    rate: Decimal,
    effective_from: date,
    effective_to: date | None,
    source_reference: str,
    reason: str,
    evidence: list[Upload],
) -> RebateRate:
    if not can(actor.role, Permission.EDIT_REBATES):
        raise RebateError("Your role does not enter rebate rates.")
    if not reason.strip():
        raise RebateError("Give the reason for the change, e.g. 'Renewed trading terms for FY27'.")
    if not source_reference.strip():
        raise RebateError("Name the source: the contract, trading terms or email that agrees this rate.")
    if not evidence:
        raise RebateError("Attach the evidence, e.g. the email agreeing the rate saved as PDF.")
    if not Decimal("0") <= rate < Decimal("1"):
        raise RebateError("A rate is a percentage between 0% and 100%.")
    if effective_to is not None and effective_to < effective_from:
        raise RebateError("The end date is before the start date.")
    for upload in evidence:
        evidence_type(upload)  # refuse the whole change before saving anything if any file is unacceptable
    previous = rate_in_force(
        session.scalars(select(RebateRate).where(RebateRate.agreement_id == agreement.id)).all(), effective_from
    )
    new = RebateRate(
        agreement_id=agreement.id, rate=rate, effective_from=effective_from, effective_to=effective_to,
        status=ReviewStatus.PROPOSED, source_reference=source_reference.strip(), reason=reason.strip(),
        load_flags=[], entered_by_id=actor.id,
    )
    session.add(new)
    session.flush()
    audit(session, actor, "rebate.propose", agreement.code, rate_id=new.id, rate=rate_text(rate),
          previous_rate=rate_text(previous.rate) if previous else None, effective_from=effective_from.isoformat(),
          effective_to=effective_to.isoformat() if effective_to else None, source=source_reference.strip(),
          reason=reason.strip())
    attach_evidence(session, actor=actor, agreement=agreement, uploads=evidence, rate=new)
    return new


AGREEMENT_FIELDS = ("customer_group_code", "brand_code", "product_scope", "rate_type", "basis", "accrual_mode", "agreed_by")


def _plain(value) -> str | None:
    return None if value is None else str(getattr(value, "value", value))


def _require_editor(actor: AppUser) -> None:
    if not can(actor.role, Permission.EDIT_REBATES):
        raise RebateError("Your role does not enter rebate changes.")


def _require_reason(reason: str, what: str) -> None:
    if not reason.strip():
        raise RebateError(f"Give the reason for {what}.")


def propose_agreement_change(
    session: Session, *, actor: AppUser, agreement: RebateAgreement, changes: dict, reason: str, source_reference: str = "",
    evidence: list[Upload],
) -> RebateChangeRequest | None:
    """Propose new details for an agreement. Nothing changes until someone else approves. None if nothing differs."""
    _require_editor(actor)
    changed = {k: [_plain(getattr(agreement, k)), _plain(v)] for k, v in changes.items()
               if k in AGREEMENT_FIELDS and _plain(getattr(agreement, k)) != _plain(v)}
    if not changed:
        return None
    _require_reason(reason, "changing the agreement")
    for upload in evidence:
        evidence_type(upload)
    request = RebateChangeRequest(agreement_id=agreement.id, kind=ChangeKind.DETAILS, payload={"changes": changed},
                                  reason=reason.strip(), source_reference=source_reference.strip(), entered_by_id=actor.id)
    session.add(request)
    session.flush()
    audit(session, actor, "rebate.change_propose", agreement.code, change_id=request.id, kind="details",
          changes=changed, reason=reason.strip())
    if evidence:
        attach_evidence(session, actor=actor, agreement=agreement, uploads=evidence, change_request=request)
    return request


def propose_end(
    session: Session, *, actor: AppUser, agreement: RebateAgreement, effective_to: date, reason: str,
    source_reference: str = "", evidence: list[Upload],
) -> RebateChangeRequest:
    """Propose ending an agreement: its rate in force stops on `effective_to`. Records are ended, never erased."""
    _require_editor(actor)
    _require_reason(reason, "ending the agreement")
    rates = session.scalars(select(RebateRate).where(RebateRate.agreement_id == agreement.id)).all()
    current = rate_in_force(rates, effective_to)
    if current is None or current.effective_to is not None:
        raise RebateError("There is no open-ended approved rate in force on that date to end.")
    for upload in evidence:
        evidence_type(upload)
    request = RebateChangeRequest(agreement_id=agreement.id, kind=ChangeKind.END,
                                  payload={"rate_id": current.id, "effective_to": effective_to.isoformat()},
                                  reason=reason.strip(), source_reference=source_reference.strip(), entered_by_id=actor.id)
    session.add(request)
    session.flush()
    audit(session, actor, "rebate.change_propose", agreement.code, change_id=request.id, kind="end",
          effective_to=effective_to.isoformat(), rate=rate_text(current.rate), reason=reason.strip())
    if evidence:
        attach_evidence(session, actor=actor, agreement=agreement, uploads=evidence, change_request=request)
    return request


def _agreement_code(session: Session, entity: str, group: str | None, scope: str) -> str:
    slug = re.sub(r"[^A-Z0-9]+", "-", scope.upper()).strip("-")[:30]
    stem = f"{entity}-{group or 'NOGROUP'}-{slug}"
    taken = set(session.scalars(select(RebateAgreement.code).where(RebateAgreement.code.like(f"{stem}%"))))
    number = 1
    while f"{stem}-N{number}" in taken:
        number += 1
    return f"{stem}-N{number}"


def propose_new_agreement(
    session: Session, *, actor: AppUser, entity: str, customer_label: str, customer_group_code: str | None,
    brand_code: str | None, product_scope: str, rate_type, basis, accrual_mode, rate: Decimal, effective_from: date,
    effective_to: date | None, source_reference: str, reason: str, evidence: list[Upload],
) -> RebateAgreement:
    """A new agreement with its first rate. It has no effect until someone else approves that rate."""
    _require_editor(actor)
    if not customer_label.strip() or not product_scope.strip():
        raise RebateError("Name the customer and the products the agreement covers.")
    _require_reason(reason, "the new agreement")
    for upload in evidence:
        evidence_type(upload)
    agreement = RebateAgreement(
        code=_agreement_code(session, entity, customer_group_code, brand_code or product_scope), entity_code=entity,
        customer_label=customer_label.strip(), customer_group_code=customer_group_code or None,
        brand_code=(brand_code or "").strip().upper() or None, product_scope=product_scope.strip(), rate_type=rate_type,
        basis=basis, accrual_mode=accrual_mode, source_reference=source_reference.strip() or "(new agreement)",
        created_by_id=actor.id,
    )
    with session.begin_nested():
        session.add(agreement)
        session.flush()
        audit(session, actor, "rebate.agreement_create", agreement.code, reason=reason.strip(),
              customer=agreement.customer_label, group=agreement.customer_group_code, scope=agreement.product_scope)
        propose_rate(session, actor=actor, agreement=agreement, rate=rate, effective_from=effective_from,
                     effective_to=effective_to, source_reference=source_reference, reason=reason, evidence=evidence)
    return agreement


def withdraw(session: Session, *, actor: AppUser, item, note: str = "") -> None:
    """The person who entered a proposal takes it back before review. It never took effect; the record stays."""
    if item.entered_by_id != actor.id:
        raise RebateError("Only the person who entered it can withdraw it.")
    if item.status is not ReviewStatus.PROPOSED:
        raise RebateError(f"It has already been {item.status.value}.")
    agreement = session.get(RebateAgreement, item.agreement_id)
    item.status = ReviewStatus.WITHDRAWN
    session.flush()
    key = "rate_id" if isinstance(item, RebateRate) else "change_id"
    audit(session, actor, "rebate.withdraw", agreement.code, **{key: item.id}, note=note.strip())


# ---------------------------------------------------------------- deciding


def _overlaps(a_from: date, a_to: date | None, b_from: date, b_to: date | None) -> bool:
    return (a_to is None or a_to >= b_from) and (b_to is None or b_to >= a_from)


def approve(session: Session, *, actor: AppUser, rate: RebateRate, note: str = "") -> None:
    agreement = session.get(RebateAgreement, rate.agreement_id)
    refusal = approval_refusal(session, actor, rate, agreement)
    if refusal:
        raise RebateError(refusal)
    approved = session.scalars(
        select(RebateRate).where(
            RebateRate.agreement_id == rate.agreement_id,
            RebateRate.status == ReviewStatus.APPROVED,
            RebateRate.band_from.is_(None) if rate.band_from is None else RebateRate.band_from == rate.band_from,
        )
    ).all()
    closing = []
    for other in approved:
        if not _overlaps(other.effective_from, other.effective_to, rate.effective_from, rate.effective_to):
            continue
        if other.effective_from < rate.effective_from and other.effective_to is None:
            closing.append(other)  # the open-ended rate this one replaces
        elif other.effective_to:
            raise RebateError(f"An approved rate of {other.rate * 100:.4g}% already covers "
                              f"{other.effective_from:%d %b %Y} to {other.effective_to:%d %b %Y}")
        else:
            raise RebateError(f"An approved rate of {other.rate * 100:.4g}% already starts on {other.effective_from:%d %b %Y}")
    for other in closing:
        other.effective_to = rate.effective_from - ONE_DAY
    rate.status = ReviewStatus.APPROVED
    rate.reviewed_by_id = actor.id
    rate.reviewed_at = datetime.now(UTC)
    rate.review_note = note.strip()
    session.flush()
    audit(session, actor, "rebate.approve", agreement.code, rate_id=rate.id, rate=rate_text(rate.rate),
          effective_from=rate.effective_from.isoformat(), closed=[c.id for c in closing], note=note.strip())


def reject(session: Session, *, actor: AppUser, rate: RebateRate, note: str) -> None:
    agreement = session.get(RebateAgreement, rate.agreement_id)
    refusal = approval_refusal(session, actor, rate, agreement)
    if refusal:
        raise RebateError(refusal)
    if not note.strip():
        raise RebateError("Say why the rate is rejected, so whoever entered it can correct it.")
    rate.status = ReviewStatus.REJECTED
    rate.reviewed_by_id = actor.id
    rate.reviewed_at = datetime.now(UTC)
    rate.review_note = note.strip()
    session.flush()
    audit(session, actor, "rebate.reject", agreement.code, rate_id=rate.id, rate=rate_text(rate.rate), note=note.strip())


def approve_change(session: Session, *, actor: AppUser, request: RebateChangeRequest, note: str = "") -> None:
    agreement = session.get(RebateAgreement, request.agreement_id)
    refusal = review_refusal(session, actor, request, agreement)
    if refusal:
        raise RebateError(refusal)
    if request.kind is ChangeKind.DETAILS:
        changes = request.payload["changes"]
        moved = [k for k, (was, _) in changes.items() if _plain(getattr(agreement, k)) != was]
        if moved:
            raise RebateError(f"The agreement has changed since this was proposed ({', '.join(moved)}). Reject it and propose again.")
        for key, (_, new) in changes.items():
            enum_class = getattr(RebateAgreement.__table__.c[key].type, "enum_class", None)
            setattr(agreement, key, enum_class(new) if enum_class and new is not None else new)
    else:
        rate = session.get(RebateRate, request.payload["rate_id"])
        if rate is None or rate.status is not ReviewStatus.APPROVED or rate.effective_to is not None:
            raise RebateError("The rate this would end is no longer open-ended. Reject it and propose again.")
        rate.effective_to = date.fromisoformat(request.payload["effective_to"])
    request.status = ReviewStatus.APPROVED
    request.reviewed_by_id = actor.id
    request.reviewed_at = datetime.now(UTC)
    request.review_note = note.strip()
    session.flush()
    audit(session, actor, "rebate.change_approve", agreement.code, change_id=request.id, kind=request.kind.value,
          note=note.strip())


def reject_change(session: Session, *, actor: AppUser, request: RebateChangeRequest, note: str) -> None:
    agreement = session.get(RebateAgreement, request.agreement_id)
    refusal = review_refusal(session, actor, request, agreement)
    if refusal:
        raise RebateError(refusal)
    if not note.strip():
        raise RebateError("Say why it is rejected, so whoever entered it can correct it.")
    request.status = ReviewStatus.REJECTED
    request.reviewed_by_id = actor.id
    request.reviewed_at = datetime.now(UTC)
    request.review_note = note.strip()
    session.flush()
    audit(session, actor, "rebate.change_reject", agreement.code, change_id=request.id, kind=request.kind.value,
          note=note.strip())


# ---------------------------------------------------------------- reading


@dataclass(frozen=True)
class CurrentRate:
    agreement: RebateAgreement
    rate: RebateRate | None  # the approved rate in force on the date, if any
    pending: int  # proposals awaiting review


def agreements_with_rates(session: Session, on: date, **filters) -> list[CurrentRate]:
    query = select(RebateAgreement).order_by(RebateAgreement.entity_code, RebateAgreement.customer_label, RebateAgreement.code)
    for field_name, value in filters.items():
        if value:
            query = query.where(getattr(RebateAgreement, field_name) == value)
    agreements = session.scalars(query).all()
    rates: dict[int, list[RebateRate]] = {}
    for rate in session.scalars(select(RebateRate).where(RebateRate.agreement_id.in_([a.id for a in agreements]))):
        rates.setdefault(rate.agreement_id, []).append(rate)
    return [
        CurrentRate(a, rate_in_force(rates.get(a.id, []), on),
                    sum(1 for r in rates.get(a.id, []) if r.status is ReviewStatus.PROPOSED))
        for a in agreements
    ]


@dataclass(frozen=True)
class ExpiryItem:
    agreement: RebateAgreement
    rate: RebateRate
    reason: str


def expiry_and_renewal(session: Session, today: date) -> tuple[list[ExpiryItem], list[ExpiryItem]]:
    """(ending within the window, running with no end date) - spec §4.5.2."""
    horizon = today + timedelta(days=EXPIRY_WINDOW_DAYS)
    ending, open_ended = [], []
    for current in agreements_with_rates(session, today):
        rate = current.rate
        if rate is None:
            continue
        if rate.effective_to is not None and rate.effective_to <= horizon:
            ending.append(ExpiryItem(current.agreement, rate, f"ends {rate.effective_to:%d %b %Y}"))
        elif rate.effective_to is None:
            open_ended.append(ExpiryItem(current.agreement, rate, f"in force since {rate.effective_from:%b %Y}, no end date"))
    return ending, open_ended
