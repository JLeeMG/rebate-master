"""The monthly accrual: scope, NetSuite sales, the schedule, the journal and its four-eyes, and the back-test.

The worked example is real: MGNZ PB Technology, June 2026. Ken's journal had "PB Technology @ 5%"
NZ$7,180.83 (every brand except Bonelk, Satechi and Spacetalk) and "@ 10%" NZ$14,708.60 (Bonelk and
Satechi); both are reproduced here from NetSuite's own sales.
"""

import csv
import io
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from mgrm.auth.roles import Role
from mgrm.data.errors import LoadRejected
from mgrm.data.registers import FileInput, load_trading_detail
from mgrm.models import (
    AccrualMode,
    AuditEvent,
    Brand,
    Customer,
    CustomerGroup,
    RateType,
    RebateAgreement,
    RebateBasis,
    RebateJournal,
    RebateRate,
    RebateWorkbookMonth,
    ReviewStatus,
    SalesLine,
)
from mgrm.rebates.accrual import build_schedule
from mgrm.rebates.backtest import backtest, summary
from mgrm.rebates.journal import approve_journal, journal_csv, prepare_journal, reject_journal, total_of, withdraw_journal
from mgrm.rebates.scope import ScopeError, make_scope, parse_scope
from mgrm.rebates.service import RebateError, approve_change, propose_agreement_change
from tests.conftest import form_token, sign_in

JUNE = date(2026, 6, 1)
HEADER = "Subsidiary,Start Date,Number,Class,Class Fields : Internal ID,Name,Customer Fields : Internal ID,Debit Amount,Credit Amount\n"
PB_HEAD_OFFICE, PB_STORE = 900001, 900002
# PB Technology's June 2026 sales by brand, as NetSuite holds them (invoices less credit notes).
PB_JUNE = {"SATECHI": ("86681.72", "986.63"), "TWELVE SOUTH": ("78775.89", "150.25"), "BONELK": ("62006.67", "615.72"),
           "WITHINGS": ("18741.51", "0"), "SPHERO": ("18155.03", "0"), "PAPERLIKE": ("14376.96", "34.56"),
           "ADONIT": ("9531.72", "52.16"), "SPACETALK": ("6553.05", "346.44"), "NANOLEAF": ("4272.45", "0")}
CLASS_IDS = {name: 7000 + i for i, name in enumerate(PB_JUNE)}


def trading_csv(rows) -> str:
    out = io.StringIO()
    out.write(HEADER)
    writer = csv.writer(out, lineterminator="\n")
    for row in rows:
        writer.writerow(row)
    return out.getvalue()


def pb_june_rows():
    rows = []
    for brand, (sales, credits) in PB_JUNE.items():
        # half the invoices through the head office, half through a store; credit notes as debits
        half = (Decimal(sales) / 2).quantize(Decimal("0.01"))
        rows.append(["Parent Company : MacGear NZ", "1/06/2026", "40010", brand, CLASS_IDS[brand], "PB Tech Head Office", PB_HEAD_OFFICE, "", str(half)])
        rows.append(["Parent Company : MacGear NZ", "1/06/2026", "40010", brand, CLASS_IDS[brand], "PB Tech Albany", PB_STORE, credits, str(Decimal(sales) - half)])
    rows.append(["Parent Company : MacGear NZ", "1/06/2026", "50010", "BONELK", CLASS_IDS["BONELK"], "PB Tech Albany", PB_STORE, "999", ""])
    return rows


@pytest.fixture
def people(make_user):
    return {"siobhan": make_user(Role.REBATE_MAINTAINER, email="siobhan@example.com", display_name="Siobhan"),
            "ken": make_user(Role.REBATE_MAINTAINER, email="ken@example.com", display_name="Ken"),
            "raymond": make_user(Role.VIEWER, email="raymond@example.com")}


