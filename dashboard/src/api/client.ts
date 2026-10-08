import { getStoredToken } from './auth';
import type { ApiErrorEnvelope } from './types';

export const DEFAULT_API_BASE = '/api/v1';

/** Typed API failure: HTTP status (0 for transport errors), machine code and human message. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

export type QueryValue = string | number | boolean | null | undefined;
export type Query = Record<string, QueryValue>;

export interface RequestOptions {
  query?: Query;
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export interface ApiClientOptions {
  /** Default `/api/v1` (the Vite dev server proxies `/api` to the control plane). */
  baseUrl?: string;
  getToken?: () => string | null | undefined;
  /** Called on every 401 before the request rejects (the app clears the token and shows /login). */
  onUnauthorized?: (error: ApiError) => void;
  /** Injected for tests and for the in-browser mock adapter. */
  fetchImpl?: typeof fetch;
}

export function isAbortError(error: unknown): boolean {
  return (
    typeof error === 'object' &&
    error !== null &&
    'name' in error &&
    (error as { name?: unknown }).name === 'AbortError'
  );
}

function isEnvelope(body: unknown): body is ApiErrorEnvelope {
  if (typeof body !== 'object' || body === null || !('error' in body)) return false;
  const error = (body as { error: unknown }).error;
  return typeof error === 'object' && error !== null && 'message' in error;
}

function detailMessage(detail: unknown): string | null {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    const parts = detail
      .map((item) =>
        typeof item === 'object' && item !== null && 'msg' in item ? String((item as { msg: unknown }).msg) : null,
      )
      .filter((part): part is string => Boolean(part));
    return parts.length ? parts.join('; ') : null;
  }
  return null;
}

async function toApiError(response: Response): Promise<ApiError> {
  const fallback = response.statusText || `Request failed with HTTP ${response.status}`;
  let body: unknown;
  try {
    const text = await response.text();
    body = text ? JSON.parse(text) : undefined;
  } catch {
    body = undefined;
  }
  if (isEnvelope(body)) {
    return new ApiError(response.status, body.error.code || `http_${response.status}`, body.error.message || fallback);
  }
  if (typeof body === 'object' && body !== null && 'detail' in body) {
    const message = detailMessage((body as { detail: unknown }).detail);
    return new ApiError(response.status, `http_${response.status}`, message ?? fallback);
  }
  return new ApiError(response.status, `http_${response.status}`, fallback);
}

export class ApiClient {
  readonly baseUrl: string;
  private readonly getToken: () => string | null | undefined;
  private readonly onUnauthorized?: (error: ApiError) => void;
  private readonly fetchImpl: typeof fetch;

  constructor(options: ApiClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? DEFAULT_API_BASE).replace(/\/+$/, '');
    this.getToken = options.getToken ?? (() => null);
    this.onUnauthorized = options.onUnauthorized;
    this.fetchImpl = options.fetchImpl ?? ((input, init) => globalThis.fetch(input, init));
  }

  url(path: string, query?: Query): string {
    const normalized = path.startsWith('/') ? path : `/${path}`;
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query ?? {})) {
      if (value === undefined || value === null || value === '') continue;
      params.append(key, String(value));
    }
    const qs = params.toString();
    return `${this.baseUrl}${normalized}${qs ? `?${qs}` : ''}`;
  }

  /** Authenticated fetch. Throws ApiError for transport failures and non-2xx responses. */
  async fetchRaw(path: string, init: RequestInit & { query?: Query } = {}): Promise<Response> {
    const { query, headers: extraHeaders, ...rest } = init;
    const headers = new Headers(extraHeaders);
    const token = this.getToken();
    if (token) headers.set('Authorization', `Bearer ${token}`);

    let response: Response;
    try {
      response = await this.fetchImpl(this.url(path, query), { ...rest, headers });
    } catch (error) {
      if (isAbortError(error)) throw error;
      const message = error instanceof Error && error.message ? error.message : 'Network request failed';
      throw new ApiError(0, 'network_error', message);
    }

    if (!response.ok) {
      const error = await toApiError(response);
      if (error.status === 401) this.onUnauthorized?.(error);
      throw error;
    }
    return response;
  }

  async request<T>(method: string, path: string, body?: unknown, options: RequestOptions = {}): Promise<T> {
    const headers = new Headers(options.headers);
    headers.set('Accept', 'application/json');
    const init: RequestInit & { query?: Query } = { method, headers, signal: options.signal, query: options.query };
    if (body !== undefined) {
      headers.set('Content-Type', 'application/json');
      init.body = JSON.stringify(body);
    }

    const response = await this.fetchRaw(path, init);
    if (response.status === 204) return undefined as T;
    const text = await response.text();
    if (!text) return undefined as T;
    try {
      return JSON.parse(text) as T;
    } catch {
      throw new ApiError(response.status, 'invalid_response', 'The server returned a response that is not JSON.');
    }
  }

  get<T>(path: string, options?: RequestOptions): Promise<T> {
    return this.request<T>('GET', path, undefined, options);
  }

  post<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> {
    return this.request<T>('POST', path, body, options);
  }

  put<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> {
    return this.request<T>('PUT', path, body, options);
  }

  patch<T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> {
    return this.request<T>('PATCH', path, body, options);
  }

  delete<T = void>(path: string, options?: RequestOptions): Promise<T> {
    return this.request<T>('DELETE', path, undefined, options);
  }
}

let defaultClient: ApiClient | null = null;

/** The app-wide client (configured in main.tsx); lazily created with the stored token otherwise. */
export function getDefaultClient(): ApiClient {
  defaultClient ??= new ApiClient({ getToken: getStoredToken });
  return defaultClient;
}

export function setDefaultClient(client: ApiClient): void {
  defaultClient = client;
}

/** A human sentence for any thrown value. */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 0) return `Cannot reach the control plane (${error.message}).`;
    return error.message;
  }
  if (error instanceof Error) return error.message;
  return 'Unexpected error.';
}
