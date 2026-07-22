import { useCallback, useEffect, useRef, useState } from "react";
import type { SyntheticEvent } from "react";

import type { Engagement, InsightRun, SafeRunEvent } from "../api/contracts";
import { Button } from "../components/button";
import { FormField, TextArea } from "../components/form-field";
import { LoadingIndicator } from "../components/loading-indicator";
import { ErrorNotice, InfoNotice, WarningNotice } from "../components/notice";
import type { EvidenceDrillDownResponse, InsightReportResponse } from "./contracts";
import { SafeFailureNotice, StatusBadge } from "./common";
import type { PmApi } from "./pm-api";
import { EvidenceDrawer, ExecutionTimeline, ReportView } from "./report-view";
import type { EvidenceSelection } from "./report-view";
import { failureFromResult, formatDateTime } from "./safe-ui";
import type { SafeUiFailure } from "./safe-ui";

const terminalStatuses = new Set(["complete", "partial", "insufficient_evidence", "failed"]);
const reportStatuses = new Set(["complete", "partial", "insufficient_evidence"]);
const monitorReconnectDelayMs = 1_000;

function monitorWasAborted(signal: AbortSignal): boolean {
  return signal.aborted;
}

function waitForMonitorRetry(signal: AbortSignal): Promise<boolean> {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve(false);
      return;
    }
    const timeout = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve(true);
    }, monitorReconnectDelayMs);
    function onAbort(): void {
      window.clearTimeout(timeout);
      resolve(false);
    }
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

function historyResult(status: InsightRun["status"]): string {
  if (status === "complete") return "Report ready";
  if (status === "partial") return "Partial report ready";
  if (status === "insufficient_evidence") return "Limited evidence report";
  if (status === "failed") return "Run failed safely";
  return "Analysis in progress";
}

function runTone(status: InsightRun["status"]) {
  if (status === "complete") {
    return "success" as const;
  }
  if (status === "partial" || status === "insufficient_evidence") {
    return "warning" as const;
  }
  if (status === "failed") {
    return "error" as const;
  }
  return "info" as const;
}

type InsightView = "research" | "history";

