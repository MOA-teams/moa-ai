from typing import Self

from pydantic import Field, field_validator, model_validator

from app.schemas.common import ContractModel
from app.schemas.pipeline import Analysis
from app.schemas.routing import TeamRouting


class AnalysisReasons(ContractModel):
    category: str = Field(min_length=1, max_length=500)
    urgency: str = Field(min_length=1, max_length=500)
    information: str = Field(min_length=1, max_length=500)


class InquiryAnalysis(Analysis):
    """Internal analysis, including a provisional team recommendation."""

    summary: str = Field(min_length=1, max_length=1000)
    symptoms: list[str] = Field(max_length=10)
    needs_more_info: bool
    additional_question: str | None
    search_query: str | None
    reasons: AnalysisReasons
    limitations: list[str] = Field(default_factory=list, max_length=10)
    routing: TeamRouting | None = None

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
