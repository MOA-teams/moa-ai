import json
import re

from pydantic import ValidationError

from app.core.errors import ServiceError
from app.schemas.analysis import InquiryAnalysis
from app.schemas.common import Sender
from app.schemas.pipeline import PipelineRequest
from app.services.llm.base import StructuredLLM
from app.services.pipeline.analysis_prompt import ANALYSIS_INSTRUCTION

CODE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])E[\s-]*(?:([A-Za-z]+)[\s-]*)?(\d{2,3})(?![A-Za-z0-9])",
    re.IGNORECASE,
)
ATTACHMENT_LIMITATION = "첨부 이미지·PDF 내용은 이번 단계에서 분석하지 않았습니다."


def observed_error_codes(request: PipelineRequest) -> set[str]:
    texts = [message.text for message in request.messages if message.sender == Sender.CUSTOMER]
    if request.env.error_code:
        texts.append(request.env.error_code)
    codes: set[str] = set()
    for text in texts:
        for match in CODE_PATTERN.finditer(text):
            area, digits = match.groups()
            codes.add(f"E-{area.upper()}-{digits}" if area else f"E-{digits}")
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
        }
        data = json.dumps(context, ensure_ascii=False)
        if len(data) > self.max_input_chars:
            raise ServiceError(
                413, "ANALYSIS_INPUT_TOO_LARGE", "Conversation exceeds analysis limit"
            )
        try:
            output = await self.llm.generate(
                instruction=ANALYSIS_INSTRUCTION, data=data, schema=InquiryAnalysis
            )
            result = InquiryAnalysis.model_validate(output.model_dump())
        except ValidationError as exc:
            raise ServiceError(
                502, "INVALID_ANALYSIS_RESPONSE", "Invalid analysis structure"
            ) from exc
        if result.normalized_error_code is not None:
            match = CODE_PATTERN.fullmatch(result.normalized_error_code.strip())
            normalized = None
            if match:
                area, digits = match.groups()
                normalized = f"E-{area.upper()}-{digits}" if area else f"E-{digits}"
            if normalized is None or normalized not in observed_error_codes(request):
                raise ServiceError(
                    502, "UNGROUNDED_ERROR_CODE", "Analysis contained an unreported error code"
                )
            result.normalized_error_code = normalized

        if result.normalized_error_code is not None and result.needs_more_info and result.symptoms:
            result = result.model_copy(
                update={
                    "needs_more_info": False,
                    "missing_fields": [],
                    "additional_question": None,
                    "search_query": f"{result.summary} {result.normalized_error_code}".strip(),
                }
            )

        if any(message.attachments for message in request.messages):
            if ATTACHMENT_LIMITATION not in result.limitations:
                result.limitations.append(ATTACHMENT_LIMITATION)
        return result