@pytest.fixture
def pb(db, people):
    """PB Technology: its group, two customers, the brands, both agreements with approved rates and scopes."""
    if db.get(CustomerGroup, "PBT") is None:  # the migrations seed the groups
        db.add(CustomerGroup(code="PBT", name="PB Technology"))
        db.flush()
    db.add_all([Customer(netsuite_customer_id=PB_HEAD_OFFICE, name="PB Tech Head Office", entity_code="MGNZ", customer_group_code="PBT"),
                Customer(netsuite_customer_id=PB_STORE, name="PB Tech Albany", entity_code="MGNZ", customer_group_code="PBT")])
    db.add_all([Brand(code=name.replace(" ", "_"), name=name, netsuite_class_id=class_id, is_brand=True)
                for name, class_id in CLASS_IDS.items()])
    agreements = {}
    for code, rate, scope in (
        ("MGNZ-PBT-1", "0.05", make_scope("all_but", ["BONELK", "SATECHI", "SPACETALK"], journal_customer=PB_HEAD_OFFICE)),
        ("MGNZ-PBT-2", "0.10", make_scope("only", ["BONELK", "SATECHI"], journal_customer=PB_HEAD_OFFICE)),
    ):
        a = RebateAgreement(code=code, entity_code="MGNZ", customer_label="PBT", customer_group_code="PBT", product_scope="test",
                            rate_type=RateType.REBATE, basis=RebateBasis.REBATE_ELIGIBLE_SALES, source_reference="terms",
                            scope=scope.to_text())
        db.add(a)
        db.flush()
        db.add(RebateRate(agreement_id=a.id, rate=Decimal(rate), effective_from=date(2024, 1, 1), status=ReviewStatus.APPROVED,
                          source_reference="terms", reason="initial", load_flags=[], entered_by_id=people["siobhan"].id,
                          reviewed_by_id=people["ken"].id, reviewed_at=datetime.now(UTC)))
        agreements[code] = a
    db.flush()
    return agreements


def load_june(db, people, rows=None):
    content = trading_csv(rows or pb_june_rows())
    return load_trading_detail(db, FileInput("trading.csv", content, str(hash(content))), people["siobhan"])


# ---------------------------------------------------------------- scope


def test_scope_is_stored_canonically_and_read_back():
    scope = make_scope("all_but", ["withings", " MOVA "], "all_but", [3, 1], journal_customer=42)
    assert scope.to_text() == ('{"brands":{"codes":["MOVA","WITHINGS"],"mode":"all_but"},"customers":{"ids":[1,3],'
                               '"mode":"all_but"},"journal_customer":42}')
    assert parse_scope(scope.to_text()) == scope
    assert scope.includes_brand("BONELK") and not scope.includes_brand("MOVA")
    assert scope.includes_customer(2, in_group=True) and not scope.includes_customer(3, in_group=True)
    assert not scope.includes_customer(2, in_group=False)
    assert "all brands except MOVA, WITHINGS" in scope.describe()


@pytest.mark.parametrize(("args", "message"), [
    (("only", []), "at least one brand"),
    (("all", ["MOVA"]), "no list of brands"),
    (("sometimes", []), "Brands must be"),
    (("all", [], "only", []), "at least one customer"),
])
def test_an_unusable_scope_is_refused(args, message):
    with pytest.raises(ScopeError, match=message):
        make_scope(*args)


def test_a_scope_change_needs_someone_else_to_approve_it(db, people, pb):
    agreement = pb["MGNZ-PBT-1"]
    before = agreement.scope
    proposed = propose_agreement_change(db, actor=people["siobhan"], agreement=agreement, reason="Confirmed with Ken",
                                        changes={"scope": make_scope("all", [], journal_customer=PB_HEAD_OFFICE).to_text()},
                                        evidence=[])
    assert agreement.scope == before
    with pytest.raises(RebateError, match="someone else"):
        approve_change(db, actor=people["siobhan"], request=proposed)
    approve_change(db, actor=people["ken"], request=proposed)
    assert parse_scope(agreement.scope).brand_mode == "all"


# ---------------------------------------------------------------- sales


def test_trading_detail_keeps_sales_and_replaces_the_months_it_covers(db, people, pb):
    first = load_june(db, people)
    assert db.query(SalesLine).count() == 18  # the 50010 cost line is checked, not kept
    assert sum(s.amount for s in db.query(SalesLine)) == sum(Decimal(s) - Decimal(c) for s, c in PB_JUNE.values())
    rows = pb_june_rows()[:2]
    second = load_june(db, people, rows)
    assert db.query(SalesLine).count() == 2 and {s.batch_id for s in db.query(SalesLine)} == {second.id}
    assert first.id != second.id


