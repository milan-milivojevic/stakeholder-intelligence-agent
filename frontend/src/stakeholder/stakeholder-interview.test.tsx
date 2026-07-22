import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { InterviewStreamEvent } from "../api/contracts";
import {
  draftStatus,
  finishResponse,
  interviewContext,
  readyStatus,
  stakeholderDocument,
  stakeholderScope,
  stakeholderUpload,
} from "../test/stakeholder-fixtures";
import type { StakeholderApi } from "./stakeholder-api";
import { StakeholderInterview } from "./stakeholder-interview";

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

function statusEvent(messageId: string, status: "started" | "succeeded"): InterviewStreamEvent {
  return {
    event: "status",
    data: {
      stage: "interview",
      status,
      message_id: messageId,
      correlation_id: "correlation-alpha",
    },
  };
}

function messageEvent(messageId: string): InterviewStreamEvent {
  return {
    event: "message",
    data: {
      message_id: messageId,
      stakeholder_turn_index: 1,
      assistant_turn_index: 2,
      assistant_text: "Who approves an exception to that weekly review?",
      correlation_id: "correlation-alpha",
    },
  };
}

function tokenEvent(messageId: string, sequence: number, delta: string): InterviewStreamEvent {
  return {
    event: "token",
    data: {
      message_id: messageId,
      sequence,
      delta,
      correlation_id: "correlation-alpha",
    },
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("StakeholderInterview", () => {
  it("starts with one saved, client-friendly assistant question", async () => {
    const user = userEvent.setup();
    const emptyStatus = {
      ...draftStatus,
      interview_session: {
        ...draftStatus.interview_session,
        transcript_id: null,
      },
      transcript: null,
      turns: [],
      turn_count: 0,
      completion_recommended: false,
    };
    const startInterview = vi.fn(() => Promise.resolve({ ok: true as const, value: draftStatus }));
    render(
      <StakeholderInterview
        api={fakeApi({ startInterview })}
        scope={stakeholderScope}
        initialStatus={emptyStatus}
        onUnauthorized={vi.fn()}
      />,
    );

    expect(screen.getByRole("heading", { name: "Stakeholder interview" })).toBeVisible();
    const initialGuidance = screen.getByText(
      "Share what you know about your responsibilities, decisions, and day-to-day work.",
    );
    expect(initialGuidance).toHaveClass("break-words", "min-w-0");
    expect(initialGuidance).not.toHaveClass("whitespace-nowrap", "overflow-x-auto");
    expect(
      screen.getByText(
        "Share what you know about your responsibilities, decisions, and day-to-day work.",
      ),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Start interview" })).toBeVisible();
    expect(screen.queryByLabelText("Your answer")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "View supporting evidence" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(
        "What are the main tasks you personally perform in your day-to-day work as Operations lead?",
      ),
    ).not.toBeInTheDocument();
    expect(startInterview).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Start interview" }));
    expect(
      await screen.findByText(
        "What are the main tasks you personally perform in your day-to-day work as Operations lead?",
      ),
    ).toBeVisible();
    expect(screen.getByLabelText("Your answer")).toBeVisible();
    expect(screen.getByRole("button", { name: "View supporting evidence" })).toBeVisible();
    const activeGuidance = screen.getByText(
      "Answer each question in your own words. Your answers are auto saved, and the interview assistant replies in English.",
    );
    expect(activeGuidance).toHaveClass("break-words", "min-w-0");
    expect(activeGuidance).not.toHaveClass("whitespace-nowrap", "overflow-x-auto");
    expect(screen.getByRole("log", { name: "Current interview turns" })).not.toHaveClass(
      "bg-surface-subtle",
    );
    expect(screen.getByText("Interview assistant").parentElement).toHaveClass("bg-surface");
    expect(screen.queryByText("IA")).not.toBeInTheDocument();
    expect(screen.queryByText(/this engagement/iu)).not.toBeInTheDocument();
    expect(startInterview).toHaveBeenCalledOnce();
  });

  it("continues from the server checkpoint and renders a safe streamed turn", async () => {
    const user = userEvent.setup();
    const exactResponse = "  I own the weekly review and record exceptions.  ";
    const getInterviewStatus = vi.fn(() =>
      Promise.resolve({
        ok: true as const,
        value: {
          ...draftStatus,
          turns: [
            ...draftStatus.turns,
            { turn_index: 1, speaker: "stakeholder" as const, text: exactResponse },
            {
              turn_index: 2,
              speaker: "assistant" as const,
              text: "Who approves an exception to that weekly review?",
            },
          ],
          turn_count: 3,
        },
      }),
    );
    const streamInterviewTurn = vi.fn(async function* (
      _scope: typeof stakeholderScope,
      _originalText: string,
      messageId: string,
    ) {
      await Promise.resolve();
      yield statusEvent(messageId, "started");
      yield messageEvent(messageId);
      yield statusEvent(messageId, "succeeded");
    });
    const storageWrite = vi.spyOn(Storage.prototype, "setItem");
    const { container } = render(
      <StakeholderInterview
        api={fakeApi({ getInterviewStatus, streamInterviewTurn })}
        scope={stakeholderScope}
        initialStatus={draftStatus}
        onUnauthorized={vi.fn()}
      />,
    );

    expect(screen.getByText("Conversation restored")).toBeVisible();
    expect(screen.getByText("Conversation restored").parentElement).toHaveClass(
      "top-4",
      "left-1/2",
      "-translate-x-1/2",
    );
    expect(screen.getByText("Conversation restored").parentElement).not.toHaveClass("bottom-4");
    expect(
      screen.queryByText(/Your saved questions and answers were restored/u),
    ).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("Your answer"), exactResponse);
    await user.click(screen.getByRole("button", { name: "Send answer" }));

    expect(
      await screen.findByText("Who approves an exception to that weekly review?"),
    ).toBeVisible();
    await waitFor(() => expect(getInterviewStatus).toHaveBeenCalledOnce());
    expect(streamInterviewTurn).toHaveBeenCalledOnce();
    expect(streamInterviewTurn.mock.calls[0]?.[0]).toEqual(stakeholderScope);
    expect(streamInterviewTurn.mock.calls[0]?.[1]).toBe(exactResponse);
    expect(streamInterviewTurn.mock.calls[0]?.[2]).toMatch(/^message-/u);
    expect(screen.getAllByRole("article")).toHaveLength(3);
    expect(storageWrite).not.toHaveBeenCalled();
    expect(
      (
        await axe.run(container, {
          rules: { "color-contrast": { enabled: false } },
        })
      ).violations,
    ).toEqual([]);
  });

  it("hides the answer composer until the interview assistant returns the next question", async () => {
    const user = userEvent.setup();
    let releaseStream = (): void => undefined;
    const streamHold = new Promise<void>((resolve) => {
      releaseStream = resolve;
    });
    const streamInterviewTurn = vi.fn(async function* (
      _scope: typeof stakeholderScope,
      _originalText: string,
      messageId: string,
    ) {
      yield statusEvent(messageId, "started");
      yield tokenEvent(messageId, 1, "Who approves an exception");
      await streamHold;
      yield messageEvent(messageId);
      yield statusEvent(messageId, "succeeded");
    });
    render(
      <StakeholderInterview
        api={fakeApi({
          streamInterviewTurn,
          getInterviewStatus: () =>
            Promise.resolve({
              ok: true,
              value: {
                ...draftStatus,
                turns: [
                  ...draftStatus.turns,
                  { turn_index: 1, speaker: "stakeholder", text: "Operations owns it." },
                  {
                    turn_index: 2,
                    speaker: "assistant",
                    text: "Who approves an exception to that weekly review?",
                  },
                ],
                turn_count: 3,
              },
            }),
        })}
        scope={stakeholderScope}
        initialStatus={draftStatus}
        onUnauthorized={vi.fn()}
      />,
    );

    await user.type(screen.getByLabelText("Your answer"), "Operations owns it.");
    await user.click(screen.getByRole("button", { name: "Send answer" }));

    await waitFor(() => expect(screen.queryByLabelText("Your answer")).not.toBeInTheDocument());
    expect(screen.getByText("Who approves an exception")).toBeVisible();
    expect(
      screen.getByText("The interview assistant is preparing the next question…"),
    ).toBeVisible();

    releaseStream();
    expect(
      await screen.findByText("Who approves an exception to that weekly review?"),
    ).toBeVisible();
    expect(await screen.findByLabelText("Your answer")).toBeVisible();
  });

  it("retries an interrupted turn with the same message identity and no duplicate transcript card", async () => {
    const user = userEvent.setup();
    const messageIds: string[] = [];
    let attempt = 0;
    const streamInterviewTurn = vi.fn(async function* (
      _scope: typeof stakeholderScope,
      _originalText: string,
      messageId: string,
    ) {
      await Promise.resolve();
      messageIds.push(messageId);
      attempt += 1;
      if (attempt === 1) {
        yield {
          event: "failure",
          data: {
            stage: "interview",
            status: "failed",
            failure_code: "INTERVIEW_EXECUTION_FAILED",
            failure_message: "The interview response could not be completed.",
            correlation_id: "correlation-retry",
          },
        } satisfies InterviewStreamEvent;
        return;
      }
      yield statusEvent(messageId, "started");
      yield messageEvent(messageId);
      yield statusEvent(messageId, "succeeded");
    });
    render(
      <StakeholderInterview
        api={fakeApi({
          streamInterviewTurn,
          getInterviewStatus: () =>
            Promise.resolve({
              ok: true,
              value: {
                ...draftStatus,
                turns: [
                  ...draftStatus.turns,
                  {
                    turn_index: 1,
                    speaker: "stakeholder",
                    text: "The owner is Operations.",
                  },
                  {
                    turn_index: 2,
                    speaker: "assistant",
                    text: "Who approves an exception to that weekly review?",
                  },
                ],
                turn_count: 3,
              },
            }),
        })}
        scope={stakeholderScope}
        initialStatus={draftStatus}
        onUnauthorized={vi.fn()}
      />,
    );

    await user.type(screen.getByLabelText("Your answer"), "The owner is Operations.");
    await user.click(screen.getByRole("button", { name: "Send answer" }));
    expect(await screen.findByText("The interview response could not be completed.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry interrupted answer" }));

    expect(
      await screen.findByText("Who approves an exception to that weekly review?"),
    ).toBeVisible();
    expect(messageIds).toHaveLength(2);
    expect(messageIds[0]).toBe(messageIds[1]);
    expect(screen.getAllByText("The owner is Operations.")).toHaveLength(1);
    expect(screen.getAllByText("Who approves an exception to that weekly review?")).toHaveLength(1);
  });

  it("shows only the assistant-gated Finish action and permanently closes the interview", async () => {
    const user = userEvent.setup();
    const recommendedStatus = {
      ...draftStatus,
      completion_recommended: true,
    };
    const finishInterview = vi.fn(() =>
      Promise.resolve({ ok: true as const, value: finishResponse }),
    );
    render(
      <StakeholderInterview
        api={fakeApi({
          finishInterview,
          getInterviewStatus: () => Promise.resolve({ ok: true, value: readyStatus }),
        })}
        scope={stakeholderScope}
        initialStatus={recommendedStatus}
        onUnauthorized={vi.fn()}
      />,
    );
    const finishButton = screen.getByRole("button", { name: "Finish interview" });
    expect(finishButton).toBeEnabled();
    expect(screen.getByRole("button", { name: "Add more information" })).toBeEnabled();
    expect(screen.queryByLabelText("Your answer")).not.toBeInTheDocument();
    expect(
      screen.queryByText(/I have finished my answers and understand/u),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/The assistant recommends finishing/u)).not.toBeInTheDocument();
    await user.click(finishButton);

    expect(await screen.findByText("Interview finished")).toBeVisible();
    expect(
      screen.getByText(
        "Your answers and any supporting documents are now locked and available for permitted project analysis.",
      ),
    ).toBeVisible();
    expect(screen.getAllByText("Interview finished")).toHaveLength(1);
    expect(screen.queryByLabelText("Your answer")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Finish interview" })).not.toBeInTheDocument();
    expect(finishInterview).toHaveBeenCalledOnce();
  });

  it("hides Finish until the assistant recommends completion and still allows more answers", async () => {
    const recommendation =
      "Thank you. I have enough information to complete this interview. You can finish now, or continue if you would like to add something else.";
    const recommendedStatus = {
      ...draftStatus,
      turns: [
        ...draftStatus.turns,
        { turn_index: 1, speaker: "stakeholder" as const, text: "Nothing else to add." },
        { turn_index: 2, speaker: "assistant" as const, text: recommendation },
      ],
      turn_count: 3,
      completion_recommended: true,
    };
    const user = userEvent.setup();
    const finishInterview = vi.fn(() =>
      Promise.resolve({ ok: true as const, value: finishResponse }),
    );
    const onUnauthorized = vi.fn();
    const { unmount } = render(
      <StakeholderInterview
        api={fakeApi({ finishInterview })}
        scope={stakeholderScope}
        initialStatus={draftStatus}
        onUnauthorized={onUnauthorized}
      />,
    );

    expect(screen.queryByRole("button", { name: "Finish interview" })).not.toBeInTheDocument();

    unmount();
    render(
      <StakeholderInterview
        api={fakeApi({ finishInterview })}
        scope={stakeholderScope}
        initialStatus={recommendedStatus}
        onUnauthorized={onUnauthorized}
      />,
    );

    expect(screen.queryByLabelText("Your answer")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Finish interview" })).toHaveClass("bg-brand");
    await user.click(screen.getByRole("button", { name: "Add more information" }));

    expect(screen.getByLabelText("Your answer")).toHaveFocus();
    expect(screen.getByRole("button", { name: "Finish interview" })).toHaveClass("bg-surface");
    expect(screen.getByRole("button", { name: "Send answer" })).toHaveClass("bg-brand");

    await user.type(screen.getByLabelText("Your answer"), "I want to add one more detail.");
    expect(finishInterview).not.toHaveBeenCalled();
  });

  it("deletes a selected answer and every later interview turn", async () => {
    const user = userEvent.setup();
    const statusWithTwoAnswers = {
      ...draftStatus,
      turns: [
        ...draftStatus.turns,
        { turn_index: 1, speaker: "stakeholder" as const, text: "First answer." },
        { turn_index: 2, speaker: "assistant" as const, text: "First follow-up?" },
        { turn_index: 3, speaker: "stakeholder" as const, text: "Second answer." },
        { turn_index: 4, speaker: "assistant" as const, text: "Second follow-up?" },
      ],
      turn_count: 5,
    };
    const truncatedStatus = {
      ...draftStatus,
      turns: draftStatus.turns,
      turn_count: 1,
      completion_recommended: false,
    };
    const deleteAnswer = vi.fn(() =>
      Promise.resolve({ ok: true as const, value: truncatedStatus }),
    );
    render(
      <StakeholderInterview
        api={fakeApi({ deleteAnswer })}
        scope={stakeholderScope}
        initialStatus={statusWithTwoAnswers}
        onUnauthorized={vi.fn()}
      />,
    );

    const firstDeleteButton = screen.getAllByRole("button", { name: "Delete answer" }).at(0);
    if (firstDeleteButton === undefined) {
      throw new Error("Expected the first draft answer delete action.");
    }
    expect(firstDeleteButton).toHaveClass("interview-message-action");
    expect(firstDeleteButton.closest("article")).toHaveClass("interview-message");
    await user.click(firstDeleteButton);
    const dialog = screen.getByRole("dialog", {
      name: "Delete this answer and every later question and answer?",
    });
    expect(dialog).toBeVisible();
    expect(dialog).toHaveClass("border-border", "shadow-panel");
    expect(dialog).not.toHaveClass("border-danger-border");
    expect(dialog.parentElement).toHaveClass("fixed", "inset-0", "items-center", "justify-center");
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus();
    await user.click(within(dialog).getByRole("button", { name: "Delete answer" }));

    await waitFor(() => expect(deleteAnswer).toHaveBeenCalledWith(stakeholderScope, 1));
    expect(screen.queryByText("First answer.")).not.toBeInTheDocument();
    expect(screen.queryByText("First follow-up?")).not.toBeInTheDocument();
    expect(screen.queryByText("Second answer.")).not.toBeInTheDocument();
    expect(screen.queryByText("Second follow-up?")).not.toBeInTheDocument();
    expect(screen.getByText("Answer deleted.")).toBeVisible();
    expect(screen.getByLabelText("Your answer")).toBeVisible();
  });

  it("does not expose answer deletion after the interview is finalized", () => {
    render(
      <StakeholderInterview
        api={fakeApi()}
        scope={stakeholderScope}
        initialStatus={readyStatus}
        onUnauthorized={vi.fn()}
      />,
    );

    expect(screen.getByText("I coordinate the weekly operations review.")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Delete answer" })).not.toBeInTheDocument();
  });

  it("removes the workspace through the unauthorized callback on an expired active session", async () => {
    const user = userEvent.setup();
    const onUnauthorized = vi.fn();
    const getInterviewStatus = vi.fn(() =>
      Promise.resolve({
        ok: false as const,
        status: 403,
        detail: {
          code: "ACCESS_SESSION_EXPIRED",
          message: "Session expired.",
          correlation_id: "correlation-expired",
        },
      }),
    );
    render(
      <StakeholderInterview
        api={fakeApi({ getInterviewStatus })}
        scope={stakeholderScope}
        initialStatus={draftStatus}
        onUnauthorized={onUnauthorized}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledWith("correlation-expired"));
    expect(screen.queryByText("Session expired.")).not.toBeInTheDocument();
  });
});
