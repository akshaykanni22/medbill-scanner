/**
 * frontend/src/components/AnomalyReport.tsx
 * ============================================================
 * PURPOSE:
 *   Displays the list of billing anomalies detected by the agent.
 *   Shows a summary header (total charges reviewed, potential
 *   overcharge amount) and an expandable card for each anomaly.
 *
 * PROPS:
 *   anomalies    — list of Anomaly objects, ordered HIGH → INFO by backend
 *   billSummary  — aggregate stats for the summary header
 *   highlightCodes — optional set of HCPCS codes to highlight
 *                    (passed from DisputeLetter when user hovers a code)
 *
 * EMPTY STATE:
 *   When anomalies is empty, renders a "clean bill" success message.
 *   This is a real outcome — not every bill has issues.
 * ============================================================
 */

import React, { useState } from 'react';
import type { Anomaly, AnomalySeverity, AnomalyType, BillSummary } from '../types';

// ============================================================
// DISPLAY MAPS
// ============================================================

const SEVERITY_STYLES: Record<
  AnomalySeverity,
  { badge: string; border: string; label: string }
> = {
  high:   { badge: 'bg-red-100 text-red-800',    border: 'border-red-300',   label: 'High'   },
  medium: { badge: 'bg-orange-100 text-orange-800', border: 'border-orange-300', label: 'Medium' },
  low:    { badge: 'bg-yellow-100 text-yellow-800', border: 'border-yellow-300', label: 'Low'    },
  info:   { badge: 'bg-blue-100 text-blue-800',   border: 'border-blue-300',  label: 'Info'   },
};

const ANOMALY_TYPE_LABELS: Record<AnomalyType, string> = {
  price_overcharge: 'Price Overcharge',
  duplicate_charge: 'Duplicate Charge',
  unbundling:       'Unbundling',
  upcoding:         'Upcoding',
  unknown_code:     'Unknown Code',
};

// ============================================================
// TYPES
// ============================================================

interface AnomalyReportProps {
  anomalies: Anomaly[];
  billSummary: BillSummary;
  /** HCPCS codes to highlight — cross-reference with DisputeLetter. */
  highlightCodes?: ReadonlySet<string>;
}

// ============================================================
// HELPERS
// ============================================================

function formatUsd(amount: number): string {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
  }).format(amount);
}

// ============================================================
// SUB-COMPONENTS
// ============================================================

/** @internal — exported for testing only */
/** Summary bar shown at the top of the report. */
export function SummaryBar({ summary }: { summary: BillSummary }): React.ReactElement {
  return (
    <div
      className="mb-6 grid grid-cols-2 gap-4 rounded-xl bg-gray-50 p-4 sm:grid-cols-4"
      data-testid="summary-bar"
    >
      <div className="text-center">
        <p className="text-2xl font-bold text-gray-900">{summary.total_line_items}</p>
        <p className="text-xs text-gray-500">Charges reviewed</p>
      </div>
      <div className="text-center">
        <p className="text-2xl font-bold text-gray-900">{summary.anomaly_count}</p>
        <p className="text-xs text-gray-500">Issues found</p>
      </div>
      <div className="text-center">
        <p className="text-2xl font-bold text-red-600">{summary.high_severity_count}</p>
        <p className="text-xs text-gray-500">High severity</p>
      </div>
      <div className="text-center">
        <p className="text-2xl font-bold text-orange-600">
          {summary.potential_overcharge_total !== null
            ? formatUsd(summary.potential_overcharge_total)
            : 'N/A'}
        </p>
        <p className="text-xs text-gray-500">Potential overcharge</p>
      </div>
    </div>
  );
}

interface AnomalyCardProps {
  anomaly: Anomaly;
  isHighlighted: boolean;
  index: number;
}

