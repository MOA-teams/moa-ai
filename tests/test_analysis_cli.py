import json

from app.cli import analyze
from app.core.errors import ServiceError


def test_cli_prints_json_result(monkeypatch, capsys):
    async def run(path):
        return {"inquiry_id": "Q-1001", "analysis": {"category": "계정"}}

    monkeypatch.setattr(analyze, "analyze_file", run)
    monkeypatch.setattr("sys.argv", ["analyze", "examples/analysis/account.json"])
    assert analyze.main() == 0
    assert json.loads(capsys.readouterr().out)["analysis"]["category"] == "계정"


def test_cli_missing_key_has_actionable_error_without_traceback(monkeypatch, capsys):
    async def run(path):
        raise ServiceError(503, "GEMINI_NOT_CONFIGURED", "Set Gemini key and model")

    monkeypatch.setattr(analyze, "analyze_file", run)
    monkeypatch.setattr("sys.argv", ["analyze", "examples/analysis/account.json"])
    assert analyze.main() == 1
    output = capsys.readouterr()
    assert not output.out
    assert json.loads(output.err)["error"]["code"] == "GEMINI_NOT_CONFIGURED"


def test_cli_missing_input_is_sanitized(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["analyze", "not-existing-file.json"])
    assert analyze.main() == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "INVALID_INPUT"


def test_debug_includes_provider_status_but_redacts_keys(monkeypatch, capsys, settings):
    from google.genai import errors
    from pydantic import SecretStr

    secret = "AIza-test-credential-for-redaction"
    settings.gemini_api_key = SecretStr(secret)
    settings.gemini_model = "gemini-test-model"
    monkeypatch.setattr(analyze, "get_settings", lambda: settings)

    async def run(path):
        try:
            raise errors.APIError(
                503,
                {
                    "error": {
                        "message": f"High demand; key={secret}",
                    }
                },
            )
        except errors.APIError as exc:
            raise ServiceError(502, "GEMINI_UNAVAILABLE", "Gemini request failed") from exc

    monkeypatch.setattr(analyze, "analyze_file", run)
    monkeypatch.setattr("sys.argv", ["analyze", "example.json", "--debug"])
    assert analyze.main() == 1
    output = capsys.readouterr()
    data = json.loads(output.err)
    assert data["diagnostics"]["provider_http_status"] == 503
    assert data["diagnostics"]["model"] == "gemini-test-model"
    assert secret not in output.err
    assert "REDACTED" in data["diagnostics"]["provider_message"]


def test_debug_distinguishes_local_timeout_from_provider_http_error(monkeypatch, capsys, settings):
    import httpx

    monkeypatch.setattr(analyze, "get_settings", lambda: settings)

    async def run(path):
        try:
            raise httpx.ReadTimeout("private transport details")
        except httpx.ReadTimeout as exc:
            raise ServiceError(504, "GEMINI_TIMEOUT", "Gemini request timed out") from exc

    monkeypatch.setattr(analyze, "analyze_file", run)
    monkeypatch.setattr("sys.argv", ["analyze", "example.json", "--debug"])
    assert analyze.main() == 1
    output = capsys.readouterr()
    data = json.loads(output.err)
    assert data["diagnostics"]["provider_http_status"] is None
    assert data["diagnostics"]["cause_type"] == "ReadTimeout"
    assert "private transport details" not in output.err
