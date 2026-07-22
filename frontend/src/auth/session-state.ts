import type { BrowserSessionView } from "../api/contracts";
import type {
  BootstrapSource,
  BrowserRoute,
  BrowserSessionOutcome,
  SafeSessionFailure,
} from "./bootstrap";

export type BrowserSessionState =
  | { phase: "loading" }
  | {
      phase: "activation-required";
      reason: "missing" | "denied";
      correlationId: string | null;
    }
  | {
      phase: "active";
      session: BrowserSessionView;
      logoutFailure: SafeSessionFailure | null;
    }
  | { phase: "logging-out"; session: BrowserSessionView }
  | { phase: "denied"; correlationId: string | null }
  | { phase: "unavailable"; correlationId: string | null };

export type BrowserSessionAction =
  | {
      type: "bootstrap-settled";
      source: BootstrapSource;
      outcome: BrowserSessionOutcome;
    }
  | { type: "activation-started" }
  | { type: "activation-settled"; outcome: BrowserSessionOutcome }
  | { type: "logout-started" }
  | { type: "logout-succeeded" }
  | { type: "logout-failed"; failure: SafeSessionFailure }
  | { type: "session-denied"; correlationId: string | null }
  | { type: "reset-activation" };

export const initialBrowserSessionState: BrowserSessionState = { phase: "loading" };

function outcomeState(
  outcome: BrowserSessionOutcome,
  deniedState: BrowserSessionState,
): BrowserSessionState {
  if (outcome.kind === "authenticated") {
    return { phase: "active", session: outcome.session, logoutFailure: null };
  }
  if (outcome.kind === "denied") {
    return deniedState;
  }
  return { phase: "unavailable", correlationId: outcome.correlationId };
}

function outcomeCorrelation(outcome: BrowserSessionOutcome): string | null {
  return outcome.kind === "authenticated" ? null : outcome.correlationId;
}

export function browserSessionReducer(
  state: BrowserSessionState,
  action: BrowserSessionAction,
): BrowserSessionState {
  switch (action.type) {
    case "bootstrap-settled":
      return outcomeState(
        action.outcome,
        action.source === "inspection"
          ? { phase: "activation-required", reason: "missing", correlationId: null }
          : { phase: "denied", correlationId: outcomeCorrelation(action.outcome) },
      );
    case "activation-started":
      return { phase: "loading" };
    case "activation-settled":
      return outcomeState(action.outcome, {
        phase: "activation-required",
        reason: "denied",
        correlationId: outcomeCorrelation(action.outcome),
      });
    case "logout-started":
      return state.phase === "active" ? { phase: "logging-out", session: state.session } : state;
    case "logout-succeeded":
      return { phase: "activation-required", reason: "missing", correlationId: null };
    case "logout-failed":
      return state.phase === "logging-out"
        ? { phase: "active", session: state.session, logoutFailure: action.failure }
        : state;
    case "session-denied":
      return { phase: "denied", correlationId: action.correlationId };
    case "reset-activation":
      return { phase: "activation-required", reason: "missing", correlationId: null };
  }
}

export type RouteDecision =
  "loading" | "activation-required" | "active" | "denied" | "unavailable" | "not-found";

export function decideRoute(route: BrowserRoute, state: BrowserSessionState): RouteDecision {
  if (route === "not-found") {
    return "not-found";
  }
  if (state.phase === "loading" || state.phase === "logging-out") {
    return "loading";
  }
  if (state.phase === "activation-required") {
    return "activation-required";
  }
  if (state.phase === "denied") {
    return "denied";
  }
  if (state.phase === "unavailable") {
    return "unavailable";
  }
  const expectedPrincipal = route === "pm" ? "pm" : "stakeholder";
  return state.session.principal_type === expectedPrincipal ? "active" : "activation-required";
}