/** @internal — exported for testing only */
/** Expandable card for a single anomaly. */
export function AnomalyCard({
  anomaly,
  isHighlighted,
  index,
}: AnomalyCardProps): React.ReactElement {
  const [isExpanded, setIsExpanded] = useState(false);
  const styles = SEVERITY_STYLES[anomaly.severity];
  const { line_item } = anomaly;

  return (
    <div
      className={[
        'rounded-xl border-2 bg-white transition-shadow',
        styles.border,
        isHighlighted ? 'ring-2 ring-blue-400 ring-offset-2' : '',
      ]
        .filter(Boolean)
        .join(' ')}
      data-testid="anomaly-card"
      data-code={line_item.code ?? undefined}
    >
      {/* Card header — always visible */}
      <button
        type="button"
        className="flex w-full items-start justify-between gap-4 p-4 text-left"
        onClick={() => setIsExpanded((prev) => !prev)}
        aria-expanded={isExpanded}
        aria-controls={`anomaly-detail-${index}`}
        aria-label={`${line_item.code ? line_item.code + ' — ' : ''}${ANOMALY_TYPE_LABELS[anomaly.anomaly_type]}, ${isExpanded ? 'collapse' : 'expand'}`}
      >
        <div className="flex-1 min-w-0">
          {/* Code + description */}
          <div className="flex flex-wrap items-center gap-2">
            {line_item.code && (
              <span className="font-mono text-sm font-semibold text-gray-700">
                {line_item.code}
              </span>
            )}
            <span className="truncate text-sm text-gray-800">{line_item.description}</span>
          </div>

          {/* Badges row */}
          <div className="mt-2 flex flex-wrap gap-2">
            <span className={`rounded-full px-2 py-0.5 text-xs font-semibold ${styles.badge}`}>
              {styles.label}
            </span>
            <span className="rounded-full bg-gray-100 px-2 py-0.5 text-xs text-gray-600">
              {ANOMALY_TYPE_LABELS[anomaly.anomaly_type]}
            </span>
          </div>
        </div>

        {/* Billed amount + overcharge ratio */}
        <div className="shrink-0 text-right">
          {line_item.billed_amount !== null ? (
            <p className="text-sm font-semibold text-gray-900">
              {formatUsd(line_item.billed_amount)}
            </p>
          ) : (
            <p className="text-sm text-gray-400">Amount N/A</p>
          )}
          {anomaly.overcharge_ratio !== null && anomaly.overcharge_ratio > 1 && (
            <p className="text-xs text-red-600">
              {anomaly.overcharge_ratio.toFixed(1)}× Medicare rate
            </p>
          )}
        </div>

        {/* Chevron */}
        <svg
          className={`h-5 w-5 shrink-0 text-gray-400 transition-transform ${isExpanded ? 'rotate-180' : ''}`}
          xmlns="http://www.w3.org/2000/svg"
          viewBox="0 0 20 20"
          fill="currentColor"
          aria-hidden="true"
        >
          <path
            fillRule="evenodd"
            d="M5.22 8.22a.75.75 0 011.06 0L10 11.94l3.72-3.72a.75.75 0 111.06 1.06l-4.25 4.25a.75.75 0 01-1.06 0L5.22 9.28a.75.75 0 010-1.06z"
            clipRule="evenodd"
          />
        </svg>
      </button>

      {/* Expanded detail */}
      {isExpanded && (
        <div id={`anomaly-detail-${index}`} className="border-t border-gray-100 px-4 pb-4 pt-3 space-y-3">
          {/* Explanation */}
          <div>
            <p className="text-xs font-semibold uppercase tracking-wide text-gray-500">
              Why this was flagged
            </p>
            <p className="mt-1 text-sm text-gray-700">{anomaly.explanation}</p>
          </div>

          {/* Medicare reference price */}
          {anomaly.medicare_reference_price !== null && (
            <div>
              <p className="text-xs font-semibold uppercase tracking-wide text-gray-500">
                Medicare reference price
              </p>
              <p className="mt-1 text-sm text-gray-700">
                {formatUsd(anomaly.medicare_reference_price)}
                {line_item.billed_amount !== null && (
                  <span className="ml-2 text-red-600">
                    (you were billed {formatUsd(line_item.billed_amount)})
                  </span>
                )}
              </p>
            </div>
          )}

          {/* Suggested action */}
          <div className="rounded-lg bg-blue-50 p-3">
            <p className="text-xs font-semibold uppercase tracking-wide text-blue-700">
              Suggested action
            </p>
            <p className="mt-1 text-sm text-blue-900">{anomaly.suggested_action}</p>
          </div>

          {/* Service details */}
          <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-gray-500">
            {line_item.service_date && (
              <span>Date of service: {line_item.service_date}</span>
            )}
            {line_item.quantity > 1 && (
              <span>Quantity: {line_item.quantity}</span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ============================================================
// MAIN COMPONENT
// ============================================================

/**
 * Full anomaly report — summary bar + list of anomaly cards.
 *
 * EMPTY STATE:
 *   Renders a green "clean bill" panel when anomalies is empty.
 *   This is a legitimate, positive outcome and should feel reassuring.
 *
 * HIGHLIGHTING:
 *   When DisputeLetter highlights a code (user hovers over a citation),
 *   the matching AnomalyCard gets a blue ring via the highlightCodes prop.
 *   This creates a visual link between the letter and the flagged charge.
 */
export function AnomalyReport({
  anomalies,
  billSummary,
  highlightCodes,
}: AnomalyReportProps): React.ReactElement {
  if (anomalies.length === 0) {
    return (
      <div
        role="status"
        className="rounded-xl border-2 border-green-300 bg-green-50 p-8 text-center"
        data-testid="clean-bill-message"
      >
        <svg
          className="mx-auto mb-3 h-12 w-12 text-green-500"
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
            d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
          />
        </svg>
        <p className="text-lg font-semibold text-green-800">No issues found</p>
        <p className="mt-1 text-sm text-green-700">
          Your bill appears to be within normal Medicare reference ranges.
          If you still have concerns, request an itemized bill from your provider.
        </p>
      </div>
    );
  }

  return (
    <section aria-label="Billing anomaly report" data-testid="anomaly-report">
      <SummaryBar summary={billSummary} />

      <div className="space-y-3">
        {anomalies.map((anomaly, index) => (
          <AnomalyCard
            key={`${anomaly.anomaly_type}-${anomaly.line_item.code ?? 'no-code'}-${index}`}
            anomaly={anomaly}
            isHighlighted={
              highlightCodes != null &&
              anomaly.line_item.code != null &&
              highlightCodes.has(anomaly.line_item.code)
            }
            index={index}
          />
        ))}
      </div>
    </section>
  );
}