def test_trading_detail_with_the_wrong_columns_or_accounts_is_refused(db, people):
    with pytest.raises(LoadRejected) as columns:
        load_trading_detail(db, FileInput("x.csv", "Subsidiary,Start Date\nMacGear NZ,1/06/2026\n", "x"), people["siobhan"])
    assert any("Missing column" in p for p in columns.value.problems)
    bad = trading_csv([["MacGear NZ", "1/06/2026", "61050", "", "", "Landlord", "", "100", ""]])
    with pytest.raises(LoadRejected) as accounts:
        load_trading_detail(db, FileInput("y.csv", bad, "y"), people["siobhan"])
    assert any("not one this search should return" in p for p in accounts.value.problems)


def test_brand_shares_add_up_to_the_agreement_rounded_once():
    from mgrm.rebates.accrual import split_rebate

    shares = split_rebate([Decimal("61390.95"), Decimal("85695.09")], Decimal("0.10"))
    assert sum(shares) == Decimal("14708.60")  # each rounded alone would give 14,708.61


# ---------------------------------------------------------------- the schedule


def test_kens_pb_technology_lines_are_reproduced_to_the_cent(db, people, pb):
    load_june(db, people)
    schedule = build_schedule(db, "MGNZ", JUNE)
    assert schedule.blockers == []
    totals = schedule.by_agreement()
    assert totals[pb["MGNZ-PBT-1"].id] == (Decimal("143616.59"), Decimal("7180.83"))
    assert totals[pb["MGNZ-PBT-2"].id] == (Decimal("147086.04"), Decimal("14708.60"))
    assert {line.brand_code for line in schedule.lines if line.agreement_id == pb["MGNZ-PBT-2"].id} == {"BONELK", "SATECHI"}


def test_a_month_without_sales_cannot_be_journalled(db, people, pb):
    assert "No NetSuite sales are loaded" in build_schedule(db, "MGNZ", JUNE).blockers[0]


def test_an_agreement_without_a_scope_blocks_the_month(db, people, pb):
    pb["MGNZ-PBT-1"].scope = None
    db.flush()
    load_june(db, people)
    assert any("not yet defined" in b for b in build_schedule(db, "MGNZ", JUNE).blockers)


def test_a_rate_awaiting_approval_blocks_the_month(db, people, pb):
    load_june(db, people)
    db.add(RebateRate(agreement_id=pb["MGNZ-PBT-1"].id, rate=Decimal("0.06"), effective_from=JUNE, status=ReviewStatus.PROPOSED,
                      source_reference="new terms", reason="renewal", load_flags=[], entered_by_id=people["siobhan"].id))
    db.flush()
    schedule = build_schedule(db, "MGNZ", JUNE)
    assert any("awaiting approval" in b for b in schedule.blockers)
    # The back-test may use it as if approved, to check the history
    backtest_schedule = build_schedule(db, "MGNZ", JUNE, include_proposed=True)
    assert backtest_schedule.by_agreement()[pb["MGNZ-PBT-1"].id][1] == Decimal("8617.00")  # 143,616.59 x 6%


def test_a_scope_awaiting_approval_counts_only_in_the_backtest(db, people, pb):
    agreement = pb["MGNZ-PBT-1"]
    agreement.scope = None
    db.flush()
    propose_agreement_change(db, actor=people["siobhan"], agreement=agreement, reason="recovered", evidence=[],
                             changes={"scope": make_scope("all_but", ["BONELK", "SATECHI", "SPACETALK"],
                                                          journal_customer=PB_HEAD_OFFICE).to_text()})
    load_june(db, people)
    assert any("not yet defined" in b for b in build_schedule(db, "MGNZ", JUNE).blockers)
    assert build_schedule(db, "MGNZ", JUNE, include_proposed=True).by_agreement()[agreement.id][1] == Decimal("7180.83")


