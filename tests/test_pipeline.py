import httpx
from fastapi.testclient import TestClient
from pipeline_support import (
    SchemaDispatchLLM,
    default_responses,
    make_doc,
    make_orchestrator,
)
from pipeline_support import rag_service as rag_service
from pydantic import SecretStr

from app.main import create_app
from app.schemas.analysis import InquiryAnalysis
from app.schemas.common import CheckStatus, DraftResult, RunStatus
from app.schemas.pipeline import PipelineRequest, PipelineResult
from app.schemas.rag import SourceInput
from app.services.backend_client import BackendClient
from app.services.pipeline.analyzer import InquiryAnalyzer
from app.services.pipeline.drafter import (
    DraftGenerationOutput,
    EvidenceEvaluationOutput,
)
from app.services.pipeline.reviewer import ReviewLLMOutput
from app.services.pipeline.revision import (
    AnswerRevisionService,
    RevisionLLMOutput,
)
from app.workers.pipeline import PipelineRunStore, execute_pipeline

# --- Task 1: 근거 검증 -> 답변 초안 -> 별도 검수 ---


async def test_full_pipeline_executes_stages_and_separates_generation_from_review(
    rag_service, pipeline_request
):
    await rag_service.ingest(make_doc())
    await rag_service.ingest(
        SourceInput(
            tenant_id="syncnote",
            source_type="우수 답변",
            source_id="BA-001",
            version=1,
            title="저장 오류 안내 예시",
            text="안녕하세요, 싱크노트 고객지원팀입니다. 확인 후 안내드리겠습니다.",
            visibility="고객 공개",
            status="유효",
            content_hash="sha256:ba001",
            evidence={"docs": [{"doc_id": "DOC-005", "version": 1}], "code": []},
        )
    )
    pipeline_request["messages"][0]["text"] = "저장할 때 E502가 떠요."
    req = PipelineRequest.model_validate(pipeline_request)

    llm = SchemaDispatchLLM(default_responses())
    orchestrator = make_orchestrator(llm, rag_service)
    stages_seen = []
    result = await orchestrator.execute(req, run_id="RUN-000001", on_stage=stages_seen.append)

    assert result.run_status == RunStatus.COMPLETED
    assert result.draft_result == DraftResult.READY
    assert result.draft is not None
    assert result.draft.evidence.docs[0].doc_id == "DOC-005"
    assert result.draft.style_references[0].best_id == "BA-001"
    assert result.verification is not None
    assert result.verification.status == CheckStatus.UNKNOWN
    assert result.verification.checks["claims_supported"] == CheckStatus.PASS
    assert result.verification.checks["auto_response_scope"] == CheckStatus.UNKNOWN

    # Ensure DraftGenerationOutput and ReviewLLMOutput were executed as separate LLM calls
    called_schemas = [schema for schema, _ in llm.calls]
    assert DraftGenerationOutput in called_schemas
    assert ReviewLLMOutput in called_schemas
    assert called_schemas.index(DraftGenerationOutput) < called_schemas.index(ReviewLLMOutput)
    assert len(stages_seen) == 8


async def test_evidence_insufficient_or_conflict_withholds_draft(rag_service, pipeline_request):
    pipeline_request["messages"][0]["text"] = "저장할 때 E502가 떠요."
    req = PipelineRequest.model_validate(pipeline_request)
    llm = SchemaDispatchLLM(default_responses())
    orchestrator = make_orchestrator(llm, rag_service)

    # No documents ingested -> insufficient evidence -> MANUAL, draft=None
    no_doc_result = await orchestrator.execute(req)
    assert no_doc_result.run_status == RunStatus.COMPLETED
    assert no_doc_result.draft_result == DraftResult.MANUAL
    assert no_doc_result.draft is None
    assert no_doc_result.verification.status == CheckStatus.UNKNOWN

    # Ingest doc but LLM detects source conflict -> MANUAL, draft=None, status=FAIL
    await rag_service.ingest(make_doc())
    conflict_llm = SchemaDispatchLLM(
        default_responses(
            {
                EvidenceEvaluationOutput: {
                    "relevant": True,
                    "has_conflict": True,
                    "sufficient_for_draft": False,
                    "requires_followup_check": False,
                    "selected_doc_ids": [],
                    "reasons": ["환불 기간이 14일과 7일로 충돌함"],
                }
            }
        )
    )
    conflict_result = await make_orchestrator(conflict_llm, rag_service).execute(req)
    assert conflict_result.draft_result == DraftResult.MANUAL
    assert conflict_result.draft is None
    assert conflict_result.verification.status == CheckStatus.FAIL
    assert conflict_result.verification.checks["source_conflict"] == CheckStatus.FAIL


