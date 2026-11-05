import json
import math

import httpx
import pytest
from pipeline_support import SchemaDispatchLLM, default_responses, make_doc, make_orchestrator
from pipeline_support import rag_service as rag_service
from pydantic import SecretStr

from app.schemas.analysis import InquiryAnalysis
from app.schemas.common import CheckStatus, DraftResult
from app.schemas.pipeline import PipelineRequest, PipelineResult
from app.schemas.rag import SearchRequest
from app.schemas.routing import TeamRouting
from app.services.backend_client import BackendClient
from app.services.pipeline.drafter import DraftGenerationOutput, EvidenceEvaluationOutput
from app.services.pipeline.reviewer import ReviewLLMOutput
from app.services.pipeline.search import evidence_search_request


async def test_hybrid_recovers_low_cosine_price_doc_beyond_vector_candidates(rag_service):
    # Simulate the reported ranking failure without calling Titan or altering a real DB.
    async def documents(texts):
        return [[0.1117, math.sqrt(1 - 0.1117**2), 0.0] for _ in texts]

    async def query(text):
        return [1.0, 0.0, 0.0]

    rag_service.embedder.documents = documents
    rag_service.embedder.query = query
    await rag_service.ingest(
        make_doc(
            source_id="DOC-001",
            title="싱크노트 요금제 안내",
            text=(
                "싱크노트 요금제는 Free, Pro, Team 세 가지입니다. "
                "Pro 월 9,900원, 연 95,040원, 저장 공간 50GB."
            ),
        )
    )

    async def unrelated(texts):
        return [[0.2151, math.sqrt(1 - 0.2151**2), 0.0] for _ in texts]

    rag_service.embedder.documents = unrelated
    for number in range(20, 42):
        await rag_service.ingest(
            make_doc(
                source_id=f"DOC-{number:03d}",
                title="첨부 파일 크기 제한",
                text="큰 파일 첨부 실패 E-413 안내.",
            )
        )
    base = dict(tenant_id="syncnote", query="Pro 요금제 월간 요금 연간 요금 저장 공간")
    assert not (await rag_service.search(SearchRequest(**base))).facts
    result = await rag_service.search(SearchRequest(**base, retrieval_mode="hybrid"))
    assert result.facts[0].source_id == "DOC-001"
    assert result.facts[0].score == pytest.approx(0.1117, abs=1e-5)
    assert result.facts[0].evidence.docs[0].version == 1


async def test_hybrid_uses_same_tenant_visibility_and_latest_version_filters(rag_service):
    price = dict(title="Pro 요금제", text="Pro 월간 연간 저장 공간 안내")
    await rag_service.ingest(make_doc(source_id="DOC-001", **price))
    await rag_service.ingest(make_doc(source_id="DOC-001", version=2, **price))
    await rag_service.ingest(make_doc(source_id="DOC-002", tenant_id="other", **price))
    await rag_service.ingest(make_doc(source_id="DOC-003", visibility="내부 전용", **price))
    await rag_service.ingest(make_doc(source_id="DOC-004", status="폐기", **price))
    result = await rag_service.search(
        SearchRequest(
            tenant_id="syncnote",
            query="Pro 요금제",
            retrieval_mode="hybrid",
        )
    )
    assert {(h.source_id, h.version) for h in result.facts} == {("DOC-001", 2)}
    # Latest pending version must block the old version in BOTH retrieval paths.
    await rag_service.prepare(make_doc(source_id="DOC-001", version=3, **price))
    assert not (
        await rag_service.search(
            SearchRequest(
                tenant_id="syncnote",
                query="Pro 요금제",
                retrieval_mode="hybrid",
            )
        )
    ).facts


def test_search_preserves_customer_service_name_without_answer_injection(pipeline_request):
    pipeline_request["messages"][0]["text"] = (
        "싱크노트 Pro의 월간·연간 요금과 저장 공간을 알려주세요."
    )
    analysis = InquiryAnalysis.model_validate(default_responses()[InquiryAnalysis])
    request = evidence_search_request(
        PipelineRequest.model_validate(pipeline_request), analysis, min_score=0.5
    )
    assert "싱크노트" in request.lexical_query
    assert "9,900" not in request.lexical_query and "50GB" not in request.lexical_query


@pytest.mark.parametrize(
    "team,requires_human", [("CS", False), ("CS", True), ("테크", True), ("판단 보류", True)]
)
async def test_evidence_routing_returned_without_extra_llm_call(
    rag_service,
    pipeline_request,
    team,
    requires_human,
):
    pipeline_request["messages"][0]["text"] = "저장 시 E502가 발생해요."
    await rag_service.ingest(make_doc())
    responses = default_responses()
    routing = dict(
        team=team, reason="현재 문의와 공개 근거에 따른 판단", requires_human=requires_human
    )
    responses[EvidenceEvaluationOutput]["routing"] = routing
    llm = SchemaDispatchLLM(responses)
    result = await make_orchestrator(llm, rag_service).execute(
        PipelineRequest.model_validate(pipeline_request)
    )
    assert result.routing == TeamRouting.model_validate(routing)
    assert len(llm.calls) == 4  # analysis, evidence, draft, separate review
    if requires_human:
        assert result.verification.checks["auto_response_scope"] != CheckStatus.PASS


