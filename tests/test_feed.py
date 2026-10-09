"""The read-only feed the forecasting platform uses. The contract (field names) is pinned here."""

from datetime import date
from decimal import Decimal

import pytest

from mgrm.api.feed import new_token
from mgrm.auth.roles import Role
from mgrm.models import ApiToken, Brand, Customer, RateType, RebateAgreement, RebateBasis
from mgrm.rebates.service import Upload, approve, propose_rate
from tests.conftest import PDF_BYTES

CUSTOMER_FIELDS = {"netsuite_customer_id", "netsuite_entity_id", "name", "entity", "customer_group_code", "category",
                   "parent_name", "terms", "is_inactive"}
BRAND_FIELDS = {"code", "name", "netsuite_class_id", "is_brand", "forecast_tier", "is_inactive"}
RATE_FIELDS = {"agreement_code", "entity", "customer_group_code", "customer_label", "brand_code", "product_scope",
               "rate_type", "basis", "accrual_mode", "rate", "effective_from", "effective_to", "band_from", "band_to",
               "rate_id", "approved_at"}
ENVELOPE_FIELDS = {"source", "feed_version", "endpoint", "generated_at", "count", "data"}


@pytest.fixture
def token(db):
    token, token_hash = new_token()
    db.add(ApiToken(name="Forecasting platform", token_hash=token_hash))
    db.flush()
    return token


@pytest.fixture
def headers(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def data(db, make_user):
    editor, ken = make_user(Role.REBATE_EDITOR), make_user(Role.REBATE_REVIEWER)
    db.add(Brand(code="EUFY", name="EUFY", netsuite_class_id=257, is_brand=True))
    db.add(Customer(netsuite_customer_id=3480, name="127 JB HI-FI WORLD SQUARE (NSW)", entity_code="MGAU", customer_group_code="JBH"))
    agreement = RebateAgreement(code="MGAU-JB-1", entity_code="MGAU", customer_label="JB Hi Fi", customer_group_code="JBH",
                                brand_code="ALL", product_scope="ALL", rate_type=RateType.REBATE,
                                basis=RebateBasis.REBATE_ELIGIBLE_SALES, source_reference="terms", created_by_id=editor.id)
    db.add(agreement)
    db.flush()

    def rate(value, start, end=None):
        return propose_rate(db, actor=editor, agreement=agreement, rate=Decimal(value), effective_from=start, effective_to=end,
                            source_reference="terms", reason="test", evidence=[Upload("a.pdf", PDF_BYTES)])

    approve(db, actor=ken, rate=rate("0.17", date(2024, 1, 1)))
    approve(db, actor=ken, rate=rate("0.19", date(2026, 1, 1)))
    rate("0.25", date(2027, 1, 1))  # proposed only: must never appear in the feed


def test_the_feed_needs_a_token(client):
    for path in ("/api/v1/customers", "/api/v1/brands", "/api/v1/customer-groups", "/api/v1/rates"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer nonsense"}).status_code == 401


def test_a_revoked_token_is_refused(client, db, token, headers):
    from datetime import UTC, datetime

    db.query(ApiToken).update({"revoked_at": datetime.now(UTC)})
    db.flush()
    assert client.get("/api/v1/brands", headers=headers).status_code == 401


def test_the_contract(client, headers, data):
    customers = client.get("/api/v1/customers", headers=headers).json()
    assert set(customers) == ENVELOPE_FIELDS and customers["source"] == "MacGear Rebate Master"
    assert set(customers["data"][0]) == CUSTOMER_FIELDS
    assert set(client.get("/api/v1/brands", headers=headers).json()["data"][0]) == BRAND_FIELDS
    groups = client.get("/api/v1/customer-groups", headers=headers).json()["data"]
    assert {"code": "IC", "name": "Intercompany", "is_intercompany": True} in groups
    rates = client.get("/api/v1/rates", headers=headers).json()
    assert set(rates["data"][0]) == RATE_FIELDS


def test_only_approved_rates_leave_the_platform(client, headers, data):
    history = client.get("/api/v1/rates", headers=headers).json()["data"]
    assert [(r["rate"], r["effective_from"], r["effective_to"]) for r in history] == [
        ("0.170000", "2024-01-01", "2025-12-31"),
        ("0.190000", "2026-01-01", None),
    ]
    in_force = client.get("/api/v1/rates?as_of=2025-06-30", headers=headers).json()
    assert [r["rate"] for r in in_force["data"]] == ["0.170000"] and in_force["as_of"] == "2025-06-30"


def test_use_of_a_token_is_recorded(client, db, headers, data):
    client.get("/api/v1/brands", headers=headers)
    assert db.query(ApiToken).one().last_used_at is not None


def test_only_the_tokens_fingerprint_is_stored(db, token):
    assert token not in {t.token_hash for t in db.query(ApiToken)}
