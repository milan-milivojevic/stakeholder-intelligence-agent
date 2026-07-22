import { useEffect, useMemo, useState } from "react";

import type { Engagement, InterviewSession, Stakeholder } from "../api/contracts";
import { Button } from "../components/button";
import { LoadingIndicator } from "../components/loading-indicator";
import { EmptyState, SafeFailureNotice, StatusBadge } from "./common";
import type { InterviewPreviewResponse } from "./contracts";
import { InterviewPreview } from "./interview-preview";
import type { PmApi } from "./pm-api";
import { failureFromResult, formatDateTime } from "./safe-ui";
import type { SafeUiFailure } from "./safe-ui";

function interviewTone(status: InterviewSession["status"]) {
  if (status === "ready") {
    return "success" as const;
  }
  if (status === "failed") {
    return "error" as const;
  }
  if (status === "draft") {
    return "neutral" as const;
  }
  return "info" as const;
}

export function PmInterviews({ api, engagement }: { api: PmApi; engagement: Engagement }) {
  const [sessions, setSessions] = useState<InterviewSession[] | null>(null);
  const [stakeholders, setStakeholders] = useState<Stakeholder[]>([]);
  const [failure, setFailure] = useState<SafeUiFailure | null>(null);
  const [preview, setPreview] = useState<InterviewPreviewResponse | null>(null);
  const [previewingSessionId, setPreviewingSessionId] = useState<string | null>(null);
  const [previewFailure, setPreviewFailure] = useState<SafeUiFailure | null>(null);

  useEffect(() => {
    let current = true;
    async function load(): Promise<void> {
      try {
        const [interviewResult, stakeholderResult] = await Promise.all([
          api.listInterviews(engagement.engagement_id),
          api.listStakeholders(engagement.engagement_id),
        ]);
        if (!current) {
          return;
        }
        if (!interviewResult.ok) {
          setFailure(failureFromResult(interviewResult));
          return;
        }
        if (!stakeholderResult.ok) {
          setFailure(failureFromResult(stakeholderResult));
          return;
        }
        setSessions(interviewResult.value);
        setStakeholders(stakeholderResult.value);
      } catch {
        if (current) {
          setFailure({ message: "Interview sessions could not be loaded.", correlationId: null });
        }
      }
    }
    void load();
    return () => {
      current = false;
    };
  }, [api, engagement.engagement_id]);

  const stakeholderNames = useMemo(
    () =>
      new Map(
        stakeholders.map((stakeholder) => [stakeholder.stakeholder_id, stakeholder.display_name]),
      ),
    [stakeholders],
  );
  const finalizedSessions = useMemo(
    () => sessions?.filter((session) => session.finalized_at !== null) ?? null,
    [sessions],
  );

  async function showPreview(session: InterviewSession): Promise<void> {
    setPreview(null);
    setPreviewFailure(null);
    setPreviewingSessionId(session.interview_session_id);
    try {
      const result = await api.getInterviewPreview(
        engagement.engagement_id,
        session.interview_session_id,
      );
      if (result.ok) {
        setPreview(result.value);
      } else {
        setPreviewFailure(failureFromResult(result));
      }
    } catch {
      setPreviewFailure({
        message: "The interview preview could not be loaded.",
        correlationId: null,
      });
    } finally {
      setPreviewingSessionId(null);
    }
  }

  return (
    <div className="grid gap-6">
      <div>
        <h4 className="text-xl font-semibold tracking-tight text-foreground">
          Finalized interviews
        </h4>
        <p className="mt-1 text-sm leading-6 text-muted-foreground">
          Only finalized interviews from the selected engagement are shown here.
        </p>
      </div>

      {failure === null ? null : <SafeFailureNotice failure={failure} />}
      {sessions === null && failure === null ? (
        <LoadingIndicator label="Loading interview sessions…" />
      ) : finalizedSessions === null || finalizedSessions.length === 0 ? (
        <EmptyState>No finalized interviews are available.</EmptyState>
      ) : (
        <>
          <div className="overflow-x-auto rounded-control border border-border">
            <table className="w-full min-w-200 border-collapse text-left text-sm">
              <thead className="bg-surface-subtle text-muted-foreground">
                <tr>
                  <th className="px-4 py-3 font-semibold">Stakeholder</th>
                  <th className="px-4 py-3 font-semibold">Status</th>
                  <th className="px-4 py-3 font-semibold">Started</th>
                  <th className="px-4 py-3 font-semibold">Finalized</th>
                  <th className="px-4 py-3 font-semibold">Transcript ingestion</th>
                  <th className="px-4 py-3 font-semibold">Actions</th>
                </tr>
              </thead>
              <tbody>
                {finalizedSessions.map((session) => (
                  <tr
                    key={session.interview_session_id}
                    className="border-t border-border align-top"
                  >
                    <td className="px-4 py-3 font-medium text-foreground">
                      {stakeholderNames.get(session.stakeholder_id) ?? "Unknown stakeholder"}
                    </td>
                    <td className="px-4 py-3">
                      <StatusBadge label={session.status} tone={interviewTone(session.status)} />
                      {session.failure_message === null ? null : (
                        <p className="mt-2 max-w-prose text-xs text-danger-foreground">
                          {session.failure_message}
                        </p>
                      )}
                    </td>
                    <td className="px-4 py-3 text-muted-foreground">
                      <time dateTime={session.started_at}>
                        {formatDateTime(session.started_at)}
                      </time>
                    </td>
                    <td className="px-4 py-3 text-muted-foreground">
                      {session.finalized_at === null ? (
                        "Not finalized"
                      ) : (
                        <time dateTime={session.finalized_at}>
                          {formatDateTime(session.finalized_at)}
                        </time>
                      )}
                    </td>
                    <td className="px-4 py-3 text-muted-foreground">
                      {session.ingestion_version_id === null
                        ? "Not available"
                        : session.status === "ready"
                          ? "Ready for permitted retrieval"
                          : "Finalized; ingestion in progress"}
                    </td>
                    <td className="px-4 py-3">
                      <Button
                        size="small"
                        variant="secondary"
                        disabled={previewingSessionId !== null}
                        aria-label={`Preview interview with ${stakeholderNames.get(session.stakeholder_id) ?? "unknown stakeholder"}`}
                        onClick={() => void showPreview(session)}
                      >
                        Preview
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {previewingSessionId === null ? null : (
            <LoadingIndicator label="Loading interview preview..." />
          )}
          {previewFailure === null ? null : <SafeFailureNotice failure={previewFailure} />}
          {preview === null ? null : (
            <InterviewPreview
              preview={preview}
              stakeholderName={
                stakeholderNames.get(preview.interview_session.stakeholder_id) ??
                "Unknown stakeholder"
              }
              onClose={() => setPreview(null)}
            />
          )}
        </>
      )}
    </div>
  );
}