def test_check_only_agreements_are_shown_but_not_accrued(db, people, pb):
    pb["MGNZ-PBT-2"].accrual_mode = AccrualMode.CHECK_ONLY
    db.flush()
    load_june(db, people)
    schedule = build_schedule(db, "MGNZ", JUNE)
    assert schedule.accrual == Decimal("7180.83")
    assert any(not line.accrue for line in schedule.lines)


def test_sales_to_customers_outside_any_group_are_reported(db, people, pb):
    rows = pb_june_rows() + [["MacGear NZ", "1/06/2026", "40010", "BONELK", CLASS_IDS["BONELK"], "New Retailer Ltd", 123456, "", "500"]]
    load_june(db, people, rows)
    assert any("New Retailer Ltd" in w for w in build_schedule(db, "MGNZ", JUNE).warnings)


# ---------------------------------------------------------------- the journal


def test_the_journal_is_balanced_and_tagged_with_brand_and_customer(db, people, pb):
    load_june(db, people)
    journal = prepare_journal(db, actor=people["siobhan"], entity_code="MGNZ", period=JUNE)
    assert journal.external_id == "MGRM-ACCRUAL-MGNZ-2026-06" and journal.total == Decimal("21889.43")
    debit, credit = total_of(journal.lines)
    assert debit == credit == Decimal("21889.43")
    expense = [l for l in journal.lines if l["account"] == "42020"]
    assert len(expense) == 8 and all(l["class_id"] and l["customer_id"] == PB_HEAD_OFFICE for l in expense)
    liability = [l for l in journal.lines if l["account"] == "22070"]
    assert liability == [dict(liability[0], credit="21889.43")]


def test_the_journal_needs_someone_else_and_is_then_locked(db, people, pb):
    load_june(db, people)
    journal = prepare_journal(db, actor=people["siobhan"], entity_code="MGNZ", period=JUNE)
    with pytest.raises(RebateError, match="someone else"):
        approve_journal(db, actor=people["siobhan"], journal=journal)
    with pytest.raises(RebateError, match="does not approve"):
        approve_journal(db, actor=people["raymond"], journal=journal)
    with pytest.raises(RebateError, match="already a journal"):
        prepare_journal(db, actor=people["ken"], entity_code="MGNZ", period=JUNE)
    approve_journal(db, actor=people["ken"], journal=journal, note="ties to the schedule")
    with pytest.raises(DBAPIError, match="part of the history"):
        with db.begin_nested():
            db.execute(text("UPDATE rebate_journal SET total = 1 WHERE id = :i"), {"i": journal.id})
    actions = {e.action for e in db.scalars(select(AuditEvent).where(AuditEvent.subject == journal.external_id))}
    assert {"journal.prepare", "journal.approve"} <= actions


def test_the_database_refuses_self_approval_of_a_journal(db, people, pb):
    load_june(db, people)
    journal = prepare_journal(db, actor=people["siobhan"], entity_code="MGNZ", period=JUNE)
    with pytest.raises(DBAPIError, match="four_eyes"):
        with db.begin_nested():
            db.execute(text("UPDATE rebate_journal SET status='approved', reviewed_by_id=entered_by_id, reviewed_at=now() "
                            "WHERE id=:i"), {"i": journal.id})


def test_a_rejected_journal_is_prepared_again_under_a_new_reference(db, people, pb):
    load_june(db, people)
    first = prepare_journal(db, actor=people["siobhan"], entity_code="MGNZ", period=JUNE)
    with pytest.raises(RebateError, match="Say why"):
        reject_journal(db, actor=people["ken"], journal=first, note="")
    reject_journal(db, actor=people["ken"], journal=first, note="PB Tech scope to be confirmed")
    second = prepare_journal(db, actor=people["siobhan"], entity_code="MGNZ", period=JUNE)
    assert second.external_id == "MGRM-ACCRUAL-MGNZ-2026-06-R2"
    withdraw_journal(db, actor=people["siobhan"], journal=second)
    assert second.status is ReviewStatus.WITHDRAWN


