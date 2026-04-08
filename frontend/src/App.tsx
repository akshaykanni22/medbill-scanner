/**
 * frontend/src/App.tsx
 * ============================================================
 * PURPOSE:
 *   Root component. Owns the top-level layout and wires together
 *   all child components via the useBillAnalysis hook.
 *
 * LAYOUT (single page, no routing):
 *   idle/error  → BillUploader
 *   uploading   → LoadingSpinner (uploading)
 *   analyzing   → LoadingSpinner (analyzing)
 *   done        → AnomalyReport + DisputeLetter (side by side on wide screens)
 *
 * STATE OWNED HERE:
 *   highlightCodes — Set of HCPCS codes currently hovered in DisputeLetter.
 *                    Passed to AnomalyReport to ring-highlight matching cards.
 *                    Lifted here (not in DisputeLetter) because it must be
 *                    shared between DisputeLetter and AnomalyReport siblings.
 *
 * HEALTH CHECK:
 *   On mount, calls checkHealth() and shows a warning banner if ChromaDB
 *   is unavailable or the HCPCS collection is empty (ingest.py not run).
 *   This catches the most common setup mistake before the user uploads.
 * ============================================================
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';

import { checkHealth } from './utils/api';
import { useBillAnalysis } from './hooks/useBillAnalysis';
import { BillUploader } from './components/BillUploader';
import { LoadingSpinner } from './components/LoadingSpinner';
import { AnomalyReport } from './components/AnomalyReport';
import { DisputeLetter } from './components/DisputeLetter';
import type { HealthResponse } from './types';
import { ApiError } from './utils/api';

// ============================================================
// TYPES
// ============================================================

type HealthStatus = 'checking' | 'ok' | 'degraded' | 'unavailable' | 'unknown';

// ============================================================
// SUB-COMPONENTS
// ============================================================

/** Banner shown when ChromaDB is degraded or unavailable. */
function HealthWarningBanner({
  health,
}: {
  health: HealthResponse;
}): React.ReactElement | null {
  if (health.status === 'ok') return null;

  const isDegraded = health.status === 'degraded';

  return (
    <div
      role="alert"
      className={`mb-4 rounded-lg border px-4 py-3 text-sm ${
        isDegraded
          ? 'border-yellow-300 bg-yellow-50 text-yellow-800'
          : 'border-red-300 bg-red-50 text-red-800'
      }`}
      data-testid="health-warning"
    >
      {isDegraded ? (
        <>
          <strong>Setup needed:</strong> The HCPCS reference database is empty.
          Price comparisons won't work until you run{' '}
          <code className="rounded bg-yellow-100 px-1">python scripts/download_cms_data.py</code>
          {' '}and{' '}
          <code className="rounded bg-yellow-100 px-1">python -m backend.rag.ingest</code>.
        </>
      ) : (
        <>
          <strong>Backend unavailable:</strong> Cannot reach the analyzer.
          Make sure Docker is running:{' '}
          <code className="rounded bg-red-100 px-1">docker-compose up</code>
        </>
      )}
    </div>
  );
}

/** Error card shown when analysis fails. */
function ErrorCard({
  error,
  onReset,
}: {
  error: Error;
  onReset: () => void;
}): React.ReactElement {
  // ApiError sets this.name = 'ApiError' in its constructor, so we check by name.
  // WHY NOT duck-typing ('code' in error): DOMException also has a .code property,
  // which would cause false positives and show the wrong error message.
  const isApiError = error instanceof ApiError;
  const message = isApiError
    ? (error as ApiError).detail
    : 'Could not connect to the analyzer. Is Docker running?';

  return (
    <div
      role="alert"
      className="rounded-xl border-2 border-red-300 bg-red-50 p-6 text-center"
      data-testid="error-card"
    >
      <svg
        className="mx-auto mb-3 h-10 w-10 text-red-400"
        xmlns="http://www.w3.org/2000/svg"
        fill="none"
        viewBox="0 0 24 24"
        strokeWidth={1.5}
        stroke="currentColor"
        aria-hidden="true"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z"
        />
      </svg>
      <p className="font-semibold text-red-800">Analysis failed</p>
      <p className="mt-1 text-sm text-red-700">{message}</p>
      <button
        type="button"
        onClick={onReset}
        className="mt-4 rounded-lg bg-red-600 px-4 py-2 text-sm font-semibold text-white
                   hover:bg-red-700 focus:outline-none focus:ring-2 focus:ring-red-500
                   focus:ring-offset-2"
      >
        Try again
      </button>
    </div>
  );
}

// ============================================================
// MAIN APP
// ============================================================

