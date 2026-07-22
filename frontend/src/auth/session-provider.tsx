import { useCallback, useEffect, useMemo, useReducer } from "react";
import type { ReactNode } from "react";

import type { BrowserSessionApi } from "./browser-api";
import type { BrowserBootstrap, SafeSessionFailure } from "./bootstrap";
import { settleSession } from "./bootstrap";
import {
  BrowserRouteContext,
  BrowserSessionActionsContext,
  BrowserSessionStateContext,
} from "./session-context";
import type { BrowserSessionActions } from "./session-context";
import { browserSessionReducer, initialBrowserSessionState } from "./session-state";

interface BrowserSessionProviderProps {
  api: BrowserSessionApi;
  bootstrap: BrowserBootstrap;
  children: ReactNode;
}

export function BrowserSessionProvider({ api, bootstrap, children }: BrowserSessionProviderProps) {
  const [state, dispatch] = useReducer(browserSessionReducer, initialBrowserSessionState);

  useEffect(() => {
    let active = true;
    void bootstrap.outcome.then((outcome) => {
      if (active) {
        dispatch({ type: "bootstrap-settled", source: bootstrap.source, outcome });
      }
    });
    return () => {
      active = false;
    };
  }, [bootstrap]);

  const activatePm = useCallback(
    async (bootstrapToken: string): Promise<void> => {
      dispatch({ type: "activation-started" });
      const outcome = await settleSession(api.activatePm(bootstrapToken));
      dispatch({ type: "activation-settled", outcome });
    },
    [api],
  );

  const logout = useCallback(async (): Promise<void> => {
    dispatch({ type: "logout-started" });
    try {
      const principal = bootstrap.route === "pm" ? "pm" : "stakeholder";
      const result = await api.logout(principal);
      if (result.ok) {
        dispatch({ type: "logout-succeeded" });
        return;
      }
      const failure: SafeSessionFailure = {
        kind: "unavailable",
        correlationId: result.detail?.correlation_id ?? null,
      };
      dispatch({ type: "logout-failed", failure });
    } catch {
      const failure: SafeSessionFailure = { kind: "unavailable", correlationId: null };
      dispatch({ type: "logout-failed", failure });
    }
  }, [api, bootstrap.route]);

  const resetActivation = useCallback(() => {
    dispatch({ type: "reset-activation" });
  }, []);

  const invalidateSession = useCallback((correlationId: string | null) => {
    dispatch({ type: "session-denied", correlationId });
  }, []);

  const actions = useMemo<BrowserSessionActions>(
    () => ({ activatePm, logout, invalidateSession, resetActivation }),
    [activatePm, invalidateSession, logout, resetActivation],
  );

  return (
    <BrowserRouteContext value={bootstrap.route}>
      <BrowserSessionActionsContext value={actions}>
        <BrowserSessionStateContext value={state}>{children}</BrowserSessionStateContext>
      </BrowserSessionActionsContext>
    </BrowserRouteContext>
  );
}
