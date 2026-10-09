"""The command-line password reset and first administrator: temporary passwords, recorded with who ran them."""

from contextlib import contextmanager

from sqlalchemy import select

from mgrm.auth.passwords import verify_password
from mgrm.auth.roles import Role
from mgrm.models import AppUser, AuditEvent
from mgrm.web.security import record_failed_sign_in, sign_in_refused


def run(monkeypatch, db, argv, answers=(), typed=()):
    import mgrm.__main__ as cli

    class Factory:
        def __call__(self):
            return self

        def __enter__(self):
            return db

        def __exit__(self, *exc):
            return False

        @contextmanager
        def begin(self):
            yield db
            db.flush()

    monkeypatch.setattr(cli, "_session_factory", lambda: Factory())
    replies, lines = iter(answers), iter(typed)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(replies))
    monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))
    return cli.main(argv)


def test_a_reset_password_is_temporary_and_records_who_ran_it(monkeypatch, db, make_user):
    admin = make_user(Role.ADMIN, email="j.lee@example.com")
    for _ in range(5):
        record_failed_sign_in(db, "j.lee@example.com", "10.0.0.5")
    assert run(monkeypatch, db, ["reset-password", "J.Lee@example.com"],
               ["short", "a brand new password", "a brand new password"]) == 0
    db.refresh(admin)
    assert verify_password(admin.password_hash, "a brand new password")
    assert admin.must_change_password  # they choose their own at the next sign-in
    assert not sign_in_refused(db, "j.lee@example.com", "10.0.0.5")  # earlier failures no longer count
    event = db.scalar(select(AuditEvent).where(AuditEvent.action == "user.reset_password"))
    assert event.actor_id is None and event.detail["windows_user"] and event.detail["computer"]


def test_a_reset_does_not_switch_an_account_back_on(monkeypatch, db, make_user):
    make_user(Role.ADMIN, email="other.admin@example.com")
    departed = make_user(Role.REBATE_MAINTAINER, email="departed@example.com")
    departed.is_active = False
    db.flush()
    assert run(monkeypatch, db, ["reset-password", "departed@example.com"], ["a brand new password"] * 2) == 0
    db.refresh(departed)
    assert not departed.is_active


def test_an_unknown_email_changes_nothing(monkeypatch, db):
    assert run(monkeypatch, db, ["reset-password", "nobody@example.com"]) == 1
    assert db.query(AppUser).count() == 0


def test_create_admin_only_when_there_is_none(monkeypatch, db, make_user):
    typed = ["first@example.com", "First Admin", "1"]
    assert run(monkeypatch, db, ["create-admin"], ["a brand new password"] * 2, typed) == 0
    event = db.scalar(select(AuditEvent).where(AuditEvent.action == "user.create_administrator"))
    assert event.subject == "first@example.com" and event.detail["windows_user"]
    assert run(monkeypatch, db, ["create-admin"], [], ["second@example.com"]) == 1
    assert db.query(AppUser).count() == 1
