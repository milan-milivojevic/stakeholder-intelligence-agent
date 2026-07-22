"""Ordered domain-database migrations for local SQLite persistence."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Migration:
    """One immutable, forward-only schema migration."""

    version: int
    name: str
    statements: tuple[str, ...]


class MigrationInventoryError(RuntimeError):
    """The forward-only migration inventory is internally inconsistent."""


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="access_domain",
        statements=(
            """
            CREATE TABLE engagements (
                engagement_id TEXT PRIMARY KEY,
                name TEXT NOT NULL CHECK(length(trim(name)) BETWEEN 1 AND 500),
                description TEXT,
                status TEXT NOT NULL CHECK(status IN ('active', 'archived')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE pm_access (
                pm_access_id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE
                    CHECK(length(token_hash) = 64 AND token_hash NOT GLOB '*[^0-9a-f]*'),
                status TEXT NOT NULL CHECK(status IN ('active', 'revoked')),
                created_at TEXT NOT NULL,
                revoked_at TEXT
            )
            """,
            """
            CREATE TABLE stakeholders (
                stakeholder_id TEXT PRIMARY KEY,
                engagement_id TEXT NOT NULL,
                display_name TEXT NOT NULL CHECK(length(trim(display_name)) BETWEEN 1 AND 500),
                role TEXT,
                department TEXT,
                status TEXT NOT NULL
                    CHECK(status IN ('invited', 'active', 'completed', 'revoked')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(stakeholder_id, engagement_id),
                FOREIGN KEY(engagement_id) REFERENCES engagements(engagement_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE invitation_tokens (
                invitation_id TEXT PRIMARY KEY,
                engagement_id TEXT NOT NULL,
                stakeholder_id TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE
                    CHECK(length(token_hash) = 64 AND token_hash NOT GLOB '*[^0-9a-f]*'),
                status TEXT NOT NULL
                    CHECK(status IN ('active', 'activated', 'expired', 'revoked')),
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                activated_at TEXT,
                revoked_at TEXT,
                created_by_pm_access_id TEXT NOT NULL,
                UNIQUE(invitation_id, stakeholder_id, engagement_id),
                FOREIGN KEY(stakeholder_id, engagement_id)
                    REFERENCES stakeholders(stakeholder_id, engagement_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT,
                FOREIGN KEY(created_by_pm_access_id) REFERENCES pm_access(pm_access_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE interview_sessions (
                interview_session_id TEXT PRIMARY KEY,
                engagement_id TEXT NOT NULL,
                stakeholder_id TEXT NOT NULL,
                invitation_id TEXT NOT NULL UNIQUE,
                thread_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL CHECK(status IN (
                    'draft', 'finalizing', 'finalized', 'ingesting', 'ready', 'failed'
                )),
                started_at TEXT NOT NULL,
                finalized_at TEXT,
                transcript_id TEXT,
                ingestion_version_id TEXT,
                failure_code TEXT,
                failure_message TEXT,
                UNIQUE(interview_session_id, stakeholder_id, engagement_id),
                FOREIGN KEY(invitation_id, stakeholder_id, engagement_id)
                    REFERENCES invitation_tokens(invitation_id, stakeholder_id, engagement_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE access_sessions (
                access_session_id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE
                    CHECK(length(token_hash) = 64 AND token_hash NOT GLOB '*[^0-9a-f]*'),
                principal_type TEXT NOT NULL CHECK(principal_type IN ('stakeholder', 'pm')),
                principal_id TEXT NOT NULL,
                engagement_id TEXT,
                stakeholder_id TEXT,
                interview_session_id TEXT,
                thread_id TEXT,
                issued_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT,
                CHECK(
                    (principal_type = 'pm' AND stakeholder_id IS NULL
                        AND interview_session_id IS NULL AND thread_id IS NULL)
                    OR
                    (principal_type = 'stakeholder' AND engagement_id IS NOT NULL
                        AND stakeholder_id IS NOT NULL AND principal_id = stakeholder_id
                        AND interview_session_id IS NOT NULL AND thread_id IS NOT NULL)
                ),
                FOREIGN KEY(engagement_id) REFERENCES engagements(engagement_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT,
                FOREIGN KEY(interview_session_id, stakeholder_id, engagement_id)
                    REFERENCES interview_sessions(
                        interview_session_id, stakeholder_id, engagement_id
                    ) ON UPDATE RESTRICT ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE operational_audit_events (
                event_id TEXT PRIMARY KEY,
                occurred_at TEXT NOT NULL,
                run_id TEXT,
                engagement_id TEXT NOT NULL,
                thread_id TEXT,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL
                    CHECK(status IN ('started', 'succeeded', 'failed', 'retried', 'denied')),
                duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms >= 0),
                source_ids_json TEXT NOT NULL DEFAULT '[]',
                evidence_ids_json TEXT NOT NULL DEFAULT '[]',
                retry_count INTEGER CHECK(retry_count IS NULL OR retry_count >= 0),
                failure_code TEXT,
                correlation_id TEXT NOT NULL,
                FOREIGN KEY(engagement_id) REFERENCES engagements(engagement_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT
            )
            """,
        ),
    ),
    Migration(
        version=2,
        name="access_indexes_and_append_only_audit",
        statements=(
            """
            CREATE UNIQUE INDEX one_active_invitation_per_stakeholder
            ON invitation_tokens(stakeholder_id)
            WHERE status = 'active'
            """,
            """
            CREATE INDEX invitation_token_status_expiry
            ON invitation_tokens(token_hash, status, expires_at)
            """,
            """
            CREATE INDEX access_session_token_expiry
            ON access_sessions(token_hash, expires_at, revoked_at)
            """,
            """
            CREATE INDEX audit_engagement_time
            ON operational_audit_events(engagement_id, occurred_at, event_id)
            """,
            """
            CREATE TRIGGER operational_audit_events_no_update
            BEFORE UPDATE ON operational_audit_events
            BEGIN
                SELECT RAISE(ABORT, 'operational audit events are append-only');
            END
            """,
            """
            CREATE TRIGGER operational_audit_events_no_delete
            BEFORE DELETE ON operational_audit_events
            BEGIN
                SELECT RAISE(ABORT, 'operational audit events are append-only');
            END
            """,
        ),
    ),
    Migration(
        version=3,
        name="document_ingestion_domain",
        statements=(
            """
            CREATE TABLE document_sources (
                document_id TEXT PRIMARY KEY,
                document_key TEXT NOT NULL UNIQUE,
                engagement_id TEXT NOT NULL,
                stakeholder_id TEXT,
                role TEXT,
                department TEXT,
                doc_type TEXT NOT NULL
                    CHECK(doc_type IN ('pdf', 'docx', 'xlsx', 'pptx', 'png', 'jpeg')),
                source_type TEXT NOT NULL
                    CHECK(source_type IN ('stakeholder_document', 'engagement_document')),
                original_filename TEXT NOT NULL,
                normalized_filename TEXT NOT NULL,
                media_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(document_id, engagement_id),
                CHECK(
                    (source_type = 'engagement_document' AND stakeholder_id IS NULL
                        AND role IS NULL AND department IS NULL)
                    OR
                    (source_type = 'stakeholder_document' AND stakeholder_id IS NOT NULL)
                ),
                FOREIGN KEY(engagement_id) REFERENCES engagements(engagement_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT,
                FOREIGN KEY(stakeholder_id, engagement_id)
                    REFERENCES stakeholders(stakeholder_id, engagement_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE document_versions (
                document_version_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                version_number INTEGER NOT NULL CHECK(version_number >= 1),
                content_hash TEXT NOT NULL
                    CHECK(length(content_hash) = 64
                        AND content_hash NOT GLOB '*[^0-9a-f]*'),
                state TEXT NOT NULL CHECK(state IN (
                    'RECEIVED', 'VALIDATING', 'EXTRACTING', 'ENRICHING',
                    'INDEXING', 'READY', 'FAILED', 'SUPERSEDED'
                )),
                is_active INTEGER NOT NULL CHECK(is_active IN (0, 1)),
                original_artifact_id TEXT NOT NULL,
                ingestion_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                ready_at TEXT,
                superseded_at TEXT,
                failure_code TEXT,
                failure_message TEXT,
                lease_token TEXT,
                lease_expires_at TEXT,
                UNIQUE(document_id, version_number),
                CHECK((state = 'READY' AND is_active = 1) OR state != 'READY'),
                CHECK(is_active = 0 OR state = 'READY'),
                CHECK(
                    (state = 'FAILED' AND failure_code IS NOT NULL
                        AND failure_message IS NOT NULL)
                    OR
                    (state != 'FAILED' AND failure_code IS NULL
                        AND failure_message IS NULL)
                ),
                CHECK(
                    (state IN ('READY', 'SUPERSEDED') AND ready_at IS NOT NULL)
                    OR
                    (state NOT IN ('READY', 'SUPERSEDED') AND ready_at IS NULL)
                ),
                CHECK(
                    (state = 'SUPERSEDED' AND superseded_at IS NOT NULL)
                    OR
                    (state != 'SUPERSEDED' AND superseded_at IS NULL)
                ),
                FOREIGN KEY(document_id) REFERENCES document_sources(document_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT
            )
            """,
            """
            CREATE UNIQUE INDEX one_active_ready_version_per_document
            ON document_versions(document_id)
            WHERE is_active = 1
            """,
            """
            CREATE UNIQUE INDEX one_current_content_version_per_document
            ON document_versions(document_id, content_hash)
            WHERE state != 'SUPERSEDED'
            """,
            """
            CREATE INDEX document_versions_document_state
            ON document_versions(document_id, state, version_number)
            """,
            """
            CREATE TABLE ingestion_attempts (
                attempt_id TEXT PRIMARY KEY,
                document_version_id TEXT NOT NULL,
                attempt_number INTEGER NOT NULL CHECK(attempt_number >= 1),
                status TEXT NOT NULL CHECK(status IN ('started', 'succeeded', 'failed')),
                started_at TEXT NOT NULL,
                finished_at TEXT,
                failure_code TEXT,
                correlation_id TEXT NOT NULL,
                UNIQUE(document_version_id, attempt_number),
                CHECK(
                    (status = 'started' AND finished_at IS NULL AND failure_code IS NULL)
                    OR
                    (status = 'succeeded' AND finished_at IS NOT NULL
                        AND failure_code IS NULL)
                    OR
                    (status = 'failed' AND finished_at IS NOT NULL
                        AND failure_code IS NOT NULL)
                ),
                FOREIGN KEY(document_version_id)
                    REFERENCES document_versions(document_version_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE document_version_events (
                event_id TEXT PRIMARY KEY,
                document_version_id TEXT NOT NULL,
                from_state TEXT,
                to_state TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                attempt_id TEXT,
                correlation_id TEXT NOT NULL,
                FOREIGN KEY(document_version_id)
                    REFERENCES document_versions(document_version_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT,
                FOREIGN KEY(attempt_id) REFERENCES ingestion_attempts(attempt_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT
            )
            """,
            """
            CREATE TRIGGER document_version_events_no_update
            BEFORE UPDATE ON document_version_events
            BEGIN
                SELECT RAISE(ABORT, 'document version events are append-only');
            END
            """,
            """
            CREATE TRIGGER document_version_events_no_delete
            BEFORE DELETE ON document_version_events
            BEGIN
                SELECT RAISE(ABORT, 'document version events are append-only');
            END
            """,
            """
            CREATE TABLE ingestion_artifacts (
                artifact_id TEXT PRIMARY KEY,
                engagement_id TEXT NOT NULL,
                document_version_id TEXT NOT NULL,
                artifact_kind TEXT NOT NULL CHECK(artifact_kind IN (
                    'original', 'page_render', 'embedded_image', 'chart_render',
                    'workbook_manifest', 'extraction_manifest', 'normalized_render'
                )),
                virtual_path TEXT NOT NULL,
                media_type TEXT NOT NULL,
                content_hash TEXT NOT NULL
                    CHECK(length(content_hash) = 64
                        AND content_hash NOT GLOB '*[^0-9a-f]*'),
                created_at TEXT NOT NULL,
                UNIQUE(document_version_id, virtual_path),
                FOREIGN KEY(document_version_id)
                    REFERENCES document_versions(document_version_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT,
                FOREIGN KEY(engagement_id) REFERENCES engagements(engagement_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE source_elements (
                element_id TEXT PRIMARY KEY,
                document_version_id TEXT NOT NULL,
                element_order INTEGER NOT NULL CHECK(element_order >= 0),
                element_type TEXT NOT NULL CHECK(element_type IN (
                    'text', 'table', 'image', 'chart', 'ocr_text', 'vision_description'
                )),
                original_content TEXT,
                english_interpretation TEXT,
                location_json TEXT NOT NULL,
                parent_element_id TEXT,
                artifact_id TEXT,
                content_hash TEXT NOT NULL
                    CHECK(length(content_hash) = 64
                        AND content_hash NOT GLOB '*[^0-9a-f]*'),
                extraction_method TEXT NOT NULL,
                UNIQUE(document_version_id, element_order),
                FOREIGN KEY(document_version_id)
                    REFERENCES document_versions(document_version_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT,
                FOREIGN KEY(parent_element_id) REFERENCES source_elements(element_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT
                    DEFERRABLE INITIALLY DEFERRED,
                FOREIGN KEY(artifact_id) REFERENCES ingestion_artifacts(artifact_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT
            )
            """,
            """
            CREATE INDEX source_elements_version_location
            ON source_elements(document_version_id, element_order)
            """,
            """
            CREATE TABLE search_chunks (
                chunk_id TEXT PRIMARY KEY,
                engagement_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_version_id TEXT NOT NULL,
                element_ids_json TEXT NOT NULL,
                text_for_retrieval TEXT NOT NULL,
                location_json TEXT NOT NULL,
                stakeholder_id TEXT,
                role TEXT,
                department TEXT,
                doc_type TEXT NOT NULL
                    CHECK(doc_type IN ('pdf', 'docx', 'xlsx', 'pptx', 'png', 'jpeg')),
                source_type TEXT NOT NULL
                    CHECK(source_type IN ('stakeholder_document', 'engagement_document')),
                dense_vector_json TEXT NOT NULL,
                sparse_vector_json TEXT NOT NULL,
                is_active_ready INTEGER NOT NULL CHECK(is_active_ready IN (0, 1)),
                vector_stage_state TEXT NOT NULL
                    CHECK(vector_stage_state IN ('STAGED', 'PREPARED', 'ACTIVE')),
                FOREIGN KEY(source_id, engagement_id)
                    REFERENCES document_sources(document_id, engagement_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT,
                FOREIGN KEY(source_version_id)
                    REFERENCES document_versions(document_version_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT
            )
            """,
            """
            CREATE INDEX search_chunks_active_scope
            ON search_chunks(engagement_id, is_active_ready, source_version_id)
            """,
        ),
    ),
    Migration(
        version=4,
        name="retrieval_evidence_registry",
        statements=(
            """
            CREATE TABLE evidence_records (
                evidence_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                engagement_id TEXT NOT NULL,
                topic_id TEXT NOT NULL,
                researcher_id TEXT NOT NULL,
                chunk_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_version_id TEXT NOT NULL,
                source_type TEXT NOT NULL CHECK(source_type IN (
                    'stakeholder_document', 'engagement_document', 'interview'
                )),
                stakeholder_id TEXT,
                location_json TEXT NOT NULL,
                original_excerpt TEXT NOT NULL CHECK(length(trim(original_excerpt)) > 0),
                english_interpretation TEXT,
                content_hash TEXT NOT NULL
                    CHECK(length(content_hash) = 64
                        AND content_hash NOT GLOB '*[^0-9a-f]*'),
                created_at TEXT NOT NULL,
                UNIQUE(run_id, topic_id, researcher_id, chunk_id),
                CHECK(
                    (source_type = 'engagement_document' AND stakeholder_id IS NULL)
                    OR
                    (source_type IN ('stakeholder_document', 'interview')
                        AND stakeholder_id IS NOT NULL)
                ),
                FOREIGN KEY(engagement_id) REFERENCES engagements(engagement_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT
            )
            """,
            """
            CREATE INDEX evidence_records_run_scope
            ON evidence_records(engagement_id, run_id, topic_id, created_at)
            """,
            """
            CREATE TRIGGER evidence_records_no_update
            BEFORE UPDATE ON evidence_records
            BEGIN
                SELECT RAISE(ABORT, 'evidence records are append-only');
            END
            """,
            """
            CREATE TRIGGER evidence_records_no_delete
            BEFORE DELETE ON evidence_records
            BEGIN
                SELECT RAISE(ABORT, 'evidence records are append-only');
            END
            """,
        ),
    ),
    Migration(
        version=5,
        name="finalized_interview_transcripts",
        statements=(
            """
            CREATE TABLE transcripts (
                transcript_id TEXT PRIMARY KEY,
                interview_session_id TEXT NOT NULL UNIQUE,
                engagement_id TEXT NOT NULL,
                stakeholder_id TEXT NOT NULL,
                role TEXT,
                department TEXT,
                status TEXT NOT NULL CHECK(status IN ('draft', 'finalized')),
                language_observations_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                finalized_at TEXT,
                content_hash TEXT CHECK(
                    content_hash IS NULL OR (
                        length(content_hash) = 64
                        AND content_hash NOT GLOB '*[^0-9a-f]*'
                    )
                ),
                CHECK(
                    (status = 'draft' AND finalized_at IS NULL AND content_hash IS NULL)
                    OR
                    (status = 'finalized' AND finalized_at IS NOT NULL
                        AND content_hash IS NOT NULL)
                ),
                UNIQUE(transcript_id, engagement_id),
                FOREIGN KEY(interview_session_id, stakeholder_id, engagement_id)
                    REFERENCES interview_sessions(
                        interview_session_id, stakeholder_id, engagement_id
                    ) ON UPDATE RESTRICT ON DELETE RESTRICT
            )
            """,
            """
            CREATE TABLE transcript_turns (
                turn_id TEXT PRIMARY KEY,
                transcript_id TEXT NOT NULL,
                turn_index INTEGER NOT NULL CHECK(turn_index >= 0),
                speaker TEXT NOT NULL CHECK(speaker IN ('stakeholder', 'assistant')),
                original_text TEXT NOT NULL CHECK(length(trim(original_text)) > 0),
                created_at TEXT NOT NULL,
                checkpoint_message_id TEXT,
                UNIQUE(transcript_id, turn_index),
                UNIQUE(transcript_id, checkpoint_message_id),
                FOREIGN KEY(transcript_id) REFERENCES transcripts(transcript_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT
            )
            """,
            """
            CREATE INDEX transcript_turns_order
            ON transcript_turns(transcript_id, turn_index)
            """,
            """
            CREATE TRIGGER transcript_turns_draft_insert_only
            BEFORE INSERT ON transcript_turns
            WHEN NOT EXISTS (
                SELECT 1 FROM transcripts
                WHERE transcript_id = NEW.transcript_id AND status = 'draft'
            )
            BEGIN
                SELECT RAISE(ABORT, 'turns can only append to a draft transcript');
            END
            """,
            """
            CREATE TRIGGER transcript_turns_no_update
            BEFORE UPDATE ON transcript_turns
            BEGIN
                SELECT RAISE(ABORT, 'transcript turns are append-only');
            END
            """,
            """
            CREATE TRIGGER transcript_turns_no_delete
            BEFORE DELETE ON transcript_turns
            BEGIN
                SELECT RAISE(ABORT, 'transcript turns are append-only');
            END
            """,
            """
            CREATE TRIGGER finalized_transcripts_no_update
            BEFORE UPDATE ON transcripts
            WHEN OLD.status = 'finalized'
            BEGIN
                SELECT RAISE(ABORT, 'finalized transcripts are immutable');
            END
            """,
            """
            CREATE TRIGGER transcripts_no_delete
            BEFORE DELETE ON transcripts
            BEGIN
                SELECT RAISE(ABORT, 'transcripts cannot be deleted');
            END
            """,
            """
            CREATE TABLE transcript_ingestion_versions (
                transcript_ingestion_version_id TEXT PRIMARY KEY,
                transcript_id TEXT NOT NULL,
                content_hash TEXT NOT NULL CHECK(
                    length(content_hash) = 64
                    AND content_hash NOT GLOB '*[^0-9a-f]*'
                ),
                state TEXT NOT NULL CHECK(state IN (
                    'RECEIVED', 'INDEXING', 'READY', 'FAILED', 'SUPERSEDED'
                )),
                is_active INTEGER NOT NULL CHECK(is_active IN (0, 1)),
                created_at TEXT NOT NULL,
                ready_at TEXT,
                failure_code TEXT,
                failure_message TEXT,
                lease_token TEXT,
                lease_expires_at TEXT,
                UNIQUE(transcript_id, content_hash),
                CHECK((state = 'READY' AND is_active = 1) OR state != 'READY'),
                CHECK(is_active = 0 OR state = 'READY'),
                CHECK(
                    (state = 'FAILED' AND failure_code IS NOT NULL
                        AND failure_message IS NOT NULL)
                    OR
                    (state != 'FAILED' AND failure_code IS NULL
                        AND failure_message IS NULL)
                ),
                CHECK(
                    (state IN ('READY', 'SUPERSEDED') AND ready_at IS NOT NULL)
                    OR
                    (state NOT IN ('READY', 'SUPERSEDED') AND ready_at IS NULL)
                ),
                FOREIGN KEY(transcript_id) REFERENCES transcripts(transcript_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT
            )
            """,
            """
            CREATE UNIQUE INDEX one_active_ready_version_per_transcript
            ON transcript_ingestion_versions(transcript_id)
            WHERE is_active = 1
            """,
            """
            CREATE TABLE transcript_version_events (
                event_id TEXT PRIMARY KEY,
                transcript_ingestion_version_id TEXT NOT NULL,
                from_state TEXT,
                to_state TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                FOREIGN KEY(transcript_ingestion_version_id)
                    REFERENCES transcript_ingestion_versions(
                        transcript_ingestion_version_id
                    ) ON UPDATE RESTRICT ON DELETE RESTRICT
            )
            """,
            """
            CREATE TRIGGER transcript_version_events_no_update
            BEFORE UPDATE ON transcript_version_events
            BEGIN
                SELECT RAISE(ABORT, 'transcript version events are append-only');
            END
            """,
            """
            CREATE TRIGGER transcript_version_events_no_delete
            BEFORE DELETE ON transcript_version_events
            BEGIN
                SELECT RAISE(ABORT, 'transcript version events are append-only');
            END
            """,
            """
            CREATE TABLE transcript_search_chunks (
                chunk_id TEXT PRIMARY KEY,
                engagement_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_version_id TEXT NOT NULL,
                element_ids_json TEXT NOT NULL,
                text_for_retrieval TEXT NOT NULL,
                location_json TEXT NOT NULL,
                stakeholder_id TEXT NOT NULL,
                role TEXT,
                department TEXT,
                doc_type TEXT NOT NULL CHECK(doc_type = 'transcript'),
                source_type TEXT NOT NULL CHECK(source_type = 'interview'),
                dense_vector_json TEXT NOT NULL,
                sparse_vector_json TEXT NOT NULL,
                is_active_ready INTEGER NOT NULL CHECK(is_active_ready IN (0, 1)),
                vector_stage_state TEXT NOT NULL CHECK(
                    vector_stage_state IN ('STAGED', 'PREPARED', 'ACTIVE')
                ),
                FOREIGN KEY(source_id, engagement_id)
                    REFERENCES transcripts(transcript_id, engagement_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT,
                FOREIGN KEY(source_version_id)
                    REFERENCES transcript_ingestion_versions(
                        transcript_ingestion_version_id
                    ) ON UPDATE RESTRICT ON DELETE RESTRICT
            )
            """,
            """
            CREATE INDEX transcript_chunks_active_scope
            ON transcript_search_chunks(
                engagement_id, is_active_ready, source_version_id
            )
            """,
        ),
    ),
    Migration(
        version=6,
        name="insight_run_and_report_lifecycle",
        statements=(
            """
            CREATE TABLE insight_runs (
                run_id TEXT PRIMARY KEY,
                engagement_id TEXT NOT NULL,
                thread_id TEXT NOT NULL UNIQUE,
                requested_by_pm_access_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN (
                    'queued', 'planning', 'researching', 'editing', 'validating',
                    'complete', 'partial', 'insufficient_evidence', 'failed'
                )),
                requested_question TEXT NOT NULL
                    CHECK(length(trim(requested_question)) > 0),
                plan_id TEXT,
                report_id TEXT,
                failure_code TEXT,
                failure_message TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE(run_id, engagement_id),
                CHECK(
                    (status IN ('researching', 'editing', 'validating', 'complete',
                        'partial', 'insufficient_evidence') AND plan_id IS NOT NULL)
                    OR
                    (status NOT IN ('researching', 'editing', 'validating', 'complete',
                        'partial', 'insufficient_evidence'))
                ),
                CHECK(
                    (status IN ('complete', 'partial', 'insufficient_evidence')
                        AND report_id IS NOT NULL AND completed_at IS NOT NULL)
                    OR
                    (status NOT IN ('complete', 'partial', 'insufficient_evidence')
                        AND report_id IS NULL)
                ),
                CHECK(
                    (status = 'failed' AND failure_code IS NOT NULL
                        AND failure_message IS NOT NULL AND completed_at IS NOT NULL)
                    OR
                    (status != 'failed' AND failure_code IS NULL
                        AND failure_message IS NULL)
                ),
                FOREIGN KEY(engagement_id) REFERENCES engagements(engagement_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT,
                FOREIGN KEY(requested_by_pm_access_id) REFERENCES pm_access(pm_access_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT
            )
            """,
            """
            CREATE INDEX insight_runs_scope_status
            ON insight_runs(engagement_id, status, started_at)
            """,
            """
            CREATE TABLE insight_run_events (
                event_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                engagement_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT,
                topic_id TEXT,
                source_ids_json TEXT NOT NULL DEFAULT '[]',
                evidence_ids_json TEXT NOT NULL DEFAULT '[]',
                artifact_name TEXT,
                failure_code TEXT,
                correlation_id TEXT NOT NULL,
                FOREIGN KEY(run_id, engagement_id)
                    REFERENCES insight_runs(run_id, engagement_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT
            )
            """,
            """
            CREATE INDEX insight_run_events_order
            ON insight_run_events(run_id, occurred_at, event_id)
            """,
            """
            CREATE TRIGGER insight_run_events_no_update
            BEFORE UPDATE ON insight_run_events
            BEGIN
                SELECT RAISE(ABORT, 'insight run events are append-only');
            END
            """,
            """
            CREATE TRIGGER insight_run_events_no_delete
            BEFORE DELETE ON insight_run_events
            BEGIN
                SELECT RAISE(ABORT, 'insight run events are append-only');
            END
            """,
            """
            CREATE TABLE insight_report_records (
                report_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL UNIQUE,
                engagement_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN (
                    'complete', 'partial', 'insufficient_evidence'
                )),
                virtual_path TEXT NOT NULL,
                content_hash TEXT NOT NULL CHECK(
                    length(content_hash) = 64
                    AND content_hash NOT GLOB '*[^0-9a-f]*'
                ),
                created_at TEXT NOT NULL,
                FOREIGN KEY(run_id, engagement_id)
                    REFERENCES insight_runs(run_id, engagement_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT
            )
            """,
            """
            CREATE TRIGGER insight_report_records_no_update
            BEFORE UPDATE ON insight_report_records
            BEGIN
                SELECT RAISE(ABORT, 'insight report records are immutable');
            END
            """,
            """
            CREATE TRIGGER insight_report_records_no_delete
            BEFORE DELETE ON insight_report_records
            BEGIN
                SELECT RAISE(ABORT, 'insight report records are immutable');
            END
            """,
        ),
    ),
    Migration(
        version=7,
        name="insight_execution_observability",
        statements=(
            """
            CREATE TABLE insight_execution_metrics (
                run_id TEXT PRIMARY KEY,
                engagement_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN (
                    'complete', 'partial', 'insufficient_evidence', 'failed'
                )),
                duration_ms INTEGER NOT NULL CHECK(duration_ms >= 0),
                topic_count INTEGER NOT NULL CHECK(topic_count BETWEEN 0 AND 5),
                researcher_calls INTEGER NOT NULL CHECK(researcher_calls >= 0),
                max_concurrent_researchers INTEGER NOT NULL
                    CHECK(max_concurrent_researchers >= 0),
                model_calls INTEGER NOT NULL CHECK(model_calls >= 0),
                model_failures INTEGER NOT NULL CHECK(model_failures >= 0),
                tool_calls INTEGER NOT NULL CHECK(tool_calls >= 0),
                tool_failures INTEGER NOT NULL CHECK(tool_failures >= 0),
                retrieval_calls INTEGER NOT NULL CHECK(retrieval_calls >= 0),
                retry_count INTEGER NOT NULL CHECK(retry_count >= 0),
                timeout_count INTEGER NOT NULL CHECK(timeout_count >= 0),
                rerank_candidates_total INTEGER NOT NULL
                    CHECK(rerank_candidates_total >= 0),
                max_rerank_candidates_per_call INTEGER NOT NULL
                    CHECK(max_rerank_candidates_per_call >= 0),
                retrieval_latency_ms INTEGER NOT NULL CHECK(retrieval_latency_ms >= 0),
                reranker_latency_ms INTEGER NOT NULL CHECK(reranker_latency_ms >= 0),
                input_tokens INTEGER NOT NULL CHECK(input_tokens >= 0),
                output_tokens INTEGER NOT NULL CHECK(output_tokens >= 0),
                total_tokens INTEGER NOT NULL CHECK(total_tokens >= 0),
                configured_topic_limit INTEGER NOT NULL
                    CHECK(configured_topic_limit BETWEEN 1 AND 5),
                configured_parallel_researcher_limit INTEGER NOT NULL
                    CHECK(configured_parallel_researcher_limit BETWEEN 1 AND 3),
                configured_model_call_limit INTEGER NOT NULL
                    CHECK(configured_model_call_limit >= 1),
                configured_tool_call_limit INTEGER NOT NULL
                    CHECK(configured_tool_call_limit >= 1),
                configured_retrieval_calls_per_researcher_limit INTEGER NOT NULL
                    CHECK(configured_retrieval_calls_per_researcher_limit >= 1),
                configured_rerank_candidate_limit INTEGER NOT NULL
                    CHECK(configured_rerank_candidate_limit >= 1),
                configured_provider_timeout_seconds INTEGER NOT NULL
                    CHECK(configured_provider_timeout_seconds >= 1),
                configured_run_timeout_seconds INTEGER NOT NULL
                    CHECK(configured_run_timeout_seconds >= 1),
                source_ids_json TEXT NOT NULL DEFAULT '[]',
                evidence_ids_json TEXT NOT NULL DEFAULT '[]',
                tool_names_json TEXT NOT NULL DEFAULT '[]',
                failure_code TEXT,
                correlation_id TEXT NOT NULL,
                UNIQUE(run_id, engagement_id, thread_id),
                CHECK(topic_count <= configured_topic_limit),
                CHECK(max_concurrent_researchers <= configured_parallel_researcher_limit),
                CHECK(max_rerank_candidates_per_call <= configured_rerank_candidate_limit),
                CHECK(model_failures <= model_calls),
                CHECK(tool_failures <= tool_calls),
                CHECK(retrieval_calls <= tool_calls),
                CHECK(
                    (status = 'failed' AND failure_code IS NOT NULL)
                    OR (status != 'failed' AND failure_code IS NULL)
                ),
                FOREIGN KEY(run_id, engagement_id)
                    REFERENCES insight_runs(run_id, engagement_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT
            )
            """,
            """
            CREATE TRIGGER insight_execution_metrics_no_update
            BEFORE UPDATE ON insight_execution_metrics
            BEGIN
                SELECT RAISE(ABORT, 'insight execution metrics are immutable');
            END
            """,
            """
            CREATE TRIGGER insight_execution_metrics_no_delete
            BEFORE DELETE ON insight_execution_metrics
            BEGIN
                SELECT RAISE(ABORT, 'insight execution metrics are immutable');
            END
            """,
            """
            CREATE TABLE insight_execution_events (
                event_id TEXT PRIMARY KEY,
                occurred_at TEXT NOT NULL,
                run_id TEXT NOT NULL,
                engagement_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                actor TEXT NOT NULL,
                operation_type TEXT NOT NULL CHECK(operation_type IN ('model', 'tool')),
                tool_name TEXT,
                status TEXT NOT NULL CHECK(status IN ('succeeded', 'failed')),
                duration_ms INTEGER NOT NULL CHECK(duration_ms >= 0),
                source_ids_json TEXT NOT NULL DEFAULT '[]',
                evidence_ids_json TEXT NOT NULL DEFAULT '[]',
                retry_count INTEGER NOT NULL CHECK(retry_count >= 0),
                failure_code TEXT,
                correlation_id TEXT NOT NULL,
                CHECK(
                    (operation_type = 'tool' AND tool_name IS NOT NULL)
                    OR (operation_type = 'model' AND tool_name IS NULL)
                ),
                CHECK(
                    (status = 'failed' AND failure_code IS NOT NULL)
                    OR (status = 'succeeded' AND failure_code IS NULL)
                ),
                FOREIGN KEY(run_id, engagement_id)
                    REFERENCES insight_runs(run_id, engagement_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT
            )
            """,
            """
            CREATE INDEX insight_execution_events_order
            ON insight_execution_events(run_id, occurred_at, event_id)
            """,
            """
            CREATE TRIGGER insight_execution_events_no_update
            BEFORE UPDATE ON insight_execution_events
            BEGIN
                SELECT RAISE(ABORT, 'insight execution events are append-only');
            END
            """,
            """
            CREATE TRIGGER insight_execution_events_no_delete
            BEFORE DELETE ON insight_execution_events
            BEGIN
                SELECT RAISE(ABORT, 'insight execution events are append-only');
            END
            """,
        ),
    ),
    Migration(
        version=8,
        name="reusable_interview_invitation_links",
        statements=(
            "ALTER TABLE invitation_tokens ADD COLUMN token_ciphertext TEXT",
            "ALTER TABLE invitation_tokens ADD COLUMN interview_session_id TEXT",
            """
            UPDATE invitation_tokens SET status = 'expired'
            WHERE token_ciphertext IS NULL AND status IN ('active', 'activated')
            """,
            "CREATE INDEX invitation_interview_session ON invitation_tokens(interview_session_id)",
        ),
    ),
    Migration(
        version=9,
        name="normalize_stakeholder_entity_status",
        statements=(
            "UPDATE stakeholders SET status = 'active' WHERE status IN ('invited', 'completed')",
            """
            CREATE TRIGGER stakeholders_valid_status_insert
            BEFORE INSERT ON stakeholders
            WHEN NEW.status NOT IN ('active', 'revoked')
            BEGIN
                SELECT RAISE(ABORT, 'invalid stakeholder status');
            END
            """,
            """
            CREATE TRIGGER stakeholders_valid_status_update
            BEFORE UPDATE OF status ON stakeholders
            WHEN NEW.status NOT IN ('active', 'revoked')
            BEGIN
                SELECT RAISE(ABORT, 'invalid stakeholder status');
            END
            """,
        ),
    ),
    Migration(
        version=10,
        name="draft_interview_edits_and_document_withdrawal",
        statements=(
            "ALTER TABLE document_sources ADD COLUMN deleted_at TEXT",
            "DROP TRIGGER transcript_turns_no_delete",
            """
            CREATE TRIGGER transcript_turns_draft_delete_only
            BEFORE DELETE ON transcript_turns
            WHEN NOT EXISTS (
                SELECT 1 FROM transcripts
                WHERE transcript_id = OLD.transcript_id AND status = 'draft'
            )
            BEGIN
                SELECT RAISE(ABORT, 'finalized transcript turns are immutable');
            END
            """,
        ),
    ),
)


def validate_migration_inventory() -> None:
    """Fail fast if versions or names drift into an unsafe migration sequence."""
    versions = tuple(migration.version for migration in MIGRATIONS)
    if versions != tuple(range(1, len(MIGRATIONS) + 1)):
        raise MigrationInventoryError
    names = tuple(migration.name for migration in MIGRATIONS)
    if len(names) != len(set(names)):
        raise MigrationInventoryError
