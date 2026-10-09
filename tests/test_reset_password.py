"""The command-line password reset for a locked-out user."""

from contextlib import contextmanager

from sqlalchemy import select

from mgrm.auth.passwords import verify_password
from mgrm.auth.roles import Role
from mgrm.models import AppUser, AuditEvent


def run_reset(monkeypatch, db, email, answers):
    import mgrm.__main__ as cli

    class Factory:
        @contextmanager
        def begin(self):
            yield db
            db.flush()

    monkeypatch.setattr(cli, "_session_factory", lambda: Factory())
    replies = iter(answers)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(replies))
    return cli.main(["reset-password", email])


def test_a_locked_out_administrator_sets_a_new_password(monkeypatch, db, make_user):
    admin = make_user(Role.ADMIN, email="j.lee@example.com")
    admin.is_active = False
    db.flush()
    assert run_reset(monkeypatch, db, "J.Lee@example.com", ["short", "a brand new password", "a brand new password"]) == 0
    db.refresh(admin)
    assert verify_password(admin.password_hash, "a brand new password") and admin.is_active and not admin.must_change_password
    actions = {e.action for e in db.scalars(select(AuditEvent).where(AuditEvent.subject == "j.lee@example.com"))}
    assert {"user.reset_password", "user.activate"} <= actions


def test_an_unknown_email_changes_nothing(monkeypatch, db):
    assert run_reset(monkeypatch, db, "nobody@example.com", []) == 1
    assert db.query(AppUser).count() == 0
