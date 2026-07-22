import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import type { DocumentSummary } from "../api/contracts";
import { Button } from "../components/button";
import { InfoNotice } from "../components/notice";
import type { DocumentElementPreview, DocumentProcessingDetailsResponse } from "./contracts";
import { EmptyState } from "./common";
import { describeSourceLocation } from "./source-location";

function sourceContext(document: DocumentSummary): string {
  if (document.source.source_type === "engagement_document") {
    return "Engagement-level source";
  }
  return [document.source.role, document.source.department].filter(Boolean).join(" · ");
}

export function DocumentPreview({
  document,
  sourcePath,
  onClose,
}: {
  document: DocumentSummary;
  sourcePath: string;
  onClose: () => void;
}) {
  const isPdf = document.source.media_type === "application/pdf";
  const isImage = document.source.media_type.startsWith("image/");

  return (
    <section
      className="grid gap-4 rounded-panel border border-info-border bg-info-surface p-4 sm:p-5"
      aria-labelledby="document-preview-title"
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <p className="text-xs font-semibold tracking-wide text-info-foreground uppercase">
            Authorized original source
          </p>
          <h5
            id="document-preview-title"
            className="mt-1 text-lg font-semibold break-all text-foreground"
          >
            {document.source.original_filename}
          </h5>
          <p className="mt-1 text-sm text-muted-foreground">{sourceContext(document)}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <a
            className="inline-flex min-h-9 items-center justify-center rounded-control border border-border-strong bg-surface px-3 py-1.5 text-sm font-semibold text-foreground transition-colors hover:border-brand hover:text-brand focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
            href={sourcePath}
            download={document.source.original_filename}
          >
            Download
          </a>
          <Button size="small" variant="quiet" onClick={onClose}>
            Close preview
          </Button>
        </div>
      </div>

      {isPdf ? (
        <iframe
          className="h-[42rem] w-full rounded-control border border-border bg-surface"
          src={sourcePath}
          title={`Preview of ${document.source.original_filename}`}
        />
      ) : isImage ? (
        <div className="flex max-h-[42rem] justify-center overflow-auto rounded-control border border-border bg-surface p-3">
          <img
            className="h-auto max-w-full object-contain"
            src={sourcePath}
            alt={`Original source ${document.source.original_filename}`}
          />
        </div>
      ) : (
        <InfoNotice title="Browser preview unavailable">
          Download the original file to review this format in its native application.
        </InfoNotice>
      )}
    </section>
  );
}

const markdownComponents: Components = {
  h1: ({ children }) => (
    <p className="mt-5 text-lg font-semibold tracking-tight text-foreground first:mt-0">
      {children}
    </p>
  ),
  h2: ({ children }) => (
    <p className="mt-5 text-base font-semibold text-foreground first:mt-0">{children}</p>
  ),
  h3: ({ children }) => <p className="mt-4 font-semibold text-foreground first:mt-0">{children}</p>,
  h4: ({ children }) => <p className="mt-4 font-semibold text-foreground first:mt-0">{children}</p>,
  h5: ({ children }) => <p className="mt-4 font-semibold text-foreground first:mt-0">{children}</p>,
  h6: ({ children }) => <p className="mt-4 font-semibold text-foreground first:mt-0">{children}</p>,
  p: ({ children }) => <p className="mt-3 leading-7 text-foreground first:mt-0">{children}</p>,
  ul: ({ children }) => (
    <ul className="mt-3 list-disc space-y-1.5 pl-6 text-foreground">{children}</ul>
  ),
  ol: ({ children }) => (
    <ol className="mt-3 list-decimal space-y-1.5 pl-6 text-foreground">{children}</ol>
  ),
  li: ({ children }) => <li className="pl-1 leading-7">{children}</li>,
  strong: ({ children }) => <strong className="font-semibold text-foreground">{children}</strong>,
  em: ({ children }) => <em className="text-foreground">{children}</em>,
  blockquote: ({ children }) => (
    <blockquote className="mt-4 border-l-4 border-info-border pl-4 text-muted-foreground">
      {children}
    </blockquote>
  ),
  table: ({ children }) => (
    <div className="mt-4 overflow-x-auto rounded-control border border-border">
      <table className="w-full border-collapse text-left text-sm">{children}</table>
    </div>
  ),
  thead: ({ children }) => <thead className="bg-surface-subtle">{children}</thead>,
  th: ({ children }) => (
    <th className="border-b border-border px-3 py-2 font-semibold text-foreground">{children}</th>
  ),
  td: ({ children }) => (
    <td className="border-t border-border px-3 py-2 align-top text-foreground">{children}</td>
  ),
  code: ({ children }) => (
    <code className="rounded bg-surface-subtle px-1.5 py-0.5 font-mono text-sm text-foreground">
      {children}
    </code>
  ),
  pre: ({ children }) => (
    <pre className="mt-4 overflow-x-auto rounded-control bg-surface-subtle p-4 text-sm">
      {children}
    </pre>
  ),
  a: ({ children }) => <span className="font-medium text-foreground">{children}</span>,
  img: () => null,
};

