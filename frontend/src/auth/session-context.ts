import { createContext, use } from "react";

import type { BrowserRoute } from "./bootstrap";
import type { BrowserSessionState } from "./session-state";

export interface BrowserSessionActions {
  activatePm: (bootstrapToken: string) => Promise<void>;
  logout: () => Promise<void>;
  invalidateSession: (correlationId: string | null) => void;
  resetActivation: () => void;
}

export const BrowserSessionStateContext = createContext<BrowserSessionState | null>(null);
export const BrowserSessionActionsContext = createContext<BrowserSessionActions | null>(null);
export const BrowserRouteContext = createContext<BrowserRoute | null>(null);

function requireContext<T>(value: T | null, name: string): T {
  if (value === null) {
    throw new Error(`${name} must be used within BrowserSessionProvider.`);
  }
  return value;
}

export function useBrowserSessionState(): BrowserSessionState {
  return requireContext(use(BrowserSessionStateContext), "useBrowserSessionState");
}

export function useBrowserSessionActions(): BrowserSessionActions {
  return requireContext(use(BrowserSessionActionsContext), "useBrowserSessionActions");
}

export function useBrowserRoute(): BrowserRoute {
  return requireContext(use(BrowserRouteContext), "useBrowserRoute");
}