async def test_reviewer_catches_internal_code_leak_and_unsupported_sentences(
    rag_service, pipeline_request
):
    await rag_service.ingest(make_doc())
    pipeline_request["messages"][0]["text"] = "저장할 때 E502가 떠요."
    req = PipelineRequest.model_validate(pipeline_request)

    llm = SchemaDispatchLLM(
        default_responses(
            {
                DraftGenerationOutput: {
                    "answer_text": "sync/saveHandler.ts의 flushBatch 문제로 100% 환불해 드립니다.",
                    "used_doc_ids": ["DOC-005"],
                    "citations": [{"text": "저장 오류", "doc_ids": ["DOC-005"]}],
                    "used_style_ids": [],
                },
                ReviewLLMOutput: {
                    "claims_supported": "실패",
                    "internal_data_excluded": "통과",  # deterministic guard must still catch it!
                    "source_conflict": "통과",
                    "auto_response_scope": "실패",
                    "reasons": ["근거 문서에 없는 보상 약속"],
                    "unsupported_sentences": [
                        "sync/saveHandler.ts의 flushBatch 문제로 100% 환불해 드립니다."
                    ],
                },
            }
        )
    )
    result = await make_orchestrator(llm, rag_service).execute(req)
    assert result.verification.status == CheckStatus.FAIL
    assert result.verification.checks["internal_data_excluded"] == CheckStatus.FAIL
    assert result.verification.checks["claims_supported"] == CheckStatus.FAIL
    assert result.verification.checks["auto_response_scope"] == CheckStatus.FAIL
    assert result.verification.unsupported_sentences


# --- Task 2: 유사 문의 탐색과 그룹 제안 (오병합 방지) ---


async def test_similar_inquiries_suggests_matching_group_and_excludes_different_error_codes(
    rag_service, pipeline_request
):
    # Index 3 past inquiry summaries with different error codes that share similar phrasing
    for inq_id, group_id, code in [
        ("Q-0174", "G-001", "E-502"),
        ("Q-0175", "G-002", "E-413"),
        ("Q-0176", "G-003", "E-401"),
    ]:
        await rag_service.ingest(
            SourceInput(
                tenant_id="syncnote",
                source_type="문의 요약",
                source_id=inq_id,
                version=1,
                title=f"저장 시 {code} 오류 발생",
                text=f"방금부터 저장이 안 되고 {code} 에러가 뜹니다",
                visibility="내부 전용",
                status="유효",
                content_hash=f"sha256:{inq_id}",
                metadata={
                    "inquiry_id": inq_id,
                    "group_id": group_id,
                    "normalized_error_code": code,
                    "category": "기능 오류",
                },
            )
        )
    await rag_service.ingest(make_doc())

    pipeline_request["messages"][0]["text"] = "저장할 때 E502가 떠요."
    req = PipelineRequest.model_validate(pipeline_request)
    llm = SchemaDispatchLLM(default_responses())
    result = await make_orchestrator(llm, rag_service).execute(req)

    assert result.similar_inquiries is not None
    matched_ids = [item.inquiry_id for item in result.similar_inquiries.items]
    assert matched_ids == ["Q-0174"]
    assert "Q-0175" not in matched_ids  # E-413 excluded
    assert "Q-0176" not in matched_ids  # E-401 excluded
    assert result.similar_inquiries.suggested_group_id == "G-001"


