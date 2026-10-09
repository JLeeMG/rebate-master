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

    entra_tenant_id: str = ""
    entra_client_id: str = ""
    entra_client_secret: str = ""

    @property
    def microsoft_sign_in_enabled(self) -> bool:
        return bool(self.entra_tenant_id and self.entra_client_id and self.entra_client_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()
