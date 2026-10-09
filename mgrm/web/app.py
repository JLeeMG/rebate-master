"""The web application. Start it with `python -m mgrm serve`."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from mgrm.auth.microsoft import build_oauth
from mgrm.auth.roles import ROLE_DESCRIPTIONS, Permission, Role, can
from mgrm.config import Settings, get_settings
from mgrm.db import make_engine, make_session_factory, session_scope
from mgrm.web.security import (
    BadFormToken,
    NotPermitted,
    PasswordChangeRequired,
    SignInRequired,
    SignInThrottle,
    csrf_token,
)

HERE = Path(__file__).resolve().parent
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60  # signed out after a working day
APP_NAME = "MacGear Rebate Master"

templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(can=can, Permission=Permission, Role=Role, ROLE_DESCRIPTIONS=ROLE_DESCRIPTIONS, APP_NAME=APP_NAME)


def render(request: Request, name: str, status_code: int = 200, **context):
    context.setdefault("user", getattr(request.state, "user", None))
    context["csrf_token"] = csrf_token(request)
    context["microsoft_enabled"] = request.app.state.oauth is not None
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None, openapi_url=None)

    factory = make_session_factory(make_engine(settings.database_url))
    app.state.settings = settings
    app.state.db_session_scope = lambda: session_scope(factory)
    app.state.oauth = build_oauth(settings)
    app.state.throttle = SignInThrottle()

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="mgrm_session",
        max_age=SESSION_MAX_AGE_SECONDS,
        same_site="lax",
        https_only=settings.session_https_only,
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.exception_handler(SignInRequired)
    async def _sign_in(request: Request, exc: SignInRequired):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(PasswordChangeRequired)
    async def _password(request: Request, exc: PasswordChangeRequired):
        return RedirectResponse("/account/password", status_code=303)

    @app.exception_handler(NotPermitted)
    async def _forbidden(request: Request, exc: NotPermitted):
        return render(request, "message.html", status_code=403, title="Not available to your role",
                      message="Your role does not include this part of the rebate master. Ask the administrator if you need it.")

    @app.exception_handler(BadFormToken)
    async def _bad_token(request: Request, exc: BadFormToken):
        return render(request, "message.html", status_code=400, title="Form expired",
                      message="That form was out of date, so nothing was changed. Go back, refresh the page and try again.")

    from mgrm.api import feed
    from mgrm.web import routes_admin, routes_auth, routes_main, routes_rebates, routes_registers

    app.include_router(routes_auth.router)
    app.include_router(routes_main.router)
    app.include_router(routes_rebates.router)
    app.include_router(routes_registers.router)
    app.include_router(routes_admin.router)
    app.include_router(feed.router)
    return app