async def test_insufficient_information_overrides_team_and_asks_question(
    rag_service, pipeline_request
):
    responses = default_responses()
    responses[InquiryAnalysis].update(
        normalized_error_code=None,
        symptoms=[],
        needs_more_info=True,
        missing_fields=["대상 기능"],
        additional_question="어떤 기능이 안 되나요?",
        search_query=None,
        routing=dict(team="테크", reason="모델의 성급한 분류", requires_human=True),
    )
    llm = SchemaDispatchLLM(responses)
    result = await make_orchestrator(llm, rag_service).execute(
        PipelineRequest.model_validate(pipeline_request)
    )
    assert result.routing.team == "판단 보류"
    assert result.draft_result == DraftResult.NEED_INFO
    assert len(llm.calls) == 1


async def test_technical_action_is_not_downgraded_by_matching_document(
    rag_service, pipeline_request
):
    pipeline_request["messages"][0]["text"] = "저장 시 E502가 발생해요."
    await rag_service.ingest(make_doc())
    responses = default_responses()
    responses[InquiryAnalysis]["routing"] = dict(
        team="테크", reason="여러 사용자 동시 저장 장애 조사", requires_human=True
    )
    responses[EvidenceEvaluationOutput]["routing"] = dict(
        team="CS", reason="오류 문서 발견", requires_human=False
    )
    result = await make_orchestrator(SchemaDispatchLLM(responses), rag_service).execute(
        PipelineRequest.model_validate(pipeline_request)
    )
    assert result.routing.team == "테크" and result.routing.requires_human


async def test_keyword_candidates_do_not_allow_unsupported_student_discount(
    rag_service, pipeline_request
):
    await rag_service.ingest(
        make_doc(source_id="DOC-001", title="요금제 안내", text="Pro 월간 요금과 저장 공간 안내")
    )
    pipeline_request["messages"][0]["text"] = "Pro 요금제 학생 할인 있나요?"
    responses = default_responses()
    responses[InquiryAnalysis].update(
        category="결제",
        summary="Pro 학생 할인 문의",
        normalized_error_code=None,
        symptoms=[],
        search_query="Pro 요금제 학생 할인",
    )
    responses[EvidenceEvaluationOutput].update(
        relevant=False,
        sufficient_for_draft=False,
        selected_doc_ids=[],
        reasons=["학생 할인 근거 없음"],
    )
    llm = SchemaDispatchLLM(responses)
    result = await make_orchestrator(llm, rag_service).execute(
        PipelineRequest.model_validate(pipeline_request)
    )
    assert result.draft_result == DraftResult.MANUAL and result.draft is None
    assert DraftGenerationOutput not in [schema for schema, _ in llm.calls]


@pytest.mark.parametrize("team", ["CS", "테크", "판단 보류"])
async def test_human_routing_blocks_otherwise_passing_non_error_answer(
    rag_service, pipeline_request, team
):
    await rag_service.ingest(make_doc())
    responses = default_responses()
    responses[InquiryAnalysis].update(normalized_error_code=None, symptoms=[], category="결제")
    responses[EvidenceEvaluationOutput].update(
        requires_followup_check=False,
        routing=dict(team=team, reason="개별 처리 권한 확인 필요", requires_human=True),
    )
    responses[ReviewLLMOutput].update(auto_response_scope="통과", reasons=[])
    result = await make_orchestrator(SchemaDispatchLLM(responses), rag_service).execute(
        PipelineRequest.model_validate(pipeline_request)
    )
    assert result.verification.checks["claims_supported"] == CheckStatus.PASS
    assert result.verification.checks["auto_response_scope"] == CheckStatus.UNKNOWN
    assert result.verification.status == CheckStatus.UNKNOWN


@pytest.mark.parametrize("team", ["테크", "판단 보류"])
def test_non_cs_cannot_disable_human_review(team):
    assert TeamRouting(team=team, requires_human=False).requires_human


@pytest.mark.parametrize("structured", [False, True])
async def test_callback_routing_compatibility(settings, structured):
    settings.backend_include_routing = structured
    result = PipelineResult(
        run_id="RUN-test",
        tenant_id="syncnote",
        inquiry_id="Q-1001",
        inquiry_revision=1,
        based_on_message_id="MSG-1",
        run_status="완료",
        routing=TeamRouting(team="테크", reason="서버 로그 조사 필요"),
        verification=dict(status="확인 불가", reasons=[], checks={}),
    )

    settings.backend_service_token = SecretStr("callback-test-token")

    def handler(request):
        payload = json.loads(request.content)
        assert ("routing" in payload) == structured
        if structured:
            assert payload["routing"]["team"] == "테크"
        else:
            assert any("테크" in reason for reason in payload["verification"]["reasons"])
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await BackendClient(settings).deliver(result, client)
    assert result.verification.reasons == []  # serialization must not mutate stored results
