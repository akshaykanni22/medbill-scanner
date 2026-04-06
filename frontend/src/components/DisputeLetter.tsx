/**
 * frontend/src/components/DisputeLetter.tsx
 * ============================================================
 * PURPOSE:
 *   Displays the AI-generated dispute letter with a copy-to-clipboard
 *   button. Highlights HCPCS codes mentioned in the letter so the
 *   user can see which charges are being disputed.
 *
 *   Also calls onCodesHover(codes) when the user hovers a highlighted
 *   code span, enabling cross-highlighting in AnomalyReport.
 *
 * PROPS:
 *   letter        — DisputeLetter from AnalysisResponse
 *   onCodesHover  — called with a Set of codes on hover, null on leave
 *                   used by App.tsx to pass highlightCodes to AnomalyReport
 *
 * COPY BUTTON:
 *   Uses the Clipboard API (navigator.clipboard.writeText).
 *   Falls back gracefully if the API is unavailable (e.g., non-HTTPS).
 *   Shows a "Copied!" confirmation for 2 seconds then resets.
 *
 * LETTER RENDERING:
 *   The letter body is plain text (line breaks preserved via whitespace-pre-wrap).
 *   HCPCS codes from anomaly_codes are highlighted inline using a simple
 *   regex replace that wraps each code in a <mark> span.
 *   WHY NOT dangerouslySetInnerHTML: we build React elements, not raw HTML,
 *   so there is no XSS risk from the letter body text.
 * ============================================================
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { DisputeLetter as DisputeLetterType } from '../types';

// ============================================================
// TYPES
// ============================================================

interface DisputeLetterProps {
  letter: DisputeLetterType;
  /**
   * Called when the user hovers over a highlighted HCPCS code in the letter.
   * Passes a ReadonlySet of the hovered codes so AnomalyReport can ring-highlight
   * the matching anomaly cards. Pass null on mouse leave to clear highlights.
   */
  onCodesHover: (codes: ReadonlySet<string> | null) => void;
}

// ============================================================
// HELPERS
// ============================================================

/**
 * Split letter body into React nodes, wrapping each HCPCS code in
 * a highlighted <mark> span.
 *
 * WHY useMemo (caller):
 *   This runs a regex over the full letter body on every render.
 *   Memoizing by [body, anomaly_codes] means it only re-runs when
 *   the letter itself changes — not on hover state changes.
 *
 * WHY NOT dangerouslySetInnerHTML:
 *   We construct React elements (not raw HTML strings), so the letter
 *   body content cannot inject scripts even if the LLM produced
 *   unexpected output. React escapes text nodes automatically.
 *
 * @param body         Full letter body text.
 * @param codes        HCPCS codes to highlight.
 * @param onEnter      Called with the code when mouse enters a highlight.
 * @param onLeave      Called when mouse leaves a highlight.
 */
function buildHighlightedBody(
  body: string,
  codes: string[],
  onEnter: (code: string) => void,
  onLeave: () => void,
): React.ReactNode[] {
  if (codes.length === 0) {
    return [body];
  }

  // Build a regex that matches any of the codes as whole words.
  // WHY \b word boundaries: avoid matching "99213" inside "A99213".
  const escapedCodes = codes.map((c) => c.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  const pattern = new RegExp(`\\b(${escapedCodes.join('|')})\\b`, 'g');

  const nodes: React.ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(body)) !== null) {
    // Text before the match
    if (match.index > lastIndex) {
      nodes.push(body.slice(lastIndex, match.index));
    }

    const code = match[1];
    nodes.push(
      <mark
        key={`${code}-${match.index}`}
        className="cursor-pointer rounded bg-yellow-200 px-0.5 font-mono text-yellow-900 hover:bg-yellow-300"
        tabIndex={0}
        onMouseEnter={() => onEnter(code)}
        onMouseLeave={onLeave}
        onFocus={() => onEnter(code)}
        onBlur={onLeave}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') onEnter(code); }}
        title={`Hover to highlight charge ${code} in the report above`}
      >
        {code}
      </mark>,
    );

    lastIndex = pattern.lastIndex;
  }

  // Remaining text after last match
  if (lastIndex < body.length) {
    nodes.push(body.slice(lastIndex));
  }

  return nodes;
}

