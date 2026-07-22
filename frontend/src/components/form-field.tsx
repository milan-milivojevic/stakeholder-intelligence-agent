import type { ComponentPropsWithRef, ReactNode } from "react";

import { classNames } from "../lib/class-names";

interface FormFieldProps {
  label: string;
  labelFor: string;
  children: ReactNode;
  hint?: string | undefined;
  error?: string | undefined;
}

export function FormField({ label, labelFor, children, hint, error }: FormFieldProps) {
  const hintId = hint === undefined ? undefined : `${labelFor}-hint`;
  const errorId = error === undefined ? undefined : `${labelFor}-error`;

  return (
    <div className="grid min-w-0 gap-2">
      <label htmlFor={labelFor} className="text-sm font-semibold text-foreground">
        {label}
      </label>
      {children}
      {hint === undefined ? null : (
        <p id={hintId} className="text-sm text-muted-foreground">
          {hint}
        </p>
      )}
      {error === undefined ? null : (
        <p id={errorId} className="text-sm font-semibold text-danger" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

export interface TextInputProps extends ComponentPropsWithRef<"input"> {
  invalid?: boolean;
}

export function TextInput({ className, invalid = false, ref, ...props }: TextInputProps) {
  return (
    <input
      ref={ref}
      aria-invalid={invalid || undefined}
      className={classNames(
        "min-h-11 w-full min-w-0 rounded-control border bg-surface px-3 py-2 text-base text-foreground",
        "placeholder:text-muted-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
        "disabled:cursor-not-allowed disabled:bg-surface-subtle disabled:text-muted-foreground",
        invalid ? "border-danger" : "border-border-strong",
        className,
      )}
      {...props}
    />
  );
}

export interface TextAreaProps extends ComponentPropsWithRef<"textarea"> {
  invalid?: boolean;
}

export function TextArea({ className, invalid = false, ref, ...props }: TextAreaProps) {
  return (
    <textarea
      ref={ref}
      aria-invalid={invalid || undefined}
      className={classNames(
        "min-h-28 w-full min-w-0 resize-y rounded-control border bg-surface px-3 py-2 text-base text-foreground",
        "placeholder:text-muted-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
        "disabled:cursor-not-allowed disabled:bg-surface-subtle disabled:text-muted-foreground",
        invalid ? "border-danger" : "border-border-strong",
        className,
      )}
      {...props}
    />
  );
}
