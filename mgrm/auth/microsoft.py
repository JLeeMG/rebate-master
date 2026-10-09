"""Microsoft 365 (Entra ID) sign-in.

Switched on only when ENTRA_TENANT_ID, ENTRA_CLIENT_ID and ENTRA_CLIENT_SECRET
are all set. Microsoft proves who someone is; the platform decides whether they
may come in. Only people the administrator has already added, with sign-in
method "microsoft", are admitted. Nobody is created automatically.
"""

from authlib.integrations.starlette_client import OAuth
from sqlalchemy import select
from sqlalchemy.orm import Session

from mgrm.config import Settings
from mgrm.models import AppUser, AuthMethod


def build_oauth(settings: Settings) -> OAuth | None:
    if not settings.microsoft_sign_in_enabled:
        return None
    oauth = OAuth()
    oauth.register(
        name="microsoft",
        client_id=settings.entra_client_id,
        client_secret=settings.entra_client_secret,
        server_metadata_url=(
            f"https://login.microsoftonline.com/{settings.entra_tenant_id}/v2.0/.well-known/openid-configuration"
        ),
        client_kwargs={"scope": "openid email profile"},
    )
    return oauth


def email_from_claims(claims: dict) -> str | None:
    """Work accounts carry the address in `email` or, failing that, `preferred_username`."""
    for key in ("email", "preferred_username"):
        value = claims.get(key)
        if value and "@" in value:
            return value.strip().lower()
    return None


def resolve_microsoft_user(session: Session, claims: dict, tenant_id: str) -> AppUser | None:
    """The active platform user these Microsoft claims belong to, or None to refuse entry."""
    if claims.get("tid") != tenant_id:
        return None  # someone signed in with an account from another organisation
    email = email_from_claims(claims)
    if email is None:
        return None
    user = session.scalar(select(AppUser).where(AppUser.email == email))
    if user is None or not user.is_active or user.auth_method is not AuthMethod.MICROSOFT:
        return None
    return user
