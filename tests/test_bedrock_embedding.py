import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from pydantic import ValidationError

from app.cli import retrieve as retrieve_cli
from app.core.config import Settings
from app.core.errors import ServiceError
from app.schemas.rag import SearchRequest, SourceInput
from app.services.rag.chroma_store import ChromaVectorStore
from app.services.rag.embedding import BedrockEmbedder, create_embedder
from app.services.rag.service import RagService


class TitanClient:
    def __init__(self):
        self.requests = []

    def invoke_model(self, **kwargs):
        self.requests.append(kwargs)
        size = json.loads(kwargs["body"])["dimensions"]
        return {"body": io.BytesIO(json.dumps({"embedding": [1.0] + [0.0] * (size - 1)}).encode())}


@pytest.mark.parametrize("dimensions", [256, 512, 1024])
async def test_titan_dimensions_normalization_and_query_cache(settings, monkeypatch, dimensions):
    settings.embedding_dimensions = dimensions
    client = TitanClient()
    monkeypatch.setattr(BedrockEmbedder, "_build_client", lambda self: client)
    embedder = create_embedder(settings)
    documents = await embedder.documents(["문서 하나", "문서 둘"])
    query = f"embedding-cache-regression-{dimensions}"
    first = await embedder.query(query)
    second = await embedder.query(query)
    assert [len(v) for v in documents] == [dimensions, dimensions]
    assert first == second
    assert len(first) == dimensions
    assert len(client.requests) == 3  # 문서 2개 + 검색어 1개. 재검색은 캐시.
    assert all(json.loads(r["body"])["normalize"] for r in client.requests)
    assert all(r["modelId"] == settings.bedrock_embedding_model for r in client.requests)


@pytest.mark.parametrize("value", [128, 768, 2048])
def test_settings_reject_unsupported_titan_dimensions(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, embedding_dimensions=value)


def test_dimensions_are_parsed_from_environment_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("MOA_EMBEDDING_DIMENSIONS=512\n")
    assert Settings(_env_file=env).embedding_dimensions == 512


async def test_empty_documents_do_not_call_aws(settings, monkeypatch):
    def unexpected(self):
        raise AssertionError("Empty input must not call AWS")

    monkeypatch.setattr(BedrockEmbedder, "_build_client", unexpected)
    assert await BedrockEmbedder(settings).documents([]) == []


async def test_missing_aws_credentials_has_embedding_error(settings, monkeypatch):
    def unavailable(self):
        raise NoCredentialsError()

    monkeypatch.setattr(BedrockEmbedder, "_build_client", unavailable)
    with pytest.raises(ServiceError) as error:
        await BedrockEmbedder(settings).documents(["입력"])
    assert error.value.code == "EMBEDDING_NOT_CONFIGURED"


@pytest.mark.parametrize("status", [429, 503, 504])
async def test_embedding_provider_errors_are_sanitized(settings, monkeypatch, status):
    class Client:
        def invoke_model(self, **kwargs):
            raise ClientError(
                {
                    "Error": {"Code": "TestProviderError", "Message": "private-token"},
                    "ResponseMetadata": {"HTTPStatusCode": status},
                },
                "InvokeModel",
            )

    monkeypatch.setattr(BedrockEmbedder, "_build_client", lambda self: Client())
    with pytest.raises(ServiceError) as error:
        await BedrockEmbedder(settings).documents(["입력"])
    assert error.value.status_code == status
    assert "private-token" not in error.value.message


async def test_invalid_titan_vector_is_rejected(settings, monkeypatch):
    class Client:
        def invoke_model(self, **kwargs):
            return {"body": io.BytesIO(b'{"embedding": [0, 0]}')}

    monkeypatch.setattr(BedrockEmbedder, "_build_client", lambda self: Client())
    with pytest.raises(ServiceError) as error:
        await BedrockEmbedder(settings).documents(["입력"])
    assert error.value.code == "INVALID_EMBEDDING_RESPONSE"


async def test_embedding_ssh_relay_keeps_text_out_of_command(settings, monkeypatch):
    settings.bedrock_ssh_host = "test-host"
    settings.bedrock_ssh_key_path = "test-key.pem"
    seen = {}

    def unavailable(self):
        raise NoCredentialsError()

    def run(args, **kwargs):
        seen.update(args=args, **kwargs)
        return SimpleNamespace(
            returncode=0, stderr=b"", stdout=json.dumps([[1.0] + [0.0] * 1023]).encode()
        )

    monkeypatch.setattr(BedrockEmbedder, "_build_client", unavailable)
    monkeypatch.setattr("app.services.rag.embedding.subprocess.run", run)
    vectors = await BedrockEmbedder(settings).documents(["private text"])
    assert len(vectors[0]) == 1024
    assert "private text" not in " ".join(seen["args"])
    assert json.loads(seen["input"])["texts"] == ["private text"]


async def test_cli_uses_titan_for_indexing_and_search(settings, monkeypatch, tmp_path):
    settings.chroma_path = str(tmp_path / "chroma")
    client = TitanClient()
    monkeypatch.setattr(BedrockEmbedder, "_build_client", lambda self: client)
    monkeypatch.setattr(retrieve_cli, "get_settings", lambda: settings)
    result = await retrieve_cli.retrieve(
        "CLI-titan-regression", Path("examples/rag/sources.json"), "syncnote"
    )
    assert result["facts"]
    assert len(client.requests) == 3
    assert all(r["modelId"] == settings.bedrock_embedding_model for r in client.requests)


async def test_legacy_vectors_require_reindex_and_are_not_reported_searchable(tmp_path):
    class FixedEmbedder:
        def __init__(self, space, dimension):
            self.space = space
            self.dimension = dimension

        async def documents(self, texts):
            return [[1.0] + [0.0] * (self.dimension - 1) for _ in texts]

        async def query(self, text):
            return [1.0] + [0.0] * (self.dimension - 1)

    store = ChromaVectorStore(str(tmp_path / "chroma"))
    try:
        old = RagService(store, FixedEmbedder("legacy:768", 768))
        current = RagService(store, FixedEmbedder("amazon.titan-embed-text-v2:0:1024", 1024))
        source = SourceInput(
            tenant_id="syncnote",
            source_type="문서",
            source_id="DOC-005",
            version=1,
            title="테스트",
            text="E-502 저장 오류 안내",
            visibility="고객 공개",
            status="유효",
            content_hash="test-hash",
        )
        await old.ingest(source)
        before = await current.get("syncnote", "문서", "DOC-005", 1)
        assert before.indexing_status == "완료"
        assert not before.searchable
        request = SearchRequest(tenant_id="syncnote", query="E-502")
        assert not (await current.search(request)).facts
        await current.ingest(source)
        assert (await current.get("syncnote", "문서", "DOC-005", 1)).searchable
        assert (await current.search(request)).facts
    finally:
        store.close()
