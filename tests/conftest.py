import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def settings():
    return Settings(_env_file=None, internal_token=SecretStr("test-service-token"))


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture
def headers():
    return {
        "Authorization": "Bearer test-service-token",
        "X-Tenant-ID": "syncnote",
        "Idempotency-Key": "test-request-001",
    }


@pytest.fixture
def pipeline_request():
    return {
        "tenant_id": "syncnote",
        "inquiry_id": "Q-1001",
        "inquiry_revision": 1,
        "based_on_message_id": "MSG-000001",
        "customer_category": "기타",
        "messages": [
            {
                "message_id": "MSG-000001",
                "sender": "customer",
                "text": "저장이 안 돼요",
                "sent_at": "2026-11-04T22:00:00+09:00",
                "attachments": [],
            }
        ],
        "env": {"os": "iOS 18", "app_version": "2.4.0", "error_code": None},
        "review_only": False,
    }
