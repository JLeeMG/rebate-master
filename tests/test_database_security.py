"""The platform's database account works with rows only; the owner cannot empty the permanent record either."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from mgrm.db_roles import privilege_problems

PROTECTIONS = {  # trigger: table
    "audit_event_append_only": "audit_event",
    "evidence_file_permanent": "evidence_file",
    "rebate_rate_history": "rebate_rate",
    "rebate_change_request_history": "rebate_change_request",
    "audit_event_not_emptied": "audit_event",
    "evidence_file_not_emptied": "evidence_file",
    "rebate_rate_not_emptied": "rebate_rate",
    "rebate_change_request_not_emptied": "rebate_change_request",
    "rebate_journal_history": "rebate_journal",
    "rebate_journal_not_emptied": "rebate_journal",
}


def test_the_platform_account_passes_the_check(engine):
    with engine.connect() as connection:
        assert privilege_problems(connection) == []


@pytest.mark.parametrize("statement", [
    "TRUNCATE audit_event",
    "ALTER TABLE audit_event DISABLE TRIGGER ALL",
    "DROP TRIGGER audit_event_append_only ON audit_event",
    "CREATE TABLE sneaky (id int)",
    "CREATE OR REPLACE FUNCTION mgrm_forbid_audit_change() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RETURN NEW; END$$",
])
def test_the_platform_account_cannot_get_round_the_protections(engine, statement):
    with engine.connect() as connection:
        with pytest.raises(DBAPIError, match="permission denied|must be owner|must be the owner"):
            connection.execute(text(statement))


@pytest.mark.parametrize("table", ["audit_event", "evidence_file"])
def test_even_the_owner_cannot_empty_the_permanent_record(owner_engine, table):
    with owner_engine.connect() as connection:
        with pytest.raises(DBAPIError, match="cannot be emptied"):
            connection.execute(text(f"TRUNCATE {table}"))


def test_every_protection_is_switched_on(engine):
    with engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT t.tgname, c.relname, t.tgenabled FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE NOT t.tgisinternal")).all()
    found = {name: (table, enabled) for name, table, enabled in rows}
    for trigger, table in PROTECTIONS.items():
        assert found.get(trigger) == (table, "O"), trigger
