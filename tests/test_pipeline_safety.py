"""리뷰에서 재현한 오통과·정보 공개·출처 조작·자료 변경을 회귀 검사한다."""

import pytest
from pipeline_support import SchemaDispatchLLM, default_responses, make_doc, make_orchestrator
from pipeline_support import rag_service as rag_service

from app.core.errors import ServiceError
from app.schemas.analysis import InquiryAnalysis
from app.schemas.common import CheckStatus, DraftResult, RunStatus
from app.schemas.pipeline import Draft, PipelineRequest
from app.schemas.rag import SearchHit, SourceInput, SourceStatusBatch
from app.services.pipeline.drafter import (
    DraftGenerationOutput,
    DraftGenerator,
    EvidenceEvaluationOutput,
    ValidatedEvidence,
)
from app.services.pipeline.reviewer import DraftReviewer, ReviewLLMOutput
from app.services.pipeline.revision import AnswerCandidate, AnswerRevisionService, RevisionLLMOutput
from app.workers.pipeline import PipelineRunStore


@pytest.fixture
def account_case(pipeline_request):
    request = PipelineRequest.model_validate(pipeline_request)
    payload = default_responses()[InquiryAnalysis]
    payload.update(category="계정", normalized_error_code=None, summary="비밀번호 변경 문의")
    analysis = InquiryAnalysis.model_validate(payload)
    hit = SearchHit(
        source_type="문서",
        source_id="DOC-005",
        version=1,
        source_revision=1,
        chunk_id="chunk",
        title="계정 안내",
        text="비밀번호는 설정에서 변경합니다.",
        score=1,
        visibility="고객 공개",
        evidence={"docs": [{"doc_id": "DOC-005", "version": 1}]},
    )
    validated = ValidatedEvidence(status="usable", facts=[hit])
    draft = Draft(answer_text="설정에서 비밀번호를 변경할 수 있습니다.", evidence=hit.evidence)
    return request, analysis, validated, draft


def passing_review(**changes):
    result = dict(
        claims_supported="통과",
        internal_data_excluded="통과",
        source_conflict="통과",
        auto_response_scope="통과",
        reasons=[],
        unsupported_sentences=[],
    )
    return {**result, **changes}


@pytest.mark.parametrize(
    "field",
    [
        "claims_supported",
        "internal_data_excluded",
        "source_conflict",
        "auto_response_scope",
    ],
)
@pytest.mark.parametrize("status", ["실패", "확인 불가", "미실행"])
async def test_review_never_promotes_nonpassing_checks(account_case, field, status):
    llm = SchemaDispatchLLM({ReviewLLMOutput: passing_review(**{field: status})})
    result = await DraftReviewer(llm).review(*account_case)
    assert result.status == (CheckStatus.FAIL if status == "실패" else CheckStatus.UNKNOWN)
    assert result.checks[field] != CheckStatus.PASS
    assert result.checks["auto_response_scope"] != CheckStatus.PASS


async def test_review_passes_only_when_every_check_passes(account_case):
    llm = SchemaDispatchLLM({ReviewLLMOutput: passing_review()})
    result = await DraftReviewer(llm).review(*account_case)
    assert result.status == CheckStatus.PASS
    assert set(result.checks.values()) == {CheckStatus.PASS}


@pytest.mark.parametrize(
    "used,cited",
    [
        (["DOC-999"], ["DOC-999"]),
        (["DOC-005"], ["DOC-999"]),
        ([], ["DOC-005"]),
        (["DOC-005"], []),
    ],
)
async def test_draft_rejects_unknown_or_missing_citations(account_case, used, cited):
    request, analysis, validated, _ = account_case
    llm = SchemaDispatchLLM(
        {
            DraftGenerationOutput: {
                "answer_text": "설정에서 변경하세요.",
                "used_doc_ids": used,
                "citations": [{"text": "설정에서 변경", "doc_ids": cited}] if cited else [],
            }
        }
    )
    with pytest.raises(ServiceError) as error:
        await DraftGenerator(llm).generate(request, analysis, validated)
    assert error.value.code == "INVALID_DRAFT_CITATIONS"