# --- Task 3 & 4: pipeline-runs 실행·상태 조회·멱등 처리 + 백엔드 콜백 + 추가 질문 흐름 ---


async def test_pipeline_runs_idempotency_conflict_and_callback_flow(
    client, headers, pipeline_request, rag_service
):
    await rag_service.ingest(make_doc())
    pipeline_request["messages"][0]["text"] = "저장할 때 E502가 떠요."

    callbacks: list[PipelineResult] = []

    def backend_handler(request: httpx.Request) -> httpx.Response:
        callbacks.append(PipelineResult.model_validate_json(request.content))
        return httpx.Response(
            200,
            json={
                "run_id": callbacks[-1].run_id,
                "apply_status": "applied",
                "inquiry_id": callbacks[-1].inquiry_id,
                "status": "담당자 확인 중",
                "draft_id": "DRAFT-000001",
                "group_id": "G-001",
                "delivery_action": "담당자 검토",
            },
        )

    client.app.state.settings.backend_service_token = SecretStr("backend-secret")
    client.app.state.settings.backend_include_routing = True
    client.app.state.rag_service = rag_service
    client.app.state.pipeline_orchestrator = make_orchestrator(
        SchemaDispatchLLM(default_responses()), rag_service
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(backend_handler)) as mock_http:
        client.app.state.callback_http_client = mock_http

        # 1. Submit run -> 202
        res1 = client.post("/internal/v1/pipeline-runs", json=pipeline_request, headers=headers)
        assert res1.status_code == 202
        run_id = res1.json()["run_id"]

        # 2. Same Idempotency-Key + same body -> returns same run_id without duplicate execution
        res2 = client.post("/internal/v1/pipeline-runs", json=pipeline_request, headers=headers)
        assert res2.status_code == 202
        assert res2.json()["run_id"] == run_id
        assert len(callbacks) == 1

        # 3. Same Idempotency-Key + different body -> 409 IDEMPOTENCY_CONFLICT
        modified = {**pipeline_request, "review_only": True}
        res_conflict = client.post("/internal/v1/pipeline-runs", json=modified, headers=headers)
        assert res_conflict.status_code == 409
        assert res_conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

        # 4. Lookup completed result via GET /internal/v1/pipeline-runs/{run_id} matches callback
        lookup = client.get(f"/internal/v1/pipeline-runs/{run_id}", headers=headers)
        assert lookup.status_code == 200
        assert PipelineResult.model_validate(lookup.json()) == callbacks[0]


