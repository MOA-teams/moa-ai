import asyncio

import httpx

from app.core.config import Settings
from app.core.errors import ServiceError
from app.schemas.common import RunStatus
from app.schemas.pipeline import PipelineResult


class BackendClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def deliver(self, result: PipelineResult, client: httpx.AsyncClient) -> None:
        if result.run_status not in {RunStatus.COMPLETED, RunStatus.FAILED}:
            raise ValueError("Only terminal pipeline results may be delivered")
        token = self.settings.backend_service_token.get_secret_value()
        if not token:
            raise ServiceError(503, "CALLBACK_NOT_CONFIGURED", "Backend token is required")
        url = str(self.settings.backend_base_url).rstrip("/") + "/internal/v1/pipeline-results"
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Tenant-ID": result.tenant_id,
            "Idempotency-Key": f"pipeline-result:{result.run_id}",
        }
        payload = result.model_dump(mode="json")
        if not self.settings.backend_include_routing:
            # Old backend schemas reject extra fields. Keep the recommendation visible
            # in existing reasons until the backend accepts structured routing.
            payload.pop("routing", None)
            if result.routing is not None and payload.get("verification") is not None:
                reasons = payload["verification"]["reasons"]
                note = f"담당 팀: {result.routing.team}. {result.routing.reason}"
                if note not in reasons:
                    reasons.append(note)
        for attempt in range(3):
            try:
                response = await client.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=10,
                    follow_redirects=False,
                )
                if 200 <= response.status_code < 300:
                    return
                if response.status_code not in {409, 429} and response.status_code < 500:
                    break
            except httpx.TransportError:
                pass
            if attempt < 2:
                await asyncio.sleep(2**attempt)
        raise ServiceError(502, "CALLBACK_DELIVERY_FAILED", "Backend did not accept the result")
