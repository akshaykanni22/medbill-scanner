/**
 * __tests__/useBillAnalysis.test.ts
 * Tests for frontend/src/hooks/useBillAnalysis.ts
 */

import { renderHook, act, waitFor } from '@testing-library/react';
import { useBillAnalysis } from '../hooks/useBillAnalysis';
import * as api from '../utils/api';
import type { AnalysisResponse } from '../types';

// ---- Mock data ----

const MOCK_RESULT: AnalysisResponse = {
  anomalies: [],
  dispute_letter: null,
  bill_summary: {
    total_line_items: 3,
    total_billed_amount: null,
    anomaly_count: 0,
    high_severity_count: 0,
    medium_severity_count: 0,
    potential_overcharge_total: null,
  },
  processing_time_seconds: 1.5,
};

function makeMockFile(): File {
  return new File(['fake'], 'bill.pdf', { type: 'application/pdf' });
}

// ---- Tests ----

describe('useBillAnalysis', () => {
  beforeEach(() => {
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.runOnlyPendingTimers();
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  it('starts in idle state', () => {
    const { result } = renderHook(() => useBillAnalysis());
    expect(result.current.status).toBe('idle');
    expect(result.current.result).toBeNull();
    expect(result.current.error).toBeNull();
  });

  it('transitions to uploading on analyze()', async () => {
    jest.spyOn(api, 'analyzeBill').mockReturnValue(new Promise(() => {})); // never resolves
    const { result } = renderHook(() => useBillAnalysis());

    act(() => {
      result.current.analyze(makeMockFile());
    });

    expect(result.current.status).toBe('uploading');
  });

  it('transitions to analyzing after delay', async () => {
    jest.spyOn(api, 'analyzeBill').mockReturnValue(new Promise(() => {}));
    const { result } = renderHook(() => useBillAnalysis());

    act(() => {
      result.current.analyze(makeMockFile());
    });

    expect(result.current.status).toBe('uploading');

    act(() => {
      jest.advanceTimersByTime(1600); // past ANALYZING_DELAY_MS = 1500
    });

    expect(result.current.status).toBe('analyzing');
  });

  it('transitions to done on successful analysis', async () => {
    jest.spyOn(api, 'analyzeBill').mockResolvedValueOnce(MOCK_RESULT);
    const { result } = renderHook(() => useBillAnalysis());

    await act(async () => {
      await result.current.analyze(makeMockFile());
    });

    expect(result.current.status).toBe('done');
    expect(result.current.result).toEqual(MOCK_RESULT);
    expect(result.current.error).toBeNull();
  });

  it('transitions to error on ApiError', async () => {
    const apiErr = new api.ApiError(429, { error: 'rate_limit_exceeded', detail: 'Too many.' });
    jest.spyOn(api, 'analyzeBill').mockRejectedValueOnce(apiErr);
    const { result } = renderHook(() => useBillAnalysis());

    await act(async () => {
      await result.current.analyze(makeMockFile());
    });

    expect(result.current.status).toBe('error');
    expect(result.current.error).toBe(apiErr);
    expect(result.current.result).toBeNull();
  });

  it('transitions to error on network Error', async () => {
    const netErr = new Error('fetch failed');
    jest.spyOn(api, 'analyzeBill').mockRejectedValueOnce(netErr);
    const { result } = renderHook(() => useBillAnalysis());

    await act(async () => {
      await result.current.analyze(makeMockFile());
    });

    expect(result.current.status).toBe('error');
    expect(result.current.error).toBeInstanceOf(Error);
  });

  it('silently swallows AbortError from reset', async () => {
    const abortErr = new DOMException('Aborted', 'AbortError');
    jest.spyOn(api, 'analyzeBill').mockRejectedValueOnce(abortErr);
    const { result } = renderHook(() => useBillAnalysis());

    await act(async () => {
      result.current.analyze(makeMockFile());
      result.current.reset(); // cancel the request
    });

    // Should stay idle (reset), not transition to error
    expect(result.current.status).toBe('idle');
    expect(result.current.error).toBeNull();
  });

  it('reset() returns to idle and clears result', async () => {
    jest.spyOn(api, 'analyzeBill').mockResolvedValueOnce(MOCK_RESULT);
    const { result } = renderHook(() => useBillAnalysis());

    await act(async () => {
      await result.current.analyze(makeMockFile());
    });
    expect(result.current.status).toBe('done');

    act(() => {
      result.current.reset();
    });

    expect(result.current.status).toBe('idle');
    expect(result.current.result).toBeNull();
    expect(result.current.error).toBeNull();
  });

  it('reset() clears error state', async () => {
    const err = new Error('oops');
    jest.spyOn(api, 'analyzeBill').mockRejectedValueOnce(err);
    const { result } = renderHook(() => useBillAnalysis());

    await act(async () => {
      await result.current.analyze(makeMockFile());
    });
    expect(result.current.status).toBe('error');

    act(() => {
      result.current.reset();
    });

    expect(result.current.status).toBe('idle');
    expect(result.current.error).toBeNull();
  });

  it('ignores second analyze() call while one is in progress', async () => {
    const spy = jest.spyOn(api, 'analyzeBill').mockReturnValue(new Promise(() => {}));
    const { result } = renderHook(() => useBillAnalysis());

    act(() => {
      result.current.analyze(makeMockFile());
    });
    act(() => {
      result.current.analyze(makeMockFile()); // second call — should be ignored
    });

    expect(spy).toHaveBeenCalledTimes(1);
  });

  it('stale callback: reset during in-flight request does not apply stale result', async () => {
    let resolveAnalysis!: (value: AnalysisResponse) => void;
    jest.spyOn(api, 'analyzeBill').mockReturnValue(
      new Promise<AnalysisResponse>((resolve) => {
        resolveAnalysis = resolve;
      }),
    );

    const { result } = renderHook(() => useBillAnalysis());

    act(() => {
      result.current.analyze(makeMockFile());
    });

    // Reset before the request resolves
    act(() => {
      result.current.reset();
    });

    // Now resolve the request
    await act(async () => {
      resolveAnalysis(MOCK_RESULT);
      await Promise.resolve();
    });

    // Should still be idle — stale callback was discarded
    expect(result.current.status).toBe('idle');
    expect(result.current.result).toBeNull();
  });
});
