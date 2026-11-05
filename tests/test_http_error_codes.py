import json

import pytest
from pipeline_support import SchemaDispatchLLM, default_responses, make_doc, make_orchestrator
from pipeline_support import rag_service as rag_service
from test_analysis import StubLLM, searchable_result

from app.core.errors import ServiceError
from app.schemas.analysis import InquiryAnalysis
from app.schemas.pipeline import PipelineRequest
from app.schemas.rag import SearchHit, SearchResult
from app.services.pipeline.analyzer import InquiryAnalyzer, observed_error_codes
from app.services.pipeline.drafter import (
    DraftGenerationOutput,
    EvidenceEvaluationOutput,
    EvidenceValidator,
)
from app.services.pipeline.error_codes import extract_error_codes, resolve_reported_code
from app.services.pipeline.orchestrator import PipelineOrchestrator
from app.services.pipeline.similar import SimilarInquiryFinder


@pytest.mark.parametrize(
    "text,expected",
    [
        ("404에러가 나면서 첫 페이지가 안 떠요", {"HTTP-404"}),
        ("크롬에서 첫 페이지를 열면 404 오류가 나요.", {"HTTP-404"}),
        ("로그인 버튼을 누르면 502 Internal Server Error 팝업이 떠요", {"HTTP-502"}),
        ("페이지를 열면 502 Internal Server Error가 떠요.", {"HTTP-502"}),
        ("404 Not Found라고 나와요", {"HTTP-404"}),
        ("상태 코드가 404입니다", {"HTTP-404"}),
        ("HTTP 상태 코드: 401", {"HTTP-401"}),
        ("HTTP/1.1 404 Not Found", {"HTTP-404"}),
        ("HTTP/2 503", {"HTTP-503"}),
        ("HTTP_STATUS_429", {"HTTP-429"}),
        ("HTTP-403 Forbidden", {"HTTP-403"}),
        ("http status code 500", {"HTTP-500"}),
        ("error code: 429", {"HTTP-429"}),
        ("Error 404", {"HTTP-404"}),
        ("404 Not Found", {"HTTP-404"}),
        ("오류 코드 503", {"HTTP-503"}),
        ("404가 떠요", {"HTTP-404"}),
        ("E-502와 HTTP 502 오류가 서로 다릅니다.", {"E-502", "HTTP-502"}),
        ("e502 E-PAY-07 E-STORAGE-502", {"E-502", "E-PAY-07", "E-STORAGE-502"}),
        ("Pro 월 500원, 404원 할인, 401명, 2026-11-05, 버전 2.4.0", set()),
        ("500원 결제 오류와 404개 파일 업로드 문제", set()),
        ("HTTP 999", set()),
        ("999 오류", set()),
    ],
)
def test_extracts_reported_http_context_without_confusing_service_codes_or_numbers(text, expected):
    assert extract_error_codes(text) == expected


@pytest.mark.parametrize("model_code", ["404", "HTTP 404", "HTTP-404", "E-404"])
async def test_followup_http_code_is_grounded_and_normalized(pipeline_request, model_code):
    pipeline_request["messages"][0]["text"] = "앱이 안 돼요."
    pipeline_request["messages"].extend(
        [
            dict(
                message_id="MSG-2",
                sender="agent",
                text="어떤 오류가 나타나나요?",
                sent_at="2026-11-05T17:37:00+09:00",
            ),
            dict(
                message_id="MSG-3",
                sender="customer",
                text="크롬에서 첫 페이지를 열면 404 오류가 나요.",
                sent_at="2026-11-05T17:38:00+09:00",
            ),
        ]
    )
    pipeline_request["based_on_message_id"] = "MSG-3"
    llm = StubLLM(
        searchable_result(normalized_error_code=model_code, search_query="첫 페이지 404 오류")
    )
    result = await InquiryAnalyzer(llm).analyze(PipelineRequest.model_validate(pipeline_request))
    assert result.normalized_error_code == "HTTP-404"
    assert "HTTP-404" in result.search_query
    assert json.loads(llm.calls[0]["data"])["reported_error_codes"] == ["HTTP-404"]


