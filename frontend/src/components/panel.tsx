import type { ComponentPropsWithRef } from "react";

import { classNames } from "../lib/class-names";

export function Panel({ className, ref, ...props }: ComponentPropsWithRef<"section">) {
  return (
    <section
      ref={ref}
      className={classNames(
        "min-w-0 rounded-panel border border-border bg-surface shadow-panel",
        className,
      )}
      {...props}
    />
  );
}

export function PanelHeader({ className, ref, ...props }: ComponentPropsWithRef<"header">) {
  return (
    <header
      ref={ref}
      className={classNames("border-b border-border px-5 py-4 sm:px-6", className)}
      {...props}
    />
  );
}

export function PanelTitle({ className, ref, ...props }: ComponentPropsWithRef<"h2">) {
  return (
    <h2
      ref={ref}
      className={classNames("text-lg font-semibold tracking-tight text-foreground", className)}
      {...props}
    />
  );
}

export function PanelBody({ className, ref, ...props }: ComponentPropsWithRef<"div">) {
  return <div ref={ref} className={classNames("min-w-0 p-5 sm:p-6", className)} {...props} />;
}