@pytest.mark.parametrize("mode", ["private", "retired", "review", "pending", "failed"])
async def test_revision_uses_only_searchable_latest_public_documents(rag_service, mode):
    await rag_service.ingest(make_doc(version=1))
    if mode == "private":
        await rag_service.ingest(make_doc(version=2, visibility="내부 전용"))
    elif mode == "retired":
        await rag_service.ingest(make_doc(version=2, status="폐기"))
    elif mode == "review":
        await rag_service.ingest(make_doc(version=2, review_required=True))
    else:
        ticket = await rag_service.prepare(make_doc(version=2))
        if mode == "failed":
            rag_service.store.fail(ticket, "INDEXING_FAILED")
    llm = SchemaDispatchLLM({})  # 불가 상태에서는 LLM을 호출할 필요가 없다.
    service = AnswerRevisionService(llm, rag=rag_service)
    assert await service.resolve_latest_facts("syncnote", ["DOC-005"]) == []
    candidate = AnswerCandidate(
        target_id="BA-001",
        answer_text="이전 안내",
        evidence={"docs": [{"doc_id": "DOC-005", "version": 1}]},
    )
    result = await service.revise_answer("syncnote", candidate)
    assert result.revised_draft is None
    assert result.verification.status != CheckStatus.PASS
    assert llm.calls == []


async def test_revision_requires_all_referenced_documents(rag_service):
    await rag_service.ingest(make_doc())
    candidate = AnswerCandidate(
        target_id="BA-001",
        answer_text="두 문서를 참고한 이전 안내",
        evidence={
            "docs": [{"doc_id": "DOC-005", "version": 1}, {"doc_id": "DOC-006", "version": 1}]
        },
    )
    llm = SchemaDispatchLLM({})
    result = await AnswerRevisionService(llm, rag=rag_service).revise_answer("syncnote", candidate)
    assert result.revised_draft is None
    assert not llm.calls


async def retire_document(rag, source_revision=2):
    await rag.update_statuses(
        SourceStatusBatch(
            tenant_id="syncnote",
            event_id=f"retire-{source_revision}",
            reason="문서 폐기",
            sources=[
                dict(
                    source_type="문서",
                    source_id="DOC-005",
                    version=1,
                    source_revision=source_revision,
                    status="폐기",
                    review_required=False,
                )
            ],
        )
    )


@pytest.mark.parametrize(
    "change_stage", [EvidenceEvaluationOutput, DraftGenerationOutput, ReviewLLMOutput]
)
async def test_pipeline_rechecks_sources_across_llm_calls(rag_service, account_case, change_stage):
    request, analysis, _, _ = account_case
    await rag_service.ingest(make_doc(text="비밀번호는 설정에서 변경합니다."))
    responses = default_responses()
    responses[InquiryAnalysis] = analysis.model_dump(mode="json")
    responses[EvidenceEvaluationOutput]["requires_followup_check"] = False
    responses[ReviewLLMOutput] = passing_review()

    class ChangingLLM(SchemaDispatchLLM):
        async def generate(self, **kwargs):
            if kwargs["schema"] == change_stage:
                await retire_document(rag_service)
            return await super().generate(**kwargs)

    result = await make_orchestrator(ChangingLLM(responses), rag_service).execute(request)
    assert result.run_status == RunStatus.COMPLETED
    assert result.verification.status == CheckStatus.FAIL
    assert result.verification.checks["evidence_current"] == CheckStatus.FAIL
    assert result.verification.checks["auto_response_scope"] == CheckStatus.FAIL
    if change_stage == EvidenceEvaluationOutput:
        assert result.draft_result == DraftResult.MANUAL
        assert result.draft is None


async def test_same_version_replacement_invalidates_snapshot(rag_service, account_case):
    request, analysis, _, _ = account_case
    await rag_service.ingest(make_doc())
    responses = default_responses()
    responses[InquiryAnalysis] = analysis.model_dump(mode="json")

    class ChangingLLM(SchemaDispatchLLM):
        async def generate(self, **kwargs):
            if kwargs["schema"] == DraftGenerationOutput:
                await rag_service.ingest(make_doc(source_revision=2, text="변경된 안내"))
            return await super().generate(**kwargs)

    result = await make_orchestrator(ChangingLLM(responses), rag_service).execute(request)
    assert result.verification.checks["evidence_current"] == CheckStatus.FAIL


