import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.errors import ServiceError
from app.schemas.analysis import InquiryAnalysis
from app.schemas.pipeline import Analysis, PipelineRequest
from app.services.pipeline.analyzer import (
    ATTACHMENT_LIMITATION,
    InquiryAnalyzer,
    observed_error_codes,
)


class StubLLM:
    """Only tests integration/validation; not a substitute for measured Gemini quality."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        return kwargs["schema"].model_validate(self.result)


def searchable_result(**changes):
    result = {
        "category": "기능 오류",
        "urgency": "보통",
        "summary": "저장 시 오류 발생",
        "symptoms": ["저장 실패"],
        "normalized_error_code": "E-502",
        "missing_fields": [],
        "needs_more_info": False,
        "additional_question": None,
        "search_query": "저장 E-502 오류",
        "reasons": {
            "category": "고객이 저장 동작 실패를 보고함",
            "urgency": "개별 사용자 오류이며 광범위한 피해 보고 없음",
            "information": "저장 작업과 오류 코드가 있어 검색 가능",
        },
        "limitations": [],
    }
    result.update(changes)
    return result


@pytest.mark.parametrize("name", ["account", "insufficient", "error"])
def test_examples_match_backend_request_contract(name):
    path = Path(f"examples/analysis/{name}.json")
    assert PipelineRequest.model_validate_json(path.read_text()).tenant_id == "syncnote"


async def test_analysis_receives_history_and_does_not_forward_identity(pipeline_request):
    pipeline_request["messages"][0]["text"] = "저장할 때 E502가 떠요"
    request = PipelineRequest.model_validate(pipeline_request)
    llm = StubLLM(searchable_result())
    result = await InquiryAnalyzer(llm).analyze(request)
    assert result.category.value == "기능 오류"
    assert not result.needs_more_info
    data = json.loads(llm.calls[0]["data"])
    assert data["messages"][0]["text"] == request.messages[0].text
    assert "tenant_id" not in data
    assert "inquiry_id" not in data
    assert data["customer_category"] == "기타"


async def test_missing_information_returns_question_without_search(pipeline_request):
    llm = StubLLM(
        searchable_result(
            category="기타",
            summary="문의 대상이 불명확함",
            symptoms=[],
            normalized_error_code=None,
            needs_more_info=True,
            missing_fields=["발생 화면"],
            additional_question="어떤 화면에서 문제가 발생했나요?",
            search_query=None,
        )
    )
    result = await InquiryAnalyzer(llm).analyze(PipelineRequest.model_validate(pipeline_request))
    assert result.needs_more_info
    assert result.additional_question
    assert result.search_query is None


@pytest.mark.parametrize(
    "changes",
    [
        {"needs_more_info": True},
        {"missing_fields": ["발생 화면"]},
        {"additional_question": "어떤 화면인가요?"},
        {"search_query": None},
        {"search_query": " "},
    ],
)
def test_contradictory_information_results_rejected(changes):
    with pytest.raises(ValidationError):
        InquiryAnalysis.model_validate(searchable_result(**changes))


async def test_invented_error_code_rejected(pipeline_request):
    llm = StubLLM(searchable_result())
    with pytest.raises(ServiceError) as exc:
        await InquiryAnalyzer(llm).analyze(PipelineRequest.model_validate(pipeline_request))
    assert exc.value.code == "UNGROUNDED_ERROR_CODE"


@pytest.mark.parametrize("code", ["E502", "e-502", " E 502 "])
async def test_model_code_spelling_is_normalized_before_grounding(pipeline_request, code):
    pipeline_request["messages"][0]["text"] = "저장 시 E502가 떠요"
    result = await InquiryAnalyzer(StubLLM(searchable_result(normalized_error_code=code))).analyze(
        PipelineRequest.model_validate(pipeline_request)
    )
    assert result.normalized_error_code == "E-502"


async def test_model_cannot_add_code_area(pipeline_request):
    pipeline_request["messages"][0]["text"] = "저장 시 E502가 떠요"
    with pytest.raises(ServiceError) as exc:
        await InquiryAnalyzer(
            StubLLM(searchable_result(normalized_error_code="E-STORAGE-502"))
        ).analyze(PipelineRequest.model_validate(pipeline_request))
    assert exc.value.code == "UNGROUNDED_ERROR_CODE"


def test_codes_normalize_only_customer_text_and_env(pipeline_request):
    pipeline_request["messages"][0]["text"] = "e502랑 E-PAY-07이 떠요"
    pipeline_request["messages"].append(
        {
            "message_id": "MSG-000002",
            "sender": "agent",
            "text": "E-401인가요?",
            "sent_at": "2026-11-04T22:01:00+09:00",
            "attachments": [],
        }
    )
    pipeline_request["env"]["error_code"] = "e-413"
    assert observed_error_codes(PipelineRequest.model_validate(pipeline_request)) == {
        "E-502",
        "E-PAY-07",
        "E-413",
    }


async def test_attachments_are_reported_as_not_read(pipeline_request):
    pipeline_request["messages"][0]["attachments"] = ["ATT-0001"]
    result = await InquiryAnalyzer(StubLLM(searchable_result(normalized_error_code=None))).analyze(
        PipelineRequest.model_validate(pipeline_request)
    )
    assert ATTACHMENT_LIMITATION in result.limitations


async def test_oversized_context_is_rejected_before_model_call(pipeline_request):
    llm = StubLLM(searchable_result())
    with pytest.raises(ServiceError) as exc:
        await InquiryAnalyzer(llm, max_input_chars=10).analyze(
            PipelineRequest.model_validate(pipeline_request)
        )
    assert exc.value.status_code == 413
    assert not llm.calls


def test_stage_result_projects_to_existing_pipeline_contract():
    result = InquiryAnalysis.model_validate(searchable_result())
    projected = Analysis.model_validate(result.model_dump(include=set(Analysis.model_fields)))
    assert projected.category == result.category
    assert projected.normalized_error_code == "E-502"


@pytest.mark.parametrize(
    "text,expected_category",
    [
        ("비밀번호 변경 방법을 알려주세요.", "계정"),
        ("Pro 요금제의 월 가격을 알려주세요.", "결제"),
    ],
)
async def test_category_boundaries_override_usage_for_account_and_payment(
    pipeline_request, text, expected_category
):
    pipeline_request["messages"][0]["text"] = text
    llm = StubLLM(
        searchable_result(
            category="이용 방법",
            normalized_error_code=None,
            summary=text,
            search_query=text,
        )
    )
    result = await InquiryAnalyzer(llm).analyze(PipelineRequest.model_validate(pipeline_request))
    assert result.category.value == expected_category


async def test_actionable_error_code_inquiry_proceeds_to_search(pipeline_request):
    pipeline_request["messages"][0]["text"] = "저장을 누르면 E502가 떠요."
    llm = StubLLM(
        searchable_result(
            category="기능 오류",
            normalized_error_code="E-502",
            summary="저장 버튼 클릭 시 E-502 오류 발생",
            symptoms=["저장 시 E-502 발생"],
            needs_more_info=True,
            missing_fields=["발생 화면", "발생 시점"],
            additional_question="어느 화면에서 저장할 때 발생하나요?",
            search_query=None,
        )
    )
    result = await InquiryAnalyzer(llm).analyze(PipelineRequest.model_validate(pipeline_request))
    assert result.needs_more_info is False
    assert result.missing_fields == []
    assert result.additional_question is None
    assert result.search_query is not None and "E-502" in result.search_query
