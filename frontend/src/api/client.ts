/**
 * API client for the Governix backend.
 *
 * - Unwraps the {success, data, error, request_id} envelope.
 * - The session lives in cookies the backend sets. The access and refresh tokens are
 *   HttpOnly, so this code never sees them; it reads only the CSRF cookie and echoes it
 *   in X-CSRF-Token on every unsafe request. Refreshes once on 401.
 * - Errors become ApiError; its `code` is the backend's stable `error.reason`
 *   (the body's `error.code` is just the HTTP status).
 */

const BASE = "/api";
const CSRF_COOKIE = "governix_csrf";
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);
// Their 401 means a wrong password or no session, never "refresh and retry".
const NO_REFRESH = ["/auth/login", "/auth/refresh", "/auth/logout"];

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details?: unknown,
    public requestId?: string,
  ) {
    super(message);
  }
}

let refreshing: Promise<boolean> | null = null;
let onSessionExpired: (() => void) | null = null;

export function setSessionExpiredHandler(handler: () => void) {
  onSessionExpired = handler;
}

function csrfToken(): string | null {
  const match = document.cookie.match(new RegExp(`(?:^|;\\s*)${CSRF_COOKIE}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : null;
}

/** Whether the browser holds a session: the CSRF cookie lives exactly as long as the refresh cookie. */
export function hasSession() {
  return csrfToken() !== null;
}

function withCsrf(init: RequestInit): Headers {
  const headers = new Headers(init.headers);
  const token = csrfToken();
  if (token && !SAFE_METHODS.has((init.method ?? "GET").toUpperCase())) headers.set("X-CSRF-Token", token);
  return headers;
}

async function reasonOf(response: Response): Promise<string | undefined> {
  const body = await response.clone().json().catch(() => null);
  return body?.error?.reason;
}

async function refresh(): Promise<boolean> {
  if (!hasSession()) return false;
  refreshing ??= (async () => {
    try {
      const response = await fetch(`${BASE}/auth/refresh`, { method: "POST", headers: withCsrf({ method: "POST" }) });
      if (response.ok) return true;
      // Another tab refreshed the shared session a moment earlier. Its new cookies are stored
      // (or about to be), so retrying the request is enough.
      if ((await reasonOf(response)) === "TOKEN_ROTATED") {
        await new Promise((resolve) => setTimeout(resolve, 500));
        return true;
      }
      return false;
    } catch {
      return false;
    } finally {
      setTimeout(() => (refreshing = null), 0);
    }
  })();
  return refreshing;
}

async function raw(path: string, init: RequestInit = {}, retry = true): Promise<Response> {
  const response = await fetch(`${BASE}${path}`, { ...init, headers: withCsrf(init) });
  if (response.status === 401 && retry && hasSession() && !NO_REFRESH.some((p) => path.startsWith(p))) {
    if (await refresh()) return raw(path, init, false);
    onSessionExpired?.();
  }
  // Another tab renewed the session between reading the CSRF cookie and sending: send it again.
  if (response.status === 403 && retry && (await reasonOf(response)) === "CSRF_FAILED") return raw(path, init, false);
  return response;
}

async function unwrap<T>(response: Response): Promise<T> {
  let body: any = null;
  try {
    body = await response.json();
  } catch {
    throw new ApiError(response.status, "BAD_RESPONSE", "The server returned an unexpected response.");
  }
  if (!response.ok || body?.success === false) {
    const error = body?.error ?? {};
    throw new ApiError(
      response.status,
      error.reason ?? "HTTP_ERROR",
      error.message ?? "Request failed.",
      error.details,
      body?.request_id,
    );
  }
  return body.data as T;
}

export type StreamHandler = (event: string, data: unknown) => void;

/**
 * POST that answers with server-sent events. Calls `onEvent` for each event as
 * it arrives and resolves when the stream ends. Non-2xx responses throw the
 * usual ApiError (rate limit, expired session, ...).
 */
async function stream(path: string, data: unknown, onEvent: StreamHandler, signal?: AbortSignal): Promise<void> {
  const response = await raw(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(data),
    signal,
  });
  if (!response.ok || !response.body) {
    await unwrap(response); // throws the backend's error
    throw new ApiError(response.status, "BAD_RESPONSE", "The server returned an unexpected response.");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const dispatch = (block: string) => {
    let event = "message";
    const lines: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) lines.push(line.slice(5).trimStart());
    }
    if (lines.length) onEvent(event, JSON.parse(lines.join("\n")));
  };
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let boundary: number;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      dispatch(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
    }
  }
  if (buffer.trim()) dispatch(buffer);
}

export interface UploadHandle<T> {
  promise: Promise<T>;
  abort: () => void;
}

/**
 * Multipart upload that reports progress (fetch cannot) and can be cancelled.
 * Refreshes the access token once on 401, like every other call.
 */
function uploadWithProgress<T>(path: string, form: FormData, onProgress: (fraction: number) => void): UploadHandle<T> {
  let xhr: XMLHttpRequest | null = null;
  let aborted = false;
  const send = (retry: boolean): Promise<T> =>
    new Promise<T>((resolve, reject) => {
      const request = new XMLHttpRequest();
      xhr = request;
      request.open("POST", `${BASE}${path}`);
      const csrf = csrfToken();
      if (csrf) request.setRequestHeader("X-CSRF-Token", csrf);
      request.upload.onprogress = (event) => event.lengthComputable && onProgress(event.loaded / event.total);
      request.onabort = () => reject(new ApiError(0, "ABORTED", "Upload cancelled."));
      request.onerror = () => reject(new ApiError(0, "NETWORK_ERROR", "The upload was interrupted."));
      request.onload = async () => {
        if (request.status === 401 && retry && hasSession() && (await refresh())) {
          if (aborted) return reject(new ApiError(0, "ABORTED", "Upload cancelled."));
          return send(false).then(resolve, reject);
        }
        unwrap<T>(new Response(request.responseText, { status: request.status })).then(resolve, reject);
      };
      request.send(form);
    });
  return {
    promise: send(true),
    abort: () => {
      aborted = true;
      xhr?.abort();
    },
  };
}

export const api = {
  stream,
  get: <T>(path: string) => raw(path).then(unwrap<T>),
  post: <T>(path: string, data?: unknown) =>
    raw(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: data === undefined ? undefined : JSON.stringify(data),
    }).then(unwrap<T>),
  patch: <T>(path: string, data: unknown) =>
    raw(path, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    }).then(unwrap<T>),
  put: <T>(path: string, data: unknown) =>
    raw(path, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    }).then(unwrap<T>),
  delete: <T>(path: string) => raw(path, { method: "DELETE" }).then(unwrap<T>),
  upload: <T>(path: string, form: FormData) => raw(path, { method: "POST", body: form }).then(unwrap<T>),
  uploadWithProgress,
  /** Authenticated binary download (e.g. page images) as an object URL. */
  blobUrl: async (path: string) => {
    const response = await raw(path);
    if (!response.ok) throw new ApiError(response.status, "HTTP_ERROR", "Could not load file.");
    return { url: URL.createObjectURL(await response.blob()), headers: response.headers };
  },
};

export function qs(params: Record<string, unknown>) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) value.forEach((v) => search.append(key, String(v)));
    else search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}