async def test_insufficient_info_then_followup_message_resume_flow(
    rag_service, pipeline_request, settings
):
    await rag_service.ingest(make_doc())
    settings.backend_service_token = SecretStr("backend-token")
    delivered: list[PipelineResult] = []

    def handler(req: httpx.Request) -> httpx.Response:
        delivered.append(PipelineResult.model_validate_json(req.content))
        return httpx.Response(200, json={"apply_status": "applied"})

    store = PipelineRunStore()
    backend_client = BackendClient(settings)

    # Step 1: Revision 1 has insufficient info -> returns NEED_INFO + additional_question
    need_info_llm = SchemaDispatchLLM(
        default_responses(
            {
                InquiryAnalysis: {
                    "category": "기타",
                    "urgency": "보통",
                    "summary": "오류 화면 불명",
                    "symptoms": [],
                    "normalized_error_code": None,
                    "missing_fields": ["발생 화면"],
                    "needs_more_info": True,
                    "additional_question": "어떤 화면에서 문제가 발생했나요?",
                    "search_query": None,
                    "reasons": {
                        "category": "불명",
                        "urgency": "보통",
                        "information": "화면 정보 누락",
                    },
                    "limitations": [],
                }
            }
        )
    )
    req_rev1 = PipelineRequest.model_validate(pipeline_request)
    sub1 = store.submit("syncnote", "idem-rev-1", req_rev1)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        res1 = await execute_pipeline(
            req_rev1,
            make_orchestrator(need_info_llm, rag_service),
            run_id=sub1.accepted.run_id,
            store=store,
            backend_client=backend_client,
            http_client=http_client,
        )
        assert res1.draft_result == DraftResult.NEED_INFO
        assert res1.additional_question == "어떤 화면에서 문제가 발생했나요?"
        assert res1.draft is None
        assert res1.verification.status == CheckStatus.SKIPPED

        # Step 2: Customer replies in revision 2 -> resumes pipeline and produces draft
        followup_payload = {
            **pipeline_request,
            "inquiry_revision": 2,
            "based_on_message_id": "MSG-000003",
            "messages": [
                pipeline_request["messages"][0],
                {
                    "message_id": "MSG-000002",
                    "sender": "ai_auto",
                    "text": "어떤 화면에서 문제가 발생했나요?",
                    "sent_at": "2026-11-04T22:01:00+09:00",
                    "attachments": [],
                },
                {
                    "message_id": "MSG-000003",
                    "sender": "customer",
                    "text": "노트 저장 누를 때 E-502가 떠요.",
                    "sent_at": "2026-11-04T22:02:00+09:00",
                    "attachments": [],
                },
            ],
        }
        req_rev2 = PipelineRequest.model_validate(followup_payload)
        sub2 = store.submit("syncnote", "idem-rev-2", req_rev2)
        res2 = await execute_pipeline(
            req_rev2,
            make_orchestrator(SchemaDispatchLLM(default_responses()), rag_service),
            run_id=sub2.accepted.run_id,
            store=store,
            backend_client=backend_client,
            http_client=http_client,
        )
        assert res2.inquiry_revision == 2
        assert res2.draft_result == DraftResult.READY
        assert res2.draft is not None
        assert len(delivered) == 2


# --- Task 5: 문서 변경에 따른 기존 답변 수정안 생성 및 오탐 방지 ---


