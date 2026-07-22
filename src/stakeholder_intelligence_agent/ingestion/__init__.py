"""Shared authorized document-ingestion domain."""

from stakeholder_intelligence_agent.ingestion.service import IngestionService
from stakeholder_intelligence_agent.ingestion.types import IngestionResult

__all__ = ["IngestionResult", "IngestionService"]
