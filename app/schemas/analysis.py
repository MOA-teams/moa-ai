from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from app.schemas.common import ContractModel
from app.schemas.pipeline import Analysis
from app.schemas.routing import TeamRouting

MissingField = Literal["발생 화면", "재현 단계", "발생 환경", "앱 버전", "오류 코드"]
MISSING_FIELD_ALIASES = {
    "구체적인 기능/화면": "발생 화면",
    "기능/화면": "발생 화면",
    "대상 기능": "발생 화면",
    "화면": "발생 화면",
    "screen": "발생 화면",
    "증상 설명": "재현 단계",
    "증상": "재현 단계",
    "재현 방법": "재현 단계",
    "steps_to_reproduce": "재현 단계",
    "환경": "발생 환경",
    "운영체제": "발생 환경",
    "os": "발생 환경",
    "app_version": "앱 버전",
    "오류 코드 또는 메시지": "오류 코드",
    "오류 메시지": "오류 코드",
    "error_code": "오류 코드",
}


class AnalysisReasons(ContractModel):
    category: str = Field(min_length=1, max_length=500)
    urgency: str = Field(min_length=1, max_length=500)
    information: str = Field(min_length=1, max_length=500)


class InquiryAnalysis(Analysis):
    """Internal analysis, including a provisional team recommendation."""

    summary: str = Field(min_length=1, max_length=1000)
    missing_fields: list[MissingField] = Field(default_factory=list, max_length=3)
    symptoms: list[str] = Field(max_length=10)
    needs_more_info: bool
    additional_question: str | None
    search_query: str | None
    reasons: AnalysisReasons
    limitations: list[str] = Field(default_factory=list, max_length=10)
    routing: TeamRouting | None = None
    has_action_context: bool | None = None
    context_evidence: str | None = Field(default=None, max_length=1000)
    requires_manual_review: bool = False

    @field_validator("missing_fields", mode="before")
    @classmethod
    def normalize_question_fields(cls, value):
        if not isinstance(value, list):
            return value
        result = []
        for item in value:
            canonical = (
                MISSING_FIELD_ALIASES.get(item.strip(), item.strip())
                if isinstance(item, str)
                else item
            )
            if canonical not in result:
                result.append(canonical)
        return result

    @field_validator("additional_question", "search_query")
    @classmethod
    def reject_blank_strings(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Use null rather than an empty string")
        return value

    @model_validator(mode="after")
    def consistent_information_branch(self) -> Self:
        if self.needs_more_info:
            if not self.missing_fields or self.additional_question is None:
                raise ValueError("Missing information requires fields and an additional question")
            if self.search_query is not None:
                raise ValueError("Wait for additional information before searching")
        elif (
            self.missing_fields or self.additional_question is not None or self.search_query is None
        ):
            raise ValueError("Searchable inquiries require a query and no additional question")
        if self.additional_question is not None and len(self.additional_question) > 500:
            raise ValueError("Additional question is too long")
        if self.search_query is not None and len(self.search_query) > 1000:
            raise ValueError("Search query is too long")
        return self
