import type { SafeError } from "./types";

export class ApiError extends Error {
  readonly code: string;
  readonly retryable: boolean;
  constructor(error: SafeError) {
    super(error.message);
    this.name = "ApiError";
    this.code = error.code;
    this.retryable = error.retryable;
  }
}

export async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...options,
      headers: { "Content-Type": "application/json", ...options.headers },
    });
  } catch {
    throw new ApiError({
      code: "CONNECTION_FAILED",
      message: "暂时无法连接服务，请检查本地服务后重试。",
      retryable: true,
    });
  }
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new ApiError({
      code: "INVALID_RESPONSE",
      message: "服务返回了无法读取的响应。",
      retryable: true,
    });
  }
  if (!response.ok) {
    const envelope = payload as { error?: Partial<SafeError> };
    const safe = envelope?.error;
    throw new ApiError({
      code:
        safe?.code && /^[A-Z0-9_]+$/.test(safe.code)
          ? safe.code
          : "REQUEST_FAILED",
      message:
        typeof safe?.message === "string"
          ? safe.message
          : "操作未能完成，请重试。",
      retryable: safe?.retryable === true,
    });
  }
  return payload as T;
}

export const post = <T>(path: string, body?: unknown) =>
  request<T>(path, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
