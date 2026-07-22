"""Safe project exception types with non-disclosing default messages."""


class StakeholderIntelligenceError(Exception):
    """Base class for safe project failures."""


class ServiceNotReadyError(StakeholderIntelligenceError, RuntimeError):
    """A domain route was called before its single-backend services were ready."""

    code = "SERVICE_NOT_READY"

    def __init__(self) -> None:
        super().__init__("The application services are not ready.")


class ArtifactScopeError(StakeholderIntelligenceError, PermissionError):
    """An artifact operation attempted to leave its authorized scope."""

    code = "ARTIFACT_SCOPE_INVALID"

    def __init__(self) -> None:
        super().__init__("Artifact access is outside the authorized scope.")


class ArtifactPathError(StakeholderIntelligenceError, ValueError):
    """A caller supplied unsafe virtual path syntax."""

    code = "ARTIFACT_PATH_INVALID"

    def __init__(self) -> None:
        super().__init__("The virtual artifact path is invalid.")


class ArtifactNotFoundError(StakeholderIntelligenceError, FileNotFoundError):
    """An authorized artifact is not available."""

    code = "ARTIFACT_NOT_FOUND"

    def __init__(self) -> None:
        super().__init__("The requested artifact is not available.")


class ProviderPolicyError(StakeholderIntelligenceError, ValueError):
    """A runtime component attempted to use a forbidden model provider."""

    def __init__(self) -> None:
        super().__init__("Only configured Gemini models are permitted.")


class ProviderPacingTimeoutError(StakeholderIntelligenceError, TimeoutError):
    """A provider call could not obtain a safe rate-limited slot in time."""

    code = "PROVIDER_PACING_TIMEOUT"

    def __init__(self) -> None:
        super().__init__("The model provider is temporarily rate limited.")


class ProviderQuotaExhaustedError(StakeholderIntelligenceError, RuntimeError):
    """A bounded project-owned retry could not clear a provider HTTP 429."""

    code = "PROVIDER_QUOTA_EXHAUSTED"

    def __init__(self) -> None:
        super().__init__("The model provider quota remained unavailable after a bounded retry.")


class ProviderTransientExhaustedError(StakeholderIntelligenceError, RuntimeError):
    """A bounded retry could not clear an allowlisted transient provider failure."""

    code = "PROVIDER_TRANSIENT_EXHAUSTED"

    def __init__(self) -> None:
        super().__init__(
            "The model provider remained temporarily unavailable after a bounded retry."
        )


class RuntimeScopeError(StakeholderIntelligenceError, PermissionError):
    """Graph runtime context and persistent thread scope do not agree."""

    code = "RUNTIME_SCOPE_INVALID"

    def __init__(self) -> None:
        super().__init__("The graph runtime scope is invalid.")


class CourseFidelityError(StakeholderIntelligenceError, RuntimeError):
    """A Deep Agent tool call violated the required execution order."""

    code = "COURSE_FIDELITY_FAILED"

    def __init__(
        self,
        message: str = "The required research workflow order was not satisfied.",
    ) -> None:
        super().__init__(message)


class TodoPlanAlignmentError(CourseFidelityError):
    """A proposed research plan does not exactly match the explicit TODO state."""


class RepeatedToolFailureError(CourseFidelityError):
    """The model repeated the same rejected tool call beyond the safe retry bound."""

    code = "REPEATED_TOOL_FAILURE"

    def __init__(self) -> None:
        super().__init__("The insight workflow repeated the same invalid action and was stopped.")


class EvidencePolicyError(StakeholderIntelligenceError, ValueError):
    """An artifact or report contains unregistered evidence references."""

    code = "EVIDENCE_POLICY_FAILED"

    def __init__(self) -> None:
        super().__init__("The evidence references could not be validated.")


class ArtifactStateError(StakeholderIntelligenceError, RuntimeError):
    """A required immutable artifact is absent or already exists."""

    code = "ARTIFACT_STATE_INVALID"

    def __init__(self) -> None:
        super().__init__("The required artifact state is invalid.")


class ToolInputError(StakeholderIntelligenceError, ValueError):
    """An agent tool received an empty or structurally invalid value."""

    code = "TOOL_INPUT_INVALID"

    def __init__(self) -> None:
        super().__init__("The tool input is invalid.")


class LifecycleTransitionError(StakeholderIntelligenceError, ValueError):
    """A domain record attempted a forbidden or scope-changing state transition."""

    def __init__(self) -> None:
        super().__init__("The requested lifecycle transition is invalid.")


class AccessDeniedError(StakeholderIntelligenceError, PermissionError):
    """Credentials, session state, or requested scope are not authorized."""

    code = "ACCESS_DENIED"

    def __init__(self) -> None:
        super().__init__("Access is not authorized.")


class DomainConflictError(StakeholderIntelligenceError, RuntimeError):
    """A requested domain mutation conflicts with current safe state."""

    code = "DOMAIN_CONFLICT"

    def __init__(self) -> None:
        super().__init__("The requested operation conflicts with the current state.")


class ReportNotProducedError(DomainConflictError):
    """The Deep Agent stopped without a complete, validated report lifecycle."""

    code = "REPORT_NOT_PRODUCED"

    def __init__(self) -> None:
        super().__init__()


class DomainPersistenceError(StakeholderIntelligenceError, RuntimeError):
    """Local domain persistence could not complete safely."""

    code = "PERSISTENCE_FAILED"

    def __init__(self) -> None:
        super().__init__("The local operation could not be completed safely.")