def test_the_netsuite_file(db, people, pb):
    load_june(db, people)
    journal = prepare_journal(db, actor=people["siobhan"], entity_code="MGNZ", period=JUNE)
    rows = list(csv.DictReader(io.StringIO(journal_csv(db, journal))))
    assert len(rows) == len(journal.lines)
    first = rows[0]
    assert (first["External ID"], first["Subsidiary Internal ID"], first["Date"], first["Posting Period"], first["Currency"]) == (
        "MGRM-ACCRUAL-MGNZ-2026-06", "3", "30/06/2026", "Jun 2026", "NZD")
    assert {r["Account Internal ID"] for r in rows} == {"445", "412"}
    assert sum(Decimal(r["Debit"] or 0) for r in rows) == sum(Decimal(r["Credit"] or 0) for r in rows)


# ---------------------------------------------------------------- the back-test


def test_the_backtest_compares_with_the_workbook(db, people, pb):
    load_june(db, people)
    from mgrm.models import LoadBatch, LoadKind

    batch = LoadBatch(kind=LoadKind.REBATE_WORKBOOK, source="wb", file_name="wb.xlsx", file_sha256="wb", row_count=1, summary={})
    db.add(batch)
    db.flush()
    db.add_all([RebateWorkbookMonth(batch_id=batch.id, agreement_id=pb["MGNZ-PBT-1"].id, period=JUNE,
                                    sales=Decimal("143616.59"), rebate_due=Decimal("7180.83")),
                RebateWorkbookMonth(batch_id=batch.id, agreement_id=pb["MGNZ-PBT-2"].id, period=JUNE,
                                    sales=Decimal("147000.00"), rebate_due=Decimal("14700.00"))])
    db.flush()
    rows, _ = backtest(db, JUNE, JUNE)
    by_code = {r.agreement_code: r for r in rows}
    assert by_code["MGNZ-PBT-1"].rebate_match and not by_code["MGNZ-PBT-2"].sales_match
    totals = summary(rows)[("MGNZ", JUNE)]
    assert (totals["matched"], totals["differ"]) == (1, 1)


# ---------------------------------------------------------------- the screens


def test_preparing_and_approving_through_the_screens(client, db, people, pb):
    load_june(db, people)
    sign_in(client, "siobhan@example.com")
    page = client.get("/rebates/accruals?entity=MGNZ&month=2026-06")
    assert page.status_code == 200 and "21,889.43" in page.text
    prepared = client.post("/rebates/accruals/prepare", data={"csrf_token": form_token(client, "/rebates/accruals"),
                                                             "entity": "MGNZ", "month": "2026-06"}, follow_redirects=False)
    journal_url = prepared.headers["location"]
    assert client.get(journal_url + "/netsuite.csv").status_code == 409  # not approved yet
    client.post("/logout", data={"csrf_token": form_token(client, "/")})
    sign_in(client, "ken@example.com")
    client.post(journal_url + "/decision", data={"csrf_token": form_token(client, journal_url), "decision": "approve"})
    download = client.get(journal_url + "/netsuite.csv")
    assert download.status_code == 200 and "MGRM-ACCRUAL-MGNZ-2026-06" in download.text


def test_viewers_do_not_see_the_accrual(client, people):
    sign_in(client, "raymond@example.com")
    assert client.get("/rebates/accruals").status_code == 403
    assert client.get("/rebates/journals").status_code == 403


def test_proposing_a_scope_through_the_agreement_page(client, db, people, pb):
    sign_in(client, "siobhan@example.com")
    page = f"/rebates/agreement/{pb['MGNZ-PBT-1'].id}"
    response = client.post(f"{page}/scope", data={
        "csrf_token": form_token(client, page), "brand_mode": "all_but", "brand_codes": "BONELK, SATECHI",
        "customer_mode": "group", "customer_ids": "", "journal_customer": str(PB_HEAD_OFFICE), "reason": "Spacetalk now included"})
    assert "takes effect once someone else approves" in response.text
    refused = client.post(f"{page}/scope", data={
        "csrf_token": form_token(client, page), "brand_mode": "only", "brand_codes": "NOT_A_BRAND",
        "customer_mode": "group", "customer_ids": "", "journal_customer": "", "reason": "x"})
    assert refused.status_code == 400 and "NOT_A_BRAND" in refused.text
