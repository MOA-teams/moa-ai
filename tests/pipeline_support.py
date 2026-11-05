import pytest

from app.schemas.analysis import InquiryAnalysis
from app.schemas.rag import SourceInput
from app.services.pipeline.analyzer import InquiryAnalyzer
from app.services.pipeline.drafter import (
    DraftGenerationOutput,
    DraftGenerator,
    EvidenceEvaluationOutput,
    EvidenceValidator,
)
from app.services.pipeline.orchestrator import PipelineOrchestrator
from app.services.pipeline.reviewer import DraftReviewer, ReviewLLMOutput
from app.services.pipeline.similar import SimilarInquiryFinder
from app.services.rag.chroma_store import ChromaVectorStore
from app.services.rag.service import RagService


class MultiEmbedder:
    space = "test-pipeline-embedding:3"

    async def documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    async def query(self, text: str) -> list[float]:
        return self._vec(text)

    @staticmethod
    def _vec(text: str) -> list[float]:
        if any(k in text for k in ("E-502", "E502", "저장", "오류", "E-413", "E-401")):
            return [1.0, 0.0, 0.0]
        if any(k in text for k in ("요금", "Pro", "Team", "9,900", "12,900", "14,900")):
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]


class SchemaDispatchLLM:
    """Dispatches stubbed outputs by schema type and tracks separate calls."""

    def __init__(self, responses: dict[type, dict]):
        self.responses = responses
        self.calls: list[tuple[type, str]] = []

    async def generate(self, *, instruction: str, data: str, schema: type):
        self.calls.append((schema, data))
        payload = self.responses[schema]
        if callable(payload):
            payload = payload(data)
        return schema.model_validate(payload)


@pytest.fixture
def rag_service(tmp_path):
    store = ChromaVectorStore(str(tmp_path / "chroma"))
    service = RagService(store, MultiEmbedder())
    yield service
    store.close()


def make_doc(
    source_id: str = "DOC-005",
    version: int = 1,
    title: str = "오류 코드 안내",
    text: str = "E-502는 노트 저장 처리 중 발생하는 오류입니다.",
    **kwargs,
) -> SourceInput:
    payload = {
        "tenant_id": "syncnote",
        "source_type": "문서",
        "source_id": source_id,
        "version": version,
        "source_revision": 1,
        "title": title,
        "text": text,
        "visibility": "고객 공개",
        "status": "유효",
        "review_required": False,
        "content_hash": f"sha256:{source_id}:{version}",
        "metadata": {"doc_type": "FAQ"},
        "evidence": {"docs": [], "code": []},
    }
    payload.update(kwargs)
    return SourceInput.model_validate(payload)


def default_responses(overrides: dict[type, dict] | None = None) -> dict[type, dict]:
    base: dict[type, dict] = {
        InquiryAnalysis: {
            "category": "기능 오류",
            "urgency": "높음",
            "summary": "재실행 후에도 저장 시 E-502 오류",
            "symptoms": ["저장 실패", "E-502 표시"],
            "normalized_error_code": "E-502",
            "missing_fields": [],
            "needs_more_info": False,
            "additional_question": None,
            "search_query": "저장 시 E-502 오류",
            "reasons": {
                "category": "저장 동작 실패",
                "urgency": "반복 저장 불가",
                "information": "오류 코드와 현상 확인됨",
            },
            "limitations": [],
        },
        EvidenceEvaluationOutput: {
            "relevant": True,
            "has_conflict": False,
            "sufficient_for_draft": True,
            "requires_followup_check": True,
            "selected_doc_ids": ["DOC-005"],
            "reasons": ["실제 장애 상태 확인 필요"],
        },
        DraftGenerationOutput: {
            "answer_text": (
                "저장 처리 중 E-502 오류가 발생한 것으로 보입니다. "
                "담당 부서에서 추가 확인이 필요합니다."
            ),
            "used_doc_ids": ["DOC-005"],
            "citations": [{"text": "저장 처리 중 오류", "doc_ids": ["DOC-005"]}],
            "used_style_ids": ["BA-001"],
        },
        ReviewLLMOutput: {
            "claims_supported": "통과",
            "internal_data_excluded": "통과",
            "source_conflict": "통과",
            "auto_response_scope": "확인 불가",
            "reasons": ["실제 장애 상태 확인 필요"],
            "unsupported_sentences": [],
        },
    }
    if overrides:
        base.update(overrides)
    return base


def make_orchestrator(llm: SchemaDispatchLLM, rag: RagService) -> PipelineOrchestrator:
    return PipelineOrchestrator(
        analyzer=InquiryAnalyzer(llm),
        rag=rag,
        similar_finder=SimilarInquiryFinder(rag, min_score=0.5),
        evidence_validator=EvidenceValidator(llm),
        draft_generator=DraftGenerator(llm),
        reviewer=DraftReviewer(llm),
        min_score=0.5,
    )
