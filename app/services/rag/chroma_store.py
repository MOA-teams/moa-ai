import hashlib
import json
import math
from collections import Counter
from threading import RLock
from uuid import uuid4

import chromadb
from chromadb.config import Settings as ChromaSettings

from app.core.errors import ServiceError
from app.schemas.common import DocEvidence, Evidence
from app.schemas.rag import (
    SearchHit,
    SearchRequest,
    SearchResult,
    SourceInput,
    SourceStatusBatch,
    SourceStatusResult,
    SourceView,
    TextChunk,
)
from app.services.rag.lexical import fuse_rankings, keyword_ranking
from app.services.rag.vector_store import IndexTicket


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def source_key(tenant: str, kind: str, source_id: str, version: int | str) -> str:
    return digest([tenant, kind, source_id, str(version)])


class ChromaVectorStore:
    """Local persistent Chroma. One service process/worker per storage directory."""

    def __init__(self, path: str):
        self.client = chromadb.PersistentClient(
            path=path, settings=ChromaSettings(anonymized_telemetry=False)
        )
        self.manifest = self.client.get_or_create_collection(
            "moa_source_manifest_v1",
            embedding_function=None,
        )
        self.lock = RLock()
        self._closed = False

    def close(self) -> None:
        with self.lock:
            if not self._closed:
                self._closed = True
                self.client.close()

    def _get(self, key: str) -> dict | None:
        result = self.manifest.get(ids=[key], include=["documents"])
        return json.loads(result["documents"][0]) if result["ids"] else None

    def _records(self, tenant: str) -> list[dict]:
        result = self.manifest.get(
            where={"$and": [{"tenant_id": tenant}, {"kind": "source"}]},
            include=["documents"],
        )
        return [json.loads(text) for text in result["documents"]]

    def _revision(self, tenant: str) -> int:
        record = self._get("revision:" + tenant)
        return record["revision"] if record else 0

    def _write(self, tenant: str, records: dict[str, dict], *, bump: bool = True) -> int:
        revision = self._revision(tenant) + int(bump)
        records = {**records, "revision:" + tenant: {"kind": "revision", "revision": revision}}
        self.manifest.upsert(
            ids=list(records),
            embeddings=[[1.0] for _ in records],
            documents=[json.dumps(value, ensure_ascii=False) for value in records.values()],
            metadatas=[{"tenant_id": tenant, "kind": value["kind"]} for value in records.values()],
        )
        return revision

    def _chunks(self, space: str):
        return self.client.get_or_create_collection(
            "moa_chunks_" + digest(space)[:16],
            embedding_function=None,
            configuration={"hnsw": {"space": "cosine"}},
        )

    def prepare(
        self, source: SourceInput, chunks: list[TextChunk], space: str
    ) -> IndexTicket | None:
        with self.lock:
            key = source_key(source.tenant_id, source.source_type, source.source_id, source.version)
            old = self._get(key)
            payload = source.model_dump(mode="json")
            if old:
                old_source = old["source"]
                if source.source_revision < old_source["source_revision"]:
                    raise ServiceError(409, "STALE_SOURCE_REVISION", "Source revision is outdated")
                if (
                    source.source_revision == old_source["source_revision"]
                    and payload != old_source
                ):
                    raise ServiceError(
                        409, "SOURCE_CONFLICT", "Same revision has different content"
                    )
                if (
                    payload == old_source
                    and old["indexing_status"] == "완료"
                    and old["space"] == space
                ):
                    return None
            generation = uuid4().hex
            usable = source.status == "유효" and not source.review_required
            record = {
                "kind": "source",
                "source": payload,
                "generation": generation,
                "space": space,
                "indexing_status": "대기" if usable else "완료",
                "error": None,
            }
            self._write(source.tenant_id, {key: record})
            return IndexTicket(key, generation, source, chunks, space) if usable else None

    def complete(self, ticket: IndexTicket, vectors: list[list[float]]) -> bool:
        with self.lock:
            record = self._get(ticket.key)
            if not record or record["generation"] != ticket.generation:
                return False
            collection = self._chunks(ticket.space)
            if ticket.chunks:
                collection.upsert(
                    ids=[ticket.generation + ":" + chunk.chunk_id for chunk in ticket.chunks],
                    embeddings=vectors,
                    documents=[chunk.text for chunk in ticket.chunks],
                    metadatas=[
                        {
                            "tenant_id": ticket.source.tenant_id,
                            "source_key": ticket.key,
                            "generation": ticket.generation,
                            "chunk_id": chunk.chunk_id,
                        }
                        for chunk in ticket.chunks
                    ],
                )
            record["indexing_status"] = "완료"
            self._write(ticket.source.tenant_id, {ticket.key: record})
            return True

    def fail(self, ticket: IndexTicket, error: str) -> None:
        with self.lock:
            record = self._get(ticket.key)
            if record and record["generation"] == ticket.generation:
                record.update(indexing_status="실패", error=error)
                self._write(ticket.source.tenant_id, {ticket.key: record})

    def _eligible_records(self, tenant: str) -> list[dict]:
        records = self._records(tenant)
        latest: dict[tuple[str, str], int] = {}
        for record in records:
            source = record["source"]
            if isinstance(source["version"], int):
                key = (source["source_type"], source["source_id"])
                latest[key] = max(latest.get(key, 0), source["version"])
        ready = []
        for record in records:
            source = record["source"]
            key = (source["source_type"], source["source_id"])
            if isinstance(source["version"], int) and source["version"] != latest[key]:
                continue
            if (
                source["status"] == "유효"
                and not source["review_required"]
                and record["indexing_status"] == "완료"
            ):
                ready.append(record)
        current_docs = {
            (r["source"]["source_id"], r["source"]["version"])
            for r in ready
            if r["source"]["source_type"] == "문서"
        }
        return [
            record
            for record in ready
            if all(
                (doc["doc_id"], doc["version"]) in current_docs
                for doc in record["source"]["evidence"]["docs"]
            )
        ]

    def get(
        self,
        tenant: str,
        source_type: str,
        source_id: str,
        version: int | str,
        *,
        space: str | None = None,
    ) -> SourceView:
        with self.lock:
            key = source_key(tenant, source_type, source_id, version)
            record = self._get(key)
            if not record:
                raise ServiceError(404, "SOURCE_NOT_FOUND", "Source not found")
            source = record["source"]
            eligible = {
                r["generation"]
                for r in self._eligible_records(tenant)
                if space is None or r["space"] == space
            }
            return SourceView(
                source_id=source_id,
                version=source["version"],
                source_revision=source["source_revision"],
                status=source["status"],
                review_required=source["review_required"],
                indexing_status=record["indexing_status"],
                searchable=record["generation"] in eligible,
                error=record["error"],
            )

    def list_current_public_sources(self, tenant: str, space: str) -> list[SourceInput]:
        # 검색과 답변 갱신 모두 같은 최신 버전/상태/인덱싱 기준을 사용한다.
        with self.lock:
            return [
                SourceInput.model_validate(record["source"])
                for record in self._eligible_records(tenant)
                if record["space"] == space
                and record["source"]["visibility"] == "고객 공개"
                and record["source"]["source_type"] in {"문서", "서비스 정보"}
            ]

    def list_sources(self, tenant: str, *, source_type: str | None = None) -> list[SourceInput]:
        with self.lock:
            records = self._records(tenant)
            items: list[SourceInput] = []
            for record in records:
                source = record["source"]
                if source_type is not None and source["source_type"] != source_type:
                    continue
                items.append(SourceInput.model_validate(source))
            return items

    def update_statuses(self, batch: SourceStatusBatch) -> SourceStatusResult:
        with self.lock:
            event_key = "event:" + digest([batch.tenant_id, batch.event_id])
            fingerprint = digest(batch.model_dump(mode="json"))
            event = self._get(event_key)
            if event:
                if event["fingerprint"] != fingerprint:
                    raise ServiceError(409, "EVENT_CONFLICT", "Event ID has different contents")
                return SourceStatusResult.model_validate(event["result"])
            updates = {}
            for change in batch.sources:
                key = source_key(
                    batch.tenant_id, change.source_type, change.source_id, change.version
                )
                if key in updates:
                    raise ServiceError(400, "DUPLICATE_SOURCE", "Source appears twice in batch")
                record = self._get(key)
                if not record:
                    raise ServiceError(404, "SOURCE_NOT_FOUND", "Batch source not found")
                source = record["source"]
                old_revision = source["source_revision"]
                if change.source_revision < old_revision:
                    raise ServiceError(409, "STALE_SOURCE_REVISION", "Source revision is outdated")
                changed = (
                    change.status != source["status"]
                    or change.review_required != source["review_required"]
                )
                if change.source_revision == old_revision and changed:
                    raise ServiceError(409, "SOURCE_CONFLICT", "Same revision has different state")
                source.update(
                    status=change.status,
                    review_required=change.review_required,
                    source_revision=change.source_revision,
                )
                if record["indexing_status"] == "대기":
                    record.update(
                        generation=uuid4().hex, indexing_status="실패", error="STALE_INDEX_TASK"
                    )
                updates[key] = record
            result = SourceStatusResult(
                knowledge_revision=self._revision(batch.tenant_id) + 1,
                updated_count=len(updates),
                search_excluded=all(c.status != "유효" or c.review_required for c in batch.sources),
            )
            updates[event_key] = {
                "kind": "event",
                "fingerprint": fingerprint,
                "result": result.model_dump(mode="json"),
            }
            self._write(batch.tenant_id, updates)
            return result

    def search(
        self,
        request: SearchRequest,
        vector: list[float],
        space: str,
        *,
        source_types: set[str] | None = None,
    ) -> SearchResult:
        with self.lock:
            records = [r for r in self._eligible_records(request.tenant_id) if r["space"] == space]
            if source_types is not None:
                records = [r for r in records if r["source"]["source_type"] in source_types]
            if request.purpose == "고객 답변":
                records = [
                    r
                    for r in records
                    if r["source"]["visibility"] == "고객 공개"
                    and r["source"]["source_type"] in {"문서", "서비스 정보", "우수 답변"}
                ]
            result = SearchResult(knowledge_revision=self._revision(request.tenant_id))
            if not records:
                result.warnings.append("검색 가능한 근거가 없습니다.")
                return result
            by_generation = {r["generation"]: r for r in records}
            collection = self._chunks(space)
            where = {
                "$and": [
                    {"tenant_id": request.tenant_id},
                    {"generation": {"$in": list(by_generation)}},
                ]
            }
            hits = collection.query(
                query_embeddings=[vector],
                n_results=max(20, request.top_k)
                if request.retrieval_mode == "hybrid"
                else request.top_k,
                where=where,
                include=["documents", "metadatas", "distances"],
            )
            candidates = {
                key: (text, meta, max(-1.0, min(1.0, 1.0 - distance)))
                for key, text, meta, distance in zip(
                    hits["ids"][0],
                    hits["documents"][0],
                    hits["metadatas"][0],
                    hits["distances"][0],
                    strict=True,
                )
            }
            ranking = [
                key for key, (_, _, score) in candidates.items() if score >= request.min_score
            ]
            if request.retrieval_mode == "hybrid":
                # Apply tenant/version/visibility filters BEFORE either retrieval method.
                corpus = collection.get(where=where, include=["documents", "metadatas"])
                texts = {
                    key: f"{by_generation[meta['generation']]['source']['title']}\n{text}"
                    for key, text, meta in zip(
                        corpus["ids"], corpus["documents"], corpus["metadatas"], strict=True
                    )
                }
                lexical = keyword_ranking(request.lexical_query or request.query, texts)[:20]
                missing = [key for key in lexical if key not in candidates]
                if missing:
                    extra = collection.get(
                        ids=missing, include=["documents", "metadatas", "embeddings"]
                    )
                    norm = math.sqrt(sum(x * x for x in vector))
                    for key, text, meta, embedding in zip(
                        extra["ids"],
                        extra["documents"],
                        extra["metadatas"],
                        extra["embeddings"],
                        strict=True,
                    ):
                        denominator = norm * math.sqrt(sum(float(x) ** 2 for x in embedding))
                        score = (
                            sum(float(x) * y for x, y in zip(embedding, vector, strict=True))
                            / denominator
                        )
                        candidates[key] = (text, meta, max(-1.0, min(1.0, score)))
                # Low cosine candidates enter only through keyword retrieval, then face
                # EvidenceValidator. Do not present BM25/RRF as a cosine probability.
                fused = fuse_rankings(list(hits["ids"][0]), lexical)
                allowed = set(ranking) | set(lexical)
                ranking = [key for key in fused if key in allowed]
                result.warnings.append(
                    "하이브리드 검색 후보입니다. score는 cosine 유사도이며 "
                    "근거 채택은 별도 검증합니다."
                )
            counts: Counter[str] = Counter()
            selected = []
            for key in ranking:
                generation = candidates[key][1]["generation"]
                if request.retrieval_mode == "hybrid" and counts[generation] >= 2:
                    continue
                counts[generation] += 1
                selected.append(key)
                if len(selected) >= request.top_k:
                    break
            for key in selected:
                text, meta, score = candidates[key]
                source = by_generation[meta["generation"]]["source"]
                evidence = Evidence.model_validate(source["evidence"])
                if source["source_type"] == "문서":
                    evidence = Evidence(
                        docs=[DocEvidence(doc_id=source["source_id"], version=source["version"])]
                    )
                hit = SearchHit(
                    source_type=source["source_type"],
                    source_id=source["source_id"],
                    version=source["version"],
                    source_revision=source["source_revision"],
                    chunk_id=meta["chunk_id"],
                    title=source["title"],
                    text=text,
                    score=score,
                    visibility=source["visibility"],
                    metadata=source.get("metadata") or {},
                    evidence=evidence,
                )
                if source["source_type"] == "우수 답변":
                    result.style_examples.append(hit)
                elif source["visibility"] == "고객 공개" and source["source_type"] in {
                    "문서",
                    "서비스 정보",
                }:
                    result.facts.append(hit)
                else:
                    result.internal_findings.append(hit)
            if not result.facts:
                result.warnings.append(
                    "고객 답변용 사실 근거가 부족합니다. 검색 점수는 정답 확률이 아닙니다."
                )
            return result
