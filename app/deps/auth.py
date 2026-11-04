from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, Header
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, get_settings
from app.core.errors import ServiceError

bearer = HTTPBearer(auto_error=False)


async def require_service_tenant(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    tenant_id: Annotated[str, Header(alias="X-Tenant-ID", examples=["syncnote"])],
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    expected = settings.internal_token.get_secret_value()
    if not expected:
        raise ServiceError(503, "AUTH_NOT_CONFIGURED", "Internal authentication is not configured")
    if credentials is None or not compare_digest(credentials.credentials, expected):
        raise ServiceError(401, "UNAUTHORIZED", "Invalid service credentials")
    if tenant_id not in settings.tenant_ids:
        raise ServiceError(403, "FORBIDDEN", "Tenant access denied")
    return tenant_id


ServiceTenant = Annotated[str, Depends(require_service_tenant)]


def check_body_tenant(body_tenant_id: str, authenticated_tenant_id: str) -> None:
    if body_tenant_id != authenticated_tenant_id:
        raise ServiceError(403, "FORBIDDEN", "Tenant does not match service context")
