import type { ApiEnvelope } from "./types";

export class ApiError extends Error {
  code: string;
  data: unknown;

  constructor(code: string, message: string, data?: unknown) {
    super(message);
    this.code = code;
    this.data = data;
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...(init?.headers ?? {})
    }
  });
  let payload: ApiEnvelope<T>;
  try {
    payload = (await response.json()) as ApiEnvelope<T>;
  } catch {
    throw new ApiError(`http_${response.status}`, `서버 응답을 읽지 못했습니다. (HTTP ${response.status})`);
  }
  if (!response.ok || !payload.ok) {
    throw new ApiError(
      payload.error?.code ?? `http_${response.status}`,
      payload.error?.message ?? "요청에 실패했습니다.",
      payload.data
    );
  }
  return payload.data;
}

export function postJson<T>(path: string, body: unknown): Promise<T> {
  return api<T>(path, { method: "POST", body: JSON.stringify(body) });
}
