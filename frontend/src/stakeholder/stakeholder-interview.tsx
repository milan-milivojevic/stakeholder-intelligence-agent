import { useCallback, useEffect, useRef, useState } from "react";
import type { SyntheticEvent } from "react";

import { apiRequestFailure } from "../api/client";
import type { ApiResult } from "../api/client";
import { Button } from "../components/button";
import { DeleteConfirmationModal } from "../components/delete-confirmation-modal";
import { ErrorNotice, SuccessNotice, WarningNotice } from "../components/notice";
import { SafeFailureNotice } from "../pm/common";
import { failureFromResult } from "../pm/safe-ui";
import type { SafeUiFailure } from "../pm/safe-ui";
import type { InterviewStatusResponse, StakeholderScope } from "./contracts";
import type { StakeholderApi } from "./stakeholder-api";
import { StakeholderDocuments } from "./stakeholder-documents";

interface LocalMessage {
  key: string;
  speaker: "stakeholder" | "assistant";
  text: string;
  turnIndex: number | null;
}

function messagesFromStatus(status: InterviewStatusResponse): LocalMessage[] {
  return status.turns.map((turn) => ({
    key: `stored-turn-${String(turn.turn_index)}`,
    speaker: turn.speaker,
    text: turn.text,
    turnIndex: turn.turn_index,
  }));
}

interface PendingTurn {
  messageId: string;
  originalText: string;
}

function newMessageId(): string {
  return `message-${globalThis.crypto.randomUUID()}`;
}

interface StakeholderInterviewProps {
  api: StakeholderApi;
  scope: StakeholderScope;
  initialStatus: InterviewStatusResponse;
  onUnauthorized: (correlationId: string | null) => void;
}

