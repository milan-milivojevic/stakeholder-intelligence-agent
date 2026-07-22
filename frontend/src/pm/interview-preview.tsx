import { Button } from "../components/button";
import { EmptyState } from "./common";
import type { InterviewPreviewResponse } from "./contracts";
import { formatDateTime } from "./safe-ui";

export function InterviewPreview({
  preview,
  stakeholderName,
  onClose,
}: {
  preview: InterviewPreviewResponse;
  stakeholderName: string;
  onClose: () => void;
}) {
  return (
    <section
      className="grid gap-5 rounded-panel border border-border bg-surface p-4 shadow-panel sm:p-5"
      aria-labelledby="interview-preview-title"
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="text-xs font-semibold tracking-wide text-brand uppercase">
            Finalized interview
          </p>
          <h5 id="interview-preview-title" className="mt-1 text-lg font-semibold text-foreground">
            {stakeholderName}
          </h5>
          <p className="mt-1 text-sm text-muted-foreground">
            Finalized {formatDateTime(preview.transcript.finalized_at)}
          </p>
        </div>
        <Button size="small" variant="quiet" onClick={onClose}>
          Close preview
        </Button>
      </div>

      {preview.turns.length === 0 ? (
        <EmptyState>No conversation turns are available for this interview.</EmptyState>
      ) : (
        <ol className="grid gap-3" aria-label="Interview transcript">
          {preview.turns.map((turn) => {
            const stakeholderTurn = turn.speaker === "stakeholder";
            return (
              <li
                key={turn.turn_index}
                className={
                  stakeholderTurn
                    ? "ml-auto w-full max-w-3xl rounded-control border border-brand/30 bg-brand/8 p-4"
                    : "mr-auto w-full max-w-3xl rounded-control border border-border bg-surface-subtle p-4"
                }
              >
                <p className="text-xs font-semibold text-muted-foreground">
                  {stakeholderTurn ? stakeholderName : "Interview assistant"}
                </p>
                <p className="mt-2 text-sm leading-7 whitespace-pre-wrap text-foreground">
                  {turn.text}
                </p>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
