import type { BrowserSessionApi } from "../auth/browser-api";
import { browserSessionApi } from "../auth/browser-api";
import type { BrowserBootstrap } from "../auth/bootstrap";
import { SessionGate } from "../auth/session-gate";
import { BrowserSessionProvider } from "../auth/session-provider";
import { AppHeader } from "./app-header";
import { pmApi as defaultPmApi } from "../pm/pm-api";
import type { PmApi } from "../pm/pm-api";
import { stakeholderApi as defaultStakeholderApi } from "../stakeholder/stakeholder-api";
import type { StakeholderApi } from "../stakeholder/stakeholder-api";

interface AppProps {
  bootstrap: BrowserBootstrap;
  api?: BrowserSessionApi;
  pmApi?: PmApi;
  stakeholderApi?: StakeholderApi;
}

export function App({
  bootstrap,
  api = browserSessionApi,
  pmApi = defaultPmApi,
  stakeholderApi = defaultStakeholderApi,
}: AppProps) {
  return (
    <BrowserSessionProvider api={api} bootstrap={bootstrap}>
      <div className="min-h-screen bg-background">
        <AppHeader />

        <main className="px-4 py-5 sm:px-6 sm:py-7">
          <div className="mx-auto max-w-shell min-w-0">
            <SessionGate pmApi={pmApi} stakeholderApi={stakeholderApi} />
          </div>
        </main>
      </div>
    </BrowserSessionProvider>
  );
}
