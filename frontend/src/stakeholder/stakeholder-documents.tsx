import { useEffect, useRef, useState } from "react";
import type { SyntheticEvent } from "react";

import type { DocumentSummary, IngestionState } from "../api/contracts";
import { Button } from "../components/button";
import { DeleteConfirmationModal } from "../components/delete-confirmation-modal";
import { LoadingIndicator } from "../components/loading-indicator";
import { EmptyState, SafeFailureNotice, StatusBadge } from "../pm/common";
import { failureFromResult, formatDateTime } from "../pm/safe-ui";
import type { SafeUiFailure } from "../pm/safe-ui";
import type { StakeholderScope } from "./contracts";
import type { StakeholderApi } from "./stakeholder-api";

const acceptedFormats = ".pdf,.docx,.pptx,.xlsx,.png,.jpg,.jpeg";
const approvedExtensions = new Set(["pdf", "docx", "pptx", "xlsx", "png", "jpg", "jpeg"]);

function extensionOf(filename: string): string {
  return filename.includes(".") ? (filename.split(".").pop()?.toLowerCase() ?? "") : "";
}

async function contentHash(file: File): Promise<string> {
  const digest = await globalThis.crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function ingestionTone(state: IngestionState): "neutral" | "success" | "warning" | "error" {
  if (state === "READY") {
    return "success";
  }
  if (state === "FAILED") {
    return "error";
  }
  if (state === "SUPERSEDED") {
    return "neutral";
  }
  return "warning";
}

function ingestionLabel(state: IngestionState): string {
  if (state === "READY") {
    return "Uploaded";
  }
  if (state === "FAILED") {
    return "Upload failed";
  }
  if (state === "SUPERSEDED") {
    return "Removed";
  }
  return "Processing";
}

interface StakeholderDocumentsProps {
  api: StakeholderApi;
  scope: StakeholderScope;
  canDelete: boolean;
  onUnauthorized: (correlationId: string | null) => void;
}

export function StakeholderDocuments({
  api,
  scope,
  canDelete,
  onUnauthorized,
}: StakeholderDocumentsProps) {
  const [expanded, setExpanded] = useState(false);
  const [documents, setDocuments] = useState<DocumentSummary[] | null>(null);
  const [failure, setFailure] = useState<SafeUiFailure | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [fileError, setFileError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadSummary, setUploadSummary] = useState<string | null>(null);
  const [deleteCandidate, setDeleteCandidate] = useState<string | null>(null);
  const [deletingDocumentId, setDeletingDocumentId] = useState<string | null>(null);
  const [deletionSummary, setDeletionSummary] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!expanded || documents !== null) {
      return;
    }
    let active = true;
    void api
      .listDocuments(scope)
      .then((result) => {
        if (!active) {
          return;
        }
        if (result.ok) {
          setDocuments(result.value);
          return;
        }
        if (result.status === 401 || result.status === 403) {
          onUnauthorized(result.detail?.correlation_id ?? null);
          return;
        }
        setFailure(failureFromResult(result));
      })
      .catch(() => {
        if (active) {
          setFailure({
            message: "The supporting document inventory could not be loaded.",
            correlationId: null,
          });
        }
      });
    return () => {
      active = false;
    };
  }, [api, documents, expanded, onUnauthorized, scope]);

  function chooseFiles(selected: File[]): void {
    setUploadSummary(null);
    if (selected.length === 0) {
      setFiles([]);
      setFileError(null);
      return;
    }
    if (selected.some((file) => !approvedExtensions.has(extensionOf(file.name)))) {
      setFiles([]);
      setFileError("Choose only PDF, DOCX, PPTX, XLSX, PNG, or JPEG files.");
      return;
    }
    setFiles(selected);
    setFileError(null);
  }

  async function recoverPersistedUpload(selected: File): Promise<DocumentSummary | null> {
    const expectedHash = await contentHash(selected);
    const result = await api.listDocuments(scope);
    if (!result.ok) {
      if (result.status === 401 || result.status === 403) {
        onUnauthorized(result.detail?.correlation_id ?? null);
      }
      return null;
    }
    setDocuments(result.value);
    return (
      result.value.find(
        (document) =>
          document.source.original_filename === selected.name &&
          document.latest_version.content_hash === expectedHash,
      ) ?? null
    );
  }

  async function upload(event: SyntheticEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (files.length === 0) {
      setFileError("Choose one or more permitted supporting documents before uploading.");
      return;
    }
    setUploading(true);
    setFailure(null);
    setUploadSummary(null);
    setDeletionSummary(null);
    try {
      for (const selected of files) {
        try {
          const result = await api.uploadDocument(scope, selected);
          if (!result.ok) {
            if (result.status === 401 || result.status === 403) {
              onUnauthorized(result.detail?.correlation_id ?? null);
            } else {
              setFailure(failureFromResult(result));
            }
            return;
          }
          setDocuments((current) => [
            result.value.document,
            ...(current ?? []).filter(
              (item) => item.source.document_id !== result.value.document.source.document_id,
            ),
          ]);
        } catch {
          const recovered = await recoverPersistedUpload(selected);
          if (recovered === null) {
            setFailure({
              message: "The supporting document could not be uploaded.",
              correlationId: null,
            });
            return;
          }
        }
      }
      setUploadSummary("Document uploaded successfully");
      setFiles([]);
      if (inputRef.current !== null) {
        inputRef.current.value = "";
      }
    } catch {
      setFailure({
        message: "The supporting document could not be uploaded.",
        correlationId: null,
      });
    } finally {
      setUploading(false);
    }
  }

  async function deleteDocument(documentId: string): Promise<void> {
    if (!canDelete) {
      return;
    }
    setDeletingDocumentId(documentId);
    setFailure(null);
    setDeletionSummary(null);
    setUploadSummary(null);
    try {
      const result = await api.deleteDocument(scope, documentId);
      if (!result.ok) {
        if (result.status === 401 || result.status === 403) {
          onUnauthorized(result.detail?.correlation_id ?? null);
        } else {
          setFailure(failureFromResult(result));
        }
        return;
      }
      setDocuments(
        (current) => current?.filter((item) => item.source.document_id !== documentId) ?? [],
      );
      setDeleteCandidate(null);
      setDeletionSummary("Document deleted successfully");
    } catch {
      setFailure({
        message: "The supporting document could not be deleted.",
        correlationId: null,
      });
    } finally {
      setDeletingDocumentId(null);
    }
  }

  const deleteCandidateDocument =
    documents?.find((document) => document.source.document_id === deleteCandidate) ?? null;

  return (
    <section aria-labelledby="supporting-documents-title" className="grid min-w-0 gap-5">
      <div className="flex flex-wrap items-start justify-between gap-3 rounded-control border border-border bg-surface-subtle p-4 sm:p-5">
        <h4 id="supporting-documents-title" className="text-base font-semibold text-foreground">
          Supporting evidence
        </h4>
        <Button
          variant="secondary"
          aria-expanded={expanded}
          aria-controls="supporting-evidence-content"
          onClick={() => setExpanded((current) => !current)}
        >
          {expanded ? "Hide supporting evidence" : "View supporting evidence"}
        </Button>
      </div>

      {expanded ? (
        <div id="supporting-evidence-content" className="grid min-w-0 gap-5">
          <form
            className="grid min-w-0 gap-4 rounded-control border border-border bg-surface p-4 sm:p-5"
            onSubmit={(event) => void upload(event)}
          >
            <div className="grid min-w-0 gap-2">
              <label
                htmlFor="stakeholder-document-upload"
                className="text-sm font-semibold text-foreground"
              >
                Choose supporting documents
              </label>
              <input
                ref={inputRef}
                id="stakeholder-document-upload"
                type="file"
                multiple
                accept={acceptedFormats}
                aria-invalid={fileError !== null || undefined}
                aria-describedby={
                  fileError === null
                    ? "stakeholder-document-upload-hint"
                    : "stakeholder-document-upload-hint stakeholder-document-upload-error"
                }
                className="w-full min-w-0 rounded-control border border-border-strong bg-surface px-3 py-2 text-sm text-foreground file:mr-3 file:rounded-control file:border file:border-border-strong file:bg-surface file:px-3 file:py-2 file:font-semibold file:text-foreground file:hover:bg-surface-subtle focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
                onChange={(event) => chooseFiles(Array.from(event.target.files ?? []))}
              />
              <p id="stakeholder-document-upload-hint" className="text-sm text-muted-foreground">
                Accepted formats: PDF, DOCX, PPTX, XLSX, PNG, and JPEG.
              </p>
              {fileError === null ? null : (
                <p
                  id="stakeholder-document-upload-error"
                  className="text-sm font-semibold text-danger"
                  role="alert"
                >
                  {fileError}
                </p>
              )}
              <div
                id="stakeholder-document-upload-status"
                className="text-sm font-semibold text-success-foreground"
                role="status"
                aria-live="polite"
                aria-atomic="true"
              >
                {uploadSummary}
              </div>
            </div>
            <div className="flex justify-end">
              <Button type="submit" disabled={uploading}>
                {uploading
                  ? "Uploading…"
                  : files.length > 1
                    ? `Upload ${String(files.length)} documents`
                    : "Upload document"}
              </Button>
            </div>
          </form>

          {failure === null ? null : <SafeFailureNotice failure={failure} />}
          {deletionSummary === null ? null : (
            <p
              className="text-sm font-semibold text-success-foreground"
              role="status"
              aria-live="polite"
            >
              {deletionSummary}
            </p>
          )}

          {documents === null && failure === null ? (
            <LoadingIndicator label="Loading supporting evidence…" />
          ) : documents === null || documents.length === 0 ? (
            <EmptyState>No supporting evidence has been added.</EmptyState>
          ) : (
            <ul className="grid gap-3" aria-label="Supporting evidence inventory">
              {documents.map((document) => (
                <li
                  key={document.source.document_id}
                  className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-start gap-3 rounded-control border border-border p-4"
                >
                  <div className="min-w-0">
                    <div className="flex min-w-0 flex-wrap items-center gap-2">
                      <p className="font-semibold break-all text-foreground">
                        {document.source.original_filename}
                      </p>
                      <StatusBadge
                        label={ingestionLabel(document.latest_version.state)}
                        tone={ingestionTone(document.latest_version.state)}
                      />
                    </div>
                    <p className="mt-1 text-sm text-muted-foreground">
                      {document.source.doc_type.toUpperCase()} ·{" "}
                      <time dateTime={document.source.created_at}>
                        {formatDateTime(document.source.created_at)}
                      </time>
                    </p>
                    {document.latest_version.failure_message === null ? null : (
                      <p className="mt-2 text-sm text-danger-foreground">
                        {document.latest_version.failure_message}
                      </p>
                    )}
                  </div>
                  {canDelete ? (
                    <Button
                      className="justify-self-end"
                      size="small"
                      variant="quiet"
                      disabled={deletingDocumentId !== null}
                      onClick={() => setDeleteCandidate(document.source.document_id)}
                    >
                      Delete document
                    </Button>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
      {deleteCandidateDocument === null ? null : (
        <DeleteConfirmationModal
          title="Delete this document from the interview?"
          description={deleteCandidateDocument.source.original_filename}
          confirmLabel="Delete document"
          busyLabel="Deleting document..."
          busy={deletingDocumentId !== null}
          onCancel={() => setDeleteCandidate(null)}
          onConfirm={() => void deleteDocument(deleteCandidateDocument.source.document_id)}
        />
      )}
    </section>
  );
}
