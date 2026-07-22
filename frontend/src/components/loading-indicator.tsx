interface LoadingIndicatorProps {
  label: string;
}

export function LoadingIndicator({ label }: LoadingIndicatorProps) {
  return (
    <div className="inline-flex items-center gap-3 text-sm text-muted-foreground" role="status">
      <span
        className="size-4 animate-spin rounded-full border-2 border-border-strong border-t-brand motion-reduce:animate-none"
        aria-hidden="true"
      />
      <span>{label}</span>
    </div>
  );
}
