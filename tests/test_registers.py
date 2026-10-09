"""Loading the NetSuite customer and class registers."""

import hashlib

import pytest
from sqlalchemy import select

from mgrm.data.errors import LoadRejected
from mgrm.data.registers import FileInput, brand_code, load_classes, load_customers
from mgrm.models import Brand, Customer

AU = "Parent Company : MacGear AU"


def file(text: str, name: str = "export.csv") -> FileInput:
    return FileInput(name, text, hashlib.sha256(text.encode()).hexdigest())


def test_brand_codes():
    assert brand_code("MOVA") == "MOVA"
    assert brand_code("3PL - Mova") == "3PL_MOVA"
    # NetSuite has both "THOMAS KENT" and "SALOMON : THOMAS KENT"; they must not collide.
    assert brand_code("SALOMON : THOMAS KENT") != brand_code("THOMAS KENT")


def test_classes_arrive_as_not_brands_and_keep_decisions_on_reload(db):
    load_classes(db, file("Internal ID,Name,Inactive\n237,MOVA,No\n231,3PL,No\n", "a.csv"), None)
    brands = {b.code: b for b in db.scalars(select(Brand))}
    assert set(brands) == {"MOVA", "3PL"} and not any(b.is_brand for b in brands.values())
    brands["MOVA"].is_brand = True
    load_classes(db, file("Internal ID,Name,Inactive\n237,MOVA,Yes\n231,3PL,No\n257,EUFY,No\n", "b.csv"), None)
    mova = db.scalar(select(Brand).where(Brand.code == "MOVA"))
    assert mova.is_brand and mova.is_inactive


def test_customers_keep_their_group_on_reload(db):
    header = "Internal ID,ID,Name,Subsidiary,Category,Parent,Terms,Inactive\n"
    load_customers(db, file(header + f'3480,00989,127 JB HI-FI WORLD SQUARE (NSW),"{AU}",Mass Retail,00267 JB HI-FI - AU,45EOM,No\n', "a.csv"), None)
    customer = db.scalar(select(Customer))
    assert customer.customer_group_code is None
    customer.customer_group_code = "JBH"
    load_customers(db, file(header + f'3480,00989,127 JB HI-FI WORLD SQUARE,"{AU}",Mass Retail,00267 JB HI-FI - AU,45EOM,No\n', "b.csv"), None)
    db.refresh(customer)
    assert (customer.customer_group_code, customer.name) == ("JBH", "127 JB HI-FI WORLD SQUARE")


def test_a_bad_file_is_rejected_whole(db):
    header = "Internal ID,ID,Name,Subsidiary,Category,Parent,Terms,Inactive\n"
    bad = header + '1,a,A,"Rewarding Concepts AU",,,,No\n2,b,B,"MacGear NZ",,,,maybe\n'
    with pytest.raises(LoadRejected) as rejected:
        load_customers(db, file(bad), None)
    assert len(rejected.value.problems) == 2 and db.query(Customer).count() == 0