@pytest.mark.parametrize("invalid_id", ["DOC-999", None])
async def test_revision_never_fabricates_citations(rag_service, invalid_id):
    await rag_service.ingest(make_doc())
    candidate = AnswerCandidate(
        target_id="BA-001",
        answer_text="기존 안내",
        evidence={"docs": [{"doc_id": "DOC-005", "version": 1}]},
    )
    llm = SchemaDispatchLLM(
        {
            RevisionLLMOutput: {
                "needs_revision": True,
                "reason": "내용 변경",
                "revised_answer_text": "새 안내",
                "citations": [{"text": "새 안내", "doc_ids": [invalid_id]}] if invalid_id else [],
            }
        }
    )
    with pytest.raises(ServiceError) as error:
        await AnswerRevisionService(llm, rag=rag_service).revise_answer("syncnote", candidate)
    assert error.value.code == "INVALID_DRAFT_CITATIONS"


async def test_revision_blocks_changes_during_impact_analysis(rag_service):
    await rag_service.ingest(make_doc())
    candidate = AnswerCandidate(
        target_id="BA-001",
        answer_text="기존 안내",
        evidence={"docs": [{"doc_id": "DOC-005", "version": 1}]},
    )

    class ChangingLLM(SchemaDispatchLLM):
        async def generate(self, **kwargs):
            await retire_document(rag_service)
            return await super().generate(**kwargs)

    llm = ChangingLLM({RevisionLLMOutput: {"needs_revision": False, "reason": "변경 불필요"}})
    result = await AnswerRevisionService(llm, rag=rag_service).revise_answer("syncnote", candidate)
    assert result.needs_revision
    assert result.verification.status == CheckStatus.FAIL


def test_run_ids_do_not_repeat_after_restart(account_case):
    request = account_case[0]
    before = PipelineRunStore().submit("syncnote", "same-key", request)
    after = PipelineRunStore().submit("syncnote", "same-key", request)
    assert before.accepted.run_id != after.accepted.run_id


def test_uuid_runs_keep_idempotency(account_case):
    request = account_case[0]
    store = PipelineRunStore()
    first = store.submit("syncnote", "same-key", request)
    duplicate = store.submit("syncnote", "same-key", request)
    assert first.accepted.run_id == duplicate.accepted.run_id
    assert not duplicate.is_new


def test_new_run_supersedes_previous_run_of_same_revision(account_case):
    request = account_case[0]
    store = PipelineRunStore()
    first = store.submit("syncnote", "first", request)
    second = store.submit("syncnote", "reanalysis", request)
    assert not store.is_latest_run(first.accepted.run_id)
    assert store.is_latest_run(second.accepted.run_id)
    # 이전 멱등키를 재전송해도 최신 실행 포인터가 되돌아가면 안 된다.
    store.submit("syncnote", "first", request)
    assert store.is_latest_run(second.accepted.run_id)


async def test_revision_rechecks_sources_after_review(rag_service):
    await rag_service.ingest(make_doc())
    candidate = AnswerCandidate(
        target_id="BA-001",
        answer_text="기존 안내",
        evidence={"docs": [{"doc_id": "DOC-005", "version": 1}]},
    )

    class ChangingLLM(SchemaDispatchLLM):
        async def generate(self, **kwargs):
            if kwargs["schema"] == ReviewLLMOutput:
                await retire_document(rag_service)
            return await super().generate(**kwargs)

    llm = ChangingLLM(
        {
            RevisionLLMOutput: {
                "needs_revision": True,
                "reason": "내용 변경",
                "revised_answer_text": "새 안내",
                "citations": [{"text": "새 안내", "doc_ids": ["DOC-005"]}],
            },
            ReviewLLMOutput: passing_review(),
        }
    )
    result = await AnswerRevisionService(llm, rag=rag_service).revise_answer("syncnote", candidate)
    assert result.revised_draft is not None
    assert result.verification.status == CheckStatus.FAIL
    assert result.verification.checks["evidence_current"] == CheckStatus.FAIL