@pytest.mark.parametrize("text", ["404원 결제 문의", "500명 사용 요금 문의", "로그인 오류가 나요"])
async def test_hallucinated_http_code_is_still_rejected(pipeline_request, text):
    pipeline_request["messages"][0]["text"] = text
    result = await InquiryAnalyzer(
        StubLLM(searchable_result(normalized_error_code="HTTP-404"))
    ).analyze(PipelineRequest.model_validate(pipeline_request))
    assert result.normalized_error_code is None
    assert result.needs_more_info and result.search_query is None


def test_agent_suggested_http_code_is_not_customer_evidence(pipeline_request):
    pipeline_request["messages"].insert(
        0,
        dict(
            message_id="MSG-agent",
            sender="agent",
            text="HTTP 404가 뜨나요?",
            sent_at="2026-11-05T17:37:00+09:00",
        ),
    )
    assert not observed_error_codes(PipelineRequest.model_validate(pipeline_request))
    pipeline_request["env"]["error_code"] = "404"
    assert observed_error_codes(PipelineRequest.model_validate(pipeline_request)) == {"HTTP-404"}


def hit(code, inquiry="Q-1002"):
    return SearchHit(
        source_type="문서",
        source_id="DOC-005",
        version=1,
        source_revision=1,
        chunk_id="CHUNK-code",
        title=f"{code} 안내",
        text=f"{code} 오류 안내",
        score=0.95,
        visibility="고객 공개",
        evidence={"docs": [{"doc_id": "DOC-005", "version": 1}], "code": []},
        metadata={"inquiry_id": inquiry},
    )


async def test_http_502_cannot_use_service_e502_as_equivalent_evidence(pipeline_request):
    request = PipelineRequest.model_validate(pipeline_request)
    analysis = InquiryAnalysis.model_validate(searchable_result(normalized_error_code="HTTP-502"))
    result = await EvidenceValidator().validate(
        request, analysis, SearchResult(knowledge_revision=1, facts=[hit("E-502")])
    )
    assert result.status == "insufficient"
    result = await EvidenceValidator().validate(
        request, analysis, SearchResult(knowledge_revision=1, facts=[hit("HTTP-502")])
    )
    assert result.status == "usable"


async def test_http_status_does_not_merge_with_other_status_or_service_error(pipeline_request):
    class Rag:
        async def search(self, *args, **kwargs):
            items = []
            for code, inquiry in [
                ("HTTP-502", "Q-1002"),
                ("E-502", "Q-1003"),
                ("HTTP-404", "Q-1004"),
            ]:
                items.append(
                    hit(code, inquiry).model_copy(
                        update={"source_type": "문의 요약", "source_id": inquiry}
                    )
                )
            return SearchResult(knowledge_revision=1, internal_findings=items)

    analysis = InquiryAnalysis.model_validate(searchable_result(normalized_error_code="HTTP-502"))
    result = await SimilarInquiryFinder(Rag()).find(
        PipelineRequest.model_validate(pipeline_request), analysis
    )
    assert [item.inquiry_id for item in result.items] == ["Q-1002"]


async def test_http_pipeline_finishes_and_uses_only_matching_evidence(
    rag_service, pipeline_request
):
    pipeline_request["messages"][0]["text"] = "페이지를 열면 502 Internal Server Error가 떠요."
    await rag_service.ingest(make_doc())  # E-502 is a DIFFERENT service error.
    await rag_service.ingest(
        make_doc(
            source_id="DOC-001",
            title="HTTP 오류 안내",
            text="HTTP 502 오류는 담당자 확인이 필요합니다.",
        )
    )
    responses = default_responses()
    responses[InquiryAnalysis].update(
        normalized_error_code="HTTP-502", search_query="페이지 HTTP 502 오류"
    )
    responses[EvidenceEvaluationOutput]["selected_doc_ids"] = ["DOC-001"]
    responses[DraftGenerationOutput].update(
        answer_text="HTTP 502 오류는 담당자 확인이 필요합니다.",
        used_doc_ids=["DOC-001"],
        citations=[dict(text="HTTP 502 오류 안내", doc_ids=["DOC-001"])],
        used_style_ids=[],
    )
    llm = SchemaDispatchLLM(responses)
    result = await make_orchestrator(llm, rag_service).execute(
        PipelineRequest.model_validate(pipeline_request)
    )
    assert result.run_status == "완료" and result.error is None
    assert result.analysis.normalized_error_code == "HTTP-502"
    assert [d.doc_id for d in result.draft.evidence.docs] == ["DOC-001"]
    evidence_payload = json.loads(
        next(data for schema, data in llm.calls if schema is EvidenceEvaluationOutput)
    )
    assert [f["source_id"] for f in evidence_payload["facts"]] == ["DOC-001"]


