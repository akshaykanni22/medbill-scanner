/**
 * frontend/src/utils/api.ts
 * ============================================================
 * PURPOSE:
 *   All HTTP calls to the MedBill Scanner backend.
 *   This is the only file that knows the API URL or makes fetch calls.
 *   Components never call fetch directly — they go through this module.
 *
 * EXPORTS:
 *   analyzeBill(file, signal?)  — POST /api/analyze, returns AnalysisResponse
 *   checkHealth()               — GET  /api/health,  returns HealthResponse
 *   ApiError                    — typed error class, thrown on non-200 responses
 *
 * ENVIRONMENT:
 *   REACT_APP_API_URL — base URL of the backend (no trailing slash).
 *   Defaults to http://localhost:8000 for local development.
 *   Set to the backend container URL in docker-compose.yml.
 *
 * MULTIPART NOTE:
 *   FastAPI reads the uploaded file from the multipart field named "file".
 *   That exact key is used in formData.append("file", ...) below.
 *   If you rename the backend parameter, update this file to match.
 * ============================================================
 */

import type {
  AnalysisResponse,
  ErrorResponse,
  HealthResponse,
} from '../types';

// ============================================================
// CONFIG
// ============================================================

// In Docker: REACT_APP_API_URL is baked in as '' (empty) at build time, so
// API calls use relative URLs (/api/...) and nginx proxy_pass routes them.
// In local dev (npm start, no env var set): REACT_APP_API_URL is undefined,
// so we fall back to http://localhost:8000 to hit the backend directly.
const _rawApiUrl = process.env.REACT_APP_API_URL;
const API_BASE_URL =
  _rawApiUrl !== undefined ? _rawApiUrl.replace(/\/$/, '') : 'http://localhost:8000';

// ============================================================
// ERROR CLASS
// ============================================================

/**
 * Thrown by all API functions on non-200 responses.
 *
 * WHY A CLASS (not plain Error):
 *   Callers need to distinguish API errors (known error codes like
 *   "rate_limit_exceeded", "file_too_large") from network errors
 *   (fetch threw, backend unreachable). instanceof ApiError lets
 *   components branch cleanly without string-matching error.message.
 *
 * USAGE:
 *   try {
 *     const result = await analyzeBill(file);
 *   } catch (err) {
 *     if (err instanceof ApiError) {
 *       // err.code  → "rate_limit_exceeded"
 *       // err.detail → "Too many requests…"
 *       // err.status → 429
 *     } else {
 *       // network failure, backend down, etc.
 *     }
 *   }
 */
export class ApiError extends Error {
  /** Machine-readable error code from ErrorResponse.error. */
  readonly code: string;
  /** Human-readable message from ErrorResponse.detail. */
  readonly detail: string;
  /** HTTP status code (413, 415, 422, 429, 500, etc.). */
  readonly status: number;

  constructor(status: number, errorResponse: ErrorResponse) {
    super(errorResponse.detail);
    this.name = 'ApiError';
    this.code = errorResponse.error;
    this.detail = errorResponse.detail;
    this.status = status;
  }
}

// ============================================================
// PRIVATE HELPERS
// ============================================================

/**
 * Parse a non-OK response into an ApiError.
 *
 * WHY TRY/CATCH around response.json():
 *   If the backend is behind a proxy that returns an HTML error page
 *   (e.g., nginx 502), response.json() will throw a SyntaxError.
 *   We fall back to a generic ApiError rather than crashing.
 */
async function parseErrorResponse(response: Response): Promise<ApiError> {
  try {
    const body = await response.json() as ErrorResponse;
    return new ApiError(response.status, body);
  } catch {
    return new ApiError(response.status, {
      error: 'unknown_error',
      detail: `Server returned ${response.status} ${response.statusText}`,
    });
  }
}

// ============================================================
// PUBLIC API
// ============================================================

/**
 * Upload a medical bill for analysis.
 *
 * WHAT:
 *   POSTs the file as multipart/form-data to /api/analyze.
 *   Returns the structured anomaly report on success.
 *   Throws ApiError on any non-200 response.
 *
 * MULTIPART KEY:
 *   The FormData field name is "file" — matches the FastAPI parameter
 *   `file: Annotated[UploadFile, File(...)]` in routes.py exactly.
 *   Do NOT change this key without updating the backend parameter name.
 *
 * WHY NOT set Content-Type manually:
 *   When using FormData, the browser sets Content-Type to
 *   "multipart/form-data; boundary=..." automatically, including
 *   the required boundary string. Setting it manually would omit
 *   the boundary and cause the backend to reject the request.
 *
 * @param file    The bill file selected by the user (PDF, JPEG, or PNG).
 * @param signal  Optional AbortSignal from an AbortController. When aborted,
 *                fetch throws a DOMException with name "AbortError". The hook
 *                (useBillAnalysis.ts) uses this to cancel in-flight requests
 *                when the user resets. Callers should check for AbortError
 *                before treating the throw as a real error.
 * @returns       AnalysisResponse with anomalies and dispute letter.
 * @throws        ApiError on HTTP error responses.
 * @throws        DOMException (name === "AbortError") if signal is aborted.
 * @throws        Error (network/fetch) if the backend is unreachable.
 */
export async function analyzeBill(
  file: File,
  signal?: AbortSignal,
): Promise<AnalysisResponse> {
  const formData = new FormData();
  // "file" must match the FastAPI parameter name in routes.py exactly.
  formData.append('file', file);

  const response = await fetch(`${API_BASE_URL}/api/analyze`, {
    method: 'POST',
    body: formData,
    signal,
    // WHY no Content-Type header: browser sets it automatically for FormData.
  });

  if (!response.ok) {
    throw await parseErrorResponse(response);
  }

  return response.json() as Promise<AnalysisResponse>;
}

/**
 * Check backend health and ChromaDB readiness.
 *
 * WHAT:
 *   GETs /api/health and returns connection + collection status.
 *   Useful for showing a warning banner if ingest.py hasn't been run
 *   (collection_size === 0) or ChromaDB is unreachable.
 *
 * WHY NOT throw on degraded/unavailable:
 *   'degraded' and 'unavailable' are valid HealthResponse values,
 *   not HTTP errors — the backend returns 200 with those status strings.
 *   The caller decides whether to show a warning or block uploads.
 *
 * @returns HealthResponse with status, chromadb_connected, collection_size.
 * @throws  ApiError on HTTP error responses.
 * @throws  Error (network/fetch) if the backend is unreachable.
 */
export async function checkHealth(): Promise<HealthResponse> {
  const response = await fetch(`${API_BASE_URL}/api/health`);

  if (!response.ok) {
    throw await parseErrorResponse(response);
  }

  return response.json() as Promise<HealthResponse>;
}
