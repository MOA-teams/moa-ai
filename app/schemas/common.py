from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


TenantID = Annotated[str, Field(pattern=r"^[a-z]+$")]
InquiryID = Annotated[str, Field(pattern=r"^Q-\d{4}$")]


class Category(StrEnum):
    USAGE = "이용 방법"
    ACCOUNT = "계정"
    PAYMENT = "결제"
    BUG = "기능 오류"
    REQUEST = "개선 요청"
    OTHER = "기타"


class Urgency(StrEnum):
    HIGH = "높음"
    NORMAL = "보통"
    LOW = "낮음"


class Sender(StrEnum):
    CUSTOMER = "customer"
    AGENT = "agent"
    AUTO = "ai_auto"


class DraftResult(StrEnum):
    READY = "초안 가능"
    MANUAL = "직접 확인 필요"
    NEED_INFO = "추가 질문 필요"


class RunStatus(StrEnum):
    QUEUED = "대기"
    RUNNING = "처리 중"
    COMPLETED = "완료"
    FAILED = "실패"


class CheckStatus(StrEnum):
    PASS = "통과"
    FAIL = "실패"
    UNKNOWN = "확인 불가"
    SKIPPED = "미실행"


class DocEvidence(ContractModel):
    doc_id: str = Field(pattern=r"^DOC-\d{3}$")
    version: int = Field(ge=1)


class CodeEvidence(ContractModel):
    path: str = Field(min_length=1)
    commit_sha: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{7,40}$")

    @field_validator("path")
    @classmethod
    def relative_repository_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "\\" in value:
            raise ValueError("Use a repository-relative POSIX path")
        return value


class Evidence(ContractModel):
    docs: list[DocEvidence] = Field(default_factory=list)
    code: list[CodeEvidence] = Field(default_factory=list)


class ErrorDetail(ContractModel):
    code: str
    message: str
    retryable: bool = False


class ErrorResponse(ContractModel):
    error: ErrorDetail
