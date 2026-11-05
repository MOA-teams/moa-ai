from functools import lru_cache

from pydantic import AliasChoices, AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MOA_", env_file=".env", extra="ignore")

    internal_token: SecretStr = SecretStr("")
    allowed_tenants: str = "syncnote"
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
    bedrock_timeout_ms: int = Field(default=30_000, ge=1000, le=120_000)
    analysis_max_input_chars: int = Field(default=30_000, ge=1000, le=200_000)
    embedding_dimensions: int = 1024
    chroma_path: str = ".data/chroma"
    rag_enabled: bool = False
    enable_preview: bool = False
    rag_min_score: float = Field(default=0.5, ge=-1, le=1)
    backend_base_url: AnyHttpUrl = AnyHttpUrl("http://localhost:8000")
    backend_service_token: SecretStr = SecretStr("")
    backend_include_routing: bool = False

    @field_validator("embedding_dimensions")
    @classmethod
    def validate_titan_dimensions(cls, value: int) -> int:
        if value not in {256, 512, 1024}:
            raise ValueError("Titan V2 supports 256, 512 or 1024 dimensions")
        return value

    @property
    def tenant_ids(self) -> set[str]:
        return {part.strip() for part in self.allowed_tenants.split(",") if part.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
