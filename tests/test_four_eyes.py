"""Ken and Siobhan both enter and approve; neither approves their own input - add, change or end.

Checked through the platform's rules and, separately, through the database.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from mgrm.auth.roles import Role
from mgrm.models import (
    AccrualMode,
    AuditEvent,
    RateType,
    RebateAgreement,
    RebateBasis,
    RebateChangeRequest,
    RebateRate,
    ReviewStatus,
)
from mgrm.rebates.service import (
    RebateError,
    Upload,
    approve,
    approve_change,
    propose_agreement_change,
    propose_end,
    propose_new_agreement,
    propose_rate,
    reject_change,
    review_refusal,
    withdraw,
)
from tests.conftest import PDF_BYTES

EVIDENCE = [Upload("email.pdf", PDF_BYTES)]


@pytest.fixture
def ken(make_user):
    return make_user(Role.REBATE_MAINTAINER, email="ken@example.com", display_name="Ken")


@pytest.fixture
def siobhan(make_user):
    return make_user(Role.REBATE_MAINTAINER, email="siobhan@example.com", display_name="Siobhan")


def new_agreement(db, actor, rate="0.15"):
    return propose_new_agreement(
        db, actor=actor, entity="MGNZ", customer_label="PB Technology", customer_group_code="PBT", brand_code="EUFY",
        product_scope="EUFY", rate_type=RateType.REBATE, basis=RebateBasis.REBATE_ELIGIBLE_SALES,
        accrual_mode=AccrualMode.ACCRUE, rate=Decimal(rate), effective_from=date(2026, 11, 1), effective_to=None,
        source_reference="Email from PB Tech, 3 Oct 2026", reason="EUFY ranged at PB Tech", evidence=EVIDENCE,
    )


def first_rate(db, agreement):
    return db.scalar(select(RebateRate).where(RebateRate.agreement_id == agreement.id))


def refused_by_database(db, sql, params, match):
    with pytest.raises((DBAPIError, IntegrityError), match=match):
        with db.begin_nested():
            db.execute(text(sql), params)


# ---------------------------------------------------------------- add


def test_each_can_add_and_the_other_approves(db, ken, siobhan):
    agreement = new_agreement(db, siobhan)
    rate = first_rate(db, agreement)
    with pytest.raises(RebateError, match="someone else"):
        approve(db, actor=siobhan, rate=rate)
    approve(db, actor=ken, rate=rate)
    assert rate.status is ReviewStatus.APPROVED

    other = new_agreement(db, ken, rate="0.1")
    with pytest.raises(RebateError, match="someone else"):
        approve(db, actor=ken, rate=first_rate(db, other))
    approve(db, actor=siobhan, rate=first_rate(db, other))


def test_a_new_agreement_without_evidence_or_reason_is_not_created(db, siobhan):
    with pytest.raises(RebateError, match="reason"):
        propose_new_agreement(
            db, actor=siobhan, entity="MGNZ", customer_label="PB", customer_group_code="PBT", brand_code="EUFY",
            product_scope="EUFY", rate_type=RateType.REBATE, basis=RebateBasis.REBATE_ELIGIBLE_SALES,
            accrual_mode=AccrualMode.ACCRUE, rate=Decimal("0.1"), effective_from=date(2026, 11, 1), effective_to=None,
            source_reference="x", reason=" ", evidence=EVIDENCE)
    with pytest.raises(RebateError, match="Attach the evidence"):
        propose_new_agreement(
            db, actor=siobhan, entity="MGNZ", customer_label="PB", customer_group_code="PBT", brand_code="EUFY",
            product_scope="EUFY", rate_type=RateType.REBATE, basis=RebateBasis.REBATE_ELIGIBLE_SALES,
            accrual_mode=AccrualMode.ACCRUE, rate=Decimal("0.1"), effective_from=date(2026, 11, 1), effective_to=None,
            source_reference="x", reason="new range", evidence=[])
    assert db.query(RebateAgreement).count() == 0


# ---------------------------------------------------------------- change


def test_each_can_change_and_the_other_approves(db, ken, siobhan):
    agreement = new_agreement(db, siobhan)
    approve(db, actor=ken, rate=first_rate(db, agreement))
    request = propose_agreement_change(db, actor=ken, agreement=agreement, changes={"accrual_mode": AccrualMode.CHECK_ONLY},
                                       reason="Built into net pricing", evidence=[])
    assert agreement.accrual_mode is AccrualMode.ACCRUE
    with pytest.raises(RebateError, match="someone else"):
        approve_change(db, actor=ken, request=request)
    approve_change(db, actor=siobhan, request=request)
    assert agreement.accrual_mode is AccrualMode.CHECK_ONLY

    rate_change = propose_rate(db, actor=ken, agreement=agreement, rate=Decimal("0.17"), effective_from=date(2027, 4, 1),
                               effective_to=None, source_reference="FY28 terms", reason="renewal", evidence=EVIDENCE)
    assert review_refusal(db, ken, rate_change, agreement) == "You entered this, so someone else must review it."
    assert review_refusal(db, siobhan, rate_change, agreement) is None


def test_a_change_is_refused_if_the_agreement_moved_since(db, ken, siobhan, make_user):
    third = make_user(Role.REBATE_MAINTAINER, email="third@example.com")
    agreement = new_agreement(db, siobhan)
    first = propose_agreement_change(db, actor=ken, agreement=agreement, changes={"product_scope": "EUFY Security"},
                                     reason="narrower", evidence=[])
    second = propose_agreement_change(db, actor=siobhan, agreement=agreement, changes={"product_scope": "EUFY Baby"},
                                      reason="different", evidence=[])
    approve_change(db, actor=third, request=first)
    with pytest.raises(RebateError, match="changed since this was proposed"):
        approve_change(db, actor=ken, request=second)
    reject_change(db, actor=ken, request=second, note="superseded by the narrower scope")


# ---------------------------------------------------------------- end ("delete")


def test_each_can_end_and_the_other_approves_and_nothing_is_erased(db, ken, siobhan):
    agreement = new_agreement(db, siobhan)
    rate = first_rate(db, agreement)
    approve(db, actor=ken, rate=rate)
    request = propose_end(db, actor=siobhan, agreement=agreement, effective_to=date(2027, 3, 31),
                          reason="Range delisted", evidence=[])
    assert rate.effective_to is None
    with pytest.raises(RebateError, match="someone else"):
        approve_change(db, actor=siobhan, request=request)
    approve_change(db, actor=ken, request=request)
    assert rate.effective_to == date(2027, 3, 31)
    assert db.get(RebateAgreement, agreement.id) is not None and db.get(RebateRate, rate.id) is not None


def test_ending_needs_a_rate_in_force(db, siobhan):
    agreement = new_agreement(db, siobhan)  # its first rate is still only proposed
    with pytest.raises(RebateError, match="no open-ended approved rate"):
        propose_end(db, actor=siobhan, agreement=agreement, effective_to=date(2027, 3, 31), reason="x", evidence=[])


# ---------------------------------------------------------------- withdrawing


def test_only_the_author_withdraws_and_the_record_stays(db, ken, siobhan):
    agreement = new_agreement(db, siobhan)
    rate = first_rate(db, agreement)
    with pytest.raises(RebateError, match="Only the person who entered it"):
        withdraw(db, actor=ken, item=rate)
    withdraw(db, actor=siobhan, item=rate, note="wrong start date")
    assert rate.status is ReviewStatus.WITHDRAWN and db.get(RebateRate, rate.id) is not None
    with pytest.raises(RebateError, match="already been withdrawn"):
        approve(db, actor=ken, rate=rate)
    event = db.scalar(select(AuditEvent).where(AuditEvent.action == "rebate.withdraw"))
    assert event.actor_id == siobhan.id and event.detail["note"] == "wrong start date"


# ---------------------------------------------------------------- the database's own guard


def test_the_database_refuses_self_approval_of_a_change(db, ken, siobhan):
    agreement = new_agreement(db, siobhan)
    request = propose_agreement_change(db, actor=ken, agreement=agreement, changes={"agreed_by": "Ken"}, reason="r", evidence=[])
    refused_by_database(
        db, "UPDATE rebate_change_request SET status='approved', reviewed_by_id=entered_by_id, reviewed_at=now() WHERE id=:i",
        {"i": request.id}, "four_eyes")


def test_a_decided_change_is_history(db, ken, siobhan):
    agreement = new_agreement(db, siobhan)
    request = propose_agreement_change(db, actor=ken, agreement=agreement, changes={"agreed_by": "Ken"}, reason="r", evidence=[])
    approve_change(db, actor=siobhan, request=request)
    refused_by_database(db, "UPDATE rebate_change_request SET reason='edited' WHERE id=:i", {"i": request.id}, "part of the history")
    refused_by_database(db, "DELETE FROM rebate_change_request WHERE id=:i", {"i": request.id}, "part of the history")


def test_a_proposed_change_cannot_be_edited_only_withdrawn(db, ken, siobhan):
    agreement = new_agreement(db, siobhan)
    request = propose_agreement_change(db, actor=ken, agreement=agreement, changes={"agreed_by": "Ken"}, reason="r", evidence=[])
    refused_by_database(db, "UPDATE rebate_change_request SET reason='edited' WHERE id=:i", {"i": request.id}, "cannot be edited")
    withdraw(db, actor=ken, item=request)
    assert db.get(RebateChangeRequest, request.id).status is ReviewStatus.WITHDRAWN
