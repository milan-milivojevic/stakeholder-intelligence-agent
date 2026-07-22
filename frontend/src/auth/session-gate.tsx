import type { ReactNode } from "react";

import { Button } from "../components/button";
import { LoadingIndicator } from "../components/loading-indicator";
import { ErrorNotice, InfoNotice } from "../components/notice";
import { Panel, PanelBody, PanelHeader, PanelTitle } from "../components/panel";
import { PmWorkspace } from "../pm/pm-workspace";
import type { PmApi } from "../pm/pm-api";
import type { StakeholderApi } from "../stakeholder/stakeholder-api";
import { StakeholderWorkspace } from "../stakeholder/stakeholder-workspace";
import { PmActivation } from "./pm-activation";
import {
  useBrowserRoute,
  useBrowserSessionActions,
  useBrowserSessionState,
} from "./session-context";
import { decideRoute } from "./session-state";

function CorrelationReference({ value }: { value: string | null }) {
  return value === null ? null : (
    <p className="mt-2 text-xs">
      Reference: <span className="font-mono">{value}</span>
    </p>
  );
}

function ActiveWorkspace({
  pmApi,
  stakeholderApi,
}: {
  pmApi: PmApi;
  stakeholderApi: StakeholderApi;
}) {
  const route = useBrowserRoute();
  const state = useBrowserSessionState();
  const { invalidateSession } = useBrowserSessionActions();
  if (state.phase !== "active") {
    return null;
  }

  if (route === "pm") {
    return <PmWorkspace api={pmApi} session={state.session} />;
  }

  return (
    <StakeholderWorkspace
      api={stakeholderApi}
      session={state.session}
      onUnauthorized={invalidateSession}
    />
  );
}

function AccessSurface({ children }: { children: ReactNode }) {
  return (
    <Panel className="mx-auto max-w-2xl" aria-labelledby="access-title">
      <PanelHeader className="py-3.5">
        <PanelTitle id="access-title">Project manager access</PanelTitle>
      </PanelHeader>
      <PanelBody className="grid gap-5">{children}</PanelBody>
    </Panel>
  );
}

export function SessionGate({
  pmApi,
  stakeholderApi,
}: {
  pmApi: PmApi;
  stakeholderApi: StakeholderApi;
}) {
  const route = useBrowserRoute();
  const state = useBrowserSessionState();
  const { resetActivation } = useBrowserSessionActions();
  const decision = decideRoute(route, state);

  switch (decision) {
    case "loading":
      return (
        <AccessSurface>
          <LoadingIndicator label="Preparing the requested workspace…" />
        </AccessSurface>
      );
    case "activation-required":
      return (
        <AccessSurface>
          {route === "pm" ? (
            <PmActivation />
          ) : (
            <InfoNotice title="Invitation required">
              Open the invitation link provided by the project manager to activate this workspace.
            </InfoNotice>
          )}
        </AccessSurface>
      );
    case "active":
      return <ActiveWorkspace pmApi={pmApi} stakeholderApi={stakeholderApi} />;
    case "denied":
      return (
        <AccessSurface>
          <ErrorNotice title="Invitation unavailable">
            This interview invitation is invalid, expired, or revoked. Ask the project manager for a
            new invitation link.
            <CorrelationReference value={state.phase === "denied" ? state.correlationId : null} />
          </ErrorNotice>
        </AccessSurface>
      );
    case "unavailable":
      return (
        <AccessSurface>
          <div className="grid gap-4">
            <ErrorNotice title="Workspace unavailable">
              The secure workspace could not be prepared. No access credential was retained.
              <CorrelationReference
                value={state.phase === "unavailable" ? state.correlationId : null}
              />
            </ErrorNotice>
            {route === "pm" ? (
              <div>
                <Button variant="secondary" onClick={resetActivation}>
                  Return to project manager access
                </Button>
              </div>
            ) : null}
          </div>
        </AccessSurface>
      );
    case "not-found":
      return (
        <AccessSurface>
          <ErrorNotice title="Page not found">
            Use the project manager entry point or an approved stakeholder invitation.
          </ErrorNotice>
        </AccessSurface>
      );
  }
}
