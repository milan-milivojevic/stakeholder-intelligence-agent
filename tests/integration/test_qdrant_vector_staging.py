"""Real in-memory Qdrant named-vector and activation-staging verification."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models

from stakeholder_intelligence_agent.contracts.source import (
    PdfPageLocation,
    SearchChunk,
    SparseVector,
)
from stakeholder_intelligence_agent.errors import IndexingFailedError
from stakeholder_intelligence_agent.ingestion.qdrant import QdrantVectorStager

if TYPE_CHECKING:
    from stakeholder_intelligence_agent.config import Settings

pytestmark = pytest.mark.integration


async def test_qdrant_stages_both_named_vectors_and_complete_null_metadata(
    settings: Settings,
) -> None:
    client = AsyncQdrantClient(location=":memory:")
    stager = QdrantVectorStager(settings, client=client)
    chunk = SearchChunk(
        chunk_id="chunk-qdrant-a",
        engagement_id="engagement-a",
        source_id="document-a",
        source_version_id="version-a",
        element_ids=("element-a",),
        text_for_retrieval="Alpha synthetic evidence.",
        location=PdfPageLocation(filename="alpha.pdf", page=1),
        stakeholder_id=None,
        role=None,
        department=None,
        doc_type="pdf",
        source_type="engagement_document",
        dense_vector=tuple(0.01 for _ in range(settings.gemini_embedding_dimension)),
        sparse_vector=SparseVector(indices=(7, 19), values=(1.0, 0.5)),
        is_active_ready=False,
    )

    await stager.initialize()
    await stager.stage((chunk,))
    await stager.verify("version-a", ("chunk-qdrant-a",))
    await stager.prepare_activation("version-a")
    records, _ = await client.scroll(
        collection_name=settings.qdrant_collection,
        scroll_filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="source_version_id",
                    match=models.MatchValue(value="version-a"),
                )
            ]
        ),
        with_payload=True,
        with_vectors=True,
    )

    assert len(records) == 1
    record = records[0]
    assert set(record.vector) == {"dense", "sparse"}  # type: ignore[arg-type]
    assert record.payload is not None
    assert record.payload["is_active_ready"] is True
    assert record.payload["vector_stage_state"] == "PREPARED"
    assert {"stakeholder_id", "role", "department"} <= set(record.payload)
    assert all(record.payload[key] is None for key in ("stakeholder_id", "role", "department"))

    await stager.deactivate("version-a")
    records, _ = await client.scroll(
        collection_name=settings.qdrant_collection,
        scroll_filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="source_version_id",
                    match=models.MatchValue(value="version-a"),
                )
            ]
        ),
        with_payload=True,
    )
    assert records[0].payload is not None
    assert records[0].payload["is_active_ready"] is False
    await client.close()


@pytest.mark.parametrize(
    ("distance", "modifier"),
    [
        (models.Distance.DOT, models.Modifier.IDF),
        (models.Distance.COSINE, None),
    ],
)
async def test_existing_collection_must_preserve_course_dense_and_sparse_semantics(
    settings: Settings,
    distance: models.Distance,
    modifier: models.Modifier | None,
) -> None:
    client = AsyncQdrantClient(location=":memory:")
    await client.create_collection(
        collection_name=settings.qdrant_collection,
        vectors_config={
            "dense": models.VectorParams(
                size=settings.gemini_embedding_dimension,
                distance=distance,
            )
        },
        sparse_vectors_config={
            "sparse": models.SparseVectorParams(modifier=modifier),
        },
    )
    stager = QdrantVectorStager(settings, client=client)

    with pytest.raises(IndexingFailedError):
        await stager.initialize()

    await client.close()
