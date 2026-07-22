import { useCallback, useEffect, useRef, useState } from "react";
import type { SyntheticEvent } from "react";

import type { DocumentSummary, Engagement, IngestionState, Stakeholder } from "../api/contracts";
import { Button } from "../components/button";
import { DeleteConfirmationModal } from "../components/delete-confirmation-modal";
import { LoadingIndicator } from "../components/loading-indicator";
import { SuccessNotice } from "../components/notice";
import { EmptyState, SafeFailureNotice, StatusBadge } from "./common";
import type { DocumentProcessingDetailsResponse } from "./contracts";
import { DocumentPreview, DocumentProcessingDetails } from "./document-review";
import type { PmApi } from "./pm-api";
import { failureFromResult, formatDateTime } from "./safe-ui";
import type { SafeUiFailure } from "./safe-ui";

const approvedExtensions = new Set(["pdf", "docx", "pptx", "xlsx", "png", "jpg", "jpeg"]);
const acceptedFormats = ".pdf,.docx,.pptx,.xlsx,.png,.jpg,.jpeg";

function extensionOf(filename: string): string {
  const separator = filename.lastIndexOf(".");
  return separator === -1 ? "" : filename.slice(separator + 1).toLowerCase();
}

function ingestionTone(state: IngestionState) {
  if (state === "READY") {
    return "success" as const;
  }
  if (state === "FAILED") {
    return "error" as const;
  }
  if (state === "SUPERSEDED") {
    return "neutral" as const;
  }
  return "info" as const;
}