function DocumentMarkdown({ children }: { children: string }) {
  return (
    <div className="text-sm">
      <Markdown remarkPlugins={[remarkGfm]} skipHtml components={markdownComponents}>
        {children}
      </Markdown>
    </div>
  );
}

function elementContent(element: DocumentElementPreview): string | null {
  const content = element.content_preview?.trim();
  return content === undefined || content.length === 0 ? null : content;
}

function visualDescriptions(details: DocumentProcessingDetailsResponse): DocumentElementPreview[] {
  return details.element_previews.filter(
    (element) => element.element_type === "vision_description" && elementContent(element) !== null,
  );
}

function representativeElements(
  details: DocumentProcessingDetailsResponse,
): DocumentElementPreview[] {
  const documentType = details.document.source.doc_type;
  const tables = details.element_previews.filter(
    (element) => element.element_type === "table" && elementContent(element) !== null,
  );
  const narrative = details.element_previews.filter(
    (element) =>
      (element.element_type === "text" || element.element_type === "ocr_text") &&
      elementContent(element) !== null,
  );
  const minimumLength = documentType === "pdf" ? 40 : documentType === "pptx" ? 1 : 20;
  const meaningfulNarrative = narrative.filter(
    (element) => (elementContent(element)?.length ?? 0) >= minimumLength,
  );
  const selectedNarrative =
    meaningfulNarrative.length === 0 ? narrative.slice(0, 4) : meaningfulNarrative.slice(0, 4);

  if (documentType === "pdf" && (tables.length > 0 || visualDescriptions(details).length > 0)) {
    return tables.slice(0, 4);
  }
  if (documentType === "xlsx") {
    return [...tables.slice(0, 5), ...selectedNarrative.slice(0, 2)];
  }
  return [...selectedNarrative, ...tables.slice(0, 3)];
}

function understandingSummary(details: DocumentProcessingDetailsResponse): string {
  switch (details.document.source.doc_type) {
    case "pdf":
      return "The system extracted readable passages and structured content from the PDF and interpreted its visuals where available.";
    case "docx":
      return "The system identified the document's written sections, structured tables, and visual content where available.";
    case "pptx":
      return "The system interpreted representative slide text, structured content, and presentation visuals.";
    case "xlsx":
      return "The system identified representative spreadsheet tables, labeled ranges, and visual content where available.";
    default:
      return "The system identified representative source content that can support later evidence-grounded research.";
  }
}

function contentLabel(element: DocumentElementPreview): string {
  if (element.element_type === "table") {
    return "Structured content";
  }
  if (element.element_type === "ocr_text") {
    return "Recognized text";
  }
  return "Source excerpt";
}

