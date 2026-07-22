import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { describe, expect, it, vi } from "vitest";

import {
  draftStatus,
  finishResponse,
  interviewContext,
  stakeholderDocument,
  stakeholderScope,
  stakeholderUpload,
} from "../test/stakeholder-fixtures";
import type { StakeholderApi } from "./stakeholder-api";
import { StakeholderDocuments } from "./stakeholder-documents";

async function* emptyStream(): AsyncGenerator<never> {
  await Promise.resolve();
  yield* [];
}

function fakeApi(overrides: Partial<StakeholderApi> = {}): StakeholderApi {
  return {
    getContext: () =>
      Promise.resolve({
        ok: true,
        value: { context: interviewContext, scope: stakeholderScope },
      }),
    listDocuments: () => Promise.resolve({ ok: true, value: [stakeholderDocument] }),
    uploadDocument: () => Promise.resolve({ ok: true, value: stakeholderUpload }),
    deleteDocument: () => Promise.resolve({ ok: true, value: { status: "ok" } }),
    startInterview: () => Promise.resolve({ ok: true, value: draftStatus }),
    getInterviewStatus: () => Promise.resolve({ ok: true, value: draftStatus }),
    deleteAnswer: () => Promise.resolve({ ok: true, value: draftStatus }),
    streamInterviewTurn: emptyStream,
    finishInterview: () => Promise.resolve({ ok: true, value: finishResponse }),
    ...overrides,
  };
}

