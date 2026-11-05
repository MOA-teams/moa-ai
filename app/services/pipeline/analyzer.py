import json

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


def observed_error_codes(request: PipelineRequest) -> set[str]:
    texts = [message.text for message in request.messages if message.sender == Sender.CUSTOMER]
    codes: set[str] = set()
    for text in texts:
        codes.update(extract_error_codes(text))
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
        try:
            output = await self.llm.generate(
                instruction=ANALYSIS_INSTRUCTION + "\n\n" + ROUTING_POLICY,
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

        if any(message.attachments for message in request.messages):
            if ATTACHMENT_LIMITATION not in result.limitations:
                result.limitations.append(ATTACHMENT_LIMITATION)
        return result