async def test_unexpected_failure_records_stage_without_customer_text(pipeline_request, caplog):
    class Analyzer:
        async def analyze(self, request):
            raise RuntimeError("PRIVATE_CUSTOMER_TEXT_TOKEN")

    result = await PipelineOrchestrator(analyzer=Analyzer()).execute(
        PipelineRequest.model_validate(pipeline_request)
    )
    assert result.error.code == "PIPELINE_EXECUTION_ERROR"
    assert result.current_stage == "문의 분석"
    assert "PRIVATE_CUSTOMER_TEXT_TOKEN" not in caplog.text
    assert "RuntimeError" in caplog.text


async def test_late_failure_keeps_error_only_backend_contract(pipeline_request):
    class Rag:
        async def search(self, *args, **kwargs):
            raise ServiceError(504, "EMBEDDING_TIMEOUT", "Embedding request timed out")

    llm = StubLLM(searchable_result(normalized_error_code=None))
    result = await PipelineOrchestrator(analyzer=InquiryAnalyzer(llm), rag=Rag()).execute(
        PipelineRequest.model_validate(pipeline_request)
    )
    assert result.run_status == "실패" and result.error.code == "EMBEDDING_TIMEOUT"
    assert result.analysis is None and result.routing is None
    assert result.draft is None and result.verification is None
    assert result.current_stage == "유사 문의 탐색"


@pytest.mark.parametrize(
    "text",
    [
        "로그인하면 503이 나옵니다",
        "503이 보여요",
        "로그인 503 나와요",
        "로그인할 때 503 떴어요",
        "503 뜸",
        "로그인 버튼을 누르면 503이 나옵니다.",
    ],
)
def test_natural_status_code_reports_are_grounded(text):
    assert "HTTP-503" in extract_error_codes(text)


@pytest.mark.parametrize(
    "text", ["ZX-999 오류가 떠요", "파일 업로드 시 AUTH_001 에러", "CUSTOM-FAIL-42가 표시됩니다"]
)
def test_unfamiliar_codes_are_preserved_only_with_error_context(text):
    codes = extract_error_codes(text)
    assert codes
    assert all(resolve_reported_code(code, codes) == code for code in codes)


@pytest.mark.parametrize(
    "text", ["가격은 503원", "503명 요금", "문서 DOC-001 참고", "버전 4.0.4", "주문 ZX-999 확인"]
)
def test_numbers_and_identifiers_are_not_assumed_errors(text):
    assert not extract_error_codes(text)


@pytest.mark.parametrize("reply", ["401", "401이요", "401요.", "401입니다", "401예요!"])
def test_short_followup_code_is_grounded_in_error_context(pipeline_request, reply):
    pipeline_request["messages"][0]["text"] = "로그인 버튼을 누르면 오류가 떠요"
    pipeline_request["messages"].append(
        {
            **pipeline_request["messages"][0],
            "message_id": "MSG-000002",
            "text": reply,
        }
    )
    pipeline_request["based_on_message_id"] = "MSG-000002"
    assert "HTTP-401" in observed_error_codes(PipelineRequest.model_validate(pipeline_request))


def test_bare_number_without_error_context_is_not_grounded(pipeline_request):
    pipeline_request["messages"][0]["text"] = "401이요"
    assert not observed_error_codes(PipelineRequest.model_validate(pipeline_request))
