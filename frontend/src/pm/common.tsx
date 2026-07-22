import { ErrorNotice } from "../components/notice";
import { classNames } from "../lib/class-names";
import type { SafeUiFailure } from "./safe-ui";

export function SafeFailureNotice({ failure }: { failure: SafeUiFailure }) {
  return (
    <ErrorNotice title="Request not completed">
      <p>{failure.message}</p>
      {failure.correlationId === null ? null : (
        <p className="mt-2 text-xs">
          Reference: <span className="font-mono">{failure.correlationId}</span>
        </p>
      )}
    </ErrorNotice>
  );
}

export function EmptyState({ children }: { children: string }) {
  return (
    <p className="rounded-control border border-dashed border-border-strong bg-surface-subtle px-4 py-6 text-center text-sm text-muted-foreground">
      {children}
    </p>
  );
}

type StatusTone = "neutral" | "success" | "warning" | "error" | "info";

const statusToneClasses = {
  neutral: "border-border bg-surface-subtle text-muted-foreground",
  success: "border-success-border bg-success-surface text-success-foreground",
  warning: "border-warning-border bg-warning-surface text-warning-foreground",
  error: "border-danger-border bg-danger-surface text-danger-foreground",
  info: "border-info-border bg-info-surface text-info-foreground",
} satisfies Record<StatusTone, string>;

export function StatusBadge({ label, tone = "neutral" }: { label: string; tone?: StatusTone }) {
  return (
    <span
      className={classNames(
        "inline-flex rounded-full border px-2.5 py-1 text-xs font-semibold whitespace-nowrap",
        statusToneClasses[tone],
      )}
    >
      {label.replaceAll("_", " ")}
    </span>
  );
}
