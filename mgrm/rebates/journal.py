"""The month's customer-rebate accrual journal, as a file for NetSuite's journal import.

One journal per entity and month, replacing Ken's: a debit to the rebate expense account for each
agreement and brand, tagged with the brand (class) and the retailer's customer record, and one
credit to 22070 Rebate Accruals for the total. It is not reversed: claims are credited against
22070 as they arrive, as now.

Prepared by one person, approved by another, and locked once decided (the database enforces both).
The platform never posts to NetSuite: whoever imports the approved file does, and the next trading
detail load shows the result.
"""

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from mgrm.auth.roles import Permission, can
from mgrm.auth.users import audit
from mgrm.models import AppUser, Brand, Customer, EntityRow, RebateJournal, ReviewStatus
from mgrm.rebates.accrual import Schedule, build_schedule, month_end
from mgrm.rebates.service import RebateError


@dataclass(frozen=True)
class Account:
    internal_id: int
    number: str
    name: str


# NetSuite accounts (internal ids read from NetSuite, 9 Oct 2026). The same as Ken's journals.
EXPENSE = {"MGAU": Account(444, "42010", "Rebates Customers"),
           "MGNZ": Account(445, "42020", "Rebate Customers Accruals NP")}
LIABILITY = Account(412, "22070", "Rebate Accruals")
CURRENCY = {"MGAU": "AUD", "MGNZ": "NZD"}

CSV_COLUMNS = ["External ID", "Subsidiary", "Subsidiary Internal ID", "Date", "Posting Period", "Currency", "Memo",
               "Account", "Account Internal ID", "Debit", "Credit", "Line Memo", "Class", "Class Internal ID",
               "Customer", "Customer Internal ID"]


def external_id(entity_code: str, period: date, attempt: int) -> str:
    """Fixed per entity and month, so NetSuite refuses a second import of the same journal."""
    return f"MGRM-ACCRUAL-{entity_code}-{period:%Y-%m}" + (f"-R{attempt}" if attempt > 1 else "")


def journal_lines(session: Session, schedule: Schedule) -> list[dict]:
    customers = {c.netsuite_customer_id: c.name for c in session.scalars(select(Customer))}
    classes = {b.netsuite_class_id: b.name for b in session.scalars(select(Brand)) if b.netsuite_class_id is not None}
    expense = EXPENSE[schedule.entity_code]
    lines = []
    for item in schedule.lines:
        if not item.accrue or not item.rebate:
            continue
        lines.append({
            "account": expense.number, "account_id": expense.internal_id, "account_name": expense.name,
            "debit": str(item.rebate) if item.rebate > 0 else "", "credit": str(-item.rebate) if item.rebate < 0 else "",
            "memo": f"{item.agreement_code} {item.rate * 100:.4g}% on sales {item.sales:,.2f}",
            "class": classes.get(item.netsuite_class_id, ""), "class_id": item.netsuite_class_id,
            "customer": customers.get(item.journal_customer, ""), "customer_id": item.journal_customer,
            "agreement_id": item.agreement_id,
        })
    total = schedule.accrual
    lines.append({
        "account": LIABILITY.number, "account_id": LIABILITY.internal_id, "account_name": LIABILITY.name,
        "debit": str(-total) if total < 0 else "", "credit": str(total) if total > 0 else "",
        "memo": f"Rebates Accrual - {schedule.period:%B %Y}", "class": "", "class_id": None,
        "customer": "", "customer_id": None, "agreement_id": None,
    })
    return lines


def journal_csv(session: Session, journal: RebateJournal) -> str:
    entity = session.get(EntityRow, journal.entity_code)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    header_memo = f"Rebates Accrual - {journal.period:%B %Y} (Rebate Master journal {journal.id})"
    for line in journal.lines:
        writer.writerow([
            journal.external_id, entity.name, entity.netsuite_subsidiary_id, f"{month_end(journal.period):%d/%m/%Y}",
            f"{journal.period:%b %Y}", CURRENCY[journal.entity_code], header_memo,
            f"{line['account']} {line['account_name']}", line["account_id"], line["debit"], line["credit"], line["memo"],
            line["class"], line["class_id"] or "", line["customer"], line["customer_id"] or "",
        ])
    return out.getvalue()


def _fingerprint(lines: list[dict]) -> str:
    text = "\n".join(f"{l['account']}|{l['debit']}|{l['credit']}|{l['class_id']}|{l['customer_id']}|{l['memo']}" for l in lines)
    return hashlib.sha256(text.encode()).hexdigest()


