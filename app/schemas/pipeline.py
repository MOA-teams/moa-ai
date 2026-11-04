from typing import Self

from pydantic import AwareDatetime, Field, model_validator

from app.schemas.common import (
    Category,
    CheckStatus,
    ContractModel,
    DocEvidence,
    DraftResult,
    ErrorDetail,
    Evidence,
    InquiryID,
    RunStatus,
    Sender,
    TenantID,
    Urgency,
)


class Message(ContractModel):
    message_id: str = Field(min_length=1)
    sender: Sender
    text: str = Field(min_length=1, max_length=50_000)
    sent_at: AwareDatetime
    attachments: list[str] = Field(default_factory=list)
    answer_id: str | None = None


class Environment(ContractModel):
    os: str | None = None
    app_version: str | None = None
    error_code: str | None = None


class PipelineRequest(ContractModel):
    tenant_id: TenantID
    inquiry_id: InquiryID
    inquiry_revision: int = Field(ge=1)
    based_on_message_id: str = Field(min_length=1)
    customer_category: Category
    messages: list[Message] = Field(min_length=1, max_length=200)
    env: Environment = Field(default_factory=Environment)
    review_only: bool = False

    @model_validator(mode="after")
    def check_snapshot(self) -> Self:
        ids = [message.message_id for message in self.messages]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate message IDs")
        customers = [message for message in self.messages if message.sender == Sender.CUSTOMER]
        if not customers or customers[-1].message_id != self.based_on_message_id:
            raise ValueError("based_on_message_id must reference the latest customer message")
        return self


class PipelineAccepted(ContractModel):
    run_id: str
    inquiry_id: InquiryID
    inquiry_revision: int
    run_status: RunStatus = RunStatus.QUEUED


class Analysis(ContractModel):
    category: Category
    urgency: Urgency
    summary: str
    normalized_error_code: str | None = None
    missing_fields: list[str] = Field(default_factory=list)


class SimilarInquiry(ContractModel):
    inquiry_id: InquiryID
    group_id: str | None = None
    score: float = Field(ge=0, le=1)


class SimilarInquiries(ContractModel):
    items: list[SimilarInquiry] = Field(default_factory=list)
    suggested_group_id: str | None = None
    reason: str | None = None


class Citation(ContractModel):
    text: str
    docs: list[DocEvidence] = Field(default_factory=list)


class StyleReference(ContractModel):
    best_id: str = Field(pattern=r"^BA-\d{3}$")
    version: int = Field(ge=1)


class Draft(ContractModel):
    answer_text: str
    evidence: Evidence
    citations: list[Citation] = Field(default_factory=list)
    style_references: list[StyleReference] = Field(default_factory=list)


class Verification(ContractModel):
    status: CheckStatus
    checks: dict[str, CheckStatus] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    unsupported_sentences: list[str] = Field(default_factory=list)


class PipelineResult(ContractModel):
    run_id: str
    tenant_id: TenantID
    inquiry_id: InquiryID
    inquiry_revision: int
    based_on_message_id: str
    run_status: RunStatus
    current_stage: str | None = None
    knowledge_revision: int | None = None
    analysis: Analysis | None = None
    similar_inquiries: SimilarInquiries | None = None
    draft_result: DraftResult | None = None
    additional_question: str | None = None
    draft: Draft | None = None
    verification: Verification | None = None
    error: ErrorDetail | None = None