export function PmDocuments({ api, engagement }: { api: PmApi; engagement: Engagement }) {
  const [documents, setDocuments] = useState<DocumentSummary[] | null>(null);
  const [stakeholders, setStakeholders] = useState<Stakeholder[] | null>(null);
  const [failure, setFailure] = useState<SafeUiFailure | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadSummary, setUploadSummary] = useState<string | null>(null);
  const [previewDocument, setPreviewDocument] = useState<DocumentSummary | null>(null);
  const [processingDetails, setProcessingDetails] =
    useState<DocumentProcessingDetailsResponse | null>(null);
  const [processingDocumentId, setProcessingDocumentId] = useState<string | null>(null);
  const [reviewFailure, setReviewFailure] = useState<SafeUiFailure | null>(null);
  const [deleteCandidate, setDeleteCandidate] = useState<DocumentSummary | null>(null);
  const [deletingDocumentId, setDeletingDocumentId] = useState<string | null>(null);
  const [deletionSummary, setDeletionSummary] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const loadDocuments = useCallback(async (): Promise<void> => {
    try {
      const [documentResult, stakeholderResult] = await Promise.all([
        api.listDocuments(engagement.engagement_id),
        api.listStakeholders(engagement.engagement_id),
      ]);
      if (documentResult.ok && stakeholderResult.ok) {
        setDocuments(documentResult.value);
        setStakeholders(stakeholderResult.value);
        setFailure(null);
      } else {
        setFailure(failureFromResult(documentResult.ok ? stakeholderResult : documentResult));
      }
    } catch {
      setFailure({ message: "Documents could not be loaded.", correlationId: null });
    }
  }, [api, engagement.engagement_id]);

  useEffect(() => {
    let current = true;
    async function load(): Promise<void> {
      try {
        const [documentResult, stakeholderResult] = await Promise.all([
          api.listDocuments(engagement.engagement_id),
          api.listStakeholders(engagement.engagement_id),
        ]);
        if (!current) {
          return;
        }
        if (documentResult.ok && stakeholderResult.ok) {
          setDocuments(documentResult.value);
          setStakeholders(stakeholderResult.value);
          setFailure(null);
        } else {
          setFailure(failureFromResult(documentResult.ok ? stakeholderResult : documentResult));
        }
      } catch {
        if (current) {
          setFailure({ message: "Documents could not be loaded.", correlationId: null });
        }
      }
    }
    void load();
    return () => {
      current = false;
    };
  }, [api, engagement.engagement_id]);

  useEffect(() => {
    const refreshWhenVisible = (): void => {
      if (document.visibilityState === "visible") {
        void loadDocuments();
      }
    };
    globalThis.addEventListener("focus", refreshWhenVisible);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      globalThis.removeEventListener("focus", refreshWhenVisible);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [loadDocuments]);

  function chooseFile(selected: File | null): void {
    setUploadSummary(null);
    if (selected === null) {
      setFile(null);
      setFileError(null);
      return;
    }
    if (!approvedExtensions.has(extensionOf(selected.name))) {
      setFile(null);
      setFileError("Choose a PDF, DOCX, PPTX, XLSX, PNG, or JPEG file.");
      return;
    }
    setFile(selected);
    setFileError(null);
  }

  async function upload(event: SyntheticEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (file === null) {
      setFileError("Choose a permitted file before uploading.");
      return;
    }
    setUploading(true);
    setFailure(null);
    setDeletionSummary(null);
    try {
      const result = await api.uploadDocument(engagement.engagement_id, file);
      if (result.ok) {
        setDocuments((current) => {
          const previous = current ?? [];
          return [
            result.value.document,
            ...previous.filter(
              (item) => item.source.document_id !== result.value.document.source.document_id,
            ),
          ];
        });
        setUploadSummary(
          result.value.idempotent
            ? "The existing immutable upload was reused."
            : `Upload completed with ${String(result.value.element_count)} elements and ${String(result.value.chunk_count)} retrieval chunks.`,
        );
        setFile(null);
        if (inputRef.current !== null) {
          inputRef.current.value = "";
        }
      } else {
        setFailure(failureFromResult(result));
      }
    } catch {
      setFailure({ message: "The document could not be uploaded.", correlationId: null });
    } finally {
      setUploading(false);
    }
  }

  function preview(document: DocumentSummary): void {
    setProcessingDetails(null);
    setReviewFailure(null);
    setPreviewDocument(document);
  }

  async function showProcessingDetails(document: DocumentSummary): Promise<void> {
    setPreviewDocument(null);
    setProcessingDetails(null);
    setReviewFailure(null);
    setProcessingDocumentId(document.source.document_id);
    try {
      const result = await api.getDocumentProcessing(
        engagement.engagement_id,
        document.source.document_id,
      );
      if (result.ok) {
        setProcessingDetails(result.value);
      } else {
        setReviewFailure(failureFromResult(result));
      }
    } catch {
      setReviewFailure({
        message: "Document analysis could not be loaded.",
        correlationId: null,
      });
    } finally {
      setProcessingDocumentId(null);
    }
  }

  async function deleteDocument(document: DocumentSummary): Promise<void> {
    if (document.source.source_type !== "engagement_document") {
      return;
    }
    setDeletingDocumentId(document.source.document_id);
    setFailure(null);
    setDeletionSummary(null);
    try {
      const result = await api.deleteDocument(
        engagement.engagement_id,
        document.source.document_id,
      );
      if (!result.ok) {
        setFailure(failureFromResult(result));
        return;
      }
      setDocuments(
        (current) =>
          current?.filter((item) => item.source.document_id !== document.source.document_id) ?? [],
      );
      setDeleteCandidate(null);
      setDeletionSummary(`${document.source.original_filename} was deleted.`);
    } catch {
      setFailure({ message: "The document could not be deleted.", correlationId: null });
    } finally {
      setDeletingDocumentId(null);
    }
  }

  const pmDocuments =
    documents?.filter((document) => document.source.source_type === "engagement_document") ?? [];
  const stakeholderDocuments =
    documents?.filter((document) => document.source.source_type === "stakeholder_document") ?? [];
  const stakeholderNames = new Map(
    (stakeholders ?? []).map((stakeholder) => [
      stakeholder.stakeholder_id,
      stakeholder.display_name,
    ]),
  );
  const stakeholderGroups = new Map<string, DocumentSummary[]>();
  for (const document of stakeholderDocuments) {
    const stakeholderId = document.source.stakeholder_id ?? "unknown-stakeholder";
    const existing = stakeholderGroups.get(stakeholderId) ?? [];
    existing.push(document);
    stakeholderGroups.set(stakeholderId, existing);
  }

  function documentTable(items: DocumentSummary[], allowDelete: boolean) {
    return (
      <div className="overflow-x-auto rounded-control border border-border">
        <table className="w-full min-w-[64rem] table-fixed border-collapse text-left text-sm">
          <colgroup>
            <col />
            <col className="w-24" />
            <col className="w-24" />
            <col className="w-32" />
            <col className="w-48" />
            <col className="w-[19rem]" />
          </colgroup>
          <thead className="bg-surface-subtle text-muted-foreground">
            <tr>
              <th className="px-4 py-3 text-left font-semibold">Document</th>
              <th className="w-24 px-4 py-3 text-left font-semibold">Format</th>
              <th className="w-24 px-4 py-3 text-left font-semibold">Version</th>
              <th className="w-32 px-4 py-3 text-left font-semibold">Ingestion</th>
              <th className="w-48 px-4 py-3 text-left font-semibold">Created</th>
              <th className="w-[19rem] px-4 py-3 text-left font-semibold">Actions</th>
            </tr>
          </thead>
          <tbody>
            {items.map((document) => (
              <tr key={document.source.document_id} className="border-t border-border align-top">
                <td className="px-4 py-3 text-left">
                  <p className="font-medium break-all text-foreground">
                    {document.source.original_filename}
                  </p>
                  {document.latest_version.failure_message === null ? null : (
                    <p className="mt-1 text-xs text-danger-foreground">
                      {document.latest_version.failure_message}
                    </p>
                  )}
                </td>
                <td className="px-4 py-3 text-left text-muted-foreground uppercase">
                  {document.source.doc_type}
                </td>
                <td className="px-4 py-3 text-left text-muted-foreground">
                  {document.latest_version.version_number}
                </td>
                <td className="px-4 py-3 text-left">
                  <div className="flex justify-start">
                    <StatusBadge
                      label={document.latest_version.state}
                      tone={ingestionTone(document.latest_version.state)}
                    />
                  </div>
                </td>
                <td className="px-4 py-3 text-left text-muted-foreground">
                  <time dateTime={document.source.created_at}>
                    {formatDateTime(document.source.created_at)}
                  </time>
                </td>
                <td className="px-4 py-3 text-left">
                  <div className="flex flex-nowrap items-center justify-start gap-2">
                    {document.source.media_type === "application/pdf" ||
                    document.source.media_type.startsWith("image/") ? (
                      <Button
                        className="w-24"
                        size="small"
                        variant="secondary"
                        onClick={() => preview(document)}
                      >
                        Preview
                      </Button>
                    ) : (
                      <a
                        className="inline-flex min-h-9 w-24 items-center justify-center rounded-control border border-border-strong bg-surface px-3 py-1.5 text-sm font-semibold text-foreground transition-colors hover:border-brand hover:text-brand focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
                        href={api.documentArtifactPath(
                          engagement.engagement_id,
                          document.source.document_id,
                          document.latest_version.original_artifact_id,
                        )}
                        download={document.source.original_filename}
                      >
                        Download
                      </a>
                    )}
                    <Button
                      size="small"
                      variant="quiet"
                      disabled={processingDocumentId !== null}
                      onClick={() => void showProcessingDetails(document)}
                    >
                      {processingDocumentId === document.source.document_id
                        ? "Loading analysis..."
                        : "View analysis"}
                    </Button>
                    {allowDelete ? (
                      <Button
                        className="px-2 text-danger-foreground"
                        size="small"
                        variant="quiet"
                        aria-label={`Delete ${document.source.original_filename}`}
                        title="Delete document"
                        disabled={deletingDocumentId !== null}
                        onClick={() => setDeleteCandidate(document)}
                      >
                        <svg
                          aria-hidden="true"
                          className="h-4 w-4"
                          viewBox="0 0 24 24"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="2"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        >
                          <path d="M3 6h18" />
                          <path d="M8 6V4h8v2" />
                          <path d="M19 6l-1 14H6L5 6" />
                          <path d="M10 11v5" />
                          <path d="M14 11v5" />
                        </svg>
                      </Button>
                    ) : null}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  return (
    <div className="grid gap-7">
      <div>
        <h4 className="text-xl font-semibold tracking-tight text-foreground">
          Engagement documents
        </h4>
        <p className="mt-1 text-sm leading-6 text-muted-foreground">
          Manage and review documents for this engagement.
        </p>
      </div>

      <form
        className="grid gap-4 rounded-control bg-surface-subtle p-4 sm:p-5"
        onSubmit={(event) => void upload(event)}
      >
        <div className="grid gap-2">
          <label htmlFor="pm-document-upload" className="text-sm font-semibold text-foreground">
            Source document
          </label>
          <input
            ref={inputRef}
            id="pm-document-upload"
            type="file"
            accept={acceptedFormats}
            aria-invalid={fileError !== null || undefined}
            aria-describedby={
              fileError === null
                ? "pm-document-upload-hint"
                : "pm-document-upload-hint pm-document-upload-error"
            }
            className="w-full min-w-0 rounded-control border border-border-strong bg-surface px-3 py-2 text-sm text-foreground file:mr-3 file:rounded-control file:border file:border-border-strong file:bg-surface file:px-3 file:py-2 file:font-semibold file:text-foreground file:hover:bg-surface-subtle focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
            onChange={(event) => chooseFile(event.target.files?.item(0) ?? null)}
          />
          <p id="pm-document-upload-hint" className="text-sm text-muted-foreground">
            Accepted formats: PDF, DOCX, PPTX, XLSX, PNG, and JPEG.
          </p>
          {fileError === null ? null : (
            <p
              id="pm-document-upload-error"
              className="text-sm font-semibold text-danger"
              role="alert"
            >
              {fileError}
            </p>
          )}
        </div>
        <div className="flex justify-end">
          <Button type="submit" disabled={uploading}>
            {uploading ? "Uploading and ingesting…" : "Upload document"}
          </Button>
        </div>
      </form>

      {failure === null ? null : <SafeFailureNotice failure={failure} />}
      {uploadSummary === null ? null : (
        <SuccessNotice title="Document accepted">{uploadSummary}</SuccessNotice>
      )}
      {deletionSummary === null ? null : (
        <SuccessNotice title="Document deleted">{deletionSummary}</SuccessNotice>
      )}

      <section aria-labelledby="document-inventory-title" className="grid gap-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h5 id="document-inventory-title" className="text-lg font-semibold text-foreground">
            Document library
          </h5>
          <Button size="small" variant="quiet" onClick={() => void loadDocuments()}>
            Refresh documents
          </Button>
        </div>
        {documents === null && failure === null ? (
          <LoadingIndicator label="Loading document inventory…" />
        ) : documents === null || documents.length === 0 ? (
          <EmptyState>No engagement documents are available.</EmptyState>
        ) : (
          <div className="grid gap-7">
            <section aria-labelledby="pm-document-title" className="grid gap-3">
              <div>
                <h6 id="pm-document-title" className="text-base font-semibold text-foreground">
                  Project manager documents
                </h6>
                <p className="mt-1 text-sm text-muted-foreground">
                  Documents you added for this engagement.
                </p>
              </div>
              {pmDocuments.length === 0 ? (
                <EmptyState>No project manager documents have been added.</EmptyState>
              ) : (
                documentTable(pmDocuments, true)
              )}
            </section>

            <section aria-labelledby="stakeholder-document-title" className="grid gap-4">
              <div>
                <h6
                  id="stakeholder-document-title"
                  className="text-base font-semibold text-foreground"
                >
                  Stakeholder documents
                </h6>
                <p className="mt-1 text-sm text-muted-foreground">
                  Documents shared by stakeholders during their interviews.
                </p>
              </div>
              {stakeholderGroups.size === 0 ? (
                <EmptyState>No stakeholder documents have been added.</EmptyState>
              ) : (
                Array.from(stakeholderGroups.entries()).map(([stakeholderId, items]) => {
                  const first = items[0];
                  const stakeholderName = stakeholderNames.get(stakeholderId) ?? "Stakeholder";
                  const details = [first?.source.role, first?.source.department]
                    .filter((value): value is string => value !== null && value !== undefined)
                    .join(" · ");
                  return (
                    <article
                      key={stakeholderId}
                      className="grid gap-3"
                      aria-label={`Documents from ${stakeholderName}`}
                    >
                      <div>
                        <p className="text-sm font-semibold text-foreground">{stakeholderName}</p>
                        {details.length === 0 ? null : (
                          <p className="mt-0.5 text-sm text-muted-foreground">{details}</p>
                        )}
                      </div>
                      {documentTable(items, false)}
                    </article>
                  );
                })
              )}
            </section>
          </div>
        )}
      </section>

      {reviewFailure === null ? null : <SafeFailureNotice failure={reviewFailure} />}
      {previewDocument === null ? null : (
        <DocumentPreview
          document={previewDocument}
          sourcePath={api.documentArtifactPath(
            engagement.engagement_id,
            previewDocument.source.document_id,
            previewDocument.latest_version.original_artifact_id,
          )}
          onClose={() => setPreviewDocument(null)}
        />
      )}
      {processingDetails === null ? null : (
        <DocumentProcessingDetails
          details={processingDetails}
          onClose={() => setProcessingDetails(null)}
        />
      )}
      {deleteCandidate === null ? null : (
        <DeleteConfirmationModal
          title="Delete this project manager document?"
          description={deleteCandidate.source.original_filename}
          confirmLabel="Delete"
          busyLabel="Deleting..."
          busy={deletingDocumentId !== null}
          onCancel={() => setDeleteCandidate(null)}
          onConfirm={() => void deleteDocument(deleteCandidate)}
        />
      )}
    </div>
  );
}