def _schedule_rows(schedule: Schedule) -> list[dict]:
    return [{"agreement_id": l.agreement_id, "agreement": l.agreement_code, "customer": l.customer_label, "brand": l.brand_code,
             "sales": str(l.sales), "rate": str(l.rate), "rebate": str(l.rebate), "accrue": l.accrue} for l in schedule.lines]


def prepare_journal(session: Session, *, actor: AppUser, entity_code: str, period: date) -> RebateJournal:
    if not can(actor.role, Permission.EDIT_REBATES):
        raise RebateError("Your role does not prepare journals.")
    if session.scalar(select(RebateJournal).where(
            RebateJournal.entity_code == entity_code, RebateJournal.period == period,
            RebateJournal.status.in_([ReviewStatus.PROPOSED, ReviewStatus.APPROVED]))):
        raise RebateError(f"There is already a journal for {entity_code} {period:%B %Y} awaiting review or approved. "
                          "Withdraw or reject it first.")
    schedule = build_schedule(session, entity_code, period)
    if schedule.blockers:
        raise RebateError("The journal cannot be prepared yet: " + "; ".join(schedule.blockers))
    lines = journal_lines(session, schedule)
    attempts = session.scalars(select(RebateJournal.id).where(
        RebateJournal.entity_code == entity_code, RebateJournal.period == period)).all()
    journal = RebateJournal(
        entity_code=entity_code, period=period, external_id=external_id(entity_code, period, len(attempts) + 1),
        status=ReviewStatus.PROPOSED, total=schedule.accrual, lines=lines, schedule=_schedule_rows(schedule),
        warnings=schedule.warnings, sales_batch_ids=schedule.sales_batch_ids, file_sha256=_fingerprint(lines),
        entered_by_id=actor.id,
    )
    session.add(journal)
    session.flush()
    audit(session, actor, "journal.prepare", journal.external_id, journal_id=journal.id, total=str(journal.total),
          lines=len(lines), warnings=len(schedule.warnings))
    return journal


def journal_refusal(actor: AppUser, journal: RebateJournal) -> str | None:
    if not can(actor.role, Permission.APPROVE_REBATES):
        return "Your role does not approve journals."
    if journal.status is not ReviewStatus.PROPOSED:
        return f"This journal has already been {journal.status.value}."
    if journal.entered_by_id == actor.id:
        return "You prepared this journal, so someone else must review it."
    return None


def approve_journal(session: Session, *, actor: AppUser, journal: RebateJournal, note: str = "") -> None:
    refusal = journal_refusal(actor, journal)
    if refusal:
        raise RebateError(refusal)
    _decide(session, actor, journal, ReviewStatus.APPROVED, note)


def reject_journal(session: Session, *, actor: AppUser, journal: RebateJournal, note: str) -> None:
    refusal = journal_refusal(actor, journal)
    if refusal:
        raise RebateError(refusal)
    if not note.strip():
        raise RebateError("Say why the journal is rejected, so it can be corrected and prepared again.")
    _decide(session, actor, journal, ReviewStatus.REJECTED, note)


def withdraw_journal(session: Session, *, actor: AppUser, journal: RebateJournal) -> None:
    if journal.entered_by_id != actor.id:
        raise RebateError("Only the person who prepared it can withdraw it.")
    if journal.status is not ReviewStatus.PROPOSED:
        raise RebateError(f"This journal has already been {journal.status.value}.")
    journal.status = ReviewStatus.WITHDRAWN
    session.flush()
    audit(session, actor, "journal.withdraw", journal.external_id, journal_id=journal.id)


def _decide(session: Session, actor: AppUser, journal: RebateJournal, status: ReviewStatus, note: str) -> None:
    journal.status = status
    journal.reviewed_by_id = actor.id
    journal.reviewed_at = datetime.now(UTC)
    journal.review_note = note.strip()
    session.flush()
    audit(session, actor, f"journal.{'approve' if status is ReviewStatus.APPROVED else 'reject'}", journal.external_id,
          journal_id=journal.id, total=str(journal.total), note=note.strip())


def total_of(lines: list[dict]) -> tuple[Decimal, Decimal]:
    """(debits, credits): equal in a balanced journal."""
    debit = sum((Decimal(l["debit"]) for l in lines if l["debit"]), Decimal(0))
    credit = sum((Decimal(l["credit"]) for l in lines if l["credit"]), Decimal(0))
    return debit, credit