// ============================================================
// COMPONENT
// ============================================================

/**
 * Dispute letter preview with copy button and code hover highlighting.
 *
 * COPY STATE:
 *   'idle'    — default, shows "Copy letter"
 *   'copied'  — shows "Copied!" for 2 seconds after successful copy
 *   'failed'  — shows "Copy failed" if Clipboard API unavailable
 *
 * HOVER CROSS-REFERENCE:
 *   When user hovers a highlighted code span in the letter, we call
 *   onCodesHover with a Set containing that code. App.tsx passes this
 *   Set to AnomalyReport as highlightCodes, which rings the matching card.
 *   On mouse leave we call onCodesHover(null) to clear.
 */
export function DisputeLetter({
  letter,
  onCodesHover,
}: DisputeLetterProps): React.ReactElement {
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle');

  // Ref for the copy reset timer — prevents stale timer if user clicks again
  // or if the component unmounts before the timeout fires (FIX 1).
  const copyTimerRef = useRef<number | null>(null);

  // ---- Hover handlers ----
  // Defined before the refs below so the refs can be initialised with the
  // correct function type on the first render (FIX 2).

  const handleCodeEnter = useCallback(
    (code: string) => {
      onCodesHover(new Set([code]));
    },
    [onCodesHover],
  );

  const handleCodeLeave = useCallback(() => {
    onCodesHover(null);
  }, [onCodesHover]);

  // Keep stable refs to hover handlers so the memoized body never needs to
  // re-run when the handler identities change (FIX 2).
  const onEnterRef = useRef(handleCodeEnter);
  const onLeaveRef = useRef(handleCodeLeave);

  // Keep refs in sync with the latest handler versions (FIX 2).
  useEffect(() => { onEnterRef.current = handleCodeEnter; }, [handleCodeEnter]);
  useEffect(() => { onLeaveRef.current = handleCodeLeave; }, [handleCodeLeave]);

  // ---- Copy handler ----

  const handleCopy = useCallback(async () => {
    if (!navigator.clipboard) {
      setCopyState('failed');
      return;
    }
    try {
      await navigator.clipboard.writeText(letter.body);
      setCopyState('copied');
      // Clear any in-flight timer before starting a new one (FIX 1).
      if (copyTimerRef.current !== null) window.clearTimeout(copyTimerRef.current);
      copyTimerRef.current = window.setTimeout(() => {
        setCopyState('idle');
        copyTimerRef.current = null;
      }, 2000);
    } catch {
      setCopyState('failed');
    }
  }, [letter.body]);

  // Clear the copy timer on unmount to prevent setState on an unmounted
  // component (FIX 1).
  useEffect(() => {
    return () => {
      if (copyTimerRef.current !== null) window.clearTimeout(copyTimerRef.current);
    };
  }, []);

  // ---- Build highlighted body ----
  // Memoized: only re-runs when letter content changes, not on hover.
  // WHY: handlers are accessed via stable refs, not captured directly,
  // so the memo only re-runs when the letter content changes (FIX 2).

  const highlightedBody = useMemo(
    () =>
      buildHighlightedBody(
        letter.body,
        letter.anomaly_codes,
        (code) => onEnterRef.current(code),
        () => onLeaveRef.current(),
      ),
    [letter.body, letter.anomaly_codes],
  );

  // ---- Copy button label ----

  const copyLabel =
    copyState === 'copied'
      ? 'Copied!'
      : copyState === 'failed'
        ? 'Copy failed — please select and copy manually'
        : 'Copy letter';

  const copyButtonClasses = [
    'flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-semibold transition-colors',
    'focus:outline-none focus:ring-2 focus:ring-offset-2',
    copyState === 'copied'
      ? 'bg-green-600 text-white focus:ring-green-500'
      : copyState === 'failed'
        ? 'bg-red-100 text-red-700 focus:ring-red-500'
        : 'bg-blue-600 text-white hover:bg-blue-700 focus:ring-blue-500',
  ].join(' ');

  return (
    <section
      aria-label="Dispute letter"
      data-testid="dispute-letter"
      className="rounded-xl border border-gray-200 bg-white shadow-sm"
    >
      {/* Header */}
      <div className="flex items-center justify-between border-b border-gray-100 px-5 py-4">
        <div>
          <h3 className="text-base font-semibold text-gray-900">Dispute Letter</h3>
          <p className="mt-0.5 text-xs text-gray-500">{letter.subject_line}</p>
        </div>
        {/* Visually-hidden live region announces copy outcome to screen readers
            without the awkward behaviour of aria-live on an interactive element (FIX 3). */}
        <span
          aria-live="polite"
          aria-atomic="true"
          className="sr-only"
        >
          {copyState === 'copied' ? 'Copied to clipboard' : ''}
        </span>
        <button
          type="button"
          onClick={handleCopy}
          className={copyButtonClasses}
          data-testid="copy-button"
        >
          {/* Icon */}
          {copyState === 'copied' ? (
            <svg
              className="h-4 w-4"
              xmlns="http://www.w3.org/2000/svg"
              viewBox="0 0 20 20"
              fill="currentColor"
              aria-hidden="true"
            >
              <path
                fillRule="evenodd"
                d="M16.704 4.153a.75.75 0 01.143 1.052l-8 10.5a.75.75 0 01-1.127.075l-4.5-4.5a.75.75 0 011.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 011.05-.143z"
                clipRule="evenodd"
              />
            </svg>
          ) : (
            <svg
              className="h-4 w-4"
              xmlns="http://www.w3.org/2000/svg"
              viewBox="0 0 20 20"
              fill="currentColor"
              aria-hidden="true"
            >
              <path d="M7 3.5A1.5 1.5 0 018.5 2h3.879a1.5 1.5 0 011.06.44l3.122 3.12A1.5 1.5 0 0117 6.62V12.5a1.5 1.5 0 01-1.5 1.5h-1v-3.379a3 3 0 00-.879-2.121L10.5 5.379A3 3 0 008.379 4.5H7v-1z" />
              <path d="M4.5 6A1.5 1.5 0 003 7.5v9A1.5 1.5 0 004.5 18h7a1.5 1.5 0 001.5-1.5v-5.879a1.5 1.5 0 00-.44-1.06L9.44 6.439A1.5 1.5 0 008.378 6H4.5z" />
            </svg>
          )}
          {copyLabel}
        </button>
      </div>

      {/* Letter body */}
      <div className="px-5 py-4">
        {letter.anomaly_codes.length > 0 && (
          <p className="mb-3 text-xs text-gray-500">
            Hover over a{' '}
            <mark className="rounded bg-yellow-200 px-0.5 font-mono text-yellow-900">
              highlighted code
            </mark>{' '}
            to see the corresponding charge in the report above.
          </p>
        )}
        <pre
          className="whitespace-pre-wrap font-sans text-sm leading-relaxed text-gray-800"
          data-testid="letter-body"
        >
          {highlightedBody}
        </pre>
      </div>

      {/* Footer disclaimer */}
      <div className="border-t border-gray-100 px-5 py-3">
        <p className="text-xs text-gray-400">
          This letter is a template generated by AI. Review it carefully before
          sending. You may need to add your name, address, and account number.
          Consider consulting a patient advocate or healthcare attorney for
          complex disputes.
        </p>
      </div>
    </section>
  );
}