export default function App(): React.ReactElement {
  const { analyze, result, status, error, reset } = useBillAnalysis();

  // Cross-highlight state: codes hovered in DisputeLetter → ringed in AnomalyReport
  const [highlightCodes, setHighlightCodes] = useState<ReadonlySet<string> | null>(null);

  // Health check state
  const [healthStatus, setHealthStatus] = useState<HealthStatus>('checking');
  const [healthData, setHealthData] = useState<HealthResponse | null>(null);

  // Track if health check ran (prevent double-run in StrictMode dev double-effect)
  const healthCheckedRef = useRef(false);

  // ---- Health check on mount ----

  useEffect(() => {
    if (healthCheckedRef.current) return;
    healthCheckedRef.current = true;

    checkHealth()
      .then((data) => {
        setHealthData(data);
        setHealthStatus(data.status);
      })
      .catch(() => {
        setHealthStatus('unavailable');
        setHealthData({ status: 'unavailable', chromadb_connected: false, collection_size: 0 });
      });
  }, []);

  // ---- Handlers ----

  const handleUpload = useCallback(
    (file: File) => {
      analyze(file);
    },
    [analyze],
  );

  const handleCodesHover = useCallback((codes: ReadonlySet<string> | null) => {
    setHighlightCodes(codes);
  }, []);

  const handleReset = useCallback(() => {
    reset();
    setHighlightCodes(null);
  }, [reset]);

  // ---- Render ----

  return (
    <div className="min-h-screen bg-gray-100">
      {/* Page header */}
      <header className="bg-white shadow-sm">
        <div className="mx-auto max-w-5xl px-4 py-4 sm:px-6">
          <div className="flex items-center gap-3">
            <svg
              className="h-7 w-7 text-blue-600"
              xmlns="http://www.w3.org/2000/svg"
              fill="none"
              viewBox="0 0 24 24"
              strokeWidth={1.5}
              stroke="currentColor"
              aria-hidden="true"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z"
              />
            </svg>
            <div>
              <h1 className="text-lg font-bold text-gray-900">MedBill Scanner</h1>
              <p className="text-xs text-gray-500">Free medical bill anomaly detector</p>
            </div>
          </div>
        </div>
      </header>

      {/* Main content */}
      <main className="mx-auto max-w-5xl px-4 py-8 sm:px-6">
        {/* Health warning banner */}
        {healthData && healthStatus !== 'ok' && healthStatus !== 'checking' && (
          <HealthWarningBanner health={healthData} />
        )}

        {/* Upload / loading phase */}
        {(status === 'idle' || status === 'error') && (
          <div className="mx-auto max-w-lg">
            <div className="mb-6 text-center">
              <h2 className="text-xl font-semibold text-gray-900">
                Check your medical bill for overcharges
              </h2>
              <p className="mt-1 text-sm text-gray-500">
                Upload a PDF or image. Patient info is stripped before AI review.
              </p>
            </div>

            {status === 'error' && error && (
              <ErrorCard error={error} onReset={handleReset} />
            )}

            {/* disabled is always false here since BillUploader only renders when status==='idle',
                but kept for clarity in case this render condition changes. */}
            {status === 'idle' && (
              <BillUploader
                onUpload={handleUpload}
                disabled={status !== 'idle'}
              />
            )}
          </div>
        )}

        {(status === 'uploading' || status === 'analyzing') && (
          <LoadingSpinner status={status} />
        )}

        {/* Results phase */}
        {status === 'done' && result && (
          <div>
            {/* Results header with reset button */}
            <div className="mb-6 flex items-center justify-between">
              <div>
                <h2 className="text-xl font-semibold text-gray-900">Analysis complete</h2>
                <p className="text-xs text-gray-500">
                  Processed in {result.processing_time_seconds.toFixed(1)}s
                </p>
              </div>
              <button
                type="button"
                onClick={handleReset}
                className="rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm
                           font-medium text-gray-700 hover:bg-gray-50 focus:outline-none
                           focus:ring-2 focus:ring-blue-500 focus:ring-offset-2"
              >
                Analyze another bill
              </button>
            </div>

            {/* Two-column layout on wide screens */}
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
              <AnomalyReport
                anomalies={result.anomalies}
                billSummary={result.bill_summary}
                highlightCodes={highlightCodes ?? undefined}
              />

              {result.dispute_letter ? (
                <DisputeLetter
                  letter={result.dispute_letter}
                  onCodesHover={handleCodesHover}
                />
              ) : (
                <div className="rounded-xl border border-gray-200 bg-white p-6 text-center text-sm text-gray-500">
                  No dispute letter generated — no anomalies were found.
                </div>
              )}
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
