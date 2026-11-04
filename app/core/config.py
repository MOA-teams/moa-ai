from functools import lru_cache

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MOA_", env_file=".env", extra="ignore")

    internal_token: SecretStr = SecretStr("")
    allowed_tenants: str = "syncnote"
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_timeout_ms: int = Field(default=30_000, ge=1000, le=120_000)
    analysis_max_input_chars: int = Field(default=30_000, ge=1000, le=200_000)
    gemini_embedding_model: str = "gemini-embedding-001"
    embedding_dimensions: int = Field(default=768, ge=128, le=3072)
    chroma_path: str = ".data/chroma"
    rag_enabled: bool = False
    enable_preview: bool = False
    rag_min_score: float = Field(default=0.5, ge=-1, le=1)
    backend_base_url: AnyHttpUrl = AnyHttpUrl("http://localhost:8000")
    backend_service_token: SecretStr = SecretStr("")

    @property
    def tenant_ids(self) -> set[str]:
        return {part.strip() for part in self.allowed_tenants.split(",") if part.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
