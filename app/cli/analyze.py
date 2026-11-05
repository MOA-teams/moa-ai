"""Run only the first pipeline feature; no database, HTTP server, or callback required."""

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

from pydantic import ValidationError

from app.core.config import get_settings
from app.core.errors import ServiceError
from app.schemas.pipeline import PipelineRequest
from app.services.llm.bedrock import create_llm
from app.services.pipeline.analyzer import InquiryAnalyzer


async def analyze_file(path: Path) -> dict:
    request = PipelineRequest.model_validate_json(path.read_text(encoding="utf-8"))
    settings = get_settings()
    if request.tenant_id not in settings.tenant_ids:
        raise ServiceError(403, "FORBIDDEN", "Tenant is not configured for this service")
    result = await InquiryAnalyzer(
        create_llm(settings), max_input_chars=settings.analysis_max_input_chars
    ).analyze(request)
    return {
        "inquiry_id": request.inquiry_id,
        "inquiry_revision": request.inquiry_revision,
        "based_on_message_id": request.based_on_message_id,
        "analysis": result.model_dump(mode="json"),
    }


def diagnostic_details(exc: ServiceError, elapsed: float) -> dict:
    settings = get_settings()
    cause = exc.__cause__
    provider_code = getattr(cause, "code", None)
    provider_message = getattr(cause, "message", None)
    secrets = [
        settings.gemini_api_key.get_secret_value(),
        settings.bedrock_bearer_token.get_secret_value(),
        settings.internal_token.get_secret_value(),
        settings.backend_service_token.get_secret_value(),
    ]
    if isinstance(provider_message, str):
        for secret in secrets:
            if secret:
                provider_message = provider_message.replace(secret, "[REDACTED]")
        provider_message = re.sub(r"AIza[\w-]+", "[REDACTED]", provider_message)
        provider_message = re.sub(r"ABSK[\w=-]+", "[REDACTED]", provider_message)
        provider_message = re.sub(r"(?i)(key=)[^\s&]+", r"\1[REDACTED]", provider_message)[:1500]
    else:
        provider_message = None
    model = settings.gemini_model
    return {
        "model": model if re.fullmatch(r"(?:models/)?gemini-[\w.-]+", model) else None,
        "provider_http_status": provider_code if isinstance(provider_code, int) else None,
        "provider_message": provider_message,
        "cause_type": type(cause).__name__ if cause else None,
        "elapsed_seconds": round(elapsed, 2),
        "timeout_ms": settings.gemini_timeout_ms,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze an inquiry using Gemini")
    parser.add_argument("input", type=Path, help="PipelineRequest JSON file")
    parser.add_argument("--debug", action="store_true", help="Show redacted provider diagnostics")
    args = parser.parse_args()
    started = time.monotonic()
    try:
        result = asyncio.run(analyze_file(args.input))
    except ServiceError as exc:
        payload = {"error": {"code": exc.code, "message": exc.message}}
        if args.debug:
            payload["diagnostics"] = diagnostic_details(exc, time.monotonic() - started)
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    except (OSError, ValidationError):
        print(
            json.dumps(
                {"error": {"code": "INVALID_INPUT", "message": "입력 파일과 설정을 확인하세요."}},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