class AccessClockError(StakeholderIntelligenceError, ValueError):
    """An injected access-service clock returned an unsafe timestamp."""

    def __init__(self) -> None:
        super().__init__("The access-service clock must return an aware timestamp.")


class IngestionError(StakeholderIntelligenceError):
    """Base class for safe, stable document-ingestion failures."""

    code = "INGESTION_FAILED"


class UnsupportedDocumentTypeError(IngestionError, ValueError):
    """The filename extension is outside the approved six-format allowlist."""

    code = "UNSUPPORTED_TYPE"

    def __init__(self) -> None:
        super().__init__("The uploaded document type is not supported.")


class MediaTypeMismatchError(IngestionError, ValueError):
    """Declared media type, extension, and content signature do not agree."""

    code = "MEDIA_TYPE_MISMATCH"

    def __init__(self) -> None:
        super().__init__("The uploaded document type does not match its content.")


class UploadSizeError(IngestionError, ValueError):
    """The upload is empty or exceeds a configured safety limit."""

    code = "UPLOAD_SIZE_INVALID"

    def __init__(self) -> None:
        super().__init__("The uploaded document size is not permitted.")


class CorruptSourceError(IngestionError, ValueError):
    """An allowed-format source cannot be parsed safely."""

    code = "CORRUPT_SOURCE"

    def __init__(self) -> None:
        super().__init__("The uploaded document is corrupt or incomplete.")


class MandatoryContentMissingError(IngestionError, RuntimeError):
    """Conversion did not recover the minimum source content."""

    code = "MANDATORY_CONTENT_MISSING"

    def __init__(self) -> None:
        super().__init__("Required document content could not be recovered.")


class ExtractionFailedError(IngestionError, RuntimeError):
    """Docling or an approved extraction supplement failed safely."""

    code = "EXTRACTION_FAILED"

    def __init__(self) -> None:
        super().__init__("Document extraction could not be completed.")


class EnrichmentFailedError(IngestionError, RuntimeError):
    """A required Gemini vision derivative could not be produced."""

    code = "ENRICHMENT_FAILED"

    def __init__(self) -> None:
        super().__init__("Document visual enrichment could not be completed.")


class IndexingFailedError(IngestionError, RuntimeError):
    """Complete dense and sparse vector staging could not be activated."""

    code = "INDEXING_FAILED"

    def __init__(self) -> None:
        super().__init__("Document indexing could not be completed.")


class IngestionInProgressError(IngestionError, RuntimeError):
    """Another unexpired worker lease owns the same stable version."""

    code = "INGESTION_IN_PROGRESS"

    def __init__(self) -> None:
        super().__init__("The same document version is already being processed.")


class IngestionAuthorizationError(IngestionError, PermissionError):
    """Authorization was withdrawn before the ingestion activation boundary."""

    code = "ACCESS_DENIED"

    def __init__(self) -> None:
        super().__init__("Access is not authorized.")


class RetrievalError(StakeholderIntelligenceError):
    """Base class for safe retrieval failures."""

    code = "RETRIEVAL_FAILED"
    safe_message = "Evidence retrieval failed."

    def __init__(self) -> None:
        super().__init__(self.safe_message)


class RetrievalFilterError(RetrievalError, ValueError):
    """Structured optional-filter extraction failed safely."""

    code = "RETRIEVAL_FILTER_FAILED"
    safe_message = "Optional evidence filters could not be applied."


class RetrievalExecutionError(RetrievalError, RuntimeError):
    """A dense or sparse retrieval boundary failed."""

    code = "RETRIEVAL_EXECUTION_FAILED"
    safe_message = "Evidence retrieval could not be completed."


class RerankingError(RetrievalError, RuntimeError):
    """The mandatory BGE reranking stage failed."""

    code = "RERANKING_FAILED"
    safe_message = "Evidence reranking could not be completed."


class EvidenceRegistrationError(StakeholderIntelligenceError, RuntimeError):
    """Evidence could not be registered against an active permitted source."""

    code = "EVIDENCE_REGISTRATION_FAILED"
    safe_message = "Evidence registration failed."

    def __init__(self) -> None:
        super().__init__(self.safe_message)


class InterviewLifecycleError(StakeholderIntelligenceError, RuntimeError):
    """A raw-turn or finalization operation could not preserve lifecycle safety."""

    code = "INTERVIEW_LIFECYCLE_FAILED"

    def __init__(self) -> None:
        super().__init__("The interview operation could not be completed safely.")


class TranscriptImmutableError(InterviewLifecycleError, PermissionError):
    """A caller attempted to append to or mutate a finalized transcript."""

    code = "TRANSCRIPT_IMMUTABLE"

    def __init__(self) -> None:
        super().__init__()


class InterviewCompletionNotReadyError(StakeholderIntelligenceError, RuntimeError):
    """A participant attempted finalization before the assistant allowed it."""

    code = "INTERVIEW_COMPLETION_NOT_READY"

    def __init__(self) -> None:
        super().__init__("The interview assistant has not recommended completion yet.")


class TranscriptIngestionError(InterviewLifecycleError):
    """Finalized transcript indexing failed without exposing provider detail."""

    code = "TRANSCRIPT_INGESTION_FAILED"

    def __init__(self) -> None:
        super().__init__()
