"""The web application. Start it with `python -m mgrm serve`."""

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from mgrm.auth.microsoft import build_oauth
from mgrm.auth.roles import ROLE_DESCRIPTIONS, Permission, Role, can
from mgrm.auth.users import audit
from mgrm.config import Settings, deployment_problem, get_settings
from mgrm.db import make_engine, make_session_factory, session_scope
from mgrm.rebates.scope import describe_text, parse_scope_safe
from mgrm.web.hardening import Hardening
from mgrm.web.security import (
    BadFormToken,
    NotPermitted,
    PasswordChangeRequired,
    SignInRequired,
    client_address,
    csrf_token,
)

HERE = Path(__file__).resolve().parent
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60  # signed out after a working day without a click
APP_NAME = "MacGear Rebate Master"
log = logging.getLogger("mgrm.security")

templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(can=can, Permission=Permission, Role=Role, ROLE_DESCRIPTIONS=ROLE_DESCRIPTIONS, APP_NAME=APP_NAME,
                             scope_text=describe_text, parse_scope_safe=parse_scope_safe)


def render(request: Request, name: str, status_code: int = 200, **context):
    context.setdefault("user", getattr(request.state, "user", None))
    context["csrf_token"] = csrf_token(request)
    context["microsoft_enabled"] = request.app.state.oauth is not None
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def record(request: Request, action: str, subject: str, **detail) -> None:
    """An audit entry written apart from the request's own work, which is being abandoned."""
    scope = request.app.state.db_session_scope()
    db = next(scope)
    audit(db, getattr(request.state, "user", None), action, subject, **detail)
    next(scope, None)  # commits


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    problem = deployment_problem(settings)
    if problem:
        raise RuntimeError(problem)
    app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None, openapi_url=None)

    factory = make_session_factory(make_engine(settings.database_url))
    app.state.settings = settings
    app.state.db_session_scope = lambda: session_scope(factory)
    app.state.oauth = build_oauth(settings)

    # Added innermost first: requests pass the host check, then hardening, then the session.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="mgrm_session",
        max_age=SESSION_MAX_AGE_SECONDS,
        same_site="lax",
        https_only=settings.session_https_only,
    )
    app.add_middleware(Hardening, https=settings.session_https_only)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.exception_handler(SignInRequired)
    async def _sign_in(request: Request, exc: SignInRequired):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(PasswordChangeRequired)
    async def _password(request: Request, exc: PasswordChangeRequired):
        return RedirectResponse("/account/password", status_code=303)

    @app.exception_handler(NotPermitted)
    async def _forbidden(request: Request, exc: NotPermitted):
        record(request, "access.refused", request.url.path, method=request.method, permission=exc.permission.value)
        return render(request, "message.html", status_code=403, title="Not available to your role",
                      message="Your role does not include this part of the rebate master. Ask the administrator if you need it.")

    @app.exception_handler(BadFormToken)
    async def _bad_token(request: Request, exc: BadFormToken):
        # Often just an old tab, and possibly not signed in, so the server log rather than the audit log.
        log.warning("Form without a valid token: %s %s from %s", request.method, request.url.path, client_address(request))
        return render(request, "message.html", status_code=400, title="Form expired",
                      message="That form was out of date, so nothing was changed. Go back, refresh the page and try again.")

    from mgrm.api import feed
    from mgrm.web import routes_accruals, routes_admin, routes_auth, routes_main, routes_rebates, routes_registers

    app.include_router(routes_auth.router)
    app.include_router(routes_main.router)
    app.include_router(routes_rebates.router)
    app.include_router(routes_accruals.router)
    app.include_router(routes_registers.router)
    app.include_router(routes_admin.router)
    app.include_router(feed.router)
    return app
