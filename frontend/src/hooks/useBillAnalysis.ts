/**
 * frontend/src/hooks/useBillAnalysis.ts
 * ============================================================
 * PURPOSE:
 *   Custom React hook that owns all state for the bill analysis
 *   workflow: idle → uploading → analyzing → done (or error).
 *
 *   Components never call the API directly — they call this hook
 *   and render whatever state it exposes. This keeps API logic,
 *   loading state, and error handling out of component JSX.
 *
 * USAGE:
 *   const { analyze, result, status, error, reset } = useBillAnalysis();
 *
 *   analyze(file)  — start analysis, updates status automatically
 *   result         — AnalysisResponse | null
 *   status         — 'idle' | 'uploading' | 'analyzing' | 'done' | 'error'
 *   error          — ApiError | Error | null
 *   reset()        — cancel any in-flight request, return to idle
 *
 * STATE MACHINE:
 *   idle ──analyze()──► uploading ──► analyzing ──► done
 *     ▲                    │               │
 *     └────────reset()─────┴───────────────┴──► error
 *                                               │
 *                                         reset()──► idle
 *
 * WHY TWO STATUSES (uploading vs analyzing):
 *   The upload (multipart POST) and the analysis (LLM + RAG) are
 *   both part of the same HTTP request, but users perceive them
 *   differently. "Uploading" = waiting for bytes to transfer.
 *   "Analyzing" = waiting for AI to process. Showing both gives
 *   the user a more accurate sense of progress and reduces anxiety
 *   on slow connections or large files.
 *
 *   Implementation: we switch from 'uploading' to 'analyzing' after
 *   a short delay (ANALYZING_DELAY_MS) since we have no server-sent
 *   progress events in MVP. Post-MVP: replace with a real progress
 *   stream from the backend.
 *
 * STALE-CALLBACK PROTECTION (generation counter):
 *   If reset() is called while a request is in flight, the in-flight
 *   promise still runs to completion internally. Without protection,
 *   its setStatus('done') would overwrite the reset idle state.
 *   We assign a generation number at call time and check it before
 *   applying any state updates after await returns.
 *
 * CANCELLATION (AbortController):
 *   reset() aborts the underlying fetch so the backend request is
 *   cancelled, freeing the rate-limit slot and TCP connection.
 *   AbortError is silently swallowed — it is not a real error.
 * ============================================================
 */

import { useCallback, useRef, useState } from 'react';

import { analyzeBill, ApiError } from '../utils/api';
import type { AnalysisResponse } from '../types';

// ============================================================
// TYPES
// ============================================================

export type AnalysisStatus =
  | 'idle'
  | 'uploading'
  | 'analyzing'
  | 'done'
  | 'error';

export interface UseBillAnalysisResult {
  /** Start analyzing a file. No-op if a request is already in progress. */
  analyze: (file: File) => Promise<void>;
  /** The analysis result. null until status is 'done'. */
  result: AnalysisResponse | null;
  /** Current workflow status. */
  status: AnalysisStatus;
  /**
   * Error from the last failed analysis.
   * ApiError for known API errors (rate limit, bad file, etc.).
   * plain Error for network failures.
   * null when status is not 'error'.
   */
  error: ApiError | Error | null;
  /** Cancel any in-flight request and return to idle. */
  reset: () => void;
}

// ============================================================
// CONSTANTS
// ============================================================

/**
 * Milliseconds after starting the request before switching the
 * displayed status from 'uploading' to 'analyzing'.
 *
 * WHY 1500ms: fast enough to feel responsive on a local connection
 * (where upload completes in <100ms), slow enough to be visible.
 * On a slow connection the upload itself takes longer and the user
 * sees 'uploading' naturally for longer before this fires.
 */
const ANALYZING_DELAY_MS = 1500;

// ============================================================
// HOOK
// ============================================================