async def test_document_change_identifies_stale_best_answers_and_guards_false_positives(
    rag_service,
):
    # Initial DOC-001 v1 (Pro 9,900, Team 14,900)
    await rag_service.ingest(
        make_doc(
            source_id="DOC-001",
            version=1,
            title="요금제 안내",
            text="Pro 월 9,900원 / 연 95,040원. Team 월 14,900원.",
            metadata={"doc_type": "정책"},
        )
    )
    # Two best answers referencing DOC-001 v1: BA-016 (Pro pricing) and BA-017 (Team pricing)
    await rag_service.ingest(
        SourceInput(
            tenant_id="syncnote",
            source_type="우수 답변",
            source_id="BA-016",
            version=1,
            title="Pro 요금제 안내",
            text="Pro 요금제는 월 9,900원(연 95,040원)입니다.",
            visibility="고객 공개",
            status="유효",
            content_hash="sha256:ba016",
            evidence={
                "docs": [{"doc_id": "DOC-001", "version": 1}],
                "code": [{"path": "billing/pricing.ts", "commit_sha": "a1b2c3d"}],
            },
        )
    )
    await rag_service.ingest(
        SourceInput(
            tenant_id="syncnote",
            source_type="우수 답변",
            source_id="BA-017",
            version=1,
            title="Team 요금제 안내",
            text="Team 요금제는 1인당 월 14,900원입니다.",
            visibility="고객 공개",
            status="유효",
            content_hash="sha256:ba017",
            evidence={
                "docs": [{"doc_id": "DOC-001", "version": 1}],
                "code": [{"path": "billing/pricing.ts", "commit_sha": "a1b2c3d"}],
            },
        )
    )

    # Upload DOC-001 v2 (Pro changed to 12,900 / 123,840, Team unchanged at 14,900)
    await rag_service.ingest(
        make_doc(
            source_id="DOC-001",
            version=2,
            title="요금제 안내",
            text="Pro 월 12,900원 / 연 123,840원. Team 월 14,900원.",
            metadata={"doc_type": "정책"},
        )
    )

    def revision_dispatch(data_str: str) -> dict:
        if "BA-016" in data_str:
            return {
                "needs_revision": True,
                "reason": "Pro 월/연 요금이 인상됨",
                "changed_facts": ["pro_monthly: 12900", "pro_annual: 123840"],
                "revised_answer_text": (
                    "안녕하세요, 싱크노트 고객지원팀입니다. "
                    "Pro 요금제는 월 12,900원(연 123,840원)입니다."
                ),
                "citations": [{"text": "Pro 월 12,900원 / 연 123,840원", "doc_ids": ["DOC-001"]}],
            }
        return {
            "needs_revision": False,
            "reason": "Team 요금제(14,900원)는 DOC-001 v2에서도 변동 없음",
            "changed_facts": [],
            "revised_answer_text": None,
            "citations": [],
        }

    llm = SchemaDispatchLLM(
        {
            RevisionLLMOutput: revision_dispatch,
            ReviewLLMOutput: {
                "claims_supported": "통과",
                "internal_data_excluded": "통과",
                "source_conflict": "통과",
                "auto_response_scope": "확인 불가",
                "reasons": ["요금 개정 수정안 담당자 확인"],
                "unsupported_sentences": [],
            },
        }
    )
    revision_service = AnswerRevisionService(llm=llm, rag=rag_service)

    stale_candidates = await revision_service.find_stale_best_answers("syncnote")
    by_id = {c.target_id: c for c in stale_candidates}
    assert {"BA-016", "BA-017"} <= set(by_id)

    # Pro answer -> needs_revision=True, updated to DOC-001 v2 and separately reviewed
    pro_rev = await revision_service.revise_answer("syncnote", by_id["BA-016"])
    assert pro_rev.needs_revision is True
    assert pro_rev.revised_draft is not None
    assert "12,900원" in pro_rev.revised_draft.answer_text
    assert pro_rev.revised_draft.evidence.docs[0].version == 2
    assert pro_rev.verification is not None
    assert pro_rev.verification.checks["claims_supported"] == CheckStatus.PASS

    # Team answer -> needs_revision=False (false-positive guard!)
    team_rev = await revision_service.revise_answer("syncnote", by_id["BA-017"])
    assert team_rev.needs_revision is False
    assert team_rev.revised_draft is None


