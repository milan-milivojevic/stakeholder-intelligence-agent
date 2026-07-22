import type { ApiErrorResponse } from "./contracts";
import { ContractError, parseApiErrorResponse } from "./validation";

const apiPrefix = "/api/v1/";
const csrfHeaderName = "X-Stakeholder-CSRF";
const csrfHeaderValue = "1";

type ResponseParser<T> = (value: unknown) => T;

export type ApiResult<T> =
  { ok: true; value: T } | { ok: false; status: number; detail: ApiErrorResponse["error"] | null };

export class ApiRequestError extends Error {
  readonly status: number;
  readonly detail: ApiErrorResponse["error"] | null;

  constructor(status: number, detail: ApiErrorResponse["error"] | null) {
    super(detail?.message ?? "The server could not complete the request.");
    this.name = "ApiRequestError";
    this.status = status;
    this.detail = detail;
  }
}

export interface ApiRequestFailure {
  status: number;
  detail: ApiErrorResponse["error"] | null;
}

export function apiRequestFailure(error: unknown): ApiRequestFailure | null {
  if (typeof error !== "object" || error === null) {
    return null;
  }
  const candidate = error as Record<string, unknown>;
  if (
    candidate.name !== "ApiRequestError" ||
    typeof candidate.status !== "number" ||
    !Number.isInteger(candidate.status) ||
    candidate.status < 100 ||
    candidate.status > 599
  ) {
    return null;
  }
  const detail = candidate.detail === null ? null : safeErrorDetail({ error: candidate.detail });
  return { status: candidate.status, detail };
}

function requireApprovedPath(path: string): void {
  if (!path.startsWith(apiPrefix) || path.startsWith("//") || path.includes("://")) {
    throw new TypeError("API requests must use an approved same-origin path.");
  }
}

function isMutation(method: string): boolean {
  return method !== "GET" && method !== "HEAD" && method !== "OPTIONS";
}

function safeErrorDetail(value: unknown): ApiErrorResponse["error"] | null {
  try {
    return parseApiErrorResponse(value).error;
  } catch {
    return null;
  }
}

async function readJson(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.toLowerCase().includes("application/json")) {
    throw new ContractError("The server returned an unexpected content type.");
  }
  return (await response.json()) as unknown;
}

async function safeErrorDetailFromResponse(
  response: Response,
): Promise<ApiErrorResponse["error"] | null> {
  try {
    return safeErrorDetail(await readJson(response));
  } catch {
    return null;
  }
}

export class ApiClient {
  readonly #fetch: typeof fetch;

  constructor(fetchImplementation: typeof fetch = fetch) {
    this.#fetch = fetchImplementation;
  }

  async json<T>(path: string, init: RequestInit, parser: ResponseParser<T>): Promise<T> {
    const result = await this.result(path, init, parser);
    if (!result.ok) {
      throw new ApiRequestError(result.status, result.detail);
    }
    return result.value;
  }

  async result<T>(
    path: string,
    init: RequestInit,
    parser: ResponseParser<T>,
  ): Promise<ApiResult<T>> {
    const response = await this.request(path, init);
    if (!response.ok) {
      return {
        ok: false,
        status: response.status,
        detail: await safeErrorDetailFromResponse(response),
      };
    }
    const value = await readJson(response);
    return { ok: true, value: parser(value) };
  }

  async stream(path: string, init: RequestInit): Promise<ReadableStream<Uint8Array>> {
    const response = await this.request(path, init);
    if (!response.ok) {
      throw new ApiRequestError(response.status, await safeErrorDetailFromResponse(response));
    }
    const contentType = response.headers.get("content-type") ?? "";
    if (!contentType.toLowerCase().includes("text/event-stream") || response.body === null) {
      throw new ContractError("The server did not return an approved event stream.");
    }
    return response.body;
  }

  async request(path: string, init: RequestInit = {}): Promise<Response> {
    requireApprovedPath(path);
    const method = (init.method ?? "GET").toUpperCase();
    const headers = new Headers(init.headers);
    headers.set(
      "Accept",
      init.headers === undefined
        ? "application/json"
        : (headers.get("Accept") ?? "application/json"),
    );
    if (isMutation(method)) {
      headers.set(csrfHeaderName, csrfHeaderValue);
    }
    headers.delete("Authorization");
    return await this.#fetch.call(globalThis, path, {
      ...init,
      method,
      headers,
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
    });
  }
}
