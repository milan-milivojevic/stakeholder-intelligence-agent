import type { ComponentPropsWithRef } from "react";

import { classNames } from "../lib/class-names";

type ButtonVariant = "primary" | "secondary" | "danger" | "quiet";
type ButtonSize = "default" | "small";

const variantClasses = {
  primary:
    "border-brand bg-brand text-brand-contrast hover:border-brand-hover hover:bg-brand-hover",
  secondary: "border-border-strong bg-surface text-foreground hover:border-brand hover:text-brand",
  danger:
    "border-danger bg-danger text-danger-contrast hover:border-danger-strong hover:bg-danger-strong",
  quiet: "border-transparent bg-transparent text-foreground hover:bg-surface-subtle",
} satisfies Record<ButtonVariant, string>;

const sizeClasses = {
  default: "min-h-11 px-4 py-2.5 text-sm",
  small: "min-h-9 px-3 py-1.5 text-sm",
} satisfies Record<ButtonSize, string>;

export interface ButtonProps extends ComponentPropsWithRef<"button"> {
  variant?: ButtonVariant;
  size?: ButtonSize;
}

export function Button({
  className,
  type = "button",
  variant = "primary",
  size = "default",
  ref,
  ...props
}: ButtonProps) {
  return (
    <button
      ref={ref}
      type={type}
      className={classNames(
        "inline-flex items-center justify-center gap-2 rounded-control border font-semibold transition-colors",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
        "disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-55",
        variantClasses[variant],
        sizeClasses[size],
        className,
      )}
      {...props}
    />
  );
}
