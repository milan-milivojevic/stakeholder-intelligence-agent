import type { ReactNode } from "react";

import { classNames } from "../lib/class-names";

type NoticeTone = "info" | "success" | "warning" | "error";

const toneClasses = {
  info: "border-info-border bg-info-surface text-info-foreground",
  success: "border-success-border bg-success-surface text-success-foreground",
  warning: "border-warning-border bg-warning-surface text-warning-foreground",
  error: "border-danger-border bg-danger-surface text-danger-foreground",
} satisfies Record<NoticeTone, string>;

const toneLabels = {
  info: "Information",
  success: "Success",
  warning: "Warning",
  error: "Error",
} satisfies Record<NoticeTone, string>;

interface NoticeFrameProps {
  tone: NoticeTone;
  children: ReactNode;
  title?: string | undefined;
}

function NoticeFrame({ tone, title, children }: NoticeFrameProps) {
  return (
    <div
      className={classNames("rounded-control border px-4 py-3", toneClasses[tone])}
      role={tone === "error" ? "alert" : "status"}
    >
      <p className="text-sm font-semibold">{title ?? toneLabels[tone]}</p>
      <div className="mt-1 text-sm leading-6">{children}</div>
    </div>
  );
}

export function InfoNotice({ children, title }: Omit<NoticeFrameProps, "tone">) {
  return (
    <NoticeFrame tone="info" title={title}>
      {children}
    </NoticeFrame>
  );
}

export function SuccessNotice({ children, title }: Omit<NoticeFrameProps, "tone">) {
  return (
    <NoticeFrame tone="success" title={title}>
      {children}
    </NoticeFrame>
  );
}

export function WarningNotice({ children, title }: Omit<NoticeFrameProps, "tone">) {
  return (
    <NoticeFrame tone="warning" title={title}>
      {children}
    </NoticeFrame>
  );
}

export function ErrorNotice({ children, title }: Omit<NoticeFrameProps, "tone">) {
  return (
    <NoticeFrame tone="error" title={title}>
      {children}
    </NoticeFrame>
  );
}