async def test_stale_revision_suppresses_outdated_callback_and_callback_failure_keeps_run_completed(
    rag_service, pipeline_request, settings
):
    await rag_service.ingest(make_doc())
    pipeline_request["messages"][0]["text"] = "저장할 때 E502가 떠요."
    settings.backend_service_token = SecretStr("backend-token")
    delivered: list[PipelineResult] = []

    def ok_handler(req: httpx.Request) -> httpx.Response:
        delivered.append(PipelineResult.model_validate_json(req.content))
        return httpx.Response(200, json={"apply_status": "applied"})

    store = PipelineRunStore()
    backend_client = BackendClient(settings)
    orchestrator = make_orchestrator(SchemaDispatchLLM(default_responses()), rag_service)

    # Submit revision 1 first, then revision 2 arrives before revision 1 finishes
    req_rev1 = PipelineRequest.model_validate(pipeline_request)
    sub1 = store.submit("syncnote", "idem-stale-1", req_rev1)

    rev2_payload = {**pipeline_request, "inquiry_revision": 2}
    req_rev2 = PipelineRequest.model_validate(rev2_payload)
    sub2 = store.submit("syncnote", "idem-stale-2", req_rev2)

    async with httpx.AsyncClient(transport=httpx.MockTransport(ok_handler)) as http_client:
        # Execute revision 1 now that revision 2 is already the latest revision
        await execute_pipeline(
            req_rev1,
            orchestrator,
            run_id=sub1.accepted.run_id,
            store=store,
            backend_client=backend_client,
            http_client=http_client,
        )
        assert store.callback_status(sub1.accepted.run_id) == "보류(구버전)"
        assert len(delivered) == 0

        # Execute revision 2 -> callback is delivered
        await execute_pipeline(
            req_rev2,
            orchestrator,
            run_id=sub2.accepted.run_id,
            store=store,
            backend_client=backend_client,
            http_client=http_client,
        )
        assert store.callback_status(sub2.accepted.run_id) == "완료"
        assert len(delivered) == 1

    # Now test callback failure: pipeline result must still remain COMPLETED for GET recovery
    def fail_handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    rev3_payload = {**pipeline_request, "inquiry_revision": 3}
    req_rev3 = PipelineRequest.model_validate(rev3_payload)
    sub3 = store.submit("syncnote", "idem-rev-3", req_rev3)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail_handler)) as fail_http:
        res3 = await execute_pipeline(
            req_rev3,
            orchestrator,
            run_id=sub3.accepted.run_id,
            store=store,
            backend_client=backend_client,
            http_client=fail_http,
        )
        assert res3.run_status == RunStatus.COMPLETED
        assert store.callback_status(sub3.accepted.run_id) == "실패"
        assert store.get("syncnote", sub3.accepted.run_id).run_status == RunStatus.COMPLETED


async def test_preview_endpoints_support_full_pipeline_and_answer_revision(
    rag_service, pipeline_request, settings, headers
):
    await rag_service.ingest(
        make_doc(
            source_id="DOC-001",
            version=2,
            title="요금제 안내",
            text="Pro 월 12,900원 / 연 123,840원.",
            metadata={"doc_type": "정책"},
        )
    )
    await rag_service.ingest(make_doc())
    settings.enable_preview = True
    app = create_app(settings)
    llm = SchemaDispatchLLM(
        {
            **default_responses(),
            RevisionLLMOutput: {
                "needs_revision": True,
                "reason": "Pro 요금 변경",
                "changed_facts": ["pro_monthly: 12900"],
                "revised_answer_text": "Pro 요금제는 월 12,900원입니다.",
                "citations": [{"text": "Pro 월 12,900원", "doc_ids": ["DOC-001"]}],
            },
        }
    )
    app.state.rag_service = rag_service
    app.state.inquiry_analyzer = InquiryAnalyzer(llm)
    app.state.pipeline_orchestrator = make_orchestrator(llm, rag_service)
    app.state.answer_revision_service = AnswerRevisionService(llm=llm, rag=rag_service)

    with TestClient(app) as client:
        pipeline_request["messages"][0]["text"] = "저장할 때 E502가 떠요."
        resp = client.post(
            "/internal/v1/pipeline-preview?full_pipeline=true",
            json=pipeline_request,
            headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["stage"] == "전체 파이프라인 완료"
        assert body["pipeline_result"]["draft_result"] == DraftResult.READY.value

        rev_resp = client.post(
            "/internal/v1/answer-revision-preview",
            json={
                "tenant_id": "syncnote",
                "candidate": {
                    "target_id": "BA-016",
                    "inquiry_id": "Q-0052",
                    "category": "결제",
                    "title": "Pro 요금제 안내",
                    "answer_text": "Pro 요금제는 월 9,900원입니다.",
                    "evidence": {
                        "docs": [{"doc_id": "DOC-001", "version": 1}],
                        "code": [],
                    },
                },
            },
            headers=headers,
        )
        assert rev_resp.status_code == 200
        rev_data = rev_resp.json()
        assert rev_data["needs_revision"] is True
        assert "12,900원" in rev_data["revised_draft"]["answer_text"]
