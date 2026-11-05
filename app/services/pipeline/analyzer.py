import json
import logging
import re

from pydantic import ValidationError

from app.core.errors import ServiceError
from app.schemas.analysis import InquiryAnalysis
from app.schemas.common import Sender
from app.schemas.pipeline import PipelineRequest
from app.schemas.routing import ROUTING_POLICY
from app.services.llm.base import StructuredLLM
from app.services.pipeline.analysis_prompt import ANALYSIS_INSTRUCTION
from app.services.pipeline.error_codes import extract_error_codes, resolve_reported_code

ATTACHMENT_LIMITATION = "첨부 이미지·PDF 내용은 이번 단계에서 분석하지 않았습니다."
logger = logging.getLogger(__name__)
RECOVERABLE_ANALYSIS_ERRORS = {
    "INVALID_LLM_RESPONSE",
    "EMPTY_LLM_RESPONSE",
    "INVALID_ANALYSIS_RESPONSE",
    "UNGROUNDED_ERROR_CODE",
}
REPAIR_INSTRUCTION = """
이전 출력은 구조 검증을 통과하지 못했다. 원본 입력을 다시 분석하여 스키마를 정확히 지킨다.
정보가 적거나 의도를 모르는 입력도 오류가 아니다. 판단 불가한 코드는 null, symptoms는 []로 둔다.
needs_more_info=true이면 missing_fields는 허용된 항목 1~2개, additional_question은 실제 질문,
search_query=null이다. false이면 missing_fields=[], additional_question=null이다.
false일 때 search_query는 비어 있지 않은 문자열이다.
첨부 ID는 이미지 내용이 아니다. 이미지 설명이나 문의 대상이 없으면 발생 화면·재현 단계를 묻는다.
이미 충분한 텍스트가 있으면 첨부 개수와 무관하게 그 텍스트를 분석한다.
""".strip()


def clarification_fallback() -> InquiryAnalysis:
    """Unreadable model output must never become a factual answer or guessed team."""
    return InquiryAnalysis(
        category="기타",
        urgency="보통",
        summary="문의 내용을 안정적으로 해석하지 못해 추가 설명이 필요합니다.",
        normalized_error_code=None,
        symptoms=[],
        missing_fields=["발생 화면", "재현 단계"],
        needs_more_info=True,
        additional_question="어떤 화면에서 무엇을 하려던 상황인지 글로 설명해 주시겠어요?",
        search_query=None,
        reasons={
            "category": "안정적인 분류 결과를 얻지 못해 임시로 기타로 접수합니다.",
            "urgency": "피해 범위와 긴급도를 확정하지 못했습니다.",
            "information": "구조 검증 재시도 후에도 해석을 확인하지 못해 설명을 요청합니다.",
        },
        limitations=["AI 분석 결과를 검증하지 못해 안전한 추가 질문으로 전환했습니다."],
        has_action_context=False,
        context_evidence=None,
    )


def observed_error_codes(request: PipelineRequest) -> set[str]:
    codes: set[str] = set()
    error_context = False
    for message in request.messages:
        if message.sender == Sender.CUSTOMER:
            codes.update(extract_error_codes(message.text, explicit_field=error_context))
        # A bare reply such as '401이요' is meaningful after an error report or
        # a question about its code; a price/count with units never matches.
        error_context = bool(re.search(r"오류|에러|\bHTTP\b|error|상태\s*코드", message.text, re.I))
    if request.env.error_code:
        codes.update(extract_error_codes(request.env.error_code, explicit_field=True))
    return codes


class InquiryAnalyzer:
    def __init__(self, llm: StructuredLLM, *, max_input_chars: int = 30_000):
        self.llm = llm
        self.max_input_chars = max_input_chars

    async def analyze(self, request: PipelineRequest) -> InquiryAnalysis:
        # IDs/revisions are used locally; the provider only needs the conversation context.
        context = {
            "customer_category": request.customer_category.value,
            "messages": [
                {
                    "sender": message.sender.value,
                    "text": message.text,
                    "attachments": message.attachments,
                }
                for message in request.messages
            ],
            "env": request.env.model_dump(mode="json"),
            "reported_error_codes": sorted(observed_error_codes(request)),
        }
        data = json.dumps(context, ensure_ascii=False)
        if len(data) > self.max_input_chars:
            raise ServiceError(
                413, "ANALYSIS_INPUT_TOO_LARGE", "Conversation exceeds analysis limit"
            )
        result = None
        for attempt in range(2):
            try:
                result = await self._generate(
                    request,
                    data,
                    ANALYSIS_INSTRUCTION
                    + "\n\n"
                    + ROUTING_POLICY
                    + ("\n\n" + REPAIR_INSTRUCTION if attempt else ""),
                )
                break
            except (ServiceError, ValidationError) as exc:
                code = exc.code if isinstance(exc, ServiceError) else "INVALID_ANALYSIS_RESPONSE"
                if code not in RECOVERABLE_ANALYSIS_ERRORS:
                    raise
                logger.warning("Analysis output rejected attempt=%s code=%s", attempt + 1, code)
        if result is None:
            result = clarification_fallback()
            if sum(m.sender == Sender.CUSTOMER for m in request.messages) > 1:
                result.requires_manual_review = True
                result.limitations.append(
                    "후속 대화 해석을 검증하지 못해 반복 질문 대신 담당자 확인이 필요합니다."
                )
        if any(message.attachments for message in request.messages):
            if ATTACHMENT_LIMITATION not in result.limitations:
                result.limitations.append(ATTACHMENT_LIMITATION)
        return result

    async def _generate(self, request: PipelineRequest, data: str, instruction: str):
        try:
            output = await self.llm.generate(
                instruction=instruction,
                data=data,
                schema=InquiryAnalysis,
            )
            result = InquiryAnalysis.model_validate(output.model_dump())
        except ValidationError as exc:
            raise ServiceError(
                502, "INVALID_ANALYSIS_RESPONSE", "Invalid analysis structure"
            ) from exc
        if result.normalized_error_code is not None:
            normalized = resolve_reported_code(
                result.normalized_error_code, observed_error_codes(request)
            )
            if normalized is None:
                raise ServiceError(
                    502, "UNGROUNDED_ERROR_CODE", "Analysis contained an unreported error code"
                )
            result.normalized_error_code = normalized
            if result.search_query and normalized not in result.search_query:
                result.search_query = f"{result.search_query} {normalized}"[:1000]

            if result.has_action_context is not None:
                # A reported code is not itself an action. Trust the structured
                # context decision only when supported by a literal customer quote.
                quote = (result.context_evidence or "").strip()
                grounded_context = (
                    result.has_action_context
                    and bool(quote)
                    and any(
                        quote in message.text
                        for message in request.messages
                        if message.sender == Sender.CUSTOMER
                    )
                )
                if grounded_context:
                    result = result.model_copy(
                        update={
                            "needs_more_info": False,
                            "missing_fields": [],
                            "additional_question": None,
                            "search_query": result.search_query
                            or f"{result.summary} {normalized}"[:1000],
                        }
                    )
                else:
                    result = result.model_copy(
                        update={
                            "needs_more_info": True,
                            "missing_fields": ["발생 화면", "재현 단계"],
                            "additional_question": "어떤 화면에서 무엇을 하다가 오류가 발생했나요?",
                            "search_query": None,
                        }
                    )

        return result
