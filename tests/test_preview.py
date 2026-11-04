from fastapi.testclient import TestClient

from app.main import create_app
from app.schemas.analysis import InquiryAnalysis


def test_preview_is_opt_in(client):
    assert "/internal/v1/pipeline-preview" not in client.get("/openapi.json").json()["paths"]


def test_information_question_preview_does_not_require_rag(settings, headers, pipeline_request):
    class Analyzer:
        async def analyze(self, request):
            return InquiryAnalysis(
                category="기타",
                urgency="보통",
                summary="대상 불명",
                symptoms=[],
                needs_more_info=True,
                missing_fields=["발생 화면"],
                additional_question="어떤 화면인가요?",
                search_query=None,
                reasons={
                    "category": "의도 불명",
                    "urgency": "영향 불명",
                    "information": "화면 불명",
                },
            )

    settings.enable_preview = True
    app = create_app(settings)
    app.state.inquiry_analyzer = Analyzer()
    with TestClient(app) as client:
        result = client.post(
            "/internal/v1/pipeline-preview", json=pipeline_request, headers=headers
        )
        assert result.status_code == 200
        assert result.json()["preview_only"]
        assert result.json()["retrieval"] is None
