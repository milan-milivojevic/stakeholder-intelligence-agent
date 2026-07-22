import type { BrowserSessionView } from "../api/contracts";
import type { ApiResult } from "../api/client";
import type { BrowserSessionApi } from "./browser-api";

export type BrowserRoute = "pm" | "stakeholder" | "not-found";
export type BootstrapSource = "inspection" | "invitation";

export interface SafeSessionFailure {
  kind: "denied" | "unavailable";
  correlationId: string | null;
}

export type BrowserSessionOutcome =
  { kind: "authenticated"; session: BrowserSessionView } | SafeSessionFailure;

export interface BrowserBootstrap {
  route: BrowserRoute;
  source: BootstrapSource;
  outcome: Promise<BrowserSessionOutcome>;
}

interface LocationPath {
  readonly pathname: string;
}

const invitationPattern = /^[A-Za-z0-9_-]{32,1024}$/u;

export function settleSession(
  sessionPromise: Promise<ApiResult<BrowserSessionView>>,
): Promise<BrowserSessionOutcome> {
  return sessionPromise.then(
    (result) => {
      if (result.ok) {
        return { kind: "authenticated", session: result.value };
      }
      if (result.status === 403) {
        return {
          kind: "denied",
          correlationId: result.detail?.correlation_id ?? null,
        };
      }
      return {
        kind: "unavailable",
        correlationId: result.detail?.correlation_id ?? null,
      };
    },
    () => ({ kind: "unavailable", correlationId: null }),
  );
}

export function prepareBrowserBootstrap(
  location: LocationPath,
  api: BrowserSessionApi,
): BrowserBootstrap {
  if (location.pathname === "/pm") {
    return {
      route: "pm",
      source: "inspection",
      outcome: settleSession(api.inspect("pm")),
    };
  }
  if (location.pathname === "/s") {
    return {
      route: "stakeholder",
      source: "inspection",
      outcome: settleSession(api.inspect("stakeholder")),
    };
  }
  if (location.pathname.startsWith("/s/")) {
    const invitationToken = location.pathname.slice(3);
    return {
      route: "stakeholder",
      source: "invitation",
      outcome: invitationPattern.test(invitationToken)
        ? settleSession(api.activateStakeholder(invitationToken))
        : Promise.resolve({ kind: "denied", correlationId: null }),
    };
  }
  return {
    route: "not-found",
    source: "inspection",
    outcome: Promise.resolve({ kind: "denied", correlationId: null }),
  };
}
