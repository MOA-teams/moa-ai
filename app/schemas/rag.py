from typing import Any, Literal, Self

from pydantic import Field, model_validator

from app.schemas.common import ContractModel, Evidence, TenantID

SourceType = Literal["문서", "서비스 정보", "우수 답변", "코드", "커밋", "GitHub 이슈", "문의 요약"]
SourceStatus = Literal["유효", "폐기", "재검토 필요", "비활성"]


class SourceInput(ContractModel):
    tenant_id: TenantID
    source_type: SourceType
    source_id: str = Field(min_length=1, max_length=200)
    version: int | str
    source_revision: int = Field(default=1, ge=1)
    title: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=200_000)
    visibility: Literal["고객 공개", "내부 전용"]
    status: SourceStatus
    review_required: bool = False
    content_hash: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    evidence: Evidence = Field(default_factory=Evidence)

    @model_validator(mode="after")
    def check_source(self) -> Self:
        if isinstance(self.version, int) and self.version < 1:
            raise ValueError("Version must be positive")
        if isinstance(self.version, str) and not self.version.strip():
            raise ValueError("Version is required")
        if self.source_type == "문서":
            import re

            if not re.fullmatch(r"DOC-\d{3}", self.source_id):
                raise ValueError("Document source_id must match DOC-000")
            if not isinstance(self.version, int):
                raise ValueError("Document version must be an integer")
        if self.source_type == "코드" and self.visibility != "내부 전용":
            raise ValueError("Code must remain internal")
        if self.source_type == "문서" and self.status not in {"유효", "폐기"}:
            raise ValueError("Document status must match InterfaceRule")
        return self


class SourceAccepted(ContractModel):
    source_id: str
    version: int | str
    indexing_status: Literal["대기", "처리 중", "완료", "실패"]


class SourceStatusChange(ContractModel):
    source_type: SourceType
    source_id: str
    version: int | str
    source_revision: int = Field(ge=1)
    status: SourceStatus
    review_required: bool

    @model_validator(mode="after")
    def check_document_status(self) -> Self:
        if self.source_type == "문서" and self.status not in {"유효", "폐기"}:
            raise ValueError("Document status must match InterfaceRule")
        return self


class SourceStatusBatch(ContractModel):
    tenant_id: TenantID
    event_id: str
    sources: list[SourceStatusChange] = Field(min_length=1)
    reason: str


class SourceStatusResult(ContractModel):
    knowledge_revision: int
    updated_count: int
    search_excluded: bool


class SourceView(SourceAccepted):
    source_revision: int
    status: SourceStatus
    review_required: bool
    searchable: bool
    error: str | None = None


class TextChunk(ContractModel):
    chunk_id: str
    text: str
    start: int
    end: int


class SearchRequest(ContractModel):
    tenant_id: TenantID
    query: str = Field(min_length=1, max_length=3000)
    purpose: Literal["고객 답변", "내부 조사"] = "고객 답변"
    top_k: int = Field(default=5, ge=1, le=20)
    min_score: float = Field(default=0.5, ge=-1, le=1)


class SearchHit(ContractModel):
    source_type: SourceType
    source_id: str
    version: int | str
    source_revision: int
    chunk_id: str
    title: str
    text: str
    score: float
    visibility: Literal["고객 공개", "내부 전용"]
    evidence: Evidence


class SearchResult(ContractModel):
    knowledge_revision: int
    facts: list[SearchHit] = Field(default_factory=list)
    style_examples: list[SearchHit] = Field(default_factory=list)
    internal_findings: list[SearchHit] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
