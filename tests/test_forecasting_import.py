"""The one-time move out of the forecasting platform: everything arrives, with its ids, or nothing does.

A stand-in for the forecasting database is built in a separate schema of the
test database, with the same table shapes and the forecasting platform's role
names.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from mgrm.data.forecasting_import import import_all
from mgrm.models import (
    AppUser,
    AuditEvent,
    Base,
    Brand,
    Customer,
    CustomerGroup,
    RebateAgreement,
    RebateRate,
)

SOURCE_SCHEMA = "forecasting_stand_in"


@pytest.fixture
def source_url(owner_test_url, owner_engine):
    # Built by the database owner: the platform's own account may not create schemas or tables.
    engine = owner_engine
    with engine.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {SOURCE_SCHEMA} CASCADE"))
        c.execute(text(f"CREATE SCHEMA {SOURCE_SCHEMA}"))
    url = f"{owner_test_url}?options=-csearch_path%3D{SOURCE_SCHEMA}"
    source = create_engine(url)
    Base.metadata.create_all(source)
    with source.begin() as c:
        c.execute(text("ALTER TABLE app_user DROP CONSTRAINT ck_app_user_role"))  # forecasting role names differ
        c.execute(text("INSERT INTO entity VALUES ('MGAU','MacGear Australia',2,'AUD'), ('MGNZ','MacGear New Zealand',3,'NZD')"))
        c.execute(text("INSERT INTO app_user (id,email,display_name,role,auth_method,password_hash) VALUES "
                       "(7,'j.lee@macgeargroup.com','Jonathan Lee','admin','local','x'), "
                       "(8,'gavin@macgeargroup.com','Gavin','rebates_only','local','x')"))
        c.execute(text("INSERT INTO customer_group (code,name,is_intercompany) VALUES ('HN','Harvey Norman',false), ('BUN','Bunnings',false)"))
        c.execute(text("INSERT INTO brand (id,code,name,netsuite_class_id,is_brand) VALUES (41,'EUFY','EUFY',257,true)"))
        c.execute(text("INSERT INTO customer (id,netsuite_customer_id,name,entity_code,customer_group_code) VALUES "
                       "(90,3480,'127 JB HI-FI WORLD SQUARE (NSW)','MGAU','BUN')"))
        c.execute(text("INSERT INTO load_batch (id,kind,source,file_name,file_sha256,row_count,summary) VALUES "
                       "(9,'classes','s','c.csv','a',1,'{}'), (3,'rebate_workbook','w','w.xlsx','b',1,'{}')"))
        c.execute(text("INSERT INTO rebate_agreement (id,code,entity_code,customer_label,customer_group_code,product_scope,"
                       "rate_type,basis,accrual_mode,source_reference,created_by_id) VALUES "
                       "(55,'MGAU-HN-1','MGAU','Harvey Norman','HN','ALL','rebate','rebate_eligible_sales','accrue','wb',7)"))
        c.execute(text("INSERT INTO rebate_rate (id,agreement_id,rate,effective_from,status,source_reference,reason,load_flags,entered_by_id) "
                       "VALUES (101,55,0.17,'2017-01-01','proposed','wb','Initial load','[]',7)"))
        c.execute(text("INSERT INTO rebate_workbook_month (id,batch_id,agreement_id,period,sales,rebate_due) VALUES (5,3,55,'2026-08-01',100,17)"))
        c.execute(text("INSERT INTO audit_event (id,action,subject,detail) VALUES "
                       "(301,'rebate.agreement_update','MGAU-HN-1','{}'), (302,'sign_in','x','{}')"))
    source.dispose()
    yield url
    with engine.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {SOURCE_SCHEMA} CASCADE"))


@pytest.fixture
def target_factory(db):
    """Import into the test session's transaction so it is rolled back afterwards."""
    class Factory:
        def __call__(self):
            return self

        def __enter__(self):
            return db

        def __exit__(self, *exc):
            return False

    return Factory()


def test_everything_arrives_with_its_ids(db, source_url, target_factory):
    report = import_all(source_url=source_url, target_factory=target_factory)
    assert report.verified, report.problems
    assert db.get(AppUser, 7).role.value == "admin"
    assert db.get(AppUser, 8).role.value == "viewer"  # rebates_only becomes viewer
    assert db.get(CustomerGroup, "BUN").name == "Bunnings"
    assert db.get(Brand, 41).netsuite_class_id == 257
    assert db.get(Customer, 90).customer_group_code == "BUN"
    rate = db.get(RebateRate, 101)
    assert (rate.agreement_id, rate.rate, rate.effective_from) == (55, Decimal("0.170000"), date(2017, 1, 1))
    assert db.get(RebateAgreement, 55).code == "MGAU-HN-1"
    actions = {e.action for e in db.scalars(select(AuditEvent))}
    assert "rebate.agreement_update" in actions and "sign_in" not in actions  # only the rebate history moves
    assert "data.forecasting_import" in actions


def test_it_runs_once(db, source_url, target_factory):
    assert import_all(source_url=source_url, target_factory=target_factory).verified
    again = import_all(source_url=source_url, target_factory=target_factory)
    assert not again.verified and "done before" in again.problems[0]
