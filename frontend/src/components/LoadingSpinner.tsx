/**
 * frontend/src/components/LoadingSpinner.tsx
 * ============================================================
 * PURPOSE:
 *   Displays an animated spinner with a status message during
 *   bill analysis. Renders different messages for 'uploading'
 *   vs 'analyzing' so the user knows what phase they're in.
 *
 * PROPS:
 *   status — 'uploading' | 'analyzing'
 *            Passed from useBillAnalysis hook via App.tsx.
 *            Only rendered when status is one of these two values.
 * ============================================================
 */

import React from 'react';
import type { AnalysisStatus } from '../hooks/useBillAnalysis';

// ============================================================
// TYPES
// ============================================================

interface LoadingSpinnerProps {
  /**
   * Current analysis phase. Only 'uploading' and 'analyzing' are
   * expected — caller should not render this component for other statuses.
   */
  status: Extract<AnalysisStatus, 'uploading' | 'analyzing'>;
}

// ============================================================
// COPY
// ============================================================

const STATUS_COPY: Record<
  Extract<AnalysisStatus, 'uploading' | 'analyzing'>,
  { heading: string; subtext: string }
> = {
  uploading: {
    heading: 'Uploading your bill…',
    subtext: 'Sending your file securely to the analyzer.',
  },
  analyzing: {
    heading: 'Analyzing your bill…',
    subtext:
      'Our AI is reviewing each charge against Medicare reference rates. This usually takes under 30 seconds.',
  },
};

// ============================================================
// COMPONENT
// ============================================================

/**
 * Animated spinner shown during uploading and analyzing phases.
 *
 * WHY TWO MESSAGES:
 *   'uploading' and 'analyzing' feel very different to the user.
 *   Showing "Analyzing…" the moment they click submit (before the
 *   upload even finishes on a slow connection) would be misleading.
 *   The hook transitions between the two states via a timer, and
 *   this component renders the appropriate copy for each.
 *
 * ACCESSIBILITY:
 *   role="status" announces the status message to screen readers when
 *   it changes (upload → analyze transition). role="status" implies
 *   aria-live="polite" per the ARIA spec — no explicit attribute needed.
 *   aria-hidden="true" on the spinner SVG marks it as decorative so screen
 *   readers skip the SVG and read the text instead.
 */
export function LoadingSpinner({ status }: LoadingSpinnerProps): React.ReactElement {
  const { heading, subtext } = STATUS_COPY[status] ?? { heading: 'Loading…', subtext: '' };

  return (
    <div
      className="flex flex-col items-center justify-center gap-6 py-16"
      role="status"
      data-testid="loading-spinner"
    >
      {/* Spinner SVG */}
      <svg
        className="h-14 w-14 animate-spin text-blue-600"
        xmlns="http://www.w3.org/2000/svg"
        fill="none"
        viewBox="0 0 24 24"
        aria-hidden="true"
      >
        <circle
          className="opacity-25"
          cx="12"
          cy="12"
          r="10"
          stroke="currentColor"
          strokeWidth="4"
        />
        <path
          className="opacity-75"
          fill="currentColor"
          d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
        />
      </svg>

      {/* Status text */}
      <div className="text-center">
        <p className="text-lg font-semibold text-gray-800">{heading}</p>
        <p className="mt-1 text-sm text-gray-500">{subtext}</p>
      </div>
    </div>
  );
}
