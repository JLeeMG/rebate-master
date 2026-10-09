"""Alembic environment. Uses DATABASE_URL unless a caller passes a URL (the tests do)."""

from alembic import context
from sqlalchemy import create_engine

from mgrm.models import Base

config = context.config
target_metadata = Base.metadata


def database_url() -> str:
    url = config.attributes.get("database_url")
    if url:
        return url
    from mgrm.config import get_settings

    return get_settings().database_url


def run_migrations_online() -> None:
    engine = create_engine(database_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    raise SystemExit("Offline (SQL script) migrations are not used on this platform.")
run_migrations_online()