async def test_unrelated_conflicting_documents_do_not_block_single_selected_evidence(
    rag_service, pipeline_request
):
    await rag_service.ingest(
        make_doc(
            source_id="DOC-101",
            title="비밀번호 변경 안내",
            text="비밀번호 변경: 싱크노트 설정 > 계정 > 보안 > 비밀번호 변경에서 변경합니다.",
        )
    )
    await rag_service.ingest(
        make_doc(
            source_id="DOC-104",
            title="일반 환불 정책",
            text="결제 후 7일 이내 전액 환불 가능합니다.",
        )
    )
    await rag_service.ingest(
        make_doc(
            source_id="DOC-106",
            title="일반 환불 정책 2",
            text="결제 후 30일 이내 전액 환불 가능합니다.",
        )
    )
    pipeline_request["messages"][0]["text"] = "비밀번호 변경 방법을 알려주세요."
    req = PipelineRequest.model_validate(pipeline_request)

    responses = default_responses(
        {
            InquiryAnalysis: {
                "category": "계정",
                "urgency": "낮음",
                "summary": "비밀번호 변경 방법 문의",
                "symptoms": [],
                "normalized_error_code": None,
                "missing_fields": [],
                "needs_more_info": False,
                "additional_question": None,
                "search_query": "비밀번호 변경 환불",
                "reasons": {
                    "category": "비밀번호 문의",
                    "urgency": "단순 안내",
                    "information": "충분",
                },
                "limitations": [],
            },
            EvidenceEvaluationOutput: lambda data: {
                "relevant": True,
                "has_conflict": "DOC-104" in data and "DOC-106" in data,
                "sufficient_for_draft": "DOC-104" not in data,
                "requires_followup_check": False,
                "selected_doc_ids": ["DOC-101"],
                "reasons": ["선택한 비밀번호 문서만 충돌 없이 사용"],
            },
            DraftGenerationOutput: {
                "answer_text": "싱크노트 설정 > 계정 > 보안 > 비밀번호 변경에서 변경합니다.",
                "used_doc_ids": ["DOC-101"],
                "citations": [
                    {
                        "text": "싱크노트 설정 > 계정 > 보안 > 비밀번호 변경에서 변경합니다.",
                        "doc_ids": ["DOC-101"],
                    }
                ],
                "used_style_ids": [],
            },
            ReviewLLMOutput: passing_review(),
        }
    )
    res = await make_orchestrator(SchemaDispatchLLM(responses), rag_service).execute(req)
    assert res.draft_result == DraftResult.READY
    assert res.draft is not None
    assert [d.doc_id for d in res.draft.evidence.docs] == ["DOC-101"]
    assert res.verification.status == CheckStatus.PASS


async def test_selected_single_document_conflict_still_blocks_draft(account_case):
    from app.schemas.rag import SearchResult
    from app.services.pipeline.drafter import EvidenceValidator

    request, analysis, validated, _ = account_case
    extra = validated.facts[0].model_copy(update={"source_id": "DOC-006"})
    llm = SchemaDispatchLLM(
        {
            EvidenceEvaluationOutput: {
                "relevant": True,
                "has_conflict": True,
                "sufficient_for_draft": False,
                "selected_doc_ids": ["DOC-005"],
            }
        }
    )
    result = await EvidenceValidator(llm).validate(
        request, analysis, SearchResult(knowledge_revision=1, facts=[*validated.facts, extra])
    )
    assert result.status == "conflict"
    assert result.facts == []
    assert len(llm.calls) == 2


async def test_unknown_selected_evidence_is_not_replaced_with_all_facts(account_case):
    from app.schemas.rag import SearchResult
    from app.services.pipeline.drafter import EvidenceValidator

    request, analysis, validated, _ = account_case
    llm = SchemaDispatchLLM(
        {
            EvidenceEvaluationOutput: {
                "relevant": True,
                "has_conflict": False,
                "sufficient_for_draft": True,
                "selected_doc_ids": ["DOC-999"],
            }
        }
    )
    with pytest.raises(ServiceError) as error:
        await EvidenceValidator(llm).validate(
            request, analysis, SearchResult(knowledge_revision=1, facts=validated.facts)
        )
    assert error.value.code == "INVALID_EVIDENCE_SELECTION"


