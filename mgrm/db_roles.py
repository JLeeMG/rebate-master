"""Two database accounts: the owner, which changes the structure, and the app, which only works with rows.

The platform runs as the app account (DATABASE_URL in .env). It can read and
write rows, but it cannot alter or drop tables, switch off the protective
triggers, empty a table, or change or delete anything in the audit log or the
evidence, even if someone copies its password out of .env.

The owner account (OWNER_DATABASE_URL in .env.owner) is used only by
`python -m mgrm migrate`, which upgrades the structure and then re-applies the
app account's privileges below. On the server, .env.owner is readable by IT
administrators only, or kept off the server between upgrades.
"""

import os
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.engine import Connection

OWNER_ENV_FILE = ".env.owner"
APPEND_ONLY_TABLES = ("audit_event", "evidence_file")  # rows are added, never changed or removed
RECORD_ONLY_TABLES = ("alembic_version",)  # read, never written, by the app


def owner_url(project_root: Path, *, test: bool = False) -> str | None:
    key = "TEST_OWNER_DATABASE_URL" if test else "OWNER_DATABASE_URL"
    if os.environ.get(key):
        return os.environ[key]
    path = project_root / OWNER_ENV_FILE
    if not path.exists():
        return None
    return dotenv_values(path, encoding="utf-8-sig").get(key) or None


def app_role(database_url: str) -> str:
    return make_url(database_url).username


def grant_app_privileges(owner_database_url: str, role: str) -> None:
    """Run as the owner after every upgrade: rows only, and append-only where the record is permanent."""
    engine = create_engine(owner_database_url)
    with engine.begin() as connection:
        quoted = connection.dialect.identifier_preparer.quote(role)
        for statement in (
            f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {quoted}",
            f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {quoted}",
            "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
            f"GRANT USAGE ON SCHEMA public TO {quoted}",
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {quoted}",
            f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {quoted}",  # UPDATE: id counters
            f"REVOKE UPDATE, DELETE ON {', '.join(APPEND_ONLY_TABLES)} FROM {quoted}",
            f"REVOKE INSERT, UPDATE, DELETE ON {', '.join(RECORD_ONLY_TABLES)} FROM {quoted}",
        ):
            connection.execute(text(statement))
    engine.dispose()


def privilege_problems(connection: Connection) -> list[str]:
    """What the connected account can do that the app account must not. Empty when it is safe."""
    problems = []
    role = connection.scalar(text("SELECT current_user"))
    powers = connection.execute(text(
        "SELECT rolsuper, rolcreaterole, rolcreatedb, rolbypassrls FROM pg_roles WHERE rolname = current_user")).one()
    if any(powers):
        problems.append(f"{role} is a superuser or can create roles or databases.")
    if connection.scalar(text("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()")) == role:
        problems.append(f"{role} owns the database.")
    owned = connection.scalar(text(
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relowner = (SELECT oid FROM pg_roles WHERE rolname = current_user)"))
    if owned:
        problems.append(f"{role} owns {owned} tables or other objects, so it could switch off their protection.")
    if connection.scalar(text("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")):
        problems.append(f"{role} can create tables.")
    if connection.scalar(text("SELECT has_database_privilege(current_user, current_database(), 'CREATE')")):
        problems.append(f"{role} can create schemas.")
    truncatable = connection.scalars(text(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND has_table_privilege(current_user, c.oid, 'TRUNCATE')")).all()
    if truncatable:
        problems.append(f"{role} can empty tables: {', '.join(sorted(truncatable))}.")
    for table in APPEND_ONLY_TABLES:
        for privilege in ("UPDATE", "DELETE"):
            if connection.scalar(text(f"SELECT has_table_privilege(current_user, 'public.{table}', '{privilege}')")):
                problems.append(f"{role} has {privilege} on {table}, which must be append-only.")
    return problems
