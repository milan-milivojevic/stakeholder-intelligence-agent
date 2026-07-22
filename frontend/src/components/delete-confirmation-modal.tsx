import { useEffect, useId, useRef } from "react";
import { createPortal } from "react-dom";

import { Button } from "./button";

interface DeleteConfirmationModalProps {
  title: string;
  description?: string;
  confirmLabel: string;
  busyLabel: string;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

export function DeleteConfirmationModal({
  title,
  description,
  confirmLabel,
  busyLabel,
  busy,
  onCancel,
  onConfirm,
}: DeleteConfirmationModalProps) {
  const titleId = useId();
  const descriptionId = useId();
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const previouslyFocused =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    cancelRef.current?.focus();
    return () => {
      if (previouslyFocused?.isConnected === true) {
        previouslyFocused.focus();
      }
    };
  }, []);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent): void => {
      if (event.key === "Escape" && !busy) {
        onCancel();
      }
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [busy, onCancel]);

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-foreground/45 p-4">
      <section
        className="grid w-full max-w-md gap-5 rounded-panel border border-border bg-surface p-5 shadow-panel sm:p-6"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description === undefined ? undefined : descriptionId}
      >
        <div>
          <h5 id={titleId} className="text-lg font-semibold text-foreground">
            {title}
          </h5>
          {description === undefined ? null : (
            <p id={descriptionId} className="mt-2 text-sm break-all text-muted-foreground">
              {description}
            </p>
          )}
        </div>
        <div className="flex flex-wrap justify-end gap-2">
          <Button ref={cancelRef} variant="secondary" disabled={busy} onClick={onCancel}>
            Cancel
          </Button>
          <Button variant="danger" disabled={busy} onClick={onConfirm}>
            {busy ? busyLabel : confirmLabel}
          </Button>
        </div>
      </section>
    </div>,
    document.body,
  );
}
