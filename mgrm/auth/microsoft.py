"""Microsoft 365 (Entra ID) sign-in.

Switched on only when ENTRA_TENANT_ID, ENTRA_CLIENT_ID and ENTRA_CLIENT_SECRET
are all set. Microsoft proves who someone is; the platform decides whether they
may come in. Only people the administrator has already added, with sign-in
method "microsoft", are admitted. Nobody is created automatically.

People are recognised by their permanent Microsoft object id (`oid`), not their
email address, which can change or be reused. The first Microsoft sign-in links
the account the administrator added, by email; after that only the id counts.
Guest accounts from other organisations are refused even though they appear in
MacGear's directory.
"""

from authlib.integrations.starlette_client import OAuth
from sqlalchemy import select
from sqlalchemy.orm import Session

from mgrm.auth.users import audit
from mgrm.config import Settings
from mgrm.models import AppUser, AuthMethod

GUEST_ACCOUNT = 1  # the optional `acct` claim: 0 member, 1 guest


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


def is_guest(claims: dict, tenant_id: str) -> bool:
    """A guest carries MacGear's tenant id but was signed in by its own organisation (`idp`)."""
    if claims.get("acct") == GUEST_ACCOUNT:
        return True
    idp = claims.get("idp")
    return bool(idp) and tenant_id not in str(idp)


def resolve_microsoft_user(session: Session, claims: dict, tenant_id: str) -> AppUser | None:
    """The active platform user these Microsoft claims belong to, or None to refuse entry."""
    if claims.get("tid") != tenant_id or is_guest(claims, tenant_id):
        return None  # another organisation's account, or a guest in ours
    object_id = claims.get("oid")
    if not object_id:
        return None
    user = session.scalar(select(AppUser).where(AppUser.entra_object_id == object_id))
    linking = user is None
    if linking:
        email = email_from_claims(claims)
        if email is None:
            return None
        user = session.scalar(select(AppUser).where(AppUser.email == email, AppUser.entra_object_id.is_(None)))
    if user is None or not user.is_active or user.auth_method is not AuthMethod.MICROSOFT:
        return None
    if linking:
        user.entra_object_id = object_id
        audit(session, user, "user.microsoft_linked", user.email)
    return user
