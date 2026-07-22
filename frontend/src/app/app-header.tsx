import { Button } from "../components/button";
import {
  useBrowserRoute,
  useBrowserSessionActions,
  useBrowserSessionState,
} from "../auth/session-context";

function formatDateTime(value: string): string {
  return new Date(value).toLocaleString();
}

export function AppHeader() {
  const route = useBrowserRoute();
  const state = useBrowserSessionState();
  const { logout } = useBrowserSessionActions();

  const session = state.phase === "active" || state.phase === "logging-out" ? state.session : null;
  const isPmWorkspace = route === "pm" && session?.principal_type === "pm";
  const isStakeholderWorkspace =
    route === "stakeholder" && session?.principal_type === "stakeholder";
  const workspace = isPmWorkspace
    ? {
        title: "Project Manager Workspace",
        expiryLabel: "Session until",
      }
    : isStakeholderWorkspace
      ? {
          title: "Stakeholder workspace",
          expiryLabel: "Session expires",
        }
      : null;
  const routeTitle =
    route === "pm"
      ? "Project Manager Workspace"
      : route === "stakeholder"
        ? "Stakeholder workspace"
        : "Workspace";
  const isLoggingOut = state.phase === "logging-out";
  const logoutFailed = state.phase === "active" && state.logoutFailure !== null;

  return (
    <header className="border-b border-border bg-surface shadow-sm">
      <div className="mx-auto flex min-h-16 max-w-shell flex-col gap-3 px-4 py-3 sm:flex-row sm:items-center sm:justify-between sm:px-6">
        <div className="flex min-w-0 items-center gap-3">
          <div
            className="flex size-10 shrink-0 items-center justify-center rounded-control bg-brand text-sm font-bold tracking-wide text-brand-contrast"
            aria-hidden="true"
          >
            SI
          </div>
          <div className="min-w-0">
            <h1 className="truncate text-base font-semibold tracking-tight text-foreground sm:text-lg">
              Stakeholder Intelligence
            </h1>
            <h2 className="truncate text-xs font-normal text-muted-foreground sm:text-sm">
              {workspace?.title ?? routeTitle}
            </h2>
          </div>
        </div>

        {workspace === null || session === null ? null : (
          <div
            className="flex min-w-0 flex-wrap items-center gap-3 sm:justify-end sm:gap-4"
            aria-label={`${workspace.title} session`}
          >
            <p className="text-xs text-muted-foreground">
              {workspace.expiryLabel}{" "}
              <time dateTime={session.expires_at}>{formatDateTime(session.expires_at)}</time>
            </p>
            <Button
              size="small"
              variant="secondary"
              disabled={isLoggingOut}
              onClick={() => void logout()}
            >
              {isLoggingOut ? "Signing out…" : "Sign out"}
            </Button>
            {logoutFailed ? (
              <p className="basis-full text-xs font-semibold text-danger-foreground" role="alert">
                Sign-out was not completed. Try again.
              </p>
            ) : null}
          </div>
        )}
      </div>
    </header>
  );
}
