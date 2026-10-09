"""The migrations and the models describe the same database."""

from alembic import command
from sqlalchemy import create_engine, inspect

from tests.conftest import alembic_config, rebuild_test_database


def test_models_match_migrations(engine, settings):
    # Fails if mgrm/models.py changed without a migration.
    command.check(alembic_config(settings.database_url))


def test_migrations_run_down_and_up_again(engine, settings):
    config = alembic_config(settings.database_url)
    try:
        command.downgrade(config, "base")
        check = create_engine(settings.database_url)
        assert set(inspect(check).get_table_names()) <= {"alembic_version"}
        check.dispose()
    finally:
        rebuild_test_database(settings.database_url)  # leave it as the other tests expect
