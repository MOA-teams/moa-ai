import os
from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MOA_", env_file=".env", extra="ignore")

    internal_token: SecretStr = SecretStr("")
    allowed_tenants: str = "syncnote"
    llm_provider: Literal["auto", "bedrock", "gemini"] = Field(
        default="auto",
        validation_alias=AliasChoices("MOA_LLM_PROVIDER", "LLM_PROVIDER"),
    )
    aws_region: str = Field(
        default="ap-northeast-1",
        validation_alias=AliasChoices("MOA_AWS_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"),
    )
    bedrock_model_id: str = Field(
        default="global.anthropic.claude-haiku-4-5-20251001-v1:0",
        validation_alias=AliasChoices("MOA_BEDROCK_MODEL_ID", "BEDROCK_MODEL_ID"),
    )
    bedrock_embedding_model: str = Field(
        default="amazon.titan-embed-text-v2:0",
        validation_alias=AliasChoices(
            "MOA_BEDROCK_EMBEDDING_MODEL",
            "BEDROCK_EMBEDDING_MODEL",
        ),
    )
    bedrock_max_tokens: int = Field(
        default=2048,
        ge=256,
        le=8192,
        validation_alias=AliasChoices("MOA_BEDROCK_MAX_TOKENS", "BEDROCK_MAX_TOKENS"),
    )
    bedrock_bearer_token: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices(
            "MOA_BEDROCK_API_KEY",
            "AWS_BEARER_TOKEN_BEDROCK",
        ),
    )
    bedrock_ssh_host: str = Field(
        default="",
        validation_alias=AliasChoices("MOA_BEDROCK_SSH_HOST", "BEDROCK_SSH_HOST"),
    )
    bedrock_ssh_user: str = Field(
        default="ubuntu",
        validation_alias=AliasChoices("MOA_BEDROCK_SSH_USER", "BEDROCK_SSH_USER"),
    )
    bedrock_ssh_key_path: str = Field(
        default="",
        validation_alias=AliasChoices("MOA_BEDROCK_SSH_KEY_PATH", "BEDROCK_SSH_KEY_PATH"),
    )
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_fallback_models: str = (
        "gemini-3-flash-preview,gemini-3.5-flash-lite,gemini-3.1-flash-lite,"
        "gemini-3.1-flash-lite-preview,gemini-flash-lite-latest,gemini-3.5-flash,gemini-3.6-flash"
    )
    gemini_max_retries: int = Field(default=2, ge=1, le=5)
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

    @property
    def has_bedrock_credentials(self) -> bool:
        return bool(
            self.bedrock_bearer_token.get_secret_value().strip()
            or (self.bedrock_ssh_host.strip() and self.bedrock_ssh_key_path.strip())
            or os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
            or os.environ.get("AWS_ACCESS_KEY_ID")
            or os.environ.get("AWS_PROFILE")
            or os.environ.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
            or os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE")
        )

    @property
    def candidate_api_keys(self) -> list[str]:
        raw = self.gemini_api_key.get_secret_value()
        keys: list[str] = []
        for part in raw.split(","):
            item = part.strip()
            if item and item not in keys:
                keys.append(item)
        return keys

    @property
    def candidate_models(self) -> list[str]:
        models: list[str] = []
        for raw in [self.gemini_model, *self.gemini_fallback_models.split(",")]:
            item = raw.strip()
            if item and item not in models:
                models.append(item)
        return models


@lru_cache
def get_settings() -> Settings:
    return Settings()
