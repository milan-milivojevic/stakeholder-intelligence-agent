"""One-collection Qdrant staging for complete dense and sparse document chunks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models

from stakeholder_intelligence_agent.errors import IndexingFailedError
from stakeholder_intelligence_agent.ingestion.identity import qdrant_point_id

if TYPE_CHECKING:
    from collections.abc import Sequence

    from stakeholder_intelligence_agent.config import Settings
    from stakeholder_intelligence_agent.contracts.source import SearchChunk


class QdrantVectorStager:
    """Stage and prepare points while SQLite remains the active-version authority."""

    dense_vector_name = "dense"
    sparse_vector_name = "sparse"

    def __init__(
        self,
        settings: Settings,
        *,
        client: AsyncQdrantClient | None = None,
    ) -> None:
        self._collection = settings.qdrant_collection
        self._dimension = settings.gemini_embedding_dimension
        self._client = client or AsyncQdrantClient(
            url=settings.qdrant_url,
            timeout=settings.provider_timeout_seconds,
            check_compatibility=True,
        )
        self._owns_client = client is None

    async def initialize(self) -> None:
        """Create or validate the approved named-vector collection."""
        try:
            if not await self._client.collection_exists(self._collection):
                await self._client.create_collection(
                    collection_name=self._collection,
                    vectors_config={
                        self.dense_vector_name: models.VectorParams(
                            size=self._dimension,
                            distance=models.Distance.COSINE,
                        )
                    },
                    sparse_vectors_config={
                        self.sparse_vector_name: models.SparseVectorParams(
                            modifier=models.Modifier.IDF,
                        )
                    },
                    on_disk_payload=True,
                )
            info = await self._client.get_collection(self._collection)
        except Exception as error:
            raise IndexingFailedError from error
        self._validate_collection(info)

    async def stage(self, chunks: Sequence[SearchChunk]) -> None:
        """Upsert inactive points with complete metadata and both named vectors."""
        if not chunks:
            raise IndexingFailedError
        points = [
            models.PointStruct(
                id=qdrant_point_id(chunk.chunk_id),
                vector={
                    self.dense_vector_name: list(chunk.dense_vector),
                    self.sparse_vector_name: models.SparseVector(
                        indices=list(chunk.sparse_vector.indices),
                        values=list(chunk.sparse_vector.values),
                    ),
                },
                payload={
                    "record_type": (
                        "transcript_chunk" if chunk.source_type == "interview" else "document_chunk"
                    ),
                    "chunk_id": chunk.chunk_id,
                    "engagement_id": chunk.engagement_id,
                    "source_id": chunk.source_id,
                    "source_version_id": chunk.source_version_id,
                    "element_ids": list(chunk.element_ids),
                    "text_for_retrieval": chunk.text_for_retrieval,
                    "location": chunk.location.model_dump(mode="json"),
                    "stakeholder_id": chunk.stakeholder_id,
                    "role": chunk.role,
                    "department": chunk.department,
                    "doc_type": chunk.doc_type,
                    "source_type": chunk.source_type,
                    "is_active_ready": False,
                    "vector_stage_state": "STAGED",
                },
            )
            for chunk in chunks
        ]
        try:
            await self._client.upsert(
                collection_name=self._collection,
                points=points,
                wait=True,
            )
        except Exception as error:
            raise IndexingFailedError from error

    async def verify(self, version_id: str, expected_chunk_ids: Sequence[str]) -> None:
        """Verify exact point identity, named vectors, and required payload keys."""
        expected = set(expected_chunk_ids)
        found: set[str] = set()
        offset: Any = None
        required_payload = {
            "record_type",
            "chunk_id",
            "engagement_id",
            "source_id",
            "source_version_id",
            "element_ids",
            "text_for_retrieval",
            "location",
            "stakeholder_id",
            "role",
            "department",
            "doc_type",
            "source_type",
            "is_active_ready",
            "vector_stage_state",
        }
        while True:
            try:
                records, offset = await self._client.scroll(
                    collection_name=self._collection,
                    scroll_filter=self._version_filter(version_id),
                    limit=256,
                    offset=offset,
                    with_payload=True,
                    with_vectors=True,
                )
            except Exception as error:
                raise IndexingFailedError from error
            found.update(
                self._validate_record(record, required_payload=required_payload)
                for record in records
            )
            if offset is None:
                break
        if found != expected:
            raise IndexingFailedError

    async def prepare_activation(self, version_id: str) -> None:
        """Prepare complete points before the authoritative SQLite version switch."""
        await self._set_eligibility(version_id, eligible=True, state="PREPARED")

    async def deactivate(self, version_id: str) -> None:
        """Make staged, failed, or superseded version points ineligible."""
        await self._set_eligibility(version_id, eligible=False, state="STAGED")

    async def close(self) -> None:
        """Close a production-owned Qdrant client."""
        if self._owns_client:
            await self._client.close()

    async def _set_eligibility(self, version_id: str, *, eligible: bool, state: str) -> None:
        try:
            await self._client.set_payload(
                collection_name=self._collection,
                payload={
                    "is_active_ready": eligible,
                    "vector_stage_state": state,
                },
                points=self._version_filter(version_id),
                wait=True,
            )
        except Exception as error:
            raise IndexingFailedError from error

    @staticmethod
    def _version_filter(version_id: str) -> models.Filter:
        return models.Filter(
            must=[
                models.FieldCondition(
                    key="source_version_id",
                    match=models.MatchValue(value=version_id),
                )
            ]
        )

    def _validate_collection(self, info: Any) -> None:
        vectors = info.config.params.vectors
        sparse_vectors = info.config.params.sparse_vectors
        if not isinstance(vectors, dict):
            raise IndexingFailedError
        dense = vectors.get(self.dense_vector_name)
        if (
            dense is None
            or dense.size != self._dimension
            or dense.distance != models.Distance.COSINE
        ):
            raise IndexingFailedError
        if not sparse_vectors:
            raise IndexingFailedError
        sparse = sparse_vectors.get(self.sparse_vector_name)
        if sparse is None or sparse.modifier != models.Modifier.IDF:
            raise IndexingFailedError

    def _validate_record(
        self,
        record: Any,
        *,
        required_payload: set[str],
    ) -> str:
        payload = record.payload or {}
        if not required_payload.issubset(payload):
            raise IndexingFailedError
        chunk_id = cast("str", payload["chunk_id"])
        if str(record.id) != qdrant_point_id(chunk_id):
            raise IndexingFailedError
        vector = record.vector
        if not isinstance(vector, dict):
            raise IndexingFailedError
        dense = vector.get(self.dense_vector_name)
        sparse = vector.get(self.sparse_vector_name)
        if not isinstance(dense, list) or len(dense) != self._dimension:
            raise IndexingFailedError
        if not isinstance(sparse, models.SparseVector):
            raise IndexingFailedError
        if not sparse.indices or len(sparse.indices) != len(sparse.values):
            raise IndexingFailedError
        if payload["is_active_ready"] is not False:
            raise IndexingFailedError
        expected_record_type = (
            "transcript_chunk" if payload.get("source_type") == "interview" else "document_chunk"
        )
        if payload.get("record_type") != expected_record_type:
            raise IndexingFailedError
        return chunk_id
