"""Shared test set-up.

Database tests run against TEST_DATABASE_URL, which is wiped and rebuilt from
the migrations at the start of every run. The live database is never touched:
the suite refuses to start unless the test database name ends in _test.
"""

import re
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.orm import Session

from mgrm.auth.roles import Role
from mgrm.auth.users import create_user
from mgrm.config import Settings
from mgrm.models import AuthMethod

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_PASSWORD = "correct horse battery"
PDF_BYTES = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"


@pytest.fixture(scope="session")
def settings() -> Settings:
    base = Settings(_env_file=PROJECT_ROOT / ".env")
    if not base.test_database_url:
        pytest.exit("TEST_DATABASE_URL is not set in .env; run scripts/setup_database.ps1", returncode=2)
    test_url = make_url(base.test_database_url)
    if not (test_url.database or "").endswith("_test") or base.test_database_url == base.database_url:
        pytest.exit("TEST_DATABASE_URL must name a separate database ending in _test", returncode=2)
    return base.model_copy(update={"database_url": base.test_database_url})


def alembic_config(url: str) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["database_url"] = url
    return config


def rebuild_test_database(url: str) -> None:
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    command.upgrade(alembic_config(url), "head")


@pytest.fixture(scope="session")
def engine(settings):
    rebuild_test_database(settings.database_url)
    engine = create_engine(settings.database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def db(engine):
    """A session whose work is rolled back after each test."""
    connection = engine.connect()
    outer = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
    yield session
    session.close()
    outer.rollback()
    connection.close()


@pytest.fixture
def app(settings, db):
    from mgrm.web.app import create_app

    app = create_app(settings)

    def scope():
        yield db
        db.flush()

    app.state.db_session_scope = scope
    return app


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


@pytest.fixture
def make_user(db):
    def make(role: Role = Role.VIEWER, email: str | None = None, must_change: bool = False, **kwargs):
        user = create_user(
            db,
            actor=None,
            email=email or f"{role.value}@example.com",
            display_name=kwargs.pop("display_name", role.title),
            role=role,
            auth_method=kwargs.pop("auth_method", AuthMethod.LOCAL),
            password=kwargs.pop("password", TEST_PASSWORD),
        )
        user.must_change_password = must_change
        db.flush()
        return user

    return make


def form_token(client: TestClient, path: str = "/login") -> str:
    """The anti-forgery token the page at `path` would put in its forms."""
    page = client.get(path)
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match, f"No form token on {path}"
    return match.group(1)


def sign_in(client: TestClient, email: str, password: str = TEST_PASSWORD):
    return client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": form_token(client)},
        follow_redirects=False,
    )


def switch(client: TestClient, email: str):
    client.post("/logout", data={"csrf_token": form_token(client, "/")})
    sign_in(client, email)
