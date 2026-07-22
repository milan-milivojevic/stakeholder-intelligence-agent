import axe from "axe-core";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  documentProcessingDetails,
  documentSummary,
  engagement,
  fakePmApi,
  uploadResponse,
  stakeholder,
} from "../test/pm-fixtures";
import { DocumentProcessingDetails } from "./document-review";
import { PmDocuments } from "./pm-documents";

describe("PmDocuments", () => {
  it("renders the permitted document inventory and immutable ingestion state", async () => {
    render(<PmDocuments api={fakePmApi()} engagement={engagement} />);

    expect(await screen.findByText(documentSummary.source.original_filename)).toBeVisible();
    expect(screen.getByText("READY")).toBeVisible();
    expect(screen.getByText("pdf", { exact: true })).toBeVisible();
    expect(
      screen.getByText("Accepted formats: PDF, DOCX, PPTX, XLSX, PNG, and JPEG."),
    ).toBeVisible();
    expect(screen.getByRole("heading", { name: "Engagement documents", level: 4 })).toHaveClass(
      "text-xl",
    );
    expect(screen.getByRole("heading", { name: "Document library", level: 5 })).toHaveClass(
      "text-lg",
    );
    expect(
      screen.getByRole("heading", { name: "Project manager documents", level: 6 }),
    ).toHaveClass("text-base");
    expect(screen.getByRole("heading", { name: "Stakeholder documents", level: 6 })).toHaveClass(
      "text-base",
    );
    expect(screen.getByText("Manage and review documents for this engagement.")).toBeVisible();
    expect(screen.queryByText("Add documents for this engagement.")).not.toBeInTheDocument();
    expect(screen.queryByText(/prepare it for secure use/iu)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Preview" })).toHaveClass("w-24");
    expect(screen.getByText("pdf", { exact: true }).closest("td")).toHaveClass("text-left");
    expect(screen.getByText("READY").closest("td")).toHaveClass("text-left");
    expect(screen.getByRole("button", { name: "View analysis" })).toBeVisible();
  });

  it("uses concise equal-width preview and download actions", async () => {
    const downloadable = structuredClone(documentSummary);
    downloadable.source.document_id = "document-downloadable";
    downloadable.source.original_filename = "operating-model.docx";
    downloadable.source.doc_type = "docx";
    downloadable.source.media_type =
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
    downloadable.latest_version.document_id = downloadable.source.document_id;
    render(
      <PmDocuments
        api={fakePmApi({
          listDocuments: () =>
            Promise.resolve({ ok: true, value: [documentSummary, downloadable] }),
        })}
        engagement={engagement}
      />,
    );

    expect(await screen.findByRole("button", { name: "Preview" })).toHaveClass("w-24");
    expect(screen.getByRole("link", { name: "Download" })).toHaveClass("w-24");
    expect(screen.queryByText("Preview source")).not.toBeInTheDocument();
    expect(screen.queryByText("Download original")).not.toBeInTheDocument();
  });

  it("refreshes the engagement inventory after a stakeholder upload", async () => {
    const user = userEvent.setup();
    const stakeholderEvidence = structuredClone(documentSummary);
    stakeholderEvidence.source.document_id = "document-stakeholder-evidence";
    stakeholderEvidence.source.original_filename = "stakeholder-evidence.pdf";
    stakeholderEvidence.source.source_type = "stakeholder_document";
    stakeholderEvidence.source.stakeholder_id = "stakeholder-alpha";
    stakeholderEvidence.source.role = "Operations manager";
    stakeholderEvidence.source.department = "Operations";
    stakeholderEvidence.latest_version.document_id = stakeholderEvidence.source.document_id;
    const listDocuments = vi
      .fn()
      .mockResolvedValueOnce({ ok: true as const, value: [documentSummary] })
      .mockResolvedValueOnce({
        ok: true as const,
        value: [stakeholderEvidence, documentSummary],
      });
    render(<PmDocuments api={fakePmApi({ listDocuments })} engagement={engagement} />);

    expect(await screen.findByText(documentSummary.source.original_filename)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Refresh documents" }));

    expect(await screen.findByText("stakeholder-evidence.pdf")).toBeVisible();
    expect(screen.getByText("Stakeholder documents")).toBeVisible();
    expect(screen.getByText(stakeholder.display_name)).toBeVisible();
    expect(
      screen.getByRole("article", { name: `Documents from ${stakeholder.display_name}` }),
    ).toBeVisible();
    expect(
      screen.queryByRole("heading", { name: stakeholder.display_name }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("Documents you added for this engagement.")).toBeVisible();
    expect(
      screen.getByText("Documents shared by stakeholders during their interviews."),
    ).toBeVisible();
    expect(screen.queryByText(/You can delete them here/iu)).not.toBeInTheDocument();
    expect(
      screen.queryByText(/cannot be deleted from the project manager workspace/iu),
    ).not.toBeInTheDocument();
    expect(screen.getAllByRole("table")).toHaveLength(2);
    for (const table of screen.getAllByRole("table")) {
      expect(table).toHaveClass("table-fixed", "min-w-[64rem]");
    }
    for (const header of screen.getAllByRole("columnheader", { name: "Actions" })) {
      expect(header).toHaveClass("w-[19rem]", "text-left");
      expect(header).not.toHaveClass("text-right");
    }
    for (const name of ["Format", "Version", "Ingestion", "Created"]) {
      for (const header of screen.getAllByRole("columnheader", { name })) {
        expect(header).toHaveClass("text-left");
        expect(header).not.toHaveClass("text-right");
      }
    }
    expect(
      screen.queryByRole("button", { name: "Delete stakeholder-evidence.pdf" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: `Delete ${documentSummary.source.original_filename}` }),
    ).toBeVisible();
    await waitFor(() => expect(listDocuments).toHaveBeenCalledTimes(2));
  });

  it("deletes only a project manager document after confirmation", async () => {
    const user = userEvent.setup();
    const deleteDocument = vi.fn(() =>
      Promise.resolve({ ok: true as const, value: { status: "ok" as const } }),
    );
    render(<PmDocuments api={fakePmApi({ deleteDocument })} engagement={engagement} />);

    await user.click(
      await screen.findByRole("button", {
        name: `Delete ${documentSummary.source.original_filename}`,
      }),
    );
    const dialog = screen.getByRole("dialog", {
      name: "Delete this project manager document?",
    });
    expect(dialog).toBeVisible();
    expect(dialog).toHaveClass("border-border", "shadow-panel");
    expect(dialog).not.toHaveClass("border-danger-border");
    expect(dialog.parentElement).toHaveClass("fixed", "inset-0", "items-center", "justify-center");
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
    expect(
      screen.getByRole("row", { name: new RegExp(documentSummary.source.original_filename, "u") })
        .textContent,
    ).not.toContain("Delete this project manager document?");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await user.click(
      screen.getByRole("button", {
        name: `Delete ${documentSummary.source.original_filename}`,
      }),
    );
    await user.click(screen.getByRole("button", { name: /^Delete$/u }));

    await waitFor(() =>
      expect(deleteDocument).toHaveBeenCalledWith(
        engagement.engagement_id,
        documentSummary.source.document_id,
      ),
    );
    expect(screen.queryByText(documentSummary.source.original_filename)).not.toBeInTheDocument();
    expect(
      screen.getByText(`${documentSummary.source.original_filename} was deleted.`),
    ).toBeVisible();
  });

  it("previews the authorized original and renders concise PDF understanding", async () => {
    const user = userEvent.setup();
    const getDocumentProcessing = vi.fn(() =>
      Promise.resolve({ ok: true as const, value: documentProcessingDetails }),
    );
    const { container } = render(
      <PmDocuments api={fakePmApi({ getDocumentProcessing })} engagement={engagement} />,
    );

    await user.click(await screen.findByRole("button", { name: "Preview" }));
    expect(
      screen.getByTitle(`Preview of ${documentSummary.source.original_filename}`),
    ).toHaveAttribute(
      "src",
      "/api/v1/pm/engagements/engagement-alpha/documents/document-alpha/artifacts/artifact-original",
    );
    expect(screen.getByRole("link", { name: "Download" })).toHaveAttribute(
      "download",
      documentSummary.source.original_filename,
    );
    await user.click(screen.getByRole("button", { name: "Close preview" }));

    await user.click(screen.getByRole("button", { name: "View analysis" }));
    expect(getDocumentProcessing).toHaveBeenCalledWith(
      engagement.engagement_id,
      documentSummary.source.document_id,
    );
    expect(await screen.findByText("What the system understood")).toBeVisible();
    expect(screen.getByText("Representative extracted content")).toBeVisible();
    expect(screen.getByText("Visual understanding")).toBeVisible();
    expect(screen.getByText("Weekly service review")).toBeVisible();
    expect(screen.getByText("Visual type:")).toBeVisible();
    expect(screen.getByText(/Product has high influence/iu)).toBeVisible();
    expect(screen.queryByText("Retrieval chunks")).not.toBeInTheDocument();
    expect(screen.queryByText("Processing lifecycle")).not.toBeInTheDocument();
    expect(screen.queryByText("Authorized source artifacts")).not.toBeInTheDocument();
    expect(screen.queryByText(/dense_vector/iu)).not.toBeInTheDocument();
    expect(
      (
        await axe.run(container, {
          rules: { region: { enabled: false } },
        })
      ).violations,
    ).toEqual([]);
  });

  it("renders only the formatted vision description for a standalone image analysis", async () => {
    const user = userEvent.setup();
    const imageDocument = structuredClone(documentSummary);
    imageDocument.source.original_filename = "organization-chart.png";
    imageDocument.source.doc_type = "png";
    imageDocument.source.media_type = "image/png";
    const imageDetails = structuredClone(documentProcessingDetails);
    imageDetails.document = imageDocument;
    imageDetails.element_previews = [
      {
        element_id: "element-ocr",
        document_version_id: imageDocument.latest_version.document_version_id,
        element_type: "ocr_text",
        location: {
          kind: "image_region",
          filename: imageDocument.source.original_filename,
          image_index: 1,
          region: "whole_image",
          bounding_box: null,
        },
        extraction_method: "rapidocr",
        content_preview: "OCR should not be shown in the image analysis.",
        english_interpretation: null,
      },
      {
        element_id: "element-image-vision",
        document_version_id: imageDocument.latest_version.document_version_id,
        element_type: "vision_description",
        location: {
          kind: "image_region",
          filename: imageDocument.source.original_filename,
          image_index: 1,
          region: "whole_image",
          bounding_box: null,
        },
        extraction_method: "gemini_vision",
        content_preview:
          "**Structure and relationships:**\n\n- A Steering Committee leads the chart.\n- Product Lead reports into the committee.",
        english_interpretation: null,
      },
    ];
    render(
      <PmDocuments
        api={fakePmApi({
          listDocuments: () => Promise.resolve({ ok: true, value: [imageDocument] }),
          getDocumentProcessing: () => Promise.resolve({ ok: true, value: imageDetails }),
        })}
        engagement={engagement}
      />,
    );

    await user.click(await screen.findByRole("button", { name: "View analysis" }));

    expect(await screen.findByText("Vision description")).toBeVisible();
    expect(screen.getByText("Structure and relationships:")).toBeVisible();
    expect(screen.getByText(/Steering Committee leads the chart/iu)).toBeVisible();
    expect(screen.queryByText(/OCR should not be shown/iu)).not.toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(
      screen.queryByTitle(`Preview of ${imageDocument.source.original_filename}`),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("Representative extracted content")).not.toBeInTheDocument();
  });

  it.each([
    [
      "docx",
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      /written sections/iu,
    ],
    [
      "pptx",
      "application/vnd.openxmlformats-officedocument.presentationml.presentation",
      /representative slide text/iu,
    ],
    [
      "xlsx",
      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      /representative spreadsheet tables/iu,
    ],
  ] as const)("uses a format-aware understanding summary for %s", (docType, mediaType, summary) => {
    const details = structuredClone(documentProcessingDetails);
    details.document.source.doc_type = docType;
    details.document.source.media_type = mediaType;
    details.document.source.original_filename = `source.${docType}`;

    render(<DocumentProcessingDetails details={details} onClose={() => undefined} />);

    expect(screen.getByText(summary)).toBeVisible();
    expect(screen.queryByText("Retrieval chunks")).not.toBeInTheDocument();
  });

  it.each([
    ["source.pdf", "application/pdf"],
    ["source.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
    ["source.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"],
    ["source.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"],
    ["source.png", "image/png"],
    ["source.jpeg", "image/jpeg"],
  ])("submits the permitted %s format to the shared upload boundary", async (name, type) => {
    const user = userEvent.setup();
    const uploadDocument = vi.fn(() =>
      Promise.resolve({ ok: true as const, value: uploadResponse }),
    );
    render(
      <PmDocuments
        api={fakePmApi({
          listDocuments: () => Promise.resolve({ ok: true, value: [] }),
          uploadDocument,
        })}
        engagement={engagement}
      />,
    );
    const input = await screen.findByLabelText("Source document");
    const file = new File(["format-specific test bytes"], name, { type });

    await user.upload(input, file);
    await user.click(screen.getByRole("button", { name: "Upload document" }));

    expect(uploadDocument).toHaveBeenCalledWith(engagement.engagement_id, file);
    expect(
      await screen.findByText(/upload completed with 4 elements and 3 retrieval chunks/iu),
    ).toBeVisible();
  });

  it("rejects an unsupported extension locally and still relies on the server for content checks", async () => {
    const user = userEvent.setup({ applyAccept: false });
    const uploadDocument = vi.fn();
    render(
      <PmDocuments
        api={fakePmApi({
          listDocuments: () => Promise.resolve({ ok: true, value: [] }),
          uploadDocument,
        })}
        engagement={engagement}
      />,
    );
    const input = await screen.findByLabelText("Source document");

    await user.upload(input, new File(["unsupported"], "source.txt", { type: "text/plain" }));
    expect(screen.getByText("Choose a PDF, DOCX, PPTX, XLSX, PNG, or JPEG file.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Upload document" }));
    expect(screen.getByText("Choose a permitted file before uploading.")).toBeVisible();
    expect(uploadDocument).not.toHaveBeenCalled();
  });

  it("shows idempotent reuse and safe server rejection states", async () => {
    const user = userEvent.setup();
    const uploadDocument = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        value: { ...uploadResponse, idempotent: true },
      })
      .mockResolvedValueOnce({
        ok: false,
        status: 415,
        detail: {
          code: "UNSUPPORTED_DOCUMENT",
          message: "The uploaded document is not supported.",
          correlation_id: "correlation-upload",
        },
      });
    render(
      <PmDocuments
        api={fakePmApi({
          listDocuments: () => Promise.resolve({ ok: true, value: [] }),
          uploadDocument,
        })}
        engagement={engagement}
      />,
    );
    const input = await screen.findByLabelText("Source document");

    await user.upload(input, new File(["first"], "source.pdf", { type: "application/pdf" }));
    await user.click(screen.getByRole("button", { name: "Upload document" }));
    expect(await screen.findByText("The existing immutable upload was reused.")).toBeVisible();

    await user.upload(input, new File(["second"], "source.pdf", { type: "application/pdf" }));
    await user.click(screen.getByRole("button", { name: "Upload document" }));
    expect(await screen.findByText("The uploaded document is not supported.")).toBeVisible();
  });
});
