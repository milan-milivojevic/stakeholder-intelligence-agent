import type { ApiResult } from "../api/client";

export interface SafeUiFailure {
  message: string;
  correlationId: string | null;
}

export function failureFromResult<T>(result: ApiResult<T>): SafeUiFailure | null {
  if (result.ok) {
    return null;
  }
  return {
    message: result.detail?.message ?? "The server could not complete the request.",
    correlationId: result.detail?.correlation_id ?? null,
  };
}

export function formatDateTime(value: string | null): string {
  return value === null ? "Not available" : new Date(value).toLocaleString();
}
