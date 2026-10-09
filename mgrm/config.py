"""Configuration, read from environment variables or the .env file (spec §3.2).

Business logic never reads files or the environment directly; it receives a
Settings object.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8-sig", extra="ignore")

    database_url: str
    test_database_url: str | None = None
    secret_key: str = Field(min_length=32)
    session_https_only: bool = False
    # The names the platform answers to. Add the server's name when it is hosted, e.g.
    # ALLOWED_HOSTS=["localhost","127.0.0.1","rebates.macgeargroup.com"]. Any name other than
    # this computer's needs SESSION_HTTPS_ONLY=true, or the platform refuses to start.
    allowed_hosts: list[str] = ["localhost", "127.0.0.1"]
    # The HTTPS proxy in front of the platform, whose word is taken for the caller's network address.
    trusted_proxies: str = "127.0.0.1"

    entra_tenant_id: str = ""
    entra_client_id: str = ""
    entra_client_secret: str = ""

    @property
    def microsoft_sign_in_enabled(self) -> bool:
        return bool(self.entra_tenant_id and self.entra_client_id and self.entra_client_secret)


LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def deployment_problem(settings: Settings) -> str | None:
    """Why these settings are unsafe to serve with, or None."""
    remote = [host for host in settings.allowed_hosts if host not in LOCAL_HOSTS]
    if remote and not settings.session_https_only:
        return (f"ALLOWED_HOSTS includes {', '.join(remote)}, so people will reach the platform over the network, "
                "but SESSION_HTTPS_ONLY is not true. Serve it through the HTTPS proxy and set SESSION_HTTPS_ONLY=true.")
    return None


@lru_cache
def get_settings() -> Settings:
    return Settings()
