import pytest

from app.core.errors import ServiceError
from app.schemas.rag import SearchRequest, SourceInput, SourceStatusBatch
from app.services.rag.chroma_store import ChromaVectorStore
from app.services.rag.chunking import split_source
from app.services.rag.service import RagService


class TestEmbedder:
    """Deterministic transport stub; these vectors do not measure semantic model quality."""

    space = "test-embedding:2"
    calls = 0

    async def documents(self, texts):
        self.calls += 1
        return [[1.0, 0.0] if "E-502" in text else [0.0, 1.0] for text in texts]

    async def query(self, text):
        return [1.0, 0.0]


@pytest.fixture
def rag(tmp_path):
    service = RagService(ChromaVectorStore(str(tmp_path / "chroma")), TestEmbedder())
    yield service
    service.store.close()


def source(**updates):
    value = {
        "tenant_id": "syncnote",
        "source_type": "문서",
        "source_id": "DOC-005",
        "version": 1,
        "title": "오류 코드",
        "text": "E-502 저장 오류",
        "visibility": "고객 공개",
        "status": "유효",
        "content_hash": "sha256:fixture",
    }
    value.update(updates)
    return SourceInput.model_validate(value)


def query(**updates):
    return SearchRequest(tenant_id="syncnote", query="저장 E-502", **updates)


def batch(**updates):
    change = {
        "source_type": "문서",
        "source_id": "DOC-005",
        "version": 1,
        "source_revision": 2,
        "status": "유효",
        "review_required": True,
    }
    change.update(updates)
    return SourceStatusBatch(
        tenant_id="syncnote", event_id="event-1", sources=[change], reason="test"
    )


def test_chunking_preserves_offsets_and_overlap():
    item = source(text="가나다라마바사" * 30)
    chunks = split_source(item, size=60, overlap=10)
    assert chunks[0].start == 0 and chunks[-1].end == len(item.text)
    assert all(c.text == item.text[c.start : c.end] for c in chunks)
    assert chunks[1].start == chunks[0].end - 10
    assert split_source(item, size=60, overlap=10) == chunks


async def test_ingest_search_and_citations(rag):
    await rag.ingest(source())
    result = await rag.search(query())
    assert result.facts[0].source_id == "DOC-005"
    assert result.facts[0].evidence.docs[0].version == 1
    assert not result.internal_findings


async def test_tenant_isolation(rag):
    await rag.ingest(source(tenant_id="other"))
    assert not (await rag.search(query())).facts
    with pytest.raises(ServiceError) as exc:
        await rag.get("syncnote", "문서", "DOC-005", 1)
    assert exc.value.status_code == 404


async def test_retired_and_review_required_are_excluded(rag):
    await rag.ingest(source(status="폐기"))
    assert not (await rag.search(query())).facts
    await rag.ingest(source(source_revision=2))
    assert (await rag.search(query())).facts
    await rag.update_statuses(batch(source_revision=3))
    assert not (await rag.search(query())).facts


async def test_new_pending_version_does_not_fall_back_to_old_fact(rag):
    await rag.ingest(source())
    ticket = await rag.prepare(source(version=2, text="E-502 최신 안내"))
    assert ticket is not None
    assert not (await rag.search(query())).facts
    await rag.index(ticket)
    assert (await rag.search(query())).facts[0].version == 2


async def test_customer_search_never_returns_internal_code(rag):
    await rag.ingest(
        source(
            source_type="코드",
            source_id="sync/saveHandler.ts",
            version="a1b2c3d",
            visibility="내부 전용",
        )
    )
    assert not (await rag.search(query())).internal_findings
    assert (await rag.search(query(purpose="내부 조사"))).internal_findings


async def test_best_answers_are_style_only_and_depend_on_current_docs(rag):
    await rag.ingest(source())
    await rag.ingest(
        source(
            source_type="우수 답변",
            source_id="BA-001",
            evidence={"docs": [{"doc_id": "DOC-005", "version": 1}], "code": []},
        )
    )
    result = await rag.search(query())
    assert len(result.facts) == 1 and len(result.style_examples) == 1
    await rag.update_statuses(batch())
    assert not (await rag.search(query())).style_examples


async def test_identical_ingestion_does_not_repeat_embedding(rag):
    await rag.ingest(source())
    await rag.ingest(source())
    assert rag.embedder.calls == 1
    with pytest.raises(ServiceError) as exc:
        await rag.ingest(source(text="changed E-502"))
    assert exc.value.code == "SOURCE_CONFLICT"


async def test_status_batch_is_validated_before_any_write(rag):
    await rag.ingest(source())
    request = batch()
    request.sources.append(request.sources[0].model_copy(update={"source_id": "DOC-999"}))
    with pytest.raises(ServiceError):
        await rag.update_statuses(request)
    assert (await rag.search(query())).facts


async def test_late_embedding_cannot_reactivate_invalidated_source(rag):
    ticket = await rag.prepare(source())
    await rag.update_statuses(batch())
    await rag.index(ticket)
    view = await rag.get("syncnote", "문서", "DOC-005", 1)
    assert view.review_required and not view.searchable
    assert not (await rag.search(query())).facts


async def test_status_events_idempotent_and_conflicts_rejected(rag):
    await rag.ingest(source())
    result = await rag.update_statuses(batch())
    assert await rag.update_statuses(batch()) == result
    with pytest.raises(ServiceError) as exc:
        await rag.update_statuses(batch(status="폐기"))
    assert exc.value.code == "EVENT_CONFLICT"


async def test_persistence_can_be_reopened(rag):
    await rag.ingest(source())
    path = str(rag.store.client.get_settings().persist_directory)
    other = ChromaVectorStore(path)
    try:
        assert other.get("syncnote", "문서", "DOC-005", 1).searchable
    finally:
        other.close()


async def test_api_ingestion_status_and_invalidation(client, headers, rag):
    client.app.state.rag_service = rag
    response = client.post(
        "/internal/v1/rag/sources", headers=headers, json=source().model_dump(mode="json")
    )
    assert response.status_code == 202
    path = "/internal/v1/rag/sources/DOC-005/versions/1?source_type=문서"
    assert client.get(path, headers=headers).json()["indexing_status"] == "완료"
    response = client.post(
        "/internal/v1/rag/source-statuses", headers=headers, json=batch().model_dump(mode="json")
    )
    assert response.status_code == 200
    assert not client.get(path, headers=headers).json()["searchable"]


async def test_source_hash_collision_does_not_allow_overwrite(rag):
    await rag.ingest(source())
    with pytest.raises(ServiceError):
        await rag.ingest(source(text="another text", content_hash="sha256:fixture"))


async def test_failed_index_has_no_searchable_data(rag):
    async def fail(texts):
        raise ServiceError(429, "EMBEDDING_API_ERROR", "quota")

    rag.embedder.documents = fail
    with pytest.raises(ServiceError):
        await rag.ingest(source())
    view = await rag.get("syncnote", "문서", "DOC-005", 1)
    assert view.indexing_status == "실패" and not view.searchable
