"""Command line: python -m mgrm <command>

    migrate                  bring the database structure up to date
    reset-password           set a new password for a locked-out user (on this computer only)
    create-admin             add an administrator (first set-up, or recovery if locked out)
    serve                    start the rebate master at http://localhost:8001
    load                     load a NetSuite register CSV export (customers or classes)
    load-rebates             first load of agreements from the legacy reconciliation workbooks
    import-from-forecasting  one-time move of the registers and rebate master out of the forecasting platform
"""

import argparse
import getpass
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_PORT = 8001  # the forecasting platform uses 8000


def _session_factory():
    from mgrm.config import get_settings
    from mgrm.db import make_engine, make_session_factory

    return make_session_factory(make_engine(get_settings().database_url))


def _user(session, email: str):
    from sqlalchemy import select

    from mgrm.models import AppUser

    user = session.scalar(select(AppUser).where(AppUser.email == email.strip().lower()))
    if user is None or not user.is_active:
        raise SystemExit(f"No active user {email} in the rebate master. The command records who did it.")
    return user


def migrate(_args) -> int:
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(PROJECT_ROOT / "alembic.ini")), "head")
    print("Database structure is up to date.")
    return 0


def create_admin(_args) -> int:
    from mgrm.auth.passwords import password_problem
    from mgrm.auth.roles import Role
    from mgrm.auth.users import UserError, create_user
    from mgrm.models import AuthMethod

    email = input("Email: ").strip()
    name = input("Name as shown in the platform: ").strip()
    method = input("Sign in with (1) a platform password or (2) Microsoft 365? [1]: ").strip() or "1"
    auth_method = AuthMethod.MICROSOFT if method == "2" else AuthMethod.LOCAL
    password = None
    if auth_method is AuthMethod.LOCAL:
        while True:
            password = getpass.getpass("Password (at least 12 characters; nothing shows as you type): ")
            problem = password_problem(password)
            if problem:
                print(problem)
                continue
            if getpass.getpass("Same password again: ") != password:
                print("They do not match. Try again.")
                continue
            break
    with _session_factory().begin() as session:
        try:
            user = create_user(session, actor=None, email=email, display_name=name, role=Role.ADMIN,
                               auth_method=auth_method, password=password)
        except UserError as exc:
            print(f"Not created: {exc}")
            return 1
    print(f"Administrator {user.email} created.")
    return 0


def serve(args) -> int:
    import uvicorn

    uvicorn.run("mgrm.web.app:create_app", factory=True, host=args.host, port=args.port, reload=args.reload)
    return 0


def load_file(args) -> int:
    from mgrm.data.errors import AlreadyLoaded, LoadRejected
    from mgrm.data.registers import LOADERS, FileInput
    from mgrm.models import LoadKind

    with _session_factory()() as session:
        actor = _user(session, args.as_user)
        try:
            batch = LOADERS[LoadKind(args.kind)](session, FileInput.from_path(Path(args.path)), actor)
            session.commit()
        except AlreadyLoaded as exc:
            print(f"SKIPPED {exc}")
            return 0
        except LoadRejected as exc:
            session.rollback()
            print(exc.report())
            return 1
        print(f"LOADED  {batch.file_name} as {batch.kind.value}: {batch.row_count} rows (load {batch.id}) {batch.summary}")
    return 0


def load_rebates(args) -> int:
    from mgrm.data.errors import AlreadyLoaded, LoadRejected
    from mgrm.data.rebate_workbooks import load_workbook

    failed = 0
    for path in args.paths:
        with _session_factory()() as session:
            actor = _user(session, args.as_user)
            try:
                batch = load_workbook(session, Path(path), actor)
                session.commit()
                s = batch.summary
                print(f"LOADED  {batch.file_name}: {s['tabs']} tabs, {s['agreements']} agreements, "
                      f"{s['proposed_rates']} proposed rates ({s['flagged_rates']} with flags for the reviewer)")
            except AlreadyLoaded as exc:
                print(f"SKIPPED {exc}")
            except LoadRejected as exc:
                session.rollback()
                failed += 1
                print(exc.report())
    return 1 if failed else 0


def import_from_forecasting(args) -> int:
    from mgrm.data.forecasting_import import import_all

    report = import_all(source_url=args.source_url, target_factory=_session_factory())
    print(report)
    return 0 if report.verified else 1


def reset_password(args) -> int:
    """For someone locked out: needs access to this computer, which is the proof of identity here."""
    from sqlalchemy import select

    from mgrm.auth.passwords import password_problem
    from mgrm.auth.users import UserError, audit, set_password
    from mgrm.models import AppUser, AuthMethod

    with _session_factory().begin() as session:
        user = session.scalar(select(AppUser).where(AppUser.email == args.email.strip().lower()))
        if user is None:
            print(f"No user {args.email}.")
            return 1
        if user.auth_method is not AuthMethod.LOCAL:
            print(f"{user.email} signs in with Microsoft 365; reset the password in Microsoft instead.")
            return 1
        while True:
            password = getpass.getpass("New password (at least 12 characters; nothing shows as you type): ")
            problem = password_problem(password)
            if problem:
                print(problem)
                continue
            if getpass.getpass("Same password again: ") != password:
                print("They do not match. Try again.")
                continue
            break
        try:
            set_password(session, actor=user, user=user, password=password)
        except UserError as exc:
            print(f"Not changed: {exc}")
            return 1
        if not user.is_active:
            user.is_active = True
            audit(session, user, "user.activate", user.email, via="command line password reset")
        audit(session, user, "user.reset_password", user.email, via="command line on this computer")
    print(f"Password changed for {user.email}. Sign in with it now.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mgrm", description="MacGear Rebate Master")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="bring the database structure up to date").set_defaults(fn=migrate)
    sub.add_parser("create-admin", help="add an administrator").set_defaults(fn=create_admin)
    serve_parser = sub.add_parser("serve", help="start the rebate master")
    serve_parser.add_argument("--host", default="127.0.0.1", help="127.0.0.1 keeps it on this computer only")
    serve_parser.add_argument("--port", type=int, default=LOCAL_PORT)
    serve_parser.add_argument("--reload", action="store_true", help="restart automatically when code changes")
    serve_parser.set_defaults(fn=serve)
    file_parser = sub.add_parser("load", help="load a NetSuite register CSV export")
    file_parser.add_argument("kind", choices=["customers", "classes"])
    file_parser.add_argument("path")
    file_parser.add_argument("--as", dest="as_user", required=True, help="email of the platform user loading it")
    file_parser.set_defaults(fn=load_file)
    rebate_parser = sub.add_parser("load-rebates", help="first load from the legacy reconciliation workbooks")
    rebate_parser.add_argument("paths", nargs="+")
    rebate_parser.add_argument("--as", dest="as_user", required=True, help="email of the platform user entering the rates")
    rebate_parser.set_defaults(fn=load_rebates)
    import_parser = sub.add_parser("import-from-forecasting", help="one-time move out of the forecasting platform")
    import_parser.add_argument("--source-url", required=True, help="the forecasting platform's DATABASE_URL")
    import_parser.set_defaults(fn=import_from_forecasting)
    reset_parser = sub.add_parser("reset-password", help="set a new password for a locked-out user")
    reset_parser.add_argument("email")
    reset_parser.set_defaults(fn=reset_password)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
