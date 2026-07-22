"""Production dependency assembly for the shared scoped retrieval pipeline."""

from __future__ import annotations

from typing import TYPE_CHECKING

from stakeholder_intelligence_agent.ingestion.adapters import GeminiBm25Vectorizer
from stakeholder_intelligence_agent.persistence import DomainDatabase
from stakeholder_intelligence_agent.retrieval.filters import GeminiFilterExtractor
from stakeholder_intelligence_agent.retrieval.qdrant import QdrantHybridSearcher
from stakeholder_intelligence_agent.retrieval.repository import RetrievalRepository
from stakeholder_intelligence_agent.retrieval.reranker import BgeReranker
from stakeholder_intelligence_agent.retrieval.service import HybridRetrievalService

if TYPE_CHECKING:
    from stakeholder_intelligence_agent.config import Settings


def build_production_retrieval(settings: Settings) -> HybridRetrievalService:
    """Wire Gemini, Qdrant, SQLite authority, and mandatory BGE reranking."""
    return HybridRetrievalService(
        settings=settings,
        repository=RetrievalRepository(DomainDatabase(settings.domain_database)),
        filter_extractor=GeminiFilterExtractor(settings),
        vectorizer=GeminiBm25Vectorizer(settings),
        search_backend=QdrantHybridSearcher(settings),
        reranker=BgeReranker(settings),
    )
