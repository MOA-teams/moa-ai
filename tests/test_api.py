from pydantic import SecretStr

from app.main import create_app


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_internal_routes_are_separate_from_backend(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "/internal/v1/pipeline-runs" in paths
    assert "/internal/v1/rag/source-statuses" in paths
    assert not any(path.startswith("/api/v1/inquiries") for path in paths)
    assert len([path for path in paths if path.startswith("/internal/v1")]) == 5


def test_auth_required(client, pipeline_request):
    response = client.post(
        "/internal/v1/pipeline-runs",
        json=pipeline_request,
        headers={"X-Tenant-ID": "syncnote", "Idempotency-Key": "test"},
    )
    assert response.status_code == 401


def test_body_tenant_must_match_credentials(client, headers, pipeline_request):
    pipeline_request["tenant_id"] = "other"
    response = client.post("/internal/v1/pipeline-runs", json=pipeline_request, headers=headers)
    assert response.status_code == 403


def test_unknown_tenant_denied(client, headers):
    headers["X-Tenant-ID"] = "other"
    assert client.get("/internal/v1/pipeline-runs/RUN-000001", headers=headers).status_code == 403


def test_pipeline_run_submission_and_lookup(client, headers, pipeline_request, monkeypatch):
    from app.core.errors import ServiceError

    async def unavailable(self, **kwargs):
        raise ServiceError(503, "LLM_NOT_CONFIGURED", "Test credentials are absent")

    monkeypatch.setattr("app.services.llm.bedrock.BedrockLLM.generate", unavailable)
    response = client.post("/internal/v1/pipeline-runs", json=pipeline_request, headers=headers)
    assert response.status_code == 202
    data = response.json()
    assert data["run_id"].startswith("RUN-")
    assert len(data["run_id"]) == 36
    assert data["inquiry_id"] == "Q-1001"
    assert data["run_status"] == "대기"

    lookup = client.get(f"/internal/v1/pipeline-runs/{data['run_id']}", headers=headers)
    assert lookup.status_code == 200
    assert lookup.json()["run_id"] == data["run_id"]


def test_invalid_input_does_not_echo_customer_text(client, headers, pipeline_request):
    pipeline_request["messages"][0]["sent_at"] = "private-invalid-value"
    response = client.post("/internal/v1/pipeline-runs", json=pipeline_request, headers=headers)
    assert response.status_code == 422
    assert "private-invalid-value" not in response.text
    assert pipeline_request["messages"][0]["text"] not in response.text


def test_no_default_secret(settings):
    from fastapi.testclient import TestClient

    settings.internal_token = SecretStr("")
    with TestClient(create_app(settings)) as client:
        response = client.get(
            "/internal/v1/pipeline-runs/RUN-000001", headers={"X-Tenant-ID": "syncnote"}
        )
        assert response.status_code == 503
