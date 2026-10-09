"""The monthly accrual schedule: each agreement's rate applied to the NetSuite sales in its scope.

For one entity and month:
  - sales are the trading detail (account 40010, net of credit notes) loaded for that month;
  - each agreement takes the sales its scope selects (customer group or listed stores x brands);
  - the rate is the approved rate in force on the first day of the month;
  - the rebate is worked out per agreement and brand, to the cent, so the journal can carry the
    brand on every line.

Problems are of two kinds. A *blocker* means the month cannot be journalled yet (an agreement in
force has no scope, a rate covering the month is still awaiting approval, a tiered rate, no sales
loaded). A *warning* is shown to the preparer and the reviewer and kept with the journal (sales to
a customer not in any group, a rate that changes part-way through the month).

`include_proposed` is for back-testing only: it uses rates and scopes still awaiting approval, as if approved.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from mgrm.data.registers import SALES_ACCOUNT, brand_code
from mgrm.models import (
    AccrualMode,
    Brand,
    Customer,
    RebateAgreement,
    RebateChangeRequest,
    RebateRate,
    ReviewStatus,
    SalesLine,
)
from mgrm.rebates.scope import NO_CLASS, ScopeError, parse_scope

CENT = Decimal("0.01")
USABLE_IN_BACKTEST = (ReviewStatus.APPROVED, ReviewStatus.PROPOSED)


@dataclass(frozen=True)
class ScheduleLine:
    agreement_id: int
    agreement_code: str
    customer_label: str
    brand_code: str
    netsuite_class_id: int | None
    sales: Decimal
    rate: Decimal
    rebate: Decimal
    accrue: bool  # False: a check-only agreement, shown but never journalled
    journal_customer: int | None
    rate_status: str


@dataclass
class Schedule:
    entity_code: str
    period: date
    lines: list[ScheduleLine] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sales_batch_ids: list[int] = field(default_factory=list)
    sales_total: Decimal = Decimal(0)

    @property
    def accrual(self) -> Decimal:
        return sum((line.rebate for line in self.lines if line.accrue), Decimal(0))

    def by_agreement(self) -> dict[int, tuple[Decimal, Decimal]]:
        """agreement id -> (sales in scope, rebate)."""
        totals: dict[int, list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
        for line in self.lines:
            totals[line.agreement_id][0] += line.sales
            totals[line.agreement_id][1] += line.rebate
        return {k: (v[0], v[1]) for k, v in totals.items()}


def month_end(period: date) -> date:
    return (period.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def _covers(rate: RebateRate, day: date) -> bool:
    return rate.effective_from <= day and (rate.effective_to is None or rate.effective_to >= day)


def rate_for_month(rates: list[RebateRate], period: date, include_proposed: bool) -> tuple[RebateRate | None, list[str]]:
    """The rate to apply for the month, and anything the preparer must know about it."""
    notes = []
    usable = USABLE_IN_BACKTEST if include_proposed else (ReviewStatus.APPROVED,)
    covering = [r for r in rates if r.status in usable and _covers(r, period)]
    covering.sort(key=lambda r: (r.effective_from, r.status is ReviewStatus.APPROVED))
    chosen = covering[-1] if covering else None
    if not include_proposed and any(r.status is ReviewStatus.PROPOSED and r.effective_from <= month_end(period)
                                    and (r.effective_to is None or r.effective_to >= period) for r in rates):
        notes.append("blocker:a rate covering this month is awaiting approval")
    end = month_end(period)
    if any(r.status in usable and period < r.effective_from <= end for r in rates):
        notes.append("warning:the rate changes part-way through the month; the rate in force on the 1st is used")
    return chosen, notes


def split_rebate(amounts: list[Decimal], rate: Decimal) -> list[Decimal]:
    """Each brand's share of the agreement's rebate, to the cent, adding up exactly to the agreement's
    rebate rounded once (as the workbooks round it). Any rounding cent goes to the largest line."""
    if not amounts:
        return []
    total = (sum(amounts, Decimal(0)) * rate).quantize(CENT, rounding=ROUND_HALF_UP)
    shares = [(a * rate).quantize(CENT, rounding=ROUND_HALF_UP) for a in amounts]
    largest = max(range(len(amounts)), key=lambda i: abs(amounts[i]))
    shares[largest] += total - sum(shares, Decimal(0))
    return shares


def build_schedule(session: Session, entity_code: str, period: date, *, include_proposed: bool = False) -> Schedule:
    schedule = Schedule(entity_code, period)
    sales = session.scalars(select(SalesLine).where(
        SalesLine.entity_code == entity_code, SalesLine.period == period, SalesLine.account_code == SALES_ACCOUNT)).all()
    if not sales:
        schedule.blockers.append(f"No NetSuite sales are loaded for {entity_code} {period:%B %Y}. Load the trading detail first.")
        return schedule
    schedule.sales_batch_ids = sorted({s.batch_id for s in sales})
    schedule.sales_total = sum((s.amount for s in sales), Decimal(0))

    group_of = {c.netsuite_customer_id: c.customer_group_code for c in session.scalars(select(Customer))}
    code_of = {b.netsuite_class_id: b.code for b in session.scalars(select(Brand)) if b.netsuite_class_id is not None}
    unknown_customers: dict[str, Decimal] = defaultdict(Decimal)
    ungrouped: dict[str, Decimal] = defaultdict(Decimal)
    unknown_classes: set[str] = set()
    keyed = []  # (customer id, group, brand code, class id, amount)
    for s in sales:
        if s.netsuite_class_id is None:
            code = NO_CLASS
        elif s.netsuite_class_id in code_of:
            code = code_of[s.netsuite_class_id]
        else:
            code = brand_code(s.class_name or str(s.netsuite_class_id))
            unknown_classes.add(s.class_name or str(s.netsuite_class_id))
        if s.netsuite_customer_id not in group_of:
            unknown_customers[s.customer_name or str(s.netsuite_customer_id)] += s.amount
        elif group_of[s.netsuite_customer_id] is None:
            ungrouped[s.customer_name or str(s.netsuite_customer_id)] += s.amount
        keyed.append((s.netsuite_customer_id, group_of.get(s.netsuite_customer_id), code, s.netsuite_class_id, s.amount))

    agreements = session.scalars(select(RebateAgreement).where(RebateAgreement.entity_code == entity_code)
                                 .order_by(RebateAgreement.code)).all()
    rates: dict[int, list[RebateRate]] = defaultdict(list)
    for r in session.scalars(select(RebateRate).where(RebateRate.agreement_id.in_([a.id for a in agreements]))):
        rates[r.agreement_id].append(r)
    pending_scopes: dict[int, str] = {}  # back-test only: scopes proposed but not yet approved
    if include_proposed:
        for change in session.scalars(select(RebateChangeRequest).where(
                RebateChangeRequest.status == ReviewStatus.PROPOSED).order_by(RebateChangeRequest.id)):
            new_scope = change.payload.get("changes", {}).get("scope")
            if new_scope:
                pending_scopes[change.agreement_id] = new_scope[1]

    for agreement in agreements:
        rate, notes = rate_for_month(rates[agreement.id], period, include_proposed)
        for note in notes:
            kind, text = note.split(":", 1)
            (schedule.blockers if kind == "blocker" else schedule.warnings).append(f"{agreement.code}: {text}")
        if rate is None:
            continue  # not in force this month
        if rate.band_from is not None or rate.band_to is not None:
            schedule.blockers.append(f"{agreement.code}: tiered rates are not calculated yet; enter this month's accrual by hand")
            continue
        try:
            scope = parse_scope(agreement.scope or (pending_scopes.get(agreement.id) if include_proposed else None))
        except ScopeError as exc:
            schedule.blockers.append(f"{agreement.code}: {exc}")
            continue
        if scope is None:
            schedule.blockers.append(f"{agreement.code}: which brands and customers it covers is not yet defined")
            continue
        accrue = agreement.accrual_mode is AccrualMode.ACCRUE
        if accrue and scope.journal_customer is None:
            schedule.blockers.append(f"{agreement.code}: no customer is set for its journal lines")
        if scope.customer_mode != "only" and not agreement.customer_group_code:
            schedule.blockers.append(f"{agreement.code}: covers a customer group, but has none")
            continue
        by_brand: dict[tuple[str, int | None], Decimal] = defaultdict(Decimal)
        for customer_id, group, code, class_id, amount in keyed:
            in_group = group is not None and group == agreement.customer_group_code
            if scope.includes_customer(customer_id, in_group) and scope.includes_brand(code):
                by_brand[(code, class_id)] += amount
        brands = [(key, amount) for key, amount in sorted(by_brand.items(), key=lambda kv: kv[0][0]) if amount]
        for ((code, class_id), amount), rebate in zip(brands, split_rebate([a for _, a in brands], rate.rate)):
            schedule.lines.append(ScheduleLine(
                agreement.id, agreement.code, agreement.customer_label, code, class_id, amount, rate.rate, rebate,
                accrue, scope.journal_customer, rate.status.value,
            ))

    for name, amount in sorted(unknown_customers.items(), key=lambda kv: -abs(kv[1]))[:20]:
        schedule.warnings.append(f"Sales of {amount:,.2f} to {name}, who is not in the customer register: load the register")
    for name, amount in sorted(ungrouped.items(), key=lambda kv: -abs(kv[1]))[:20]:
        schedule.warnings.append(f"Sales of {amount:,.2f} to {name}, who is in no customer group: no rebate can apply")
    for name in sorted(unknown_classes):
        schedule.warnings.append(f"Class '{name}' is not in the brand register: load the class register")
    return schedule