export function StakeholderInterview({
  api,
  scope,
  initialStatus,
  onUnauthorized,
}: StakeholderInterviewProps) {
  const [status, setStatus] = useState(initialStatus);
  const [messages, setMessages] = useState<LocalMessage[]>(() => messagesFromStatus(initialStatus));
  const [responseText, setResponseText] = useState("");
  const [pendingTurn, setPendingTurn] = useState<PendingTurn | null>(null);
  const [starting, setStarting] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [streamedAssistantText, setStreamedAssistantText] = useState("");
  const [streamLabel, setStreamLabel] = useState<string | null>(null);
  const [failure, setFailure] = useState<SafeUiFailure | null>(null);
  const [showRestoredToast, setShowRestoredToast] = useState(
    initialStatus.turns.length > 0 && initialStatus.interview_session.status === "draft",
  );
  const [addingMoreInformation, setAddingMoreInformation] = useState(false);
  const [deleteCandidate, setDeleteCandidate] = useState<number | null>(null);
  const [deletingTurnIndex, setDeletingTurnIndex] = useState<number | null>(null);
  const [finishing, setFinishing] = useState(false);
  const streamController = useRef<AbortController | null>(null);
  const responseInput = useRef<HTMLTextAreaElement | null>(null);

  useEffect(
    () => () => {
      streamController.current?.abort();
    },
    [],
  );

  useEffect(() => {
    if (!showRestoredToast) {
      return;
    }
    const timeout = globalThis.setTimeout(() => setShowRestoredToast(false), 4_000);
    return () => globalThis.clearTimeout(timeout);
  }, [showRestoredToast]);

  useEffect(() => {
    if (addingMoreInformation) {
      responseInput.current?.focus();
    }
  }, [addingMoreInformation]);

  const applyStatus = useCallback((nextStatus: InterviewStatusResponse): void => {
    setStatus(nextStatus);
    setMessages(messagesFromStatus(nextStatus));
  }, []);

  const handleResultFailure = useCallback(
    (result: Extract<ApiResult<unknown>, { ok: false }>): void => {
      if (result.status === 401 || result.status === 403) {
        onUnauthorized(result.detail?.correlation_id ?? null);
        return;
      }
      setFailure(failureFromResult(result));
    },
    [onUnauthorized],
  );

  const handleThrownFailure = useCallback(
    (error: unknown, fallback: string): void => {
      const requestFailure = apiRequestFailure(error);
      if (
        requestFailure !== null &&
        (requestFailure.status === 401 || requestFailure.status === 403)
      ) {
        onUnauthorized(requestFailure.detail?.correlation_id ?? null);
        return;
      }
      setFailure({
        message: requestFailure?.detail?.message ?? fallback,
        correlationId: requestFailure?.detail?.correlation_id ?? null,
      });
    },
    [onUnauthorized],
  );

  const startInterview = useCallback(async (): Promise<void> => {
    setStarting(true);
    setFailure(null);
    setStreamedAssistantText("");
    try {
      const result = await api.startInterview(scope);
      if (result.ok) {
        applyStatus(result.value);
      } else {
        handleResultFailure(result);
      }
    } catch (error) {
      handleThrownFailure(error, "The first interview question could not be prepared.");
    } finally {
      setStarting(false);
    }
  }, [api, applyStatus, handleResultFailure, handleThrownFailure, scope]);

  async function refreshStatus(): Promise<void> {
    try {
      const result = await api.getInterviewStatus(scope);
      if (result.ok) {
        applyStatus(result.value);
        setFailure(null);
      } else {
        handleResultFailure(result);
      }
    } catch (error) {
      handleThrownFailure(error, "The saved interview could not be refreshed.");
    }
  }

  function addAssistantMessage(turn: PendingTurn, text: string, turnIndex: number): void {
    setMessages((current) => {
      const key = `${turn.messageId}-assistant-${String(turnIndex)}`;
      if (current.some((message) => message.key === key)) {
        return current;
      }
      return [...current, { key, speaker: "assistant", text, turnIndex }];
    });
  }

  async function executeTurn(turn: PendingTurn): Promise<void> {
    const controller = new AbortController();
    streamController.current = controller;
    setStreaming(true);
    setFailure(null);
    setStreamedAssistantText("");
    setStreamLabel("Connecting to the interview stream…");
    let receivedMessage = false;
    let succeeded = false;
    let safeFailure = false;
    try {
      for await (const event of api.streamInterviewTurn(
        scope,
        turn.originalText,
        turn.messageId,
        controller.signal,
      )) {
        if (event.event === "status") {
          setStreamLabel(
            event.data.status === "started"
              ? "The interview assistant is preparing the next question…"
              : "Answer saved.",
          );
          succeeded ||= event.data.status === "succeeded";
        } else if (event.event === "token") {
          setStreamedAssistantText((current) => current + event.data.delta);
        } else if (event.event === "message") {
          receivedMessage = true;
          addAssistantMessage(turn, event.data.assistant_text, event.data.assistant_turn_index);
          setStreamedAssistantText("");
        } else {
          safeFailure = true;
          setFailure({
            message: event.data.failure_message,
            correlationId: event.data.correlation_id,
          });
          setStreamLabel("The interview answer was not completed.");
        }
      }
      if (succeeded && receivedMessage) {
        setPendingTurn(null);
        setResponseText("");
        setAddingMoreInformation(false);
        setStreamLabel("Answer saved.");
        await refreshStatus();
      } else if (!safeFailure) {
        setFailure({
          message: "The interview stream ended before a complete answer was confirmed.",
          correlationId: null,
        });
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        handleThrownFailure(error, "The interview stream was interrupted.");
      }
    } finally {
      if (streamController.current === controller) {
        streamController.current = null;
      }
      setStreaming(false);
      setStreamedAssistantText("");
    }
  }

  async function submitTurn(event: SyntheticEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (responseText.trim().length === 0 || streaming) {
      setFailure({ message: "Enter an answer before continuing.", correlationId: null });
      return;
    }
    const turn = { messageId: newMessageId(), originalText: responseText };
    setPendingTurn(turn);
    setMessages((current) => [
      ...current,
      {
        key: `${turn.messageId}-stakeholder`,
        speaker: "stakeholder",
        text: turn.originalText,
        turnIndex: null,
      },
    ]);
    await executeTurn(turn);
  }

  async function finishInterview(): Promise<void> {
    if (
      !status.completion_recommended ||
      status.interview_session.status !== "draft" ||
      streaming
    ) {
      return;
    }
    setFinishing(true);
    setFailure(null);
    try {
      const result = await api.finishInterview(scope);
      if (!result.ok) {
        handleResultFailure(result);
        return;
      }
      setStatus({
        interview_session: result.value.interview_session,
        transcript: result.value.transcript,
        ingestion_version: result.value.ingestion_version,
        turns: status.turns,
        turn_count: status.turn_count,
        completion_recommended: status.completion_recommended,
      });
      setPendingTurn(null);
    } catch (error) {
      handleThrownFailure(error, "The interview could not be finalized.");
    } finally {
      setFinishing(false);
    }
  }

  async function deleteAnswer(turnIndex: number): Promise<void> {
    if (status.interview_session.status !== "draft" || streaming || finishing) {
      return;
    }
    setDeletingTurnIndex(turnIndex);
    setFailure(null);
    try {
      const result = await api.deleteAnswer(scope, turnIndex);
      if (!result.ok) {
        handleResultFailure(result);
        return;
      }
      applyStatus(result.value);
      setPendingTurn(null);
      setResponseText("");
      setAddingMoreInformation(false);
      setDeleteCandidate(null);
      setStreamLabel("Answer deleted.");
    } catch (error) {
      handleThrownFailure(error, "The answer could not be deleted.");
    } finally {
      setDeletingTurnIndex(null);
    }
  }

  const interviewState = status.interview_session.status;
  const canContinue = interviewState === "draft";
  const completionRecommended = status.completion_recommended;
  const interviewStarted = messages.length > 0;
  const showComposer =
    canContinue &&
    interviewStarted &&
    !streaming &&
    (!completionRecommended || addingMoreInformation);
  const showCompletionActions =
    canContinue &&
    interviewStarted &&
    completionRecommended &&
    !addingMoreInformation &&
    !streaming;

  return (
    <section aria-labelledby="stakeholder-interview-title" className="grid min-w-0 gap-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <h4 id="stakeholder-interview-title" className="text-xl font-semibold text-foreground">
            Stakeholder interview
          </h4>
          <p className="mt-1 min-w-0 text-sm leading-6 break-words text-muted-foreground">
            {interviewStarted
              ? "Answer each question in your own words. Your answers are auto saved, and the interview assistant replies in English."
              : "Share what you know about your responsibilities, decisions, and day-to-day work."}
          </p>
        </div>
        {interviewStarted ? (
          <Button
            variant="quiet"
            size="small"
            disabled={starting || streaming || finishing}
            onClick={() => void refreshStatus()}
          >
            Refresh
          </Button>
        ) : null}
      </div>

      {canContinue && !interviewStarted ? (
        <section className="grid gap-4 rounded-control border border-border bg-surface-subtle p-4 sm:p-5">
          <p className="max-w-prose text-sm leading-6 text-foreground">
            You will answer one question at a time. There are no right or wrong answers, and you can
            add an optional supporting document after the interview begins. Your first question will
            appear when you start.
          </p>
          <div>
            <Button disabled={starting} onClick={() => void startInterview()}>
              {starting ? "Preparing your first question…" : "Start interview"}
            </Button>
          </div>
        </section>
      ) : null}

      {showRestoredToast ? (
        <div
          className="fixed top-4 left-1/2 z-50 max-w-[calc(100vw-2rem)] -translate-x-1/2 rounded-control border border-border bg-surface px-4 py-3 text-center shadow-lg"
          role="status"
          aria-live="polite"
        >
          <p className="font-semibold text-foreground">Conversation restored</p>
        </div>
      ) : null}

      {interviewState === "ready" ? (
        <SuccessNotice title="Interview finished">
          Your answers and any supporting documents are now locked and available for permitted
          project analysis.
        </SuccessNotice>
      ) : interviewState === "failed" ? (
        <ErrorNotice title="Finalization or indexing failed">
          {status.interview_session.failure_message ??
            status.ingestion_version?.failure_message ??
            "The finalized interview could not be prepared for retrieval."}
        </ErrorNotice>
      ) : canContinue ? null : (
        <WarningNotice title="Finalization in progress">
          The interview is permanently closed to new answers. Refresh the conversation to see the
          latest transcript processing state.
        </WarningNotice>
      )}

      {interviewStarted ? (
        <div
          className="grid gap-4"
          role="log"
          aria-live="polite"
          aria-label="Current interview turns"
        >
          {messages.map((message) => (
            <article
              key={message.key}
              className={`interview-message group flex flex-col gap-1 ${
                message.speaker === "stakeholder" ? "items-end" : "items-start"
              }`}
            >
              <div
                className={
                  message.speaker === "stakeholder"
                    ? "max-w-[82%] rounded-2xl rounded-br-sm bg-brand px-4 py-3 text-brand-contrast shadow-sm"
                    : "max-w-[82%] rounded-2xl rounded-bl-sm bg-surface px-4 py-3 text-foreground shadow-sm"
                }
              >
                <p className="text-xs font-semibold opacity-75">
                  {message.speaker === "stakeholder" ? "You" : "Interview assistant"}
                </p>
                <p className="mt-1 text-sm leading-6 whitespace-pre-wrap">{message.text}</p>
              </div>
              {canContinue && message.speaker === "stakeholder" && message.turnIndex !== null ? (
                <Button
                  className="interview-message-action"
                  size="small"
                  variant="quiet"
                  disabled={deletingTurnIndex !== null || streaming || finishing}
                  onClick={() => setDeleteCandidate(message.turnIndex)}
                >
                  Delete answer
                </Button>
              ) : null}
            </article>
          ))}
          {streamedAssistantText.length === 0 ? null : (
            <article className="interview-message flex flex-col items-start gap-1">
              <div className="max-w-[82%] rounded-2xl rounded-bl-sm bg-surface px-4 py-3 text-foreground shadow-sm">
                <p className="text-xs font-semibold opacity-75">Interview assistant</p>
                <p className="mt-1 text-sm leading-6 whitespace-pre-wrap">
                  {streamedAssistantText}
                </p>
              </div>
            </article>
          )}
        </div>
      ) : null}

      {showCompletionActions ? (
        <div className="flex flex-wrap justify-end gap-3">
          <Button
            variant="secondary"
            disabled={starting || finishing}
            onClick={() => {
              setStreamLabel(null);
              setAddingMoreInformation(true);
            }}
          >
            Add more information
          </Button>
          <Button disabled={starting || finishing} onClick={() => void finishInterview()}>
            {finishing ? "Finishing interview…" : "Finish interview"}
          </Button>
        </div>
      ) : null}

      {streamLabel === null ? null : (
        <p className="text-sm text-muted-foreground" role="status">
          {streamLabel}
        </p>
      )}

      {showComposer ? (
        <form className="grid gap-3" onSubmit={(event) => void submitTurn(event)}>
          <label htmlFor="stakeholder-response" className="text-sm font-semibold text-foreground">
            Your answer
          </label>
          <textarea
            ref={responseInput}
            id="stakeholder-response"
            rows={5}
            value={responseText}
            disabled={starting || finishing}
            className="w-full min-w-0 resize-y rounded-control border border-border-strong bg-surface px-3 py-2 text-base text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus disabled:opacity-60"
            onChange={(event) => setResponseText(event.target.value)}
          />
          <div className="flex flex-wrap justify-end gap-3">
            {completionRecommended && addingMoreInformation ? (
              <Button
                variant="secondary"
                disabled={starting || finishing}
                onClick={() => void finishInterview()}
              >
                {finishing ? "Finishing interview…" : "Finish interview"}
              </Button>
            ) : null}
            <Button type="submit" disabled={starting || finishing}>
              Send answer
            </Button>
            {pendingTurn === null ? null : (
              <Button variant="secondary" onClick={() => void executeTurn(pendingTurn)}>
                Retry interrupted answer
              </Button>
            )}
          </div>
        </form>
      ) : null}

      {interviewStarted ? (
        <StakeholderDocuments
          api={api}
          scope={scope}
          canDelete={canContinue}
          onUnauthorized={onUnauthorized}
        />
      ) : null}

      {deleteCandidate === null ? null : (
        <DeleteConfirmationModal
          title="Delete this answer and every later question and answer?"
          confirmLabel="Delete answer"
          busyLabel="Deleting answer..."
          busy={deletingTurnIndex !== null}
          onCancel={() => setDeleteCandidate(null)}
          onConfirm={() => void deleteAnswer(deleteCandidate)}
        />
      )}

      {failure === null ? null : <SafeFailureNotice failure={failure} />}
    </section>
  );
}