describe("StakeholderDocuments", () => {
  it("renders an accessible scoped inventory", async () => {
    const user = userEvent.setup();
    const listDocuments = vi.fn(() =>
      Promise.resolve({ ok: true as const, value: [stakeholderDocument] }),
    );
    const { container } = render(
      <StakeholderDocuments
        api={fakeApi({ listDocuments })}
        scope={stakeholderScope}
        canDelete
        onUnauthorized={vi.fn()}
      />,
    );

    expect(
      screen.queryByText(stakeholderDocument.source.original_filename),
    ).not.toBeInTheDocument();
    expect(listDocuments).not.toHaveBeenCalled();
    expect(screen.getByRole("heading", { name: "Supporting evidence" })).toBeVisible();
    expect(screen.queryByText("(optional)")).not.toBeInTheDocument();
    expect(
      screen.queryByText(
        "If a document helps explain one of your answers, you can add it now or later.",
      ),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "View supporting evidence" }));
    expect(await screen.findByText(stakeholderDocument.source.original_filename)).toBeVisible();
    expect(listDocuments).toHaveBeenCalledOnce();
    expect(screen.getByText("Uploaded")).toBeVisible();
    expect(
      screen.getByText(stakeholderDocument.source.original_filename).parentElement,
    ).toHaveTextContent("Uploaded");
    expect(screen.getByRole("button", { name: "Delete document" })).toHaveClass("justify-self-end");
    expect(screen.getByLabelText("Choose supporting documents")).toHaveAttribute(
      "accept",
      ".pdf,.docx,.pptx,.xlsx,.png,.jpg,.jpeg",
    );
    expect(screen.getByLabelText("Choose supporting documents")).toHaveAttribute("multiple");
    expect(
      (
        await axe.run(container, {
          rules: { "color-contrast": { enabled: false } },
        })
      ).violations,
    ).toEqual([]);
  });

  it("uploads PDF, DOCX, PPTX, XLSX, PNG, and JPEG through the stakeholder boundary", async () => {
    const user = userEvent.setup();
    const formats = [
      ["support.pdf", "application/pdf", "pdf"],
      [
        "support.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "docx",
      ],
      [
        "support.pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "pptx",
      ],
      ["support.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"],
      ["support.png", "image/png", "png"],
      ["support.jpeg", "image/jpeg", "jpeg"],
    ] as const;
    const uploadDocument = vi.fn((scope: typeof stakeholderScope, file: File) => {
      const format = formats.find(([name]) => name === file.name);
      expect(scope).toEqual(stakeholderScope);
      expect(format).toBeDefined();
      const document = structuredClone(stakeholderDocument);
      document.source.document_id = `document-${file.name}`;
      document.source.original_filename = file.name;
      document.source.doc_type = format?.[2] ?? "pdf";
      document.source.media_type = file.type;
      document.latest_version.document_id = document.source.document_id;
      return Promise.resolve({
        ok: true as const,
        value: { ...stakeholderUpload, document },
      });
    });
    render(
      <StakeholderDocuments
        api={fakeApi({
          listDocuments: () => Promise.resolve({ ok: true, value: [] }),
          uploadDocument,
        })}
        scope={stakeholderScope}
        canDelete
        onUnauthorized={vi.fn()}
      />,
    );
    await user.click(screen.getByRole("button", { name: "View supporting evidence" }));
    const input = screen.getByLabelText("Choose supporting documents");
    const files = formats.map(
      ([name, mediaType]) => new File([`bytes-${name}`], name, { type: mediaType }),
    );
    await user.upload(input, files);
    expect(input).toHaveProperty("files.length", formats.length);
    await user.click(screen.getByRole("button", { name: "Upload 6 documents" }));
    await waitFor(() => expect(uploadDocument).toHaveBeenCalledTimes(formats.length));

    expect(uploadDocument.mock.calls.map(([, file]) => file.name)).toEqual(
      formats.map(([name]) => name),
    );
    expect(screen.getByText("support.jpeg")).toBeVisible();
  });

  it("reconciles a persisted upload when the successful 201 response cannot be consumed", async () => {
    const user = userEvent.setup();
    const file = new File(["persisted bytes"], "persisted.pdf", { type: "application/pdf" });
    const digest = await globalThis.crypto.subtle.digest("SHA-256", await file.arrayBuffer());
    const expectedHash = Array.from(new Uint8Array(digest), (byte) =>
      byte.toString(16).padStart(2, "0"),
    ).join("");
    const persisted = structuredClone(stakeholderDocument);
    persisted.source.document_id = "document-persisted";
    persisted.source.original_filename = file.name;
    persisted.latest_version.document_id = persisted.source.document_id;
    persisted.latest_version.content_hash = expectedHash;
    const listDocuments = vi
      .fn()
      .mockResolvedValueOnce({ ok: true as const, value: [] })
      .mockResolvedValueOnce({ ok: true as const, value: [persisted] });

    render(
      <StakeholderDocuments
        api={fakeApi({
          listDocuments,
          uploadDocument: () => Promise.reject(new Error("Invalid 201 response contract")),
        })}
        scope={stakeholderScope}
        canDelete
        onUnauthorized={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "View supporting evidence" }));
    await waitFor(() => expect(listDocuments).toHaveBeenCalledOnce());
    await user.upload(screen.getByLabelText("Choose supporting documents"), file);
    await user.click(screen.getByRole("button", { name: "Upload document" }));

    expect(await screen.findByText("persisted.pdf")).toBeVisible();
    expect(screen.getByText("Document uploaded successfully")).toBeVisible();
    expect(screen.queryByText("Evidence added")).not.toBeInTheDocument();
    expect(screen.queryByText("Request not completed")).not.toBeInTheDocument();
    expect(listDocuments).toHaveBeenCalledTimes(2);
  });

  it("deletes one selected document only while the interview is still mutable", async () => {
    const user = userEvent.setup();
    const deleteDocument = vi.fn(() =>
      Promise.resolve({ ok: true as const, value: { status: "ok" as const } }),
    );
    const { unmount } = render(
      <StakeholderDocuments
        api={fakeApi({ deleteDocument })}
        scope={stakeholderScope}
        canDelete
        onUnauthorized={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "View supporting evidence" }));
    expect(await screen.findByText(stakeholderDocument.source.original_filename)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Delete document" }));
    const dialog = screen.getByRole("dialog", {
      name: "Delete this document from the interview?",
    });
    expect(dialog).toBeVisible();
    expect(dialog).toHaveClass("border-border", "shadow-panel");
    expect(dialog).not.toHaveClass("border-danger-border");
    expect(dialog.parentElement).toHaveClass("fixed", "inset-0", "items-center", "justify-center");
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus();
    await user.click(within(dialog).getByRole("button", { name: "Delete document" }));

    await waitFor(() =>
      expect(deleteDocument).toHaveBeenCalledWith(
        stakeholderScope,
        stakeholderDocument.source.document_id,
      ),
    );
    expect(
      screen.queryByText(stakeholderDocument.source.original_filename),
    ).not.toBeInTheDocument();
    expect(screen.getByText("Document deleted successfully")).toBeVisible();

    unmount();
    render(
      <StakeholderDocuments
        api={fakeApi({ deleteDocument })}
        scope={stakeholderScope}
        canDelete={false}
        onUnauthorized={vi.fn()}
      />,
    );
    await user.click(screen.getByRole("button", { name: "View supporting evidence" }));
    expect(await screen.findByText(stakeholderDocument.source.original_filename)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Delete document" })).not.toBeInTheDocument();
  });

  it("rejects unsupported local extensions and forwards authorization denial safely", async () => {
    const user = userEvent.setup({ applyAccept: false });
    const onUnauthorized = vi.fn();
    const uploadDocument = vi.fn(() =>
      Promise.resolve({
        ok: false as const,
        status: 403,
        detail: {
          code: "ACCESS_DENIED",
          message: "Access denied.",
          correlation_id: "correlation-denied",
        },
      }),
    );
    render(
      <StakeholderDocuments
        api={fakeApi({ uploadDocument })}
        scope={stakeholderScope}
        canDelete
        onUnauthorized={onUnauthorized}
      />,
    );
    await user.click(screen.getByRole("button", { name: "View supporting evidence" }));
    const input = screen.getByLabelText("Choose supporting documents");
    await user.upload(input, new File(["text"], "notes.txt", { type: "text/plain" }));
    expect(await screen.findByText(/Choose only PDF/u)).toBeVisible();
    expect(uploadDocument).not.toHaveBeenCalled();

    await user.upload(input, new File(["pdf"], "support.pdf", { type: "application/pdf" }));
    await user.click(screen.getByRole("button", { name: "Upload document" }));
    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledWith("correlation-denied"));
    expect(screen.queryByText("Access denied.")).not.toBeInTheDocument();
  });

  it("requires a permitted local file before starting an upload", async () => {
    const user = userEvent.setup();
    const uploadDocument = vi.fn(() =>
      Promise.resolve({ ok: true as const, value: stakeholderUpload }),
    );
    render(
      <StakeholderDocuments
        api={fakeApi({ uploadDocument })}
        scope={stakeholderScope}
        canDelete
        onUnauthorized={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "View supporting evidence" }));
    await user.click(screen.getByRole("button", { name: "Upload document" }));

    expect(
      await screen.findByText(
        "Choose one or more permitted supporting documents before uploading.",
      ),
    ).toBeVisible();
    expect(uploadDocument).not.toHaveBeenCalled();
  });
});
