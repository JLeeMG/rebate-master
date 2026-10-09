"""The audit trail: every change has a who, when, what, why and its evidence; nothing can be rewritten."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from mgrm.auth.roles import Role
from mgrm.data.files import bytes_sha256
from mgrm.models import AuditEvent, EvidenceFile, RateType, RebateAgreement, RebateBasis
from mgrm.rebates.service import (
    MAX_EVIDENCE_BYTES,
    RebateError,
    Upload,
    approve,
    attach_evidence,
    evidence_type,
    propose_rate,
    update_agreement,
)
from tests.conftest import PDF_BYTES

OUTLOOK_MSG = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64
EML = b"Received: from mail.example.com\r\nFrom: buyer@noelleeming.co.nz\r\nSubject: EUFY rebate\r\n\r\nAgreed at 25.3%.\r\n"


@pytest.fixture
def siobhan(make_user):
    return make_user(Role.REBATE_EDITOR, email="siobhan@example.com", display_name="Siobhan")


@pytest.fixture
def ken(make_user):
    return make_user(Role.REBATE_REVIEWER, email="ken@example.com", display_name="Ken")


@pytest.fixture
def agreement(db, siobhan):
    a = RebateAgreement(code="MGNZ-NLG-EUFY-1", entity_code="MGNZ", customer_label="Noel Leeming", customer_group_code="NL",
                        brand_code="EUFY", product_scope="EUFY (Security & Baby)", rate_type=RateType.REBATE,
                        basis=RebateBasis.REBATE_ELIGIBLE_SALES, source_reference="Trading terms 2026", created_by_id=siobhan.id)
    db.add(a)
    db.flush()
    return a


def propose(db, actor, agreement, *, reason="FY27 renewal", evidence=None, rate="0.253"):
    return propose_rate(db, actor=actor, agreement=agreement, rate=Decimal(rate), effective_from=date(2026, 8, 1),
                        effective_to=None, source_reference="Email from NLG, 14 Aug 2026", reason=reason,
                        evidence=evidence if evidence is not None else [Upload("NLG email.pdf", PDF_BYTES)])


# ---------------------------------------------------------------- what counts as evidence


@pytest.mark.parametrize(
    ("name", "content", "expected"),
    [("email.pdf", PDF_BYTES, "application/pdf"), ("email.msg", OUTLOOK_MSG, "application/vnd.ms-outlook"),
     ("email.eml", EML, "message/rfc822"), ("shot.png", b"\x89PNG\r\n\x1a\n" + b"0" * 10, "image/png"),
     ("photo.jpg", b"\xff\xd8\xff\xe0" + b"0" * 10, "image/jpeg")],
)
def test_accepted_evidence(name, content, expected):
    assert evidence_type(Upload(name, content)) == expected


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [("email.pdf", b"not really a pdf", "not a PDF"), ("notes.txt", b"hello", "not a PDF"), ("empty.pdf", b"", "empty"),
     ("macro.xlsm", b"PK\x03\x04", "not a PDF")],
)
def test_refused_evidence(name, content, message):
    # Judged by the file's own content, not its name: a renamed file is refused.
    with pytest.raises(RebateError, match=message):
        evidence_type(Upload(name, content))


def test_evidence_over_the_size_limit_is_refused():
    with pytest.raises(RebateError, match="larger than"):
        evidence_type(Upload("big.pdf", b"%PDF-" + b"0" * MAX_EVIDENCE_BYTES))


# ---------------------------------------------------------------- reasons and evidence are required


def test_a_rate_change_needs_a_reason(db, siobhan, agreement):
    with pytest.raises(RebateError, match="reason"):
        propose(db, siobhan, agreement, reason="  ")


def test_a_rate_change_needs_evidence(db, siobhan, agreement):
    with pytest.raises(RebateError, match="Attach the evidence"):
        propose(db, siobhan, agreement, evidence=[])


def test_one_bad_file_stops_the_whole_change(db, siobhan, agreement):
    with pytest.raises(RebateError):
        propose(db, siobhan, agreement, evidence=[Upload("ok.pdf", PDF_BYTES), Upload("bad.exe", b"MZ\x90")])
    assert db.query(EvidenceFile).count() == 0


def test_the_database_refuses_a_rate_without_a_reason(db, siobhan, agreement):
    rate = propose(db, siobhan, agreement)
    with pytest.raises(DBAPIError, match="reason_stated"):
        with db.begin_nested():
            db.execute(text("UPDATE rebate_rate SET reason = ' ' WHERE id = :i"), {"i": rate.id})


# ---------------------------------------------------------------- the record


def test_a_proposal_records_who_what_why_and_the_evidence(db, siobhan, agreement):
    rate = propose(db, siobhan, agreement)
    evidence = db.scalars(select(EvidenceFile).where(EvidenceFile.rate_id == rate.id)).all()
    assert [(e.file_name, e.content_type, e.sha256, e.uploaded_by_id) for e in evidence] == [
        ("NLG email.pdf", "application/pdf", bytes_sha256(PDF_BYTES), siobhan.id)
    ]
    events = {e.action: e for e in db.scalars(select(AuditEvent).where(AuditEvent.subject == agreement.code))}
    assert events["rebate.propose"].actor_id == siobhan.id
    assert events["rebate.propose"].detail["reason"] == "FY27 renewal"
    assert events["rebate.propose"].detail["rate"] == "0.253"
    assert events["rebate.evidence"].detail["sha256"] == bytes_sha256(PDF_BYTES)


def test_the_previous_rate_is_recorded_with_the_change(db, siobhan, ken, agreement):
    from mgrm.rebates.service import propose_rate as propose_any

    first = propose_any(db, actor=siobhan, agreement=agreement, rate=Decimal("0.20"), effective_from=date(2025, 4, 1),
                        effective_to=None, source_reference="2025 terms", reason="initial",
                        evidence=[Upload("a.pdf", PDF_BYTES)])
    approve(db, actor=ken, rate=first)
    propose(db, siobhan, agreement)
    event = db.scalars(select(AuditEvent).where(AuditEvent.action == "rebate.propose").order_by(AuditEvent.id.desc())).first()
    assert (event.detail["previous_rate"], event.detail["rate"]) == ("0.2", "0.253")


def test_evidence_can_never_be_changed_or_removed(db, siobhan, agreement):
    rate = propose(db, siobhan, agreement)
    evidence = db.scalar(select(EvidenceFile).where(EvidenceFile.rate_id == rate.id))
    for statement in ("UPDATE evidence_file SET content = 'x' WHERE id = :i", "DELETE FROM evidence_file WHERE id = :i"):
        with pytest.raises(DBAPIError, match="kept exactly as uploaded"):
            with db.begin_nested():
                db.execute(text(statement), {"i": evidence.id})


def test_more_evidence_can_be_added(db, siobhan, agreement):
    rate = propose(db, siobhan, agreement)
    attach_evidence(db, actor=siobhan, agreement=agreement, uploads=[Upload("signed terms.pdf", PDF_BYTES, "signed")], rate=rate)
    assert db.query(EvidenceFile).filter(EvidenceFile.rate_id == rate.id).count() == 2


def test_a_reviewer_cannot_add_evidence(db, ken, agreement):
    with pytest.raises(RebateError, match="does not add evidence"):
        attach_evidence(db, actor=ken, agreement=agreement, uploads=[Upload("a.pdf", PDF_BYTES)])


def test_changing_an_agreement_needs_a_reason_and_is_logged(db, siobhan, agreement):
    with pytest.raises(RebateError, match="reason"):
        update_agreement(db, actor=siobhan, agreement=agreement, changes={"customer_group_code": "OTHER"}, reason="", evidence=[])
    assert agreement.customer_group_code == "NL"
    update_agreement(db, actor=siobhan, agreement=agreement, changes={"customer_group_code": "OTHER"},
                     reason="NLG Commercial accounts are not covered", evidence=[Upload("email.pdf", PDF_BYTES)])
    event = db.scalar(select(AuditEvent).where(AuditEvent.action == "rebate.agreement_update"))
    assert event.detail == {"reason": "NLG Commercial accounts are not covered", "changes": {"customer_group_code": ["NL", "OTHER"]}}
    assert db.query(EvidenceFile).filter(EvidenceFile.rate_id.is_(None)).count() == 1


def test_an_unchanged_agreement_needs_no_reason(db, siobhan, agreement):
    assert update_agreement(db, actor=siobhan, agreement=agreement, changes={"customer_group_code": "NL"}, reason="", evidence=[]) == {}