export function PmInsights({
  api,
  engagement,
  view = "research",
}: {
  api: PmApi;
  engagement: Engagement;
  view?: InsightView;
}) {
  const [question, setQuestion] = useState("");
  const [questionError, setQuestionError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [history, setHistory] = useState<InsightRun[]>([]);
  const [historyLoading, setHistoryLoading] = useState(view === "history");
  const [historyOpeningId, setHistoryOpeningId] = useState<string | null>(null);
  const [historyFailure, setHistoryFailure] = useState<SafeUiFailure | null>(null);
  const [run, setRun] = useState<InsightRun | null>(null);
  const [events, setEvents] = useState<SafeRunEvent[]>([]);
  const [reportResponse, setReportResponse] = useState<InsightReportResponse | null>(null);
  const [failure, setFailure] = useState<SafeUiFailure | null>(null);
  const [streamFailure, setStreamFailure] = useState<SafeUiFailure | null>(null);
  const [evidence, setEvidence] = useState<EvidenceDrillDownResponse | null>(null);
  const [evidenceSelection, setEvidenceSelection] = useState<EvidenceSelection | null>(null);
  const [evidenceIndex, setEvidenceIndex] = useState(0);
  const [evidenceLoadingId, setEvidenceLoadingId] = useState<string | null>(null);
  const [evidenceFailure, setEvidenceFailure] = useState<SafeUiFailure | null>(null);
  const evidenceRequest = useRef(0);
  const historyRequest = useRef(0);
  const historyReport = useRef<HTMLDivElement | null>(null);
  const streamController = useRef<AbortController | null>(null);

  const loadHistory = useCallback(async (): Promise<void> => {
    setHistoryLoading(true);
    setHistoryFailure(null);
    try {
      const result = await api.listInsights(engagement.engagement_id);
      if (result.ok) {
        setHistory(
          result.value.filter((item) => item.report_id !== null && reportStatuses.has(item.status)),
        );
      } else {
        setHistoryFailure(failureFromResult(result));
      }
    } catch {
      setHistoryFailure({
        message: "Saved insight reports could not be loaded.",
        correlationId: null,
      });
    } finally {
      setHistoryLoading(false);
    }
  }, [api, engagement.engagement_id]);

  useEffect(() => {
    const timeout = view === "history" ? window.setTimeout(() => void loadHistory(), 0) : undefined;
    return () => {
      if (timeout !== undefined) {
        window.clearTimeout(timeout);
      }
      streamController.current?.abort();
    };
  }, [loadHistory, view]);

  useEffect(() => {
    if (view === "history" && reportResponse !== null) {
      historyReport.current?.focus();
    }
  }, [reportResponse, view]);

  async function loadReport(currentRun: InsightRun): Promise<void> {
    try {
      const result = await api.getInsightReport(engagement.engagement_id, currentRun.run_id);
      if (result.ok) {
        setReportResponse(result.value);
        setRun(result.value.run);
      } else {
        setFailure(failureFromResult(result));
      }
    } catch {
      setFailure({ message: "The structured report could not be loaded.", correlationId: null });
    }
  }

  async function refreshStatus(runId: string): Promise<InsightRun | null> {
    try {
      const result = await api.getInsightStatus(engagement.engagement_id, runId);
      if (!result.ok) {
        setFailure(failureFromResult(result));
        return null;
      }
      setRun(result.value);
      if (result.value.status !== "failed" && terminalStatuses.has(result.value.status)) {
        setStreamFailure(null);
        await loadReport(result.value);
      }
      return result.value;
    } catch {
      setFailure({ message: "The insight status could not be refreshed.", correlationId: null });
      return null;
    }
  }

  async function monitor(currentRun: InsightRun, controller: AbortController): Promise<void> {
    while (!monitorWasAborted(controller.signal)) {
      try {
        for await (const event of api.streamInsight(
          engagement.engagement_id,
          currentRun.run_id,
          controller.signal,
        )) {
          if (event.event === "failure") {
            setStreamFailure({
              message: event.data.failure_message,
              correlationId: event.data.correlation_id,
            });
            continue;
          }
          setStreamFailure(null);
          setEvents((current) =>
            current.some((item) => item.event_id === event.data.event_id)
              ? current
              : [...current, event.data],
          );
        }
      } catch {
        if (!monitorWasAborted(controller.signal)) {
          setStreamFailure({
            message: "Live progress became unavailable. Reconnecting to the persisted run.",
            correlationId: null,
          });
        }
      }
      if (monitorWasAborted(controller.signal)) return;
      const latest = await refreshStatus(currentRun.run_id);
      if (latest === null || terminalStatuses.has(latest.status)) return;
      if (!(await waitForMonitorRetry(controller.signal))) return;
    }
  }

  async function submit(event: SyntheticEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const cleanQuestion = question.trim();
    if (cleanQuestion.length === 0) {
      setQuestionError("Enter the research question.");
      return;
    }
    streamController.current?.abort();
    setSubmitting(true);
    setFailure(null);
    setStreamFailure(null);
    setEvents([]);
    setReportResponse(null);
    setEvidence(null);
    setEvidenceSelection(null);
    setEvidenceFailure(null);
    try {
      const result = await api.createInsight(engagement.engagement_id, cleanQuestion);
      if (!result.ok) {
        setFailure(failureFromResult(result));
        return;
      }
      setRun(result.value);
      const controller = new AbortController();
      streamController.current = controller;
      void monitor(result.value, controller);
    } catch {
      setFailure({ message: "The insight run could not be started.", correlationId: null });
    } finally {
      setSubmitting(false);
    }
  }

  async function loadSavedEvents(savedRun: InsightRun, controller: AbortController): Promise<void> {
    try {
      for await (const event of api.streamInsight(
        engagement.engagement_id,
        savedRun.run_id,
        controller.signal,
      )) {
        if (event.event === "progress") {
          setEvents((current) =>
            current.some((item) => item.event_id === event.data.event_id)
              ? current
              : [...current, event.data],
          );
        }
      }
    } catch {
      // The persisted report remains usable even if optional execution details cannot be loaded.
    }
  }

  async function openHistory(savedRun: InsightRun): Promise<void> {
    streamController.current?.abort();
    const requestId = historyRequest.current + 1;
    historyRequest.current = requestId;
    setRun(savedRun);
    setHistoryOpeningId(savedRun.run_id);
    setEvents([]);
    setReportResponse(null);
    setFailure(null);
    setStreamFailure(null);
    setEvidence(null);
    setEvidenceSelection(null);
    setEvidenceFailure(null);
    const controller = new AbortController();
    streamController.current = controller;
    void loadSavedEvents(savedRun, controller);
    try {
      const result = await api.getInsightReport(engagement.engagement_id, savedRun.run_id);
      if (requestId !== historyRequest.current) {
        return;
      }
      if (result.ok) {
        setReportResponse(result.value);
        setRun(result.value.run);
      } else {
        setFailure(failureFromResult(result));
      }
    } catch {
      if (requestId === historyRequest.current) {
        setFailure({ message: "The saved report could not be loaded.", correlationId: null });
      }
    } finally {
      if (requestId === historyRequest.current) {
        setHistoryOpeningId(null);
      }
    }
  }

  function closeHistoryReport(): void {
    historyRequest.current += 1;
    streamController.current?.abort();
    setRun(null);
    setEvents([]);
    setReportResponse(null);
    setFailure(null);
    setStreamFailure(null);
    closeEvidence();
  }

  async function loadEvidence(evidenceId: string, index: number): Promise<void> {
    if (run === null) {
      return;
    }
    const requestId = evidenceRequest.current + 1;
    evidenceRequest.current = requestId;
    setEvidenceIndex(index);
    setEvidenceLoadingId(evidenceId);
    setEvidence(null);
    setEvidenceFailure(null);
    try {
      const result = await api.getEvidence(engagement.engagement_id, run.run_id, evidenceId);
      if (requestId !== evidenceRequest.current) {
        return;
      }
      if (result.ok) {
        setEvidence(result.value);
      } else {
        setEvidenceFailure(failureFromResult(result));
      }
    } catch {
      if (requestId !== evidenceRequest.current) {
        return;
      }
      setEvidenceFailure({
        message: "The authorized evidence source could not be resolved.",
        correlationId: null,
      });
    } finally {
      if (requestId === evidenceRequest.current) {
        setEvidenceLoadingId(null);
      }
    }
  }

  function openEvidence(selection: EvidenceSelection): void {
    const first = selection.items.at(0);
    if (first === undefined) {
      return;
    }
    setEvidenceSelection(selection);
    void loadEvidence(first.evidenceId, 0);
  }

  function selectEvidenceIndex(index: number): void {
    const item = evidenceSelection?.items[index];
    if (item !== undefined) {
      void loadEvidence(item.evidenceId, index);
    }
  }

  function closeEvidence(): void {
    evidenceRequest.current += 1;
    setEvidenceSelection(null);
    setEvidence(null);
    setEvidenceLoadingId(null);
    setEvidenceFailure(null);
  }

  const running = run !== null && !terminalStatuses.has(run.status);

  if (view === "history") {
    return (
      <div className="grid gap-6">
        {run === null ? (
          <>
            <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
              <div>
                <h4 className="text-xl font-semibold tracking-tight text-foreground">
                  Insight history
                </h4>
                <p className="mt-1 text-sm leading-6 text-muted-foreground">
                  Revisit completed reports.
                </p>
              </div>
              <Button
                size="small"
                variant="secondary"
                disabled={historyLoading}
                onClick={() => void loadHistory()}
              >
                Refresh history
              </Button>
            </div>

            {historyFailure === null ? null : <SafeFailureNotice failure={historyFailure} />}
            {historyLoading && history.length === 0 ? (
              <LoadingIndicator label="Loading saved reports…" />
            ) : history.length === 0 ? (
              <InfoNotice title="No ready reports yet">
                Completed, partial, and limited-evidence reports will appear here. Failed and
                in-progress runs are intentionally hidden.
              </InfoNotice>
            ) : (
              <ul className="grid gap-3" aria-label="Ready insight reports">
                {history.map((savedRun) => (
                  <li
                    key={savedRun.run_id}
                    className="grid gap-3 rounded-control border border-border bg-surface-subtle p-4 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center"
                  >
                    <div className="min-w-0">
                      <p className="leading-6 font-medium text-foreground">
                        {savedRun.requested_question}
                      </p>
                      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                        <StatusBadge
                          label={historyResult(savedRun.status)}
                          tone={runTone(savedRun.status)}
                        />
                        <span>
                          Ready{" "}
                          <time dateTime={savedRun.completed_at ?? savedRun.started_at}>
                            {formatDateTime(savedRun.completed_at ?? savedRun.started_at)}
                          </time>
                        </span>
                      </div>
                    </div>
                    <Button
                      size="small"
                      variant="secondary"
                      onClick={() => void openHistory(savedRun)}
                    >
                      Open report
                    </Button>
                  </li>
                ))}
              </ul>
            )}
          </>
        ) : (
          <div ref={historyReport} tabIndex={-1} className="grid gap-5 outline-none">
            <div className="flex flex-col gap-3 border-b border-border pb-4 sm:flex-row sm:items-start sm:justify-between">
              <div>
                <p className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
                  Saved insight report
                </p>
                <h4 className="mt-1 text-xl font-semibold tracking-tight text-foreground">
                  {run.requested_question}
                </h4>
              </div>
              <Button
                className="whitespace-nowrap"
                size="small"
                variant="secondary"
                onClick={closeHistoryReport}
              >
                Back to insight history
              </Button>
            </div>
            {failure === null ? null : <SafeFailureNotice failure={failure} />}
            {historyOpeningId === null ? null : <LoadingIndicator label="Opening saved report…" />}
            {reportResponse === null ? null : (
              <ReportView
                report={reportResponse.report}
                metrics={reportResponse.metrics}
                events={events}
                onOpenEvidence={openEvidence}
              />
            )}
          </div>
        )}

        {evidenceSelection === null || run === null ? null : (
          <EvidenceDrawer
            selection={evidenceSelection}
            selectedIndex={evidenceIndex}
            value={evidence}
            loading={evidenceLoadingId !== null}
            errorMessage={evidenceFailure?.message ?? null}
            api={api}
            engagementId={engagement.engagement_id}
            runId={run.run_id}
            onSelectIndex={selectEvidenceIndex}
            onClose={closeEvidence}
          />
        )}
      </div>
    );
  }

  return (
    <div className="grid gap-7">
      <div>
        <h4 className="text-xl font-semibold tracking-tight text-foreground">
          Insight research and report
        </h4>
        <p className="mt-1 text-sm leading-6 text-muted-foreground">
          Ask a question about the current engagement and follow the research as it progresses.
        </p>
      </div>

      <form
        className="grid gap-4 rounded-control bg-surface-subtle p-4 sm:p-5"
        onSubmit={(event) => void submit(event)}
      >
        <FormField
          label="Research question"
          labelFor="insight-question"
          hint="Ask about responsibilities, risks, stakeholder support, conflicting information, missing details, or next steps."
          error={questionError ?? undefined}
        >
          <TextArea
            id="insight-question"
            value={question}
            maxLength={4000}
            invalid={questionError !== null}
            aria-describedby={
              questionError === null
                ? "insight-question-hint"
                : "insight-question-hint insight-question-error"
            }
            onChange={(event) => {
              setQuestion(event.target.value);
              setQuestionError(null);
            }}
          />
        </FormField>
        <div className="flex justify-end">
          <Button type="submit" disabled={submitting || running}>
            {submitting ? "Starting…" : running ? "Research in progress" : "Run insight research"}
          </Button>
        </div>
      </form>

      {failure === null ? null : <SafeFailureNotice failure={failure} />}
      {streamFailure === null ? null : (
        <WarningNotice title="Live progress interrupted">
          <p>{streamFailure.message}</p>
          {streamFailure.correlationId === null ? null : (
            <p className="mt-2 text-xs">
              Reference: <span className="font-mono">{streamFailure.correlationId}</span>
            </p>
          )}
        </WarningNotice>
      )}

      {run === null ? (
        <InfoNotice title="No insight run yet">
          Enter a question to start the research and follow its progress here.
        </InfoNotice>
      ) : (
        <section
          aria-labelledby="insight-progress-title"
          className="grid gap-4 rounded-control border border-border bg-surface-subtle p-4 sm:p-5"
        >
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <h5 id="insight-progress-title" className="text-lg font-semibold text-foreground">
                Analysis status
              </h5>
              <p className="mt-1 text-sm text-muted-foreground">
                Started <time dateTime={run.started_at}>{formatDateTime(run.started_at)}</time>
              </p>
              {reportResponse === null ? null : (
                <p className="mt-1 text-sm font-medium text-foreground">
                  {String(reportResponse.metrics.topic_count)} topics researched ·{" "}
                  {String(
                    new Set(
                      reportResponse.report.citations.map((citation) => citation.display_label),
                    ).size,
                  )}{" "}
                  sources used
                </p>
              )}
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge label={run.status} tone={runTone(run.status)} />
              <Button
                size="small"
                variant="secondary"
                onClick={() => void refreshStatus(run.run_id)}
              >
                Refresh status
              </Button>
            </div>
          </div>

          {run.status === "failed" ? (
            <ErrorNotice title="Insight run failed safely">
              <p>{run.failure_message ?? "The insight run could not be completed."}</p>
              {run.failure_code === null ? null : (
                <p className="mt-2 text-xs">Failure code: {run.failure_code}</p>
              )}
            </ErrorNotice>
          ) : null}

          {reportResponse !== null ? null : (
            <details
              open={running}
              className="group rounded-control border border-border bg-surface"
            >
              <summary className="cursor-pointer list-none px-4 py-3 text-sm font-semibold text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus">
                <span className="flex items-center justify-between gap-3">
                  <span>{running ? "Live analysis progress" : "Technical details"}</span>
                  <span
                    aria-hidden="true"
                    className="text-lg text-muted-foreground group-open:rotate-45"
                  >
                    +
                  </span>
                </span>
              </summary>
              <div className="border-t border-border p-4">
                {events.length === 0 && running ? (
                  <LoadingIndicator label="Waiting for persisted orchestration events…" />
                ) : events.length === 0 ? (
                  <p className="text-sm text-muted-foreground">
                    No operational events were published.
                  </p>
                ) : (
                  <ExecutionTimeline events={events} />
                )}
              </div>
            </details>
          )}
        </section>
      )}

      {reportResponse === null ? null : (
        <ReportView
          report={reportResponse.report}
          metrics={reportResponse.metrics}
          events={events}
          onOpenEvidence={openEvidence}
        />
      )}

      {evidenceSelection === null || run === null ? null : (
        <EvidenceDrawer
          selection={evidenceSelection}
          selectedIndex={evidenceIndex}
          value={evidence}
          loading={evidenceLoadingId !== null}
          errorMessage={evidenceFailure?.message ?? null}
          api={api}
          engagementId={engagement.engagement_id}
          runId={run.run_id}
          onSelectIndex={selectEvidenceIndex}
          onClose={closeEvidence}
        />
      )}
    </div>
  );
}
