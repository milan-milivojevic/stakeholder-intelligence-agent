import { useCallback, useEffect, useState } from "react";

import type { BrowserSessionView } from "../api/contracts";
import { LoadingIndicator } from "../components/loading-indicator";
import { SafeFailureNotice } from "../pm/common";
import { failureFromResult } from "../pm/safe-ui";
import type { SafeUiFailure } from "../pm/safe-ui";
import type { InterviewStatusResponse, ResolvedStakeholderContext } from "./contracts";
import type { StakeholderApi } from "./stakeholder-api";
import { StakeholderInterview } from "./stakeholder-interview";

interface StakeholderWorkspaceProps {
  api: StakeholderApi;
  session: BrowserSessionView;
  onUnauthorized: (correlationId: string | null) => void;
}

export function StakeholderWorkspace({ api, session, onUnauthorized }: StakeholderWorkspaceProps) {
  const [resolved, setResolved] = useState<ResolvedStakeholderContext | null>(null);
  const [status, setStatus] = useState<InterviewStatusResponse | null>(null);
  const [failure, setFailure] = useState<SafeUiFailure | null>(null);

  const deny = useCallback(
    (correlationId: string | null) => {
      setResolved(null);
      setStatus(null);
      setFailure(null);
      onUnauthorized(correlationId);
    },
    [onUnauthorized],
  );

  useEffect(() => {
    const lifecycle = { active: true };
    void api
      .getContext(session)
      .then(async (contextResult) => {
        if (!lifecycle.active) {
          return;
        }
        if (!contextResult.ok) {
          if (contextResult.status === 401 || contextResult.status === 403) {
            deny(contextResult.detail?.correlation_id ?? null);
          } else {
            setFailure(failureFromResult(contextResult));
          }
          return;
        }
        const statusResult = await api.getInterviewStatus(contextResult.value.scope);
        if (!statusResult.ok) {
          if (statusResult.status === 401 || statusResult.status === 403) {
            deny(statusResult.detail?.correlation_id ?? null);
          } else {
            setFailure(failureFromResult(statusResult));
          }
          return;
        }
        setResolved(contextResult.value);
        setStatus(statusResult.value);
      })
      .catch(() => {
        if (lifecycle.active) {
          setFailure({
            message: "The fixed stakeholder context could not be verified.",
            correlationId: null,
          });
        }
      });
    return () => {
      lifecycle.active = false;
    };
  }, [api, deny, session]);

  return (
    <div className="grid min-w-0 gap-7">
      {failure === null ? null : <SafeFailureNotice failure={failure} />}

      {resolved === null || status === null ? (
        failure === null ? (
          <LoadingIndicator label="Opening your interview…" />
        ) : null
      ) : (
        <>
          <section
            aria-labelledby="stakeholder-context-title"
            className="grid min-w-0 gap-3 border-b border-border pb-6"
          >
            <div>
              <p className="text-xs font-semibold tracking-wide text-brand uppercase">
                Interview invitation
              </p>
              <h3
                id="stakeholder-context-title"
                className="mt-1 text-xl font-semibold text-foreground"
              >
                {resolved.context.stakeholder.display_name}
              </h3>
            </div>
            <p className="text-sm text-muted-foreground">
              {[resolved.context.stakeholder.role, resolved.context.stakeholder.department]
                .filter((value): value is string => value !== null)
                .join(" · ") || "Role and department not specified"}
            </p>
            <p className="min-w-0 text-sm leading-6 break-words text-muted-foreground">
              You have been invited to a guided interview for engagement{" "}
              <span className="font-semibold text-foreground">
                {resolved.context.engagement.name}
              </span>
              .
            </p>
          </section>

          <StakeholderInterview
            api={api}
            scope={resolved.scope}
            initialStatus={status}
            onUnauthorized={deny}
          />
        </>
      )}
    </div>
  );
}
