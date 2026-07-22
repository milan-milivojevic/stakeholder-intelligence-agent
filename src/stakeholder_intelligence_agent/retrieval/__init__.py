"""Server-scoped hybrid retrieval, evidence registration, and source drill-down."""

from stakeholder_intelligence_agent.retrieval.evidence import EvidenceRegistry
from stakeholder_intelligence_agent.retrieval.factory import build_production_retrieval
from stakeholder_intelligence_agent.retrieval.filters import GeminiFilterExtractor
from stakeholder_intelligence_agent.retrieval.qdrant import QdrantHybridSearcher
from stakeholder_intelligence_agent.retrieval.repository import RetrievalRepository
from stakeholder_intelligence_agent.retrieval.reranker import BgeReranker
from stakeholder_intelligence_agent.retrieval.service import HybridRetrievalService
from stakeholder_intelligence_agent.retrieval.types import (
    ChannelHit,
    RetrievalResult,
    RetrievalTrace,
    RetrievedItem,
    SourceDrillDown,
    StakeholderFilterCandidate,
)

__all__ = [
    "BgeReranker",
    "ChannelHit",
    "EvidenceRegistry",
    "GeminiFilterExtractor",
    "HybridRetrievalService",
    "QdrantHybridSearcher",
    "RetrievalRepository",
    "RetrievalResult",
    "RetrievalTrace",
    "RetrievedItem",
    "SourceDrillDown",
    "StakeholderFilterCandidate",
    "build_production_retrieval",
]
