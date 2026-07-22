"""Canonical runtime contracts."""

from stakeholder_intelligence_agent.contracts.access import (
    AccessContext,
    InsightRuntimeContext,
    InterviewRuntimeContext,
)
from stakeholder_intelligence_agent.contracts.domain import (
    Engagement,
    InterviewSession,
    InvitationToken,
    PMAccess,
    Stakeholder,
    Transcript,
    TranscriptIngestionVersion,
    TranscriptTurn,
)
from stakeholder_intelligence_agent.contracts.evidence import Citation, EvidenceRecord
from stakeholder_intelligence_agent.contracts.execution import (
    InsightExecutionEvent,
    InsightExecutionMetrics,
    InsightRun,
    OperationalAuditEvent,
    SafeProgressEvent,
)
from stakeholder_intelligence_agent.contracts.insight import (
    InsightReport,
    ResearchPlan,
    ResearchTopic,
)
from stakeholder_intelligence_agent.contracts.retrieval import (
    RetrievalCandidate,
    RetrievalFilter,
    RetrievalFilterInput,
    RetrievalMetadata,
)
from stakeholder_intelligence_agent.contracts.source import (
    BoundingBox,
    DocumentSource,
    DocumentVersion,
    DocxRenderedPageLocation,
    ImageRegionLocation,
    PdfPageLocation,
    PptxSlideLocation,
    SearchChunk,
    SourceElement,
    SourceLocation,
    SparseVector,
    TranscriptTurnsLocation,
    XlsxRangeLocation,
)

__all__ = [
    "AccessContext",
    "BoundingBox",
    "Citation",
    "DocumentSource",
    "DocumentVersion",
    "DocxRenderedPageLocation",
    "Engagement",
    "EvidenceRecord",
    "ImageRegionLocation",
    "InsightExecutionEvent",
    "InsightExecutionMetrics",
    "InsightReport",
    "InsightRun",
    "InsightRuntimeContext",
    "InterviewRuntimeContext",
    "InterviewSession",
    "InvitationToken",
    "OperationalAuditEvent",
    "PMAccess",
    "PdfPageLocation",
    "PptxSlideLocation",
    "ResearchPlan",
    "ResearchTopic",
    "RetrievalCandidate",
    "RetrievalFilter",
    "RetrievalFilterInput",
    "RetrievalMetadata",
    "SafeProgressEvent",
    "SearchChunk",
    "SourceElement",
    "SourceLocation",
    "SparseVector",
    "Stakeholder",
    "Transcript",
    "TranscriptIngestionVersion",
    "TranscriptTurn",
    "TranscriptTurnsLocation",
    "XlsxRangeLocation",
]
