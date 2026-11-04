from functools import lru_cache

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MOA_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://moa:moa_local@localhost:5432/moa"
    internal_token: SecretStr = SecretStr("")
    allowed_tenants: str = "syncnote"
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = ""
    gemini_timeout_ms: int = Field(default=30_000, ge=1000, le=120_000)
    backend_base_url: AnyHttpUrl = AnyHttpUrl("http://localhost:8000")
    backend_service_token: SecretStr = SecretStr("")

    @field_validator("database_url")
    @classmethod
    def postgres_only(cls, value: str) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("database_url must use postgresql+asyncpg")
        return value

    @property
    def tenant_ids(self) -> set[str]:
        return {part.strip() for part in self.allowed_tenants.split(",") if part.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