function UnderstandingItem({ element }: { element: DocumentElementPreview }) {
  const content = elementContent(element);
  if (content === null) {
    return null;
  }
  return (
    <li className="rounded-control border border-border bg-surface p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <p className="text-sm font-semibold text-foreground">{contentLabel(element)}</p>
        <p className="text-xs text-muted-foreground">{describeSourceLocation(element.location)}</p>
      </div>
      <div className="mt-3">
        <DocumentMarkdown>{content}</DocumentMarkdown>
      </div>
      {element.english_interpretation === null ? null : (
        <div className="mt-4 border-t border-border pt-3">
          <p className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
            English interpretation
          </p>
          <p className="mt-1 text-sm leading-7 text-foreground">{element.english_interpretation}</p>
        </div>
      )}
    </li>
  );
}

export function DocumentProcessingDetails({
  details,
  onClose,
}: {
  details: DocumentProcessingDetailsResponse;
  onClose: () => void;
}) {
  const document = details.document;
  const isImage = document.source.media_type.startsWith("image/");
  const visions = visualDescriptions(details);
  const primaryVision = visions.at(0);
  const representative = representativeElements(details);

  return (
    <section
      className="grid gap-6 rounded-panel border border-border bg-surface p-4 shadow-panel sm:p-5"
      aria-labelledby="processing-details-title"
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <p className="text-xs font-semibold tracking-wide text-brand uppercase">
            {isImage ? "Vision description" : "What the system understood"}
          </p>
          <h5
            id="processing-details-title"
            className="mt-1 text-lg font-semibold break-all text-foreground"
          >
            {document.source.original_filename}
          </h5>
          <p className="mt-1 text-sm text-muted-foreground">{sourceContext(document)}</p>
        </div>
        <Button size="small" variant="quiet" onClick={onClose}>
          Close analysis
        </Button>
      </div>

      {isImage ? (
        <>
          <p className="max-w-prose text-sm leading-6 text-muted-foreground">
            AI-generated description of the visual. Verify important details against the original
            source using the separate preview action.
          </p>
          {primaryVision === undefined ? (
            <EmptyState>
              A vision description is not available for this document version.
            </EmptyState>
          ) : (
            <div className="rounded-control border border-info-border bg-info-surface p-4 sm:p-5">
              <DocumentMarkdown>{elementContent(primaryVision) ?? ""}</DocumentMarkdown>
            </div>
          )}
        </>
      ) : (
        <>
          <div className="rounded-control border border-info-border bg-info-surface p-4">
            <p className="font-semibold text-foreground">{understandingSummary(details)}</p>
            <p className="mt-2 text-sm leading-6 text-muted-foreground">
              This is a concise, source-grounded reading aid—not the final stakeholder insight
              report.
            </p>
          </div>

          {representative.length === 0 ? null : (
            <section className="grid gap-3" aria-labelledby="representative-content-title">
              <h6 id="representative-content-title" className="font-semibold text-foreground">
                Representative extracted content
              </h6>
              <ol className="grid gap-3">
                {representative.map((element) => (
                  <UnderstandingItem key={element.element_id} element={element} />
                ))}
              </ol>
            </section>
          )}

          {visions.length === 0 ? null : (
            <section className="grid gap-3" aria-labelledby="visual-understanding-title">
              <h6 id="visual-understanding-title" className="font-semibold text-foreground">
                Visual understanding
              </h6>
              <ol className="grid gap-3">
                {visions.slice(0, 4).map((element) => (
                  <li
                    key={element.element_id}
                    className="rounded-control border border-info-border bg-info-surface p-4 sm:p-5"
                  >
                    <p className="mb-3 text-xs font-semibold tracking-wide text-info-foreground uppercase">
                      {describeSourceLocation(element.location)}
                    </p>
                    <DocumentMarkdown>{elementContent(element) ?? ""}</DocumentMarkdown>
                  </li>
                ))}
              </ol>
            </section>
          )}

          {representative.length === 0 && visions.length === 0 ? (
            <EmptyState>No presentation-ready extracted content is available.</EmptyState>
          ) : null}
        </>
      )}
    </section>
  );
}