/**
 * Manages the full lifecycle of a single bill analysis request.
 *
 * WHAT:
 *   Wraps analyzeBill() from api.ts with React state.
 *   Exposes a simple status machine to components so they only
 *   need to render, not manage async logic.
 *
 * REFS USED:
 *   generationRef  — incremented on every analyze()/reset() call.
 *                    Callbacks check their captured generation against
 *                    the current value before applying state updates,
 *                    preventing stale in-flight callbacks from
 *                    overwriting a reset.
 *   abortCtrlRef   — holds the AbortController for the current fetch.
 *                    reset() calls .abort() on it to cancel the request.
 *   timerRef       — holds the uploading→analyzing transition timer.
 *                    Cancelled on completion, error, and reset().
 */
export function useBillAnalysis(): UseBillAnalysisResult {
  const [status, setStatus] = useState<AnalysisStatus>('idle');
  const [result, setResult] = useState<AnalysisResponse | null>(null);
  const [error, setError] = useState<ApiError | Error | null>(null);

  const generationRef = useRef(0);
  const abortCtrlRef = useRef<AbortController | null>(null);
  // WHY number | null with window.setTimeout (not ReturnType<typeof setTimeout>):
  //   ReturnType<typeof setTimeout> resolves to NodeJS.Timeout when @types/node
  //   is installed (common in CRA/Vite), causing a type mismatch with clearTimeout.
  //   window.setTimeout always returns number in browser context.
  const timerRef = useRef<number | null>(null);

  const analyze = useCallback(async (file: File): Promise<void> => {
    // Guard: ignore if already in progress.
    if (abortCtrlRef.current !== null) return;

    // Capture generation at call time. Any state updates after await must
    // compare against this value before applying — if reset() fired while
    // we were waiting, the generation will have advanced and we bail out.
    const generation = ++generationRef.current;

    const controller = new AbortController();
    abortCtrlRef.current = controller;

    setStatus('uploading');
    setResult(null);
    setError(null);

    // Schedule uploading → analyzing transition.
    timerRef.current = window.setTimeout(() => {
      // Only advance if we're still in uploading and generation is still ours.
      if (generationRef.current === generation) {
        setStatus((prev) => (prev === 'uploading' ? 'analyzing' : prev));
      }
    }, ANALYZING_DELAY_MS);

    try {
      const response = await analyzeBill(file, controller.signal);

      // Cancel the transition timer — request completed before it fired.
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }

      // Bail if reset() was called while we were awaiting.
      if (generationRef.current !== generation) return;

      setResult(response);
      setStatus('done');
    } catch (err) {
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }

      // Bail if reset() fired — this catch is from the aborted fetch.
      if (generationRef.current !== generation) return;

      // Silently swallow AbortError — it means reset() cancelled the fetch.
      // We check instanceof Error (not DOMException) because older Safari and
      // some test runners throw a plain Error with name === 'AbortError' instead
      // of a DOMException. DOMException extends Error in modern browsers, so
      // instanceof Error still matches DOMException abort errors correctly while
      // also catching the plain-Error variant.
      if (err instanceof Error && err.name === 'AbortError') return;

      setError(err instanceof Error ? err : new Error(String(err)));
      setStatus('error');
    } finally {
      // Only clear the controller ref if this generation still owns it.
      if (generationRef.current === generation) {
        abortCtrlRef.current = null;
      }
    }
  }, []); // [] is safe: only closes over stable refs, state setters, and a module import.

  const reset = useCallback((): void => {
    // Advance generation — invalidates any in-flight callbacks.
    generationRef.current += 1;

    // Cancel the fetch if one is in flight.
    if (abortCtrlRef.current !== null) {
      abortCtrlRef.current.abort();
      abortCtrlRef.current = null;
    }

    // Cancel the uploading→analyzing transition timer.
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }

    setStatus('idle');
    setResult(null);
    setError(null);
  }, []); // [] is safe: only closes over stable refs and state setters.

  return { analyze, result, status, error, reset };
}