async def test_orchestrator_regenerates_once_when_draft_adds_unsupported_procedure(
    rag_service, pipeline_request
):
    await rag_service.ingest(
        make_doc(
            source_id="DOC-101",
            title="비밀번호 변경 안내",
            text="비밀번호 변경: 싱크노트 설정 > 계정 > 보안 > 비밀번호 변경에서 변경합니다.",
        )
    )
    pipeline_request["messages"][0]["text"] = "비밀번호 변경 방법을 알려주세요."
    req = PipelineRequest.model_validate(pipeline_request)

    def draft_dispatch(data_str: str) -> dict:
        if "rejected_unsupported_sentences" in data_str:
            return {
                "answer_text": "싱크노트 설정 > 계정 > 보안 > 비밀번호 변경에서 변경합니다.",
                "used_doc_ids": ["DOC-101"],
                "citations": [
                    {
                        "text": "싱크노트 설정 > 계정 > 보안 > 비밀번호 변경에서 변경합니다.",
                        "doc_ids": ["DOC-101"],
                    }
                ],
                "used_style_ids": [],
            }
        return {
            "answer_text": (
                "싱크노트 설정 > 계정 > 보안 > 비밀번호 변경으로 이동하세요. "
                "현재 비밀번호를 입력하고 완료 버튼을 누르면 변경됩니다."
            ),
            "used_doc_ids": ["DOC-101"],
            "citations": [
                {
                    "text": "싱크노트 설정 > 계정 > 보안 > 비밀번호 변경",
                    "doc_ids": ["DOC-101"],
                }
            ],
            "used_style_ids": [],
        }

    def review_dispatch(data_str: str) -> dict:
        if "현재 비밀번호를 입력하고" in data_str:
            return passing_review(
                claims_supported="실패",
                auto_response_scope="실패",
                reasons=["문서에 없는 현재 비밀번호 입력 절차 추가"],
                unsupported_sentences=["현재 비밀번호를 입력하고 완료 버튼을 누르면 변경됩니다."],
            )
        return passing_review()

    responses = default_responses(
        {
            InquiryAnalysis: {
                "category": "계정",
                "urgency": "낮음",
                "summary": "비밀번호 변경 방법 문의",
                "symptoms": [],
                "normalized_error_code": None,
                "missing_fields": [],
                "needs_more_info": False,
                "additional_question": None,
                "search_query": "비밀번호 변경",
                "reasons": {
                    "category": "비밀번호 문의",
                    "urgency": "단순 안내",
                    "information": "충분",
                },
                "limitations": [],
            },
            EvidenceEvaluationOutput: {
                "relevant": True,
                "has_conflict": False,
                "sufficient_for_draft": True,
                "requires_followup_check": False,
                "selected_doc_ids": ["DOC-101"],
                "reasons": [],
            },
            DraftGenerationOutput: draft_dispatch,
            ReviewLLMOutput: review_dispatch,
        }
    )
    res = await make_orchestrator(SchemaDispatchLLM(responses), rag_service).execute(req)
    assert res.draft_result == DraftResult.READY
    assert res.draft is not None
    assert "현재 비밀번호" not in res.draft.answer_text
    assert res.verification.status == CheckStatus.PASS


def test_code_path_source_id_status_lookup_supports_plain_and_encoded_slashes(
    client, headers, rag_service
):
    client.app.state.rag_service = rag_service
    body = {
        "tenant_id": "syncnote",
        "source_type": "코드",
        "source_id": "sync/saveHandler.ts",
        "version": "123abcd",
        "source_revision": 1,
        "title": "saveHandler.ts",
        "text": "saveHandler implementation",
        "visibility": "내부 전용",
        "status": "유효",
        "review_required": False,
        "content_hash": "sha256:code-path",
        "metadata": {},
        "evidence": {"docs": [], "code": []},
    }
    post_res = client.post("/internal/v1/rag/sources", json=body, headers=headers)
    assert post_res.status_code == 202

    plain_res = client.get(
        "/internal/v1/rag/sources/sync/saveHandler.ts/versions/123abcd",
        params={"source_type": "코드"},
        headers=headers,
    )
    encoded_res = client.get(
        "/internal/v1/rag/sources/sync%2FsaveHandler.ts/versions/123abcd",
        params={"source_type": "코드"},
        headers=headers,
    )
    assert plain_res.status_code == 200
    assert encoded_res.status_code == 200
    assert plain_res.json()["searchable"] is True
    assert encoded_res.json()["searchable"] is True


async def test_similar_inquiry_search_not_crowded_out_by_general_documents(
    tmp_path, pipeline_request
):
    from app.services.pipeline.similar import SimilarInquiryFinder
    from app.services.rag.chroma_store import ChromaVectorStore
    from app.services.rag.service import RagService

    class CrowdingEmbedder:
        space = "crowding-edge:2"

        async def documents(self, texts: list[str]) -> list[list[float]]:
            return [[0.8, 0.6] if "PAST_INQUIRY" in t else [1.0, 0.0] for t in texts]

        async def query(self, text: str) -> list[float]:
            return [1.0, 0.0]

    store = ChromaVectorStore(str(tmp_path / "chroma-crowding"))
    try:
        rag = RagService(store, CrowdingEmbedder())
        for n in range(10):
            await rag.ingest(make_doc(source_id=f"DOC-{200 + n}", text="E-502 저장 오류 안내"))
        await rag.ingest(
            SourceInput(
                tenant_id="syncnote",
                source_type="문의 요약",
                source_id="Q-2001",
                version=1,
                title="과거 문의",
                text="PAST_INQUIRY E-502 노트 저장 오류",
                visibility="내부 전용",
                status="유효",
                content_hash="sha256:q2001",
                metadata={
                    "inquiry_id": "Q-2001",
                    "group_id": "G-001",
                    "normalized_error_code": "E-502",
                },
            )
        )
        req = PipelineRequest.model_validate(pipeline_request)
        analysis = InquiryAnalysis.model_validate(default_responses()[InquiryAnalysis])
        found = await SimilarInquiryFinder(rag).find(req, analysis)
        assert [item.inquiry_id for item in found.items] == ["Q-2001"]
        assert found.suggested_group_id == "G-001"
    finally:
        store.close()


