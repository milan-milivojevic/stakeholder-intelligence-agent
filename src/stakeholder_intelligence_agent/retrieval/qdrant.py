"""Separate dense and BM25 sparse Qdrant searches with server-owned scope."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, cast

from pydantic import TypeAdapter, ValidationError
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models

from stakeholder_intelligence_agent.contracts.retrieval import (
    RetrievalFilter,
    RetrievalMetadata,
)
from stakeholder_intelligence_agent.contracts.source import SourceLocation
from stakeholder_intelligence_agent.errors import RetrievalExecutionError
from stakeholder_intelligence_agent.retrieval.types import ChannelHit

if TYPE_CHECKING:
    from collections.abc import Sequence

    from stakeholder_intelligence_agent.config import Settings
    from stakeholder_intelligence_agent.ingestion.types import VectorPair

_LOCATION_ADAPTER: TypeAdapter[SourceLocation] = TypeAdapter(SourceLocation)


class QdrantHybridSearcher:
    """Query one collection through Qdrant's native dense/sparse RRF fusion."""

    dense_vector_name = "dense"
    sparse_vector_name = "sparse"

    def __init__(
        self,
        settings: Settings,
        *,
        client: AsyncQdrantClient | None = None,
    ) -> None:
        self._collection = settings.qdrant_collection
        self._timeout = settings.provider_timeout_seconds
        self._client = client or AsyncQdrantClient(
            url=settings.qdrant_url,
            timeout=settings.provider_timeout_seconds,
            check_compatibility=True,
        )
        self._owns_client = client is None

    async def search_hybrid(
        self,
        vectors: VectorPair,
        retrieval_filter: RetrievalFilter,
        active_version_ids: Sequence[str],
        *,
        prefetch_limit: int,
        limit: int,
    ) -> tuple[ChannelHit, ...]:
        """Run the course-aligned Qdrant built-in RRF over dense and BM25 prefetches."""
        if not active_version_ids:
            return ()
        if not vectors.sparse.indices or len(vectors.sparse.indices) != len(vectors.sparse.values):
            raise RetrievalExecutionError
        qdrant_filter = self._qdrant_filter(retrieval_filter, active_version_ids)
        try:
            response = await self._client.query_points(
                collection_name=self._collection,
                prefetch=[
                    models.Prefetch(
                        query=[float(value) for value in vectors.dense],
                        using=self.dense_vector_name,
                        filter=qdrant_filter,
                        limit=prefetch_limit,
                    ),
                    models.Prefetch(
                        query=models.SparseVector(
                            indices=[int(value) for value in vectors.sparse.indices],
                            values=[float(value) for value in vectors.sparse.values],
                        ),
                        using=self.sparse_vector_name,
                        filter=qdrant_filter,
                        limit=prefetch_limit,
                    ),
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                query_filter=qdrant_filter,
                limit=limit,
                with_payload=True,
                with_vectors=False,
                timeout=self._timeout,
            )
            return self._parse_points(response.points, retrieval_filter, active_version_ids)
        except RetrievalExecutionError:
            raise
        except Exception as error:
            raise RetrievalExecutionError from error

    async def close(self) -> None:
        """Close only a production-owned client."""
        if self._owns_client:
            await self._client.close()

    @staticmethod
    def _qdrant_filter(
        retrieval_filter: RetrievalFilter,
        active_version_ids: Sequence[str],
    ) -> models.Filter:
        must: list[models.Condition] = [
            models.FieldCondition(
                key="record_type",
                match=models.MatchAny(any=["document_chunk", "transcript_chunk"]),
            ),
            models.FieldCondition(
                key="engagement_id",
                match=models.MatchValue(value=retrieval_filter.engagement_id),
            ),
            models.FieldCondition(
                key="source_version_id",
                match=models.MatchAny(any=sorted(set(active_version_ids))),
            ),
            models.FieldCondition(
                key="is_active_ready",
                match=models.MatchValue(value=True),
            ),
            models.FieldCondition(
                key="vector_stage_state",
                match=models.MatchValue(value="PREPARED"),
            ),
        ]
        for key in ("stakeholder_id", "role", "department", "doc_type", "source_type"):
            value = getattr(retrieval_filter, key)
            if value is not None:
                must.append(
                    models.FieldCondition(
                        key=key,
                        match=models.MatchValue(value=value),
                    )
                )
        return models.Filter(must=must)

    @classmethod
    def _parse_points(
        cls,
        points: Sequence[Any],
        retrieval_filter: RetrievalFilter,
        active_version_ids: Sequence[str],
    ) -> tuple[ChannelHit, ...]:
        active = set(active_version_ids)
        parsed = tuple(cls._parse_point(point, retrieval_filter, active) for point in points)
        if len({item.chunk_id for item in parsed}) != len(parsed):
            raise RetrievalExecutionError
        return tuple(sorted(parsed, key=lambda item: (-item.score, item.chunk_id)))

    @staticmethod
    def _parse_point(
        point: Any,
        retrieval_filter: RetrievalFilter,
        active_version_ids: set[str],
    ) -> ChannelHit:
        payload = point.payload
        score = float(point.score)
        if not isinstance(payload, dict) or not math.isfinite(score):
            raise RetrievalExecutionError
        required = {
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
        if not required.issubset(payload):
            raise RetrievalExecutionError
        version_id = cast("str", payload["source_version_id"])
        if (
            payload["record_type"] not in {"document_chunk", "transcript_chunk"}
            or payload["engagement_id"] != retrieval_filter.engagement_id
            or version_id not in active_version_ids
            or payload["is_active_ready"] is not True
            or payload["vector_stage_state"] != "PREPARED"
        ):
            raise RetrievalExecutionError
        try:
            metadata = RetrievalMetadata.model_validate(
                {
                    "engagement_id": payload["engagement_id"],
                    "stakeholder_id": payload["stakeholder_id"],
                    "role": payload["role"],
                    "department": payload["department"],
                    "doc_type": payload["doc_type"],
                    "source_type": payload["source_type"],
                    "source_version_state": "READY",
                    "is_active_ready": True,
                }
            )
            location = _LOCATION_ADAPTER.validate_python(payload["location"])
        except ValidationError as error:
            raise RetrievalExecutionError from error
        expected_record_type = (
            "transcript_chunk" if metadata.source_type == "interview" else "document_chunk"
        )
        if payload["record_type"] != expected_record_type:
            raise RetrievalExecutionError
        QdrantHybridSearcher._validate_optional_filter(metadata, retrieval_filter)
        element_ids = payload["element_ids"]
        text = payload["text_for_retrieval"]
        if (
            not isinstance(element_ids, list)
            or not element_ids
            or not all(isinstance(item, str) and item for item in element_ids)
            or not isinstance(text, str)
            or not text.strip()
        ):
            raise RetrievalExecutionError
        return ChannelHit(
            chunk_id=cast("str", payload["chunk_id"]),
            score=score,
            source_id=cast("str", payload["source_id"]),
            source_version_id=version_id,
            element_ids=tuple(element_ids),
            text=text,
            location=location,
            metadata=metadata,
        )

    @staticmethod
    def _validate_optional_filter(
        metadata: RetrievalMetadata,
        retrieval_filter: RetrievalFilter,
    ) -> None:
        for key in ("stakeholder_id", "role", "department", "doc_type", "source_type"):
            expected = getattr(retrieval_filter, key)
            if expected is not None and getattr(metadata, key) != expected:
                raise RetrievalExecutionError
