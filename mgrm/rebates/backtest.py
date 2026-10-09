"""Back-test: the platform's accrual against Ken's reconciliation workbooks, agreement by agreement.

Uses rates still awaiting approval as if approved (rate history is under review), and only
agreements whose scope is defined. Each workbook month is matched on sales in scope and on the
rebate; a difference is listed, never absorbed.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from mgrm.models import AccrualMode, RebateAgreement, RebateWorkbookMonth
from mgrm.rebates.accrual import build_schedule

TOLERANCE = Decimal("0.05")


@dataclass(frozen=True)
class Comparison:
    entity_code: str
    period: date
    agreement_code: str
    accrue: bool
    scope_defined: bool
    workbook_sales: Decimal | None
    workbook_rebate: Decimal | None
    platform_sales: Decimal
    platform_rebate: Decimal

    @property
    def sales_match(self) -> bool:
        return self.workbook_sales is not None and abs(self.workbook_sales - self.platform_sales) <= TOLERANCE

    @property
    def rebate_match(self) -> bool:
        return self.workbook_rebate is not None and abs(self.workbook_rebate - self.platform_rebate) <= TOLERANCE


def months_between(first: date, last: date) -> list[date]:
    months, current = [], first
    while current <= last:
        months.append(current)
        current = date(current.year + current.month // 12, current.month % 12 + 1, 1)
    return months


def backtest(session: Session, first: date, last: date) -> tuple[list[Comparison], list[str]]:
    agreements = {a.id: a for a in session.scalars(select(RebateAgreement))}
    workbook = {(m.agreement_id, m.period): m for m in session.scalars(
        select(RebateWorkbookMonth).where(RebateWorkbookMonth.period >= first, RebateWorkbookMonth.period <= last))}
    rows, problems = [], []
    for entity in ("MGAU", "MGNZ"):
        for period in months_between(first, last):
            schedule = build_schedule(session, entity, period, include_proposed=True)
            problems += [f"{entity} {period:%b %Y}: {b}" for b in schedule.blockers if "not yet defined" not in b]
            platform = schedule.by_agreement()
            if not schedule.sales_batch_ids:
                continue  # no sales loaded for the month: nothing to compare
            ids = [aid for aid, a in agreements.items() if a.entity_code == entity]
            for agreement_id in sorted(ids, key=lambda i: agreements[i].code):
                a = agreements[agreement_id]
                wb = workbook.get((agreement_id, period))
                sales, rebate = platform.get(agreement_id, (Decimal(0), Decimal(0)))
                if wb is None and not sales:
                    continue
                rows.append(Comparison(entity, period, a.code, a.accrual_mode is AccrualMode.ACCRUE,
                                       a.scope is not None or agreement_id in platform,
                                       wb.sales if wb else None, wb.rebate_due if wb else None, sales, rebate))
    return rows, problems


def summary(rows: list[Comparison]) -> dict[tuple[str, date], dict[str, Decimal | int]]:
    """Per entity and month: the accrue totals both ways, and how many agreements match."""
    out: dict[tuple[str, date], dict] = defaultdict(lambda: {"workbook": Decimal(0), "platform": Decimal(0), "matched": 0,
                                                              "differ": 0, "undefined": 0})
    for r in rows:
        bucket = out[(r.entity_code, r.period)]
        if r.accrue:
            bucket["workbook"] += r.workbook_rebate or Decimal(0)
            bucket["platform"] += r.platform_rebate
        if not r.scope_defined:
            bucket["undefined"] += 1
        elif r.rebate_match and r.sales_match:
            bucket["matched"] += 1
        else:
            bucket["differ"] += 1
    return dict(out)