def test_pipeline_run_rolls_back_queued_entry_when_orchestrator_init_fails(
    client, headers, pipeline_request
):
    from unittest.mock import patch

    def unavailable(request):
        raise ServiceError(503, "RAG_NOT_CONFIGURED", "test initialization failure")

    with patch("app.api.internal.v1.pipeline_runs.get_orchestrator", side_effect=unavailable):
        first = client.post("/internal/v1/pipeline-runs", headers=headers, json=pipeline_request)
        second = client.post("/internal/v1/pipeline-runs", headers=headers, json=pipeline_request)
        assert first.status_code == 503
        assert second.status_code == 503


def test_preview_reports_failed_stage_when_pipeline_result_fails(
    settings, headers, pipeline_request, rag_service
):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.schemas.pipeline import PipelineResult

    settings.enable_preview = True
    app = create_app(settings)
    app.state.rag_service = rag_service

    class Analyzer:
        async def analyze(self, request):
            return InquiryAnalysis.model_validate(default_responses()[InquiryAnalysis])

    class FailingPipeline:
        async def execute(self, request, **kwargs):
            return PipelineResult(
                run_id="RUN-PREVIEW",
                tenant_id="syncnote",
                inquiry_id=request.inquiry_id,
                inquiry_revision=1,
                based_on_message_id=request.based_on_message_id,
                run_status="실패",
                error={"code": "LLM_RATE_LIMITED", "message": "test", "retryable": True},
            )

    app.state.inquiry_analyzer = Analyzer()
    app.state.pipeline_orchestrator = FailingPipeline()
    with TestClient(app) as test_client:
        res = test_client.post(
            "/internal/v1/pipeline-preview?full_pipeline=true",
            headers=headers,
            json=pipeline_request,
        )
        assert res.status_code == 200
        data = res.json()
        assert data["stage"] == "파이프라인 실행 실패"
        assert data["pipeline_result"]["run_status"] == "실패"


def test_bedrock_bearer_token_updates_on_new_settings(settings):
    import os
    from unittest.mock import patch

    from pydantic import SecretStr

    from app.services.llm.bedrock import BedrockLLM

    seen: list[str | None] = []
    with (
        patch.dict(os.environ, {}, clear=True),
        patch(
            "app.services.llm.bedrock.boto3.client",
            side_effect=lambda *a, **kw: seen.append(os.environ.get("AWS_BEARER_TOKEN_BEDROCK")),
        ),
    ):
        for value in ["fake-first", "fake-second"]:
            cfg = settings.model_copy(update={"bedrock_bearer_token": SecretStr(value)})
            BedrockLLM(cfg)._build_client()
    assert seen == ["fake-first", "fake-second"]


async def test_bedrock_embedder_is_the_only_embedding_provider(settings):
    import io
    import json
    from unittest.mock import MagicMock, patch

    from app.services.rag.embedding import BedrockEmbedder, create_embedder

    cfg = settings.model_copy(
        update={
            "embedding_dimensions": 1024,
        }
    )
    embedder = create_embedder(cfg)
    assert isinstance(embedder, BedrockEmbedder)
    assert embedder.space == "amazon.titan-embed-text-v2:0:1024"

    mock_client = MagicMock()
    mock_client.invoke_model.side_effect = lambda **kw: {
        "body": io.BytesIO(json.dumps({"embedding": [1.0] + [0.0] * 1023}).encode("utf-8"))
    }
    with patch.object(BedrockEmbedder, "_build_client", return_value=mock_client):
        doc_vecs = await embedder.documents(["문서 1", "문서 2"])
        q_vec = await embedder.query("비밀번호 변경 방법")
    assert len(doc_vecs) == 2
    assert len(doc_vecs[0]) == 1024
    assert len(q_vec) == 1024
    assert mock_client.invoke_model.call_count == 3
