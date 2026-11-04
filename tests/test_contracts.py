import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.common import CodeEvidence
from app.schemas.pipeline import PipelineRequest, PipelineResult
from app.schemas.rag import SourceInput, SourceStatusBatch


@pytest.mark.parametrize(
    "section,model",
    [
        ("I01", PipelineRequest),
        ("I02", PipelineResult),
        ("I03", SourceInput),
        ("I04", SourceStatusBatch),
        ("B01", PipelineResult),
    ],
)
def test_backend_document_examples_match_schemas(section, model):
    spec = Path("docs/APISpec.md").read_text()
    chunk = re.search(rf"### {section} ·(.*?)(?=\n### |\n## |\Z)", spec, re.S).group(1)
    example = re.search(r"```json\n(.*?)\n```", chunk, re.S).group(1)
    model.model_validate(json.loads(example))


def test_naive_time_rejected(pipeline_request):
    pipeline_request["messages"][0]["sent_at"] = "2026-11-04T22:00:00"
    with pytest.raises(ValidationError):
        PipelineRequest.model_validate(pipeline_request)


def test_label_leakage_rejected(pipeline_request):
    pipeline_request["customer_reaction"] = "해결"
    with pytest.raises(ValidationError):
        PipelineRequest.model_validate(pipeline_request)


def test_stale_snapshot_rejected(pipeline_request):
    pipeline_request["based_on_message_id"] = "MSG-000999"
    with pytest.raises(ValidationError):
        PipelineRequest.model_validate(pipeline_request)


@pytest.mark.parametrize("path", ["/etc/passwd", "../secret", "a/../../secret", "a\\secret"])
def test_unsafe_code_path_rejected(path):
    with pytest.raises(ValidationError):
        CodeEvidence(path=path, commit_sha="a1b2c3d")


def test_document_status_is_not_answer_status():
    with pytest.raises(ValidationError):
        SourceInput(
            tenant_id="syncnote",
            source_type="문서",
            source_id="DOC-001",
            version=1,
            title="policy",
            text="policy",
            visibility="고객 공개",
            status="재검토 필요",
            content_hash="sha256:example",
        )
