"""Course-aligned Qdrant-native hybrid/RRF query verification."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models

from stakeholder_intelligence_agent.contracts.retrieval import RetrievalFilter
from stakeholder_intelligence_agent.contracts.source import SparseVector
from stakeholder_intelligence_agent.ingestion.types import VectorPair
from stakeholder_intelligence_agent.retrieval.qdrant import QdrantHybridSearcher

if TYPE_CHECKING:
    from stakeholder_intelligence_agent.config import Settings


async def test_search_uses_one_qdrant_native_rrf_query(settings: Settings) -> None:
    client = AsyncMock(spec=AsyncQdrantClient)
    client.query_points.return_value = SimpleNamespace(points=[])
    searcher = QdrantHybridSearcher(
        settings,
        client=cast("AsyncQdrantClient", client),
    )

    result = await searcher.search_hybrid(
        VectorPair(
            dense=(0.25,) * settings.gemini_embedding_dimension,
            sparse=SparseVector(indices=(1, 2), values=(0.5, 0.25)),
        ),
        RetrievalFilter(engagement_id="engagement-a"),
        ("version-a",),
        prefetch_limit=20,
        limit=10,
    )

    assert result == ()
    client.query_points.assert_awaited_once()
    arguments = client.query_points.await_args.kwargs
    assert arguments["query"].fusion == models.Fusion.RRF
    assert [prefetch.using for prefetch in arguments["prefetch"]] == ["dense", "sparse"]
    assert all(prefetch.filter is not None for prefetch in arguments["prefetch"])
    assert arguments["query_filter"] is not None
